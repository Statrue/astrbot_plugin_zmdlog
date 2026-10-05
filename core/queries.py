"""The query pipeline: from a parsed route to one image or one short text.

:class:`QueryService` is everything between the AstrBot handlers and the
renderer. It resolves references and keywords, decides which page a route
draws, and turns the upstream's known refusals (a vanished account, an old
upload without casts, a rate limit) into the short replies the user sees.
Unknown failures propagate; the host's error ladder turns those into the
generic wording.

Two kinds of route exist. A *direct* route names its target — help, an
account id or link, a battle id or link — and never consults the board
matcher. A *keyword* route goes through the matcher
(``_dispatch_keyword``) and, when the best board hit is weak, is weighed
against character names and public nicknames, because the smart route
searches boards and accounts alike.

榜单 on its own is a pick list of the dungeons, not a page. It drew every
board's top three until forty-nine cards made a page nobody scrolled, and
the site had dropped the home page it mirrored; a dungeon picked from the
list draws what typing its name draws.
"""

import asyncio
from collections.abc import Callable
from dataclasses import replace

from . import messages
from .account_binding import AccountBinding, to_binding_page
from .bindings import BoundAccount, UserBindings
from .candidates import (
    MAX_CANDIDATES,
    CandidateStore,
    CandidateView,
    PendingCandidates,
    account_choice,
)
from .characters import (
    CharacterResolution,
    CharacterResolutionStatus,
    resolve_character_name,
    resolve_standing_names,
)
from .client import (
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsClientError,
    is_valid_boss_slug,
    searchable_nickname,
)
from .datasource import CharacterCatalogEntry, ZmdLogsDataSource
from .identifiers import (
    PublicReferenceError,
    parse_account_reference,
    parse_battle_reference,
)
from .logs import LogSink
from .matcher import (
    BOARD_QUERY_TARGETS,
    MatchChoice,
    MatchLevel,
    MatchStatus,
    RankingMatcher,
    TargetType,
    fold_text,
)
from .messages import shorten
from .metrics import METRIC_DPS, board_qualifier
from .models import (
    BattleDetailSummary,
    BossRanking,
    CharacterType,
    HotBossCard,
)
from .outcome import Outcome, PageSubject, PageTarget
from .rank_trend import RankTrend
from .recipes import (
    BattleRecipe,
    IndexSnapshot,
    export_refusal,
    find_profile_character,
    index_snapshot,
    prepare_account,
    prepare_battle,
    prepare_champions,
    prepare_character_boss,
    prepare_character_profile,
    prepare_character_stats,
    prepare_compare,
    prepare_dungeon_overview,
    prepare_player_champions,
    prepare_ranking,
    prepare_records,
    prepare_standings,
)
from .render import LongImageRenderer
from .routing import DEFAULT_RANKING_TOP, OPTION_USAGE, RouteKind, RouteRequest
from .settings import PluginSettings

# 战报 / 配装 / 技能 / 技能轴 share one argument shape and one lookup; only
# the page drawn from the battle differs.
BATTLE_STYLE_ROUTES = frozenset(
    {
        RouteKind.BATTLE_QUERY,
        RouteKind.LOADOUT_QUERY,
        RouteKind.SKILL_QUERY,
        RouteKind.TIMELINE_QUERY,
    }
)
_BATTLE_VIEWS = frozenset(
    {
        CandidateView.BATTLE,
        CandidateView.LOADOUT,
        CandidateView.SKILLS,
        CandidateView.TIMELINE,
    }
)
# Pages that exist per board only: a dungeon or scope hit is flattened to a
# pick list of its boards for these. 角色档案 is one when ``--榜单`` names a
# board, and draws its list itself (``_render_character_profile``).
_BOARD_ONLY_VIEWS = (
    CandidateView.CHARACTER_STATS,
    CandidateView.CHARACTER_PROFILE,
    CandidateView.ROSTER,
    CandidateView.BATTLE,
    CandidateView.LOADOUT,
    CandidateView.SKILLS,
    CandidateView.TIMELINE,
    CandidateView.COMPARE,
)
_ROUTE_VIEWS = {
    RouteKind.CHARACTER_STATS: CandidateView.CHARACTER_STATS,
    RouteKind.ROSTER_QUERY: CandidateView.ROSTER,
    RouteKind.BATTLE_QUERY: CandidateView.BATTLE,
    RouteKind.LOADOUT_QUERY: CandidateView.LOADOUT,
    RouteKind.SKILL_QUERY: CandidateView.SKILLS,
    RouteKind.TIMELINE_QUERY: CandidateView.TIMELINE,
    RouteKind.COMPARE_QUERY: CandidateView.COMPARE,
    RouteKind.TREND_QUERY: CandidateView.TREND,
}
_BOARD_ROUTES = frozenset(
    {
        RouteKind.RANKING_QUERY,
        RouteKind.SMART_QUERY,
        RouteKind.CHARACTER_STATS,
        RouteKind.ROSTER_QUERY,
    }
)
_ACCOUNT_ROUTES = frozenset({RouteKind.ACCOUNT_QUERY, RouteKind.TREND_QUERY})
CHARACTER_STATS_UNAVAILABLE = "character_statistics_not_available"
# The only rarity 角色统计 has statistics for.
SIX_STAR = 6

BoardMatcher = Callable[[tuple[HotBossCard, ...]], RankingMatcher]


class QueryService:
    """Resolve one parsed route into an :class:`Outcome`."""

    def __init__(
        self,
        *,
        client: ZmdLogsClient,
        data: ZmdLogsDataSource,
        renderer: Callable[[], LongImageRenderer],
        candidates: CandidateStore,
        board_matcher: BoardMatcher,
        trend: RankTrend,
        settings: PluginSettings,
        logger: LogSink,
        bindings: AccountBinding | None = None,
    ) -> None:
        self._client = client
        self._data = data
        # Looked up per query: the host may lose or replace its renderer.
        self._renderer = renderer
        self._candidates = candidates
        self._board_matcher = board_matcher
        # Every account's rank trace, recorded from the index's board reads.
        self._trend = trend
        # The binding book behind 我的 and 对比 … 我; None where the host has
        # none.
        self._bindings = bindings
        self._web_base_url = settings.web_base_url
        self._logger = logger

    # --- entry points ----------------------------------------------------------------

    async def dispatch(
        self,
        route: RouteRequest,
        *,
        command_prefix: str,
        origin: str = "",
        requester_key: str = "",
        official: bool = False,
    ) -> Outcome:
        """Answer one parsed command.

        ``origin`` keys any pick list it posts; ``requester_key`` is the
        sender, whose bindings 我的 and ``对比 … 我`` read.
        ``official`` says the chat is the QQ official bot's, which only the
        help page reads.
        """

        if route.compare_self:
            # Who 我 is, is settled before the board: someone unbound is
            # told how to bind without a board lookup that could only fail.
            mine = self._primary_account(
                requester_key, command=f"{command_prefix}zmdlog"
            )
            if isinstance(mine, Outcome):
                return mine
            return await self._dispatch_keyword(route, origin=origin, me=mine)
        direct = await self._dispatch_direct(
            route,
            command_prefix=command_prefix,
            origin=origin,
            requester_key=requester_key,
            official=official,
        )
        if direct is not None:
            return direct
        return await self._dispatch_keyword(route, origin=origin)

    async def render_pick(
        self,
        entry: PendingCandidates,
        choice: MatchChoice,
        *,
        requester_key: str = "",
        command: str = "/zmdlog",
    ) -> Outcome:
        """Draw the page a quoted pick-list reply selected.

        ``requester_key`` is whoever picked, the 我 of a ``对比 … 我`` list;
        ``command`` the prefixed command a how-to-bind reply names.
        """

        if entry.compare_self:
            me = self._primary_account(requester_key, command=command)
            if isinstance(me, Outcome):
                return me
            entry = replace(entry, me=me)
        if entry.view is CandidateView.DUNGEONS:
            # Drawn as its typed name is: the one board of a one-board
            # dungeon is a ranking page, sibling buttons and all.
            entry = replace(entry, view=CandidateView.RANKING)
        cards = await self._data.list_hot_bosses()
        return await self._render_choice(
            choice, cards, query=entry.query, pending=entry
        )

    async def render_battle_card(self, battle_id: str) -> Outcome:
        """The battle card for an auto-expanded link."""

        recipe = await prepare_battle(
            self._data, battle_id, query=battle_id, web_base_url=self._web_base_url
        )
        return Outcome.image(
            await recipe.draw(self._renderer()),
            target=_card_target(recipe, battle_id),
        )

    async def _index(self, metric: str = METRIC_DPS) -> IndexSnapshot | Outcome:
        """The ranking index for ``metric``, or the reply for one still filling."""

        snapshot = await index_snapshot(self._data, metric=metric)
        if snapshot is None:
            return Outcome(message=messages.INDEX_FILLING)
        return snapshot

    # --- routes that name their target -----------------------------------------------

    async def _dispatch_direct(
        self,
        route: RouteRequest,
        *,
        command_prefix: str,
        origin: str,
        requester_key: str = "",
        official: bool = False,
    ) -> Outcome | None:
        """Routes that need no board keyword; None hands over to the matcher."""

        renderer = self._renderer()
        if route.kind is RouteKind.HELP:
            return Outcome.image(
                await renderer.render_help(
                    command_prefix=command_prefix, official=official
                )
            )

        if route.kind is RouteKind.MY_ACCOUNT:
            return await self._render_my_account(
                route.query,
                origin=origin,
                requester_key=requester_key,
                command=f"{command_prefix}zmdlog",
            )

        if route.kind is RouteKind.DUNGEON_LIST:
            return await self._dungeon_list(origin=origin)

        if route.kind is RouteKind.ACCOUNT_QUERY:
            account_id = self._account_reference(route.query)
            if account_id is not None:
                return await self._render_account(account_id, query=route.query)
            # Not an ID or trusted URL: treat the text as a nickname.
            outcome = await self._account_search_outcome(route.query, origin=origin)
            return outcome or Outcome(message=messages.ACCOUNT_REFERENCE_NEEDED)

        if route.kind is RouteKind.COMPARE_QUERY and route.compare_target:
            # Two explicit references: no board lookup at all.
            try:
                first = parse_battle_reference(
                    route.query, web_base_url=self._web_base_url
                )
                second = parse_battle_reference(
                    route.compare_target, web_base_url=self._web_base_url
                )
            except PublicReferenceError:
                return Outcome(message=messages.COMPARE_REFERENCE_NEEDED)
            return await self._render_compare(
                first, second, query=f"{route.query} vs {route.compare_target}"
            )

        if route.kind in BATTLE_STYLE_ROUTES:
            try:
                battle_id = parse_battle_reference(
                    route.query, web_base_url=self._web_base_url
                )
            except PublicReferenceError:
                # Not an exact reference: the text is a board keyword whose
                # rank-N battle should be shown.
                return None
            return await self._render_battle_view(
                battle_id, route_view(route), query=route.query
            )

        if route.kind is RouteKind.TREND_QUERY:
            return await self._dispatch_trend(route, origin=origin)

        if route.kind is RouteKind.PLAYER_CHAMPIONS:
            return await self._render_player_champions(
                route.stats_range, metric=route.metric
            )

        if route.kind is RouteKind.RECORDS_QUERY:
            return await self._render_records(route.stats_range, metric=route.metric)

        if route.kind is RouteKind.CHARACTER_STANDINGS:
            return await self._render_character_standings(
                route.query,
                element_filter=route.element_filter,
                profession_filter=route.profession_filter,
                time_range=route.stats_range,
                metric=route.metric,
            )

        if route.kind is RouteKind.CHARACTER_PROFILE:
            return await self._render_character_profile(
                route.query,
                time_range=route.stats_range,
                board_query=route.board_query,
                origin=origin,
            )

        if route.kind is RouteKind.CHARACTER_STATS and not route.query.strip():
            recipe = await prepare_character_stats(
                self._data,
                None,
                time_range=route.stats_range,
                potential=route.stats_potential,
                query="角色统计",
                web_base_url=self._web_base_url,
                metric=route.metric,
            )
            return Outcome.image(await recipe.draw(renderer))
        return None

    async def _dungeon_list(self, *, origin: str) -> Outcome:
        """榜单: every dungeon to pick from, however many there are."""

        cards = await self._data.list_hot_bosses()
        choices = self._board_matcher(cards).dungeon_choices()
        if not choices:
            return Outcome(message=messages.NO_PUBLIC_BOARDS)
        entry = self._candidates.remember(
            "榜单",
            choices,
            origin=origin,
            view=CandidateView.DUNGEONS,
            limit=None,
        )
        return self._pick_list(entry)

    async def _dispatch_trend(self, route: RouteRequest, *, origin: str) -> Outcome:
        account_id = self._account_reference(route.query)
        if account_id is not None:
            return await self._render_trend(
                account_id, query=route.query, time_range=route.stats_range
            )
        # An account with a trace is addressable by the nickname the trace
        # holds, with no upstream search at all.
        local = self._trend.by_name(route.query)
        if len(local) == 1:
            return await self._render_trend(
                local[0].account_id,
                query=route.query,
                time_range=route.stats_range,
            )
        if len(local) > 1:
            entry = self._candidates.remember(
                route.query,
                tuple(
                    account_choice(
                        item.account_id, item.display_name, query=route.query
                    )
                    for item in local[:MAX_CANDIDATES]
                ),
                origin=origin,
                view=CandidateView.TREND,
                stats_range=route.stats_range,
            )
            # Every account on a board has a trace, so a short name can
            # match more than a list holds.
            more = len(local) > MAX_CANDIDATES
            return self._pick_list(
                entry, note=messages.MORE_NICKNAME_HITS if more else None
            )
        outcome = await self._account_search_outcome(
            route.query,
            origin=origin,
            view=CandidateView.TREND,
            stats_range=route.stats_range,
        )
        return outcome or Outcome(message=messages.ACCOUNT_REFERENCE_NEEDED)

    def _account_reference(self, text: str) -> str | None:
        try:
            return parse_account_reference(text, web_base_url=self._web_base_url)
        except PublicReferenceError:
            return None

    # --- routes that carry a board keyword -------------------------------------------

    async def _dispatch_keyword(
        self,
        route: RouteRequest,
        *,
        origin: str,
        me: BoundAccount | None = None,
    ) -> Outcome:
        """Resolve the keyword against the board index, then draw the view.

        ``me`` is the asker's primary account for ``对比 … 我``. A pick list
        keeps only that 我 was asked for: whoever picks is 我 then.
        """

        view = route_view(route)
        pending = pending_from_route(route)
        if me is not None:
            pending = replace(pending, me=me)
        try:
            cards = await self._data.list_hot_bosses()
        except ZmdLogsClientError:
            if looks_like_direct_slug(route.query):
                return await self._render_board(
                    route.query, query=route.query, pending=pending
                )
            raise

        matcher = self._board_matcher(cards)
        match = matcher.match(route.query, allowed_types=BOARD_QUERY_TARGETS)
        if match.status is MatchStatus.NOT_FOUND:
            return await self._answer_board_miss(
                route.query, view=view, pending=pending, origin=origin
            )

        candidates: tuple[MatchChoice, ...] = ()
        choice: MatchChoice | None = None
        if match.status is MatchStatus.AMBIGUOUS:
            candidates = match.candidates
        else:
            choice = match.selected

        best_level = (
            choice.level
            if choice is not None
            else min((entry.level for entry in candidates), default=None)
        )
        weak_boards = best_level is not None and best_level > MatchLevel.PINYIN_EXACT
        if weak_boards and view is CandidateView.CHARACTER_STATS:
            # 角色统计 <角色名>: a recognised character beats fuzzy board hits.
            outcome = await self._character_boss_outcome(route.query, pending)
            if outcome is not None:
                return outcome
        if weak_boards and _plain_ranking_query(view, pending):
            # An exact public nickname must beat fuzzy board hits (the smart
            # route searches boards AND accounts). Exact board tiers never
            # reach here and skip the extra request entirely.
            account_choices = await self._search_account_choices(route.query)
            folded_query = fold_text(route.query)
            exact = tuple(
                entry
                for entry in account_choices
                if fold_text(entry.target.name) == folded_query
            )
            if len(exact) == 1:
                return await self._render_choice(
                    exact[0], cards, query=route.query, pending=pending
                )
            if len(exact) > 1:
                choice, candidates = None, exact
            elif candidates and account_choices:
                # Fuzzy on both sides: one typed pick list.
                candidates = (candidates + account_choices)[:MAX_CANDIDATES]

        if view in _BOARD_ONLY_VIEWS:
            # Character statistics, roster and battle pages exist per board
            # only, so a dungeon / scope hit becomes a pick list of its boards.
            if choice is not None and choice.target.target_type is not TargetType.BOARD:
                candidates, choice = (choice,), None
            if candidates:
                candidates = matcher.expand_to_boards(candidates)
                if len(candidates) == 1:
                    choice, candidates = candidates[0], ()
        if candidates:
            entry = self._candidates.remember(
                route.query,
                candidates,
                origin=origin,
                ranking_top=pending.ranking_top,
                view=pending.view,
                character_filter=pending.character_filter,
                element_filter=pending.element_filter,
                stats_range=pending.stats_range,
                stats_potential=pending.stats_potential,
                battle_rank=pending.battle_rank,
                compare_rank=pending.compare_rank,
                metric=pending.metric,
                compare_self=pending.compare_self,
            )
            return self._pick_list(entry)
        if choice is None:
            return Outcome(message=_not_found_message(route.query, view))
        return await self._render_choice(
            choice, cards, query=route.query, pending=pending
        )

    async def _answer_board_miss(
        self,
        query: str,
        *,
        view: CandidateView,
        pending: PendingCandidates,
        origin: str,
    ) -> Outcome:
        """The keyword matched no board: try a raw slug, a character, a nickname."""

        if looks_like_direct_slug(query):
            try:
                return await self._render_board(query, query=query, pending=pending)
            except ZmdLogsAPIError as exc:
                if exc.status_code != 404:
                    raise
                # Not a slug after all: nicknames such as Re-Zero or
                # xiao_ming pass the slug shape too, so keep looking.
        if view is CandidateView.CHARACTER_STATS:
            outcome = await self._character_boss_outcome(query, pending)
            if outcome is not None:
                return outcome
        if _plain_ranking_query(view, pending):
            outcome = await self._account_search_outcome(
                query, quiet=True, origin=origin
            )
            if outcome is not None:
                return outcome
        hint = await self._character_name_hint(query, view)
        return Outcome(message=hint or _not_found_message(query, view))

    async def _render_choice(
        self,
        choice: MatchChoice,
        cards: tuple[HotBossCard, ...],
        *,
        query: str,
        pending: PendingCandidates,
    ) -> Outcome:
        """Draw one resolved target in the view the request asked for."""

        target = choice.target
        if target.target_type is TargetType.ACCOUNT:
            if pending.view is CandidateView.TREND:
                return await self._render_trend(
                    target.key, query=query, time_range=pending.stats_range
                )
            return await self._render_account(target.key, query=query)
        if target.target_type is TargetType.BOARD:
            return await self._render_board(target.key, query=query, pending=pending)
        if len(target.boss_slugs) == 1:
            # 危机合约 is one board; its top three would be one card.
            return await self._render_board(
                target.boss_slugs[0], query=query, pending=pending
            )

        # A dungeon or a scope draws top-three cards, which none of the row
        # options can filter; say where the option works instead.
        if pending.ranking_top is not None:
            return Outcome(message=OPTION_USAGE["top"])
        if pending.character_filter is not None:
            return Outcome(message=OPTION_USAGE["character"])
        if pending.element_filter is not None:
            return Outcome(message=OPTION_USAGE["element"])
        recipe = prepare_dungeon_overview(
            choice, cards, query=query, web_base_url=self._web_base_url
        )
        return Outcome.image(
            await recipe.draw(self._renderer()),
            target=PageTarget(
                PageSubject.DUNGEON,
                target.name,
                metric=pending.metric,
                boards=tuple((card.boss_slug, card.boss_name) for card in recipe.cards),
            ),
        )

    # --- accounts --------------------------------------------------------------------

    async def _account_search_outcome(
        self,
        query: str,
        *,
        quiet: bool = False,
        origin: str = "",
        view: CandidateView = CandidateView.RANKING,
        stats_range: str = "all",
    ) -> Outcome | None:
        """Resolve a nickname via upstream search; None means "not applicable".

        ``quiet`` marks the smart-query fallback where an empty result should
        fall through to other hints instead of producing a message. ``view``
        says which page a hit should draw: the account page by default, the
        rank trend for 趋势.
        """

        nickname = searchable_nickname(query)
        if nickname is None:
            return None
        try:
            search = await self._client.search_public_accounts(
                nickname, limit=MAX_CANDIDATES
            )
        except ZmdLogsClientError:
            if quiet:
                return None
            raise
        if not search.accounts:
            if quiet:
                return None
            return Outcome(
                message=f"没有找到昵称包含「{shorten(nickname)}」的公开账号。"
            )
        if len(search.accounts) == 1 and not search.has_more:
            hit = search.accounts[0]
            if view is CandidateView.TREND:
                return await self._render_trend(
                    hit.account_id, query=query, time_range=stats_range
                )
            return await self._render_account(hit.account_id, query=query)
        entry = self._candidates.remember(
            nickname,
            tuple(
                account_choice(hit.account_id, hit.account_display_name, query=nickname)
                for hit in search.accounts
            ),
            origin=origin,
            view=view,
            stats_range=stats_range,
        )
        return self._pick_list(
            entry,
            note=(
                messages.MORE_NICKNAME_HITS
                if search.has_more
                else None
            ),
        )

    async def _search_account_choices(self, query: str) -> tuple[MatchChoice, ...]:
        """Quiet nickname lookup for the smart route; empty on any failure."""

        nickname = searchable_nickname(query)
        if nickname is None:
            return ()
        try:
            search = await self._client.search_public_accounts(
                nickname, limit=MAX_CANDIDATES
            )
        except ZmdLogsClientError:
            return ()
        return tuple(
            account_choice(hit.account_id, hit.account_display_name, query=nickname)
            for hit in search.accounts
        )

    async def _render_account(self, account_id: str, *, query: str) -> Outcome:
        """Render one public account; a 404 answers in account terms.

        The same lookup is reached from the 账号 route, from a nickname pick
        list and from the smart route, and only the first of those knows from
        its route kind that an account is meant — so the wording is decided
        here rather than by whoever catches the error.
        """

        renderer = self._renderer()
        try:
            recipe = await prepare_account(
                self._data, account_id, query=query, web_base_url=self._web_base_url
            )
        except ZmdLogsAPIError as exc:
            if exc.status_code != 404:
                raise
            self._logger.warning("ZmdLogBot API request failed: %s", exc.code)
            return Outcome(message=messages.ACCOUNT_NOT_FOUND)
        # A trend exists for an account seen on a board since the trend began.
        history = self._trend.history_for(account_id)
        no_trend = history is None or not history.boards
        return Outcome.image(
            await recipe.draw(renderer),
            target=PageTarget(
                PageSubject.ACCOUNT,
                account_id,
                unavailable=(
                    frozenset({CandidateView.TREND}) if no_trend else frozenset()
                ),
            ),
        )

    async def _render_my_account(
        self,
        selector: str,
        *,
        origin: str,
        requester_key: str,
        command: str,
    ) -> Outcome:
        """The sender's bound account, drawn as the account page.

        ``我的`` is the primary account, ``我的 2`` / ``我的 <昵称>`` another
        one of the sender's own; the selector is resolved inside that list,
        never against the whole site. In a private chat as in a group.
        """

        mine = self._own_bindings(requester_key, command=command)
        if isinstance(mine, Outcome):
            return mine
        if not selector.strip():
            account = mine.primary
        else:
            matches = mine.resolve_matches(selector)
            if len(matches) > 1:
                names = "、".join(entry.display_name for entry in matches)
                return Outcome(
                    message=(
                        f"「{shorten(selector)}」匹配到多个绑定：{names}，请改用序号。"
                    )
                )
            if not matches:
                return Outcome(
                    message=(
                        f"绑定列表里没有「{shorten(selector)}」；"
                        f"发送 {command} 主账号 查看序号。"
                    )
                )
            account = matches[0]
        assert account is not None
        query = "我的" if not selector.strip() else f"我的 {selector.strip()}"
        return await self._render_account(account.account_id, query=query)

    def _own_bindings(
        self, requester_key: str, *, command: str
    ) -> UserBindings | Outcome:
        """The sender's bindings, or the reply for why there are none."""

        bindings = self._bindings
        if bindings is None or (
            not bindings.enabled and bindings.book.total_users == 0
        ):
            return Outcome(message=messages.BINDINGS_DISABLED)
        if not requester_key:
            return Outcome(message=messages.NO_SENDER)
        mine = bindings.bindings_for(requester_key)
        if mine is None:
            return to_binding_page(messages.NOT_BOUND.format(command=command))
        return mine

    def _primary_account(
        self, requester_key: str, *, command: str
    ) -> BoundAccount | Outcome:
        """Whom ``对比 … 我`` means: the sender's primary account, no other."""

        mine = self._own_bindings(requester_key, command=command)
        if isinstance(mine, Outcome):
            return mine
        assert mine.primary is not None
        return mine.primary

    async def _render_trend(
        self,
        account_id: str,
        *,
        query: str,
        time_range: str,
    ) -> Outcome:
        """Draw the rank trace of one account; text when nothing was recorded.

        The trace is recorded from the board reads, so this never talks to
        upstream: an account no board read has seen has no trace yet.
        """

        history = self._trend.history_for(account_id)
        if history is None or not history.boards:
            return Outcome(message=messages.TREND_NO_DATA)
        rendered = await self._renderer().render_trend(
            history,
            query=query,
            web_base_url=self._web_base_url,
            time_range=time_range,
            last_checked=self._trend.last_checked(account_id),
        )
        return Outcome.image(
            rendered,
            target=PageTarget(PageSubject.ACCOUNT, account_id, CandidateView.TREND),
        )

    # --- characters ------------------------------------------------------------------

    async def _render_records(
        self, time_range: str, *, metric: str = METRIC_DPS
    ) -> Outcome:
        """New records and first places changing hands, from the event log."""

        snapshot = await self._index(metric)
        if isinstance(snapshot, Outcome):
            return snapshot
        recipe = prepare_records(self._data, snapshot, time_range=time_range)
        return Outcome.image(await recipe.draw(self._renderer()))

    async def _render_player_champions(
        self, time_range: str, *, metric: str = METRIC_DPS
    ) -> Outcome:
        """Which public accounts uploaded the most first places, from the index."""

        snapshot = await self._index(metric)
        if isinstance(snapshot, Outcome):
            return snapshot
        recipe = prepare_player_champions(snapshot, time_range=time_range)
        return Outcome.image(await recipe.draw(self._renderer()))

    async def _render_character_standings(
        self,
        query: str,
        *,
        element_filter: str | None = None,
        profession_filter: str | None = None,
        time_range: str = "all",
        metric: str = METRIC_DPS,
    ) -> Outcome:
        """Where the teams fielding one character stand on every board.

        Drawn from the ranking index, never from upstream directly: the name
        is resolved against every roster the index holds (four-stars count),
        and the page says how old the index is. Without a name, the same
        records counted per character: the champions board.
        """

        snapshot = await self._index(metric)
        if isinstance(snapshot, Outcome):
            return snapshot
        if not query.strip():
            recipe = await prepare_champions(
                self._data,
                snapshot,
                web_base_url=self._web_base_url,
                element=element_filter,
                profession=profession_filter,
                time_range=time_range,
            )
            return Outcome.image(await recipe.draw(self._renderer()))
        names = resolve_standing_names(query, snapshot.fielded)
        if isinstance(names, str):
            return Outcome(message=names)
        recipe = await prepare_standings(
            self._data,
            snapshot,
            names,
            query=query,
            web_base_url=self._web_base_url,
        )
        if isinstance(recipe, str):
            return Outcome(message=recipe)
        return Outcome.image(await recipe.draw(self._renderer()))

    async def _render_character_profile(
        self,
        query: str,
        *,
        time_range: str,
        board_query: str | None,
        origin: str,
    ) -> Outcome:
        """角色档案: the site's character page for the character ``query`` names.

        ``board_query`` (``--榜单``) cuts it to one board. The character is
        resolved first, so a misspelt name is answered before any board
        lookup; then a keyword naming one board draws the page there, and
        one naming a dungeon or a scope lists its boards to pick from, as
        every page that exists per board does.
        """

        character = await find_profile_character(self._data, query)
        if isinstance(character, str):
            return Outcome(message=character)
        if board_query is None:
            return await self._draw_character_profile(
                character, query=query, time_range=time_range
            )
        try:
            cards = await self._data.list_hot_bosses()
        except ZmdLogsClientError:
            # As with every board keyword, a slug names its board without
            # the list; it is what a pick list's button sends.
            if looks_like_direct_slug(board_query):
                return await self._draw_character_profile(
                    character,
                    query=query,
                    time_range=time_range,
                    boss_slug=board_query,
                )
            raise
        matcher = self._board_matcher(cards)
        match = matcher.match(board_query, allowed_types=BOARD_QUERY_TARGETS)
        boards: tuple[MatchChoice, ...] = ()
        if match.status is MatchStatus.AMBIGUOUS:
            boards = matcher.expand_to_boards(match.candidates)
        elif match.selected is not None:
            boards = matcher.expand_to_boards((match.selected,))
        if not boards:
            return Outcome(
                message=_not_found_message(board_query, CandidateView.CHARACTER_PROFILE)
            )
        if len(boards) == 1:
            return await self._draw_character_profile(
                character,
                query=query,
                time_range=time_range,
                boss_slug=boards[0].target.key,
            )
        entry = self._candidates.remember(
            board_query,
            boards,
            origin=origin,
            view=CandidateView.CHARACTER_PROFILE,
            stats_range=time_range,
            profile_character=character.name,
        )
        return self._pick_list(entry)

    async def _draw_character_profile(
        self,
        character: CharacterType,
        *,
        query: str,
        time_range: str,
        boss_slug: str | None = None,
    ) -> Outcome:
        """The 角色档案 page of ``character``, on ``boss_slug`` alone if given.

        Its buttons open the character's other pages, so the target carries
        its name too; 角色统计 covers six-stars only and is not offered for
        the rest.
        """

        recipe = await prepare_character_profile(
            self._data,
            character,
            time_range=time_range,
            query=query,
            web_base_url=self._web_base_url,
            boss_slug=boss_slug,
        )
        if isinstance(recipe, str):
            return Outcome(message=recipe)
        return Outcome.image(
            await recipe.draw(self._renderer()),
            target=PageTarget(
                PageSubject.CHARACTER,
                recipe.profile.character_key,
                CandidateView.CHARACTER_PROFILE,
                stats_range=time_range,
                name=character.name,
                unavailable=(
                    frozenset()
                    if character.rarity == SIX_STAR
                    else frozenset({CandidateView.CHARACTER_STATS})
                ),
                boss_slug=boss_slug,
            ),
        )

    async def _resolve_catalog_character(
        self,
        query: str,
        *,
        refresh_on_miss: bool = False,
    ) -> tuple[tuple[CharacterCatalogEntry, ...], CharacterResolution] | None:
        """Match ``query`` against the six-star catalog; None when unavailable.

        The catalog is the data source's long-lived name/key list, so
        recognising a name costs nothing after the first read. A miss may be
        a character added since that read; ``refresh_on_miss`` asks for one
        bounded re-read before giving up.
        """

        try:
            entries = await self._data.get_character_catalog()
            resolution = resolve_character_name(query, _catalog_names(entries))
            if (
                refresh_on_miss
                and resolution.status is CharacterResolutionStatus.NOT_FOUND
            ):
                entries = await self._data.get_character_catalog(refresh=True)
                resolution = resolve_character_name(query, _catalog_names(entries))
        except ZmdLogsClientError:
            return None
        return entries, resolution

    async def _character_boss_outcome(
        self,
        query: str,
        pending: PendingCandidates,
    ) -> Outcome | None:
        """Render one character's all-boards page when ``query`` names one.

        Returns None when the query is not a recognised character (or the
        catalog is unavailable) so board handling can continue.
        """

        resolved = await self._resolve_catalog_character(
            query, refresh_on_miss=True
        )
        if resolved is None:
            return None
        entries, resolution = resolved
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            return None
        if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
            return Outcome(
                message=messages.ambiguous_character(
                    resolution.query, resolution.candidates
                )
            )
        character_key = next(
            entry.key for entry in entries if entry.name == resolution.name
        )
        recipe = await prepare_character_boss(
            self._data,
            character_key,
            time_range=pending.stats_range,
            potential=pending.stats_potential,
            query=query,
            web_base_url=self._web_base_url,
            metric=pending.metric,
        )
        return Outcome.image(await recipe.draw(self._renderer()))

    async def _character_name_hint(
        self,
        query: str,
        view: CandidateView,
    ) -> str | None:
        """Explain the right command when a board query is really a character.

        ``/zmdlog 角色统计 庄方宜`` is a natural misreading of the board-only
        ``角色统计`` route.
        """

        resolved = await self._resolve_catalog_character(query)
        if resolved is None:
            return None
        _, resolution = resolved
        if resolution.status is not CharacterResolutionStatus.MATCHED:
            return None
        name = resolution.name
        if view is CandidateView.ROSTER:
            return f"「{name}」是角色名。`阵容` 后面接榜单关键词，例如 `阵容 罗丹`。"
        return (
            f"「{name}」是角色名，不是榜单。"
            f"用 `角色统计 {name}` 看其全部榜单的分布，"
            f"或在榜单后加 `--角色 {name}` 只看该榜排名。"
        )

    # --- boards ----------------------------------------------------------------------

    async def _render_board(
        self,
        boss_slug: str,
        *,
        query: str,
        pending: PendingCandidates,
    ) -> Outcome:
        """Render one concrete board in whichever view the request asked for."""

        renderer = self._renderer()
        ranking_limit = (
            pending.ranking_top
            if pending.ranking_top is not None
            else DEFAULT_RANKING_TOP
        )
        if pending.view is CandidateView.CHARACTER_PROFILE:
            # A pick off 角色档案's list of boards; the list kept the
            # catalog's name, which resolves to the same character again.
            character = await find_profile_character(
                self._data, pending.profile_character
            )
            if isinstance(character, str):
                return Outcome(message=character)
            return await self._draw_character_profile(
                character,
                query=pending.profile_character,
                time_range=pending.stats_range,
                boss_slug=boss_slug,
            )
        if pending.view is CandidateView.CHARACTER_STATS:
            recipe = await prepare_character_stats(
                self._data,
                boss_slug,
                time_range=pending.stats_range,
                potential=pending.stats_potential,
                query=query,
                web_base_url=self._web_base_url,
                metric=pending.metric,
            )
            return Outcome.image(
                await recipe.draw(renderer),
                target=PageTarget(
                    PageSubject.BOARD,
                    boss_slug,
                    CandidateView.CHARACTER_STATS,
                    metric=pending.metric,
                    stats_range=pending.stats_range,
                    stats_potential=pending.stats_potential,
                ),
            )

        # Every view below is a question about this one board, so a copy
        # held a while is re-read for whoever asks next.
        ranking = await self._data.get_boss_ranking(
            boss_slug, metric=pending.metric, on_demand=True
        )
        board = PageTarget(
            PageSubject.BOARD,
            boss_slug,
            pending.view,
            metric=pending.metric,
            ranking_top=pending.ranking_top,
        )
        if pending.view is CandidateView.COMPARE and pending.compare_self:
            if pending.me is None:
                # Every path that draws one sets it; never compare nobody.
                return Outcome(message=messages.NO_SENDER)
            return await self._render_compare_self(
                ranking, pending.me, rank=pending.compare_rank, query=query
            )
        if pending.view is CandidateView.COMPARE:
            wanted = (pending.battle_rank, pending.compare_rank)
            rows = {
                entry.rank: entry for entry in ranking.rows if entry.rank in wanted
            }
            missing = [rank for rank in wanted if rank not in rows]
            if missing:
                return Outcome(message=_no_such_rank(ranking, missing[0]))
            return await self._render_compare(
                rows[wanted[0]].battle_id,
                rows[wanted[1]].battle_id,
                query=query,
                rank_a=wanted[0],
                rank_b=wanted[1],
                metric=pending.metric,
            )
        if pending.view in _BATTLE_VIEWS:
            row = next(
                (entry for entry in ranking.rows if entry.rank == pending.battle_rank),
                None,
            )
            if row is None:
                return Outcome(message=_no_such_rank(ranking, pending.battle_rank))
            return await self._render_battle_view(
                row.battle_id, pending.view, query=query
            )
        if pending.view is CandidateView.ROSTER:
            rendered = await renderer.render_roster(
                ranking,
                query=query,
                ranking_limit=ranking_limit,
                web_base_url=self._web_base_url,
            )
            return Outcome.image(rendered, target=board)

        recipe = await prepare_ranking(
            self._data,
            ranking,
            query=query,
            web_base_url=self._web_base_url,
            ranking_limit=ranking_limit,
            character_filter=pending.character_filter,
            element_filter=pending.element_filter,
        )
        if isinstance(recipe, str):
            return Outcome(message=recipe)
        return Outcome.image(await recipe.draw(renderer), target=board)

    # --- battles ---------------------------------------------------------------------

    async def _render_battle_view(
        self,
        battle_id: str,
        view: CandidateView,
        *,
        query: str,
    ) -> Outcome:
        """Draw the page a 战报 / 配装 / 技能 / 技能轴 request asked for.

        The timeline reads the public export, the other three the battle
        detail. Older uploads carry no roster loadout, skill statistics or
        cast sequence; those get a short text instead of an empty page.
        """

        renderer = self._renderer()
        if view is CandidateView.TIMELINE:
            # The detail only adds the BUFF 覆盖 band, so it is fetched
            # alongside the export rather than after it; awaiting it second
            # put its whole client budget behind the export's.
            export, battle = await asyncio.gather(
                self._data.get_battle_export(battle_id),
                self._battle_detail_if_available(battle_id),
                return_exceptions=True,
            )
            if isinstance(export, ZmdLogsAPIError):
                refusal = export_refusal(export)
                if refusal is None:
                    raise export
                self._logger.warning(
                    "ZmdLogBot API request failed: %s", export.code
                )
                return Outcome(message=refusal)
            if isinstance(export, BaseException):
                raise export
            detail = None if isinstance(battle, BaseException) else battle
            rendered = await renderer.render_timeline(
                export,
                query=query,
                web_base_url=self._web_base_url,
                battle=detail,
            )
            return Outcome.image(
                rendered, target=_battle_target(battle_id, view, detail)
            )
        if view in (CandidateView.LOADOUT, CandidateView.SKILLS):
            # Neither page reads the cast export, so neither pays for it.
            battle = await self._data.get_battle_detail(battle_id)
            if view is CandidateView.LOADOUT:
                if not battle.roster:
                    return Outcome(message=messages.NO_LOADOUT)
                rendered = await renderer.render_loadout(
                    battle,
                    query=query,
                    web_base_url=self._web_base_url,
                    suits=await self._data.equip_suits_for(battle),
                )
            else:
                if not battle.skill_stats:
                    return Outcome(message=messages.NO_SKILL_STATS)
                rendered = await renderer.render_skills(
                    battle, query=query, web_base_url=self._web_base_url
                )
            return Outcome.image(
                rendered, target=_battle_target(battle_id, view, battle)
            )
        recipe = await prepare_battle(
            self._data, battle_id, query=query, web_base_url=self._web_base_url
        )
        return Outcome.image(
            await recipe.draw(renderer), target=_card_target(recipe, battle_id)
        )

    async def _render_compare_self(
        self, ranking: BossRanking, me: BoundAccount, *, rank: int, query: str
    ) -> Outcome:
        """``对比 … 我``: the asker's best record on the board against one rank.

        The best record is the account's highest row on the ranking the
        metric names; the primary account's only, so one without a row is
        told so rather than compared through another account. The better
        rank is drawn first, as the two-rank form draws the leader first.
        """

        name = shorten(me.display_name)
        ranking_name = board_qualifier(ranking.metric)
        mine = next(
            (row for row in ranking.rows if row.account_id == me.account_id), None
        )
        if mine is None:
            return Outcome(
                message=messages.COMPARE_SELF_NO_RECORD.format(
                    name=name, board=ranking.boss_name, ranking=ranking_name
                )
            )
        if mine.rank == rank:
            return Outcome(
                message=messages.COMPARE_SELF_IS_RANK.format(
                    name=name, board=ranking.boss_name, ranking=ranking_name, rank=rank
                )
            )
        other = next((row for row in ranking.rows if row.rank == rank), None)
        if other is None:
            return Outcome(message=_no_such_rank(ranking, rank))
        first, second = sorted((mine, other), key=lambda row: row.rank)
        return await self._render_compare(
            first.battle_id,
            second.battle_id,
            query=query,
            rank_a=first.rank,
            rank_b=second.rank,
            metric=ranking.metric,
        )

    async def _render_compare(
        self,
        battle_id_a: str,
        battle_id_b: str,
        *,
        query: str,
        rank_a: int | None = None,
        rank_b: int | None = None,
        metric: str = METRIC_DPS,
    ) -> Outcome:
        """Two battles side by side; both details are fetched concurrently.

        ``metric`` names the ranking the two ranks were read off.
        """

        if battle_id_a == battle_id_b:
            return Outcome(message=messages.COMPARE_SAME_BATTLE)
        renderer = self._renderer()
        recipe = await prepare_compare(
            self._data,
            battle_id_a,
            battle_id_b,
            query=query,
            web_base_url=self._web_base_url,
            rank_a=rank_a,
            rank_b=rank_b,
            metric=metric,
        )
        if recipe.refusal is not None:
            return Outcome(message=recipe.refusal)
        # Two battles: neither one's ZMDLogs page is this page.
        return Outcome.image(await recipe.draw(renderer))

    async def _battle_detail_if_available(
        self,
        battle_id: str,
    ) -> BattleDetailSummary | None:
        """The battle detail for a page that can do without it."""

        try:
            return await self._data.get_battle_detail(battle_id)
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot battle detail unavailable: %s",
                getattr(exc, "code", type(exc).__name__),
            )
            return None

    def _pick_list(
        self,
        entry: PendingCandidates,
        *,
        note: str | None = None,
    ) -> Outcome:
        return Outcome.pick_list(
            entry, ttl_seconds=self._candidates.ttl_seconds, note=note
        )


# --- page targets -------------------------------------------------------------------


def _battle_target(
    battle_id: str,
    view: CandidateView,
    battle: BattleDetailSummary | None = None,
    *,
    casts_refused: bool = False,
) -> PageTarget:
    """The battle a page is about, less the pages its upload cannot draw.

    Only what the page already read counts: without the detail the loadout
    and skill pages are assumed there, and only the export endpoint's own
    refusal of an old upload rules the rail out (a rate limit passes).
    """

    unavailable = set()
    if battle is not None and not battle.roster:
        unavailable.add(CandidateView.LOADOUT)
    if battle is not None and not battle.skill_stats:
        unavailable.add(CandidateView.SKILLS)
    if casts_refused:
        unavailable.add(CandidateView.TIMELINE)
    return PageTarget(
        PageSubject.BATTLE, battle_id, view, unavailable=frozenset(unavailable)
    )


def _card_target(recipe: BattleRecipe, battle_id: str) -> PageTarget:
    """The battle card's target: it read the detail and asked for the casts."""

    return _battle_target(
        battle_id,
        CandidateView.BATTLE,
        recipe.battle,
        casts_refused=recipe.casts_unsupported,
    )


# --- route helpers ------------------------------------------------------------------


def route_view(route: RouteRequest) -> CandidateView:
    """The page family a route draws; the ranking page unless it says otherwise."""

    return _ROUTE_VIEWS.get(route.kind, CandidateView.RANKING)


def pending_from_route(route: RouteRequest) -> PendingCandidates:
    """Carry the route's view and options in the same shape candidates use."""

    return PendingCandidates(
        code="",
        query=route.query,
        choices=(),
        ranking_top=route.ranking_top,
        created_at=0.0,
        view=route_view(route),
        character_filter=route.character_filter,
        element_filter=route.element_filter,
        stats_range=route.stats_range,
        stats_potential=route.stats_potential,
        battle_rank=route.battle_rank,
        compare_rank=route.compare_rank if route.compare_rank is not None else 2,
        metric=route.metric,
        compare_self=route.compare_self,
    )


def looks_like_direct_slug(query: str) -> bool:
    # Battle and account ids share the slug alphabet but never name a board.
    return (
        is_valid_boss_slug(query)
        and ("_" in query or "-" in query)
        and not query.startswith(("btl_", "usr_"))
    )


def _plain_ranking_query(view: CandidateView, pending: PendingCandidates) -> bool:
    """A bare keyword on the smart route, where an account may be meant too."""

    return (
        view is CandidateView.RANKING
        and pending.ranking_top is None
        and pending.character_filter is None
        and pending.element_filter is None
    )


def _not_found_message(query: str, view: CandidateView) -> str:
    shown = shorten(query)
    if view in _BOARD_ONLY_VIEWS:
        return f"没有找到与「{shown}」匹配的榜单。"
    return f"没有找到与「{shown}」匹配的榜单或副本。"


def _no_such_rank(ranking: BossRanking, rank: int) -> str:
    return (
        f"「{ranking.boss_name}」{board_qualifier(ranking.metric)}"
        f"公开排名共 {len(ranking.rows)} 条，没有第 {rank} 名。"
    )


# --- error wording ------------------------------------------------------------------


def api_error_message(route: RouteRequest, error: ZmdLogsAPIError) -> str:
    """The reply for an upstream error on a route, by what the route asked for."""

    if error.status_code == 404:
        if route.kind in _ACCOUNT_ROUTES:
            return messages.ACCOUNT_NOT_FOUND
        if route.kind in BATTLE_STYLE_ROUTES or route.kind is RouteKind.COMPARE_QUERY:
            return messages.BATTLE_NOT_FOUND
        if route.kind in _BOARD_ROUTES:
            return board_api_error_message(error)
    return messages.UPSTREAM_UNAVAILABLE


def board_api_error_message(error: ZmdLogsAPIError) -> str:
    if error.status_code == 404:
        if error.code == CHARACTER_STATS_UNAVAILABLE:
            return messages.CRISIS_CONTRACT_NO_STATISTICS
        return messages.BOARD_NOT_FOUND
    return messages.UPSTREAM_UNAVAILABLE


def battle_link_error_message(error: ZmdLogsAPIError) -> str:
    if error.status_code == 404:
        return messages.BATTLE_LINK_NOT_FOUND
    return messages.UPSTREAM_UNAVAILABLE


def tool_api_error_message(error: ZmdLogsAPIError) -> str:
    """The reply for an upstream error inside a tool, by the code upstream gave.

    A tool has no route kind to say what was asked, but upstream's own code
    does: a crisis-contract statistics read must not come back as "the
    service is down".
    """

    if error.status_code == 404:
        if error.code == CHARACTER_STATS_UNAVAILABLE:
            return messages.CRISIS_CONTRACT_NO_STATISTICS
        if error.code == "boss_not_found":
            return messages.BOARD_NOT_FOUND
        if error.code == "character_not_found":
            return messages.CHARACTER_NOT_IN_RECORDS
        if error.code == "battle_not_found":
            return messages.BATTLE_NOT_FOUND
        return messages.PUBLIC_DATA_NOT_FOUND
    if error.status_code == 429:
        return messages.RATE_LIMITED
    return messages.UPSTREAM_UNAVAILABLE


def _catalog_names(entries: tuple[CharacterCatalogEntry, ...]) -> tuple[str, ...]:
    return tuple(entry.name for entry in entries)

