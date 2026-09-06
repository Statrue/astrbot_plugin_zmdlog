"""The query pipeline: from a parsed route to one image or one short text.

:class:`QueryService` is everything between the AstrBot handlers and the
renderer. It resolves references and keywords, decides which page a route
draws, and turns the upstream's known refusals (a vanished account, an old
upload without casts, a rate limit) into the short replies the user sees.
Unknown failures propagate; the host's error ladder turns those into the
generic wording.

Two kinds of route exist. A *direct* route names its target — help, the
board index, an account id or link, a battle id or link — and never
consults the board matcher. A *keyword* route goes through the matcher
(``_dispatch_keyword``) and, when the best board hit is weak, is weighed
against character names and public nicknames, because the smart route
searches boards and accounts alike.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import messages
from .candidates import (
    MAX_CANDIDATES,
    CandidateStore,
    CandidateView,
    PendingCandidates,
    account_choice,
    format_candidates,
)
from .characters import (
    CharacterFilterScope,
    CharacterResolution,
    CharacterResolutionStatus,
    pick_character_filter_scope,
    ranking_character_names,
    resolve_character_name,
)
from .client import (
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsClientError,
    is_valid_boss_slug,
    searchable_nickname,
)
from .datasource import CharacterCatalogEntry, ZmdLogsDataSource
from .events import board_activity
from .history import window_label, window_start
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
from .models import (
    BattleDetailSummary,
    BattleExport,
    HotBossCard,
)
from .rank_watch import RankWatcher
from .render import LongImageRenderer
from .routing import DEFAULT_RANKING_TOP, RouteKind, RouteRequest
from .settings import PluginSettings
from .standings import (
    account_tallies,
    by_profession,
    character_standings,
    character_tallies,
    first_place_teams,
    profession_usage,
    roster_character_names,
    unseen_characters,
)

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
# pick list of its boards for these.
_BOARD_ONLY_VIEWS = (
    CandidateView.CHARACTER_STATS,
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
EXPORT_UNSUPPORTED = "battle_export_unsupported"

BoardMatcher = Callable[[tuple[HotBossCard, ...]], RankingMatcher]


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a query produced: a rendered image, or a short text instead."""

    image_path: str | None = None
    message: str | None = None


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
        watcher: RankWatcher,
        settings: PluginSettings,
        logger: LogSink,
    ) -> None:
        self._client = client
        self._data = data
        # Looked up per query: the host may lose or replace its renderer.
        self._renderer = renderer
        self._candidates = candidates
        self._board_matcher = board_matcher
        self._watcher = watcher
        self._web_base_url = settings.web_base_url
        self._logger = logger

    # --- entry points ----------------------------------------------------------------

    async def dispatch(
        self,
        route: RouteRequest,
        *,
        command_prefix: str,
        origin: str = "",
    ) -> Outcome:
        """Answer one parsed command; ``origin`` keys any pick list it posts."""

        direct = await self._dispatch_direct(
            route, command_prefix=command_prefix, origin=origin
        )
        if direct is not None:
            return direct
        return await self._dispatch_keyword(route, origin=origin)

    async def render_pick(
        self,
        entry: PendingCandidates,
        choice: MatchChoice,
    ) -> Outcome:
        """Draw the page a quoted pick-list reply selected."""

        cards = await self._data.list_hot_bosses()
        return await self._render_choice(
            choice, cards, query=entry.query, pending=entry
        )

    async def render_battle_card(self, battle_id: str) -> Outcome:
        """The battle card for an auto-expanded link."""

        battle, export, note = await self._battle_with_export(battle_id)
        image_path = await self._renderer().render_battle(
            battle,
            query=battle_id,
            web_base_url=self._web_base_url,
            export=export,
            export_note=note,
            suits=await self._equip_suits(),
        )
        return Outcome(image_path=image_path)

    async def _equip_suits(self) -> dict[str, str]:
        """The suit catalog, or nothing; a gear page must render without it."""

        try:
            return await self._data.get_equip_suits()
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot equip catalog unavailable: %s", type(exc).__name__
            )
            return {}

    # --- routes that name their target -----------------------------------------------

    async def _dispatch_direct(
        self,
        route: RouteRequest,
        *,
        command_prefix: str,
        origin: str,
    ) -> Outcome | None:
        """Routes that need no board keyword; None hands over to the matcher."""

        renderer = self._renderer()
        if route.kind is RouteKind.HELP:
            image_path = await renderer.render_help(command_prefix=command_prefix)
            return Outcome(image_path=image_path)

        if route.kind is RouteKind.ALL_RANKINGS:
            cards = await self._data.list_hot_bosses()
            image_path = await renderer.render_all_top3(
                cards, query="榜单", web_base_url=self._web_base_url
            )
            return Outcome(image_path=image_path)

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
            return await self._render_player_champions(route.stats_range)

        if route.kind is RouteKind.RECORDS_QUERY:
            return await self._render_records(route.stats_range)

        if route.kind is RouteKind.CHARACTER_STANDINGS:
            return await self._render_character_standings(
                route.query,
                element_filter=route.element_filter,
                profession_filter=route.profession_filter,
                time_range=route.stats_range,
            )

        if route.kind is RouteKind.CHARACTER_STATS and not route.query.strip():
            stats = await self._data.get_character_statistics(
                None,
                time_range=route.stats_range,
                potential=route.stats_potential,
            )
            image_path = await renderer.render_character_stats(
                stats, query="角色统计", web_base_url=self._web_base_url
            )
            return Outcome(image_path=image_path)
        return None

    async def _dispatch_trend(self, route: RouteRequest, *, origin: str) -> Outcome:
        account_id = self._account_reference(route.query)
        if account_id is not None:
            return await self._render_trend(
                account_id, query=route.query, time_range=route.stats_range
            )
        # A watched account is addressable by the nickname its history
        # already holds, with no upstream search at all.
        local = self._watcher.history_by_name(route.query)
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
            return Outcome(message=self._format_candidates(entry))
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

    async def _dispatch_keyword(self, route: RouteRequest, *, origin: str) -> Outcome:
        """Resolve the keyword against the board index, then draw the view."""

        view = route_view(route)
        pending = pending_from_route(route)
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
            )
            return Outcome(message=self._format_candidates(entry))
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

        if pending.ranking_top is not None:
            return Outcome(
                message="--top 仅适用于具体榜单查询，请补充具体榜单关键词。"
            )
        if pending.character_filter is not None:
            return Outcome(
                message="--角色 仅适用于具体榜单查询，请补充具体榜单关键词。"
            )
        selected_slugs = set(target.boss_slugs)
        selected_cards = tuple(
            card for card in cards if card.boss_slug in selected_slugs
        )
        image_path = await self._renderer().render_dungeon_top3(
            choice, selected_cards, query=query, web_base_url=self._web_base_url
        )
        return Outcome(image_path=image_path)

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
        return Outcome(
            message=self._format_candidates(
                entry,
                note=(
                    "还有更多同名结果未列出，可输入更完整的昵称。"
                    if search.has_more
                    else None
                ),
            )
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
            account = await self._data.get_public_user_rankings(account_id)
        except ZmdLogsAPIError as exc:
            if exc.status_code != 404:
                raise
            self._logger.warning("ZmdLogBot API request failed: %s", exc.code)
            return Outcome(message=messages.ACCOUNT_NOT_FOUND)
        # Avatars, professions and the main C of each record come off the
        # ranking index; the user endpoint only names the roster.
        rows, listed = await self._data.index_rows_for(
            row.battle_id for row in account.rankings
        )
        image_path = await renderer.render_account(
            account,
            query=query,
            web_base_url=self._web_base_url,
            rows_by_battle=rows,
            listed_boards=listed,
            elements=await self._data.character_elements(),
        )
        return Outcome(image_path=image_path)

    async def _render_trend(
        self,
        account_id: str,
        *,
        query: str,
        time_range: str,
    ) -> Outcome:
        """Draw the rank trace of one account; text when nothing was recorded.

        The trace only exists for accounts the rank watch polls, so this never
        talks to upstream: an unknown account is answered with how to start.
        """

        history = self._watcher.history_for(account_id)
        if history is None or not history.boards:
            return Outcome(message=messages.TREND_NO_DATA)
        image_path = await self._renderer().render_trend(
            history,
            query=query,
            web_base_url=self._web_base_url,
            time_range=time_range,
            last_checked=self._watcher.last_checked(account_id),
        )
        return Outcome(image_path=image_path)

    # --- characters ------------------------------------------------------------------

    async def _render_records(self, time_range: str) -> Outcome:
        """New records and first places changing hands, from the event log."""

        index = self._data.ranking_index
        await index.ensure_filled()
        rankings = tuple(entry.ranking for entry in index.entries())
        span = time_range if time_range != "all" else "30d"
        since = window_start(span, now=datetime.now(UTC))
        log = self._data.event_log
        image_path = await self._renderer().render_records(
            log.recent(since=since),
            board_activity(rankings, since=since),
            query="新纪录",
            window_label=window_label(span),
            age_seconds=index.oldest_age_seconds(),
            log_since=log.oldest_seen_at(),
        )
        return Outcome(image_path=image_path)

    async def _render_player_champions(self, time_range: str) -> Outcome:
        """Which public accounts uploaded the most first places, from the index."""

        index = self._data.ranking_index
        await index.ensure_filled()
        rankings = tuple(entry.ranking for entry in index.entries())
        since = window_start(time_range, now=datetime.now(UTC))
        image_path = await self._renderer().render_player_champions(
            account_tallies(rankings, since=since),
            board_count=len(rankings),
            query="玩家冠军榜",
            age_seconds=index.oldest_age_seconds(),
            window_label=window_label(time_range),
        )
        return Outcome(image_path=image_path)

    async def _render_character_standings(
        self,
        query: str,
        *,
        element_filter: str | None = None,
        profession_filter: str | None = None,
        time_range: str = "all",
    ) -> Outcome:
        """Where the teams fielding one character stand on every board.

        Drawn from the ranking index, never from upstream directly: the name
        is resolved against every roster the index holds (four-stars count),
        and the page says how old the index is.
        """

        index = self._data.ranking_index
        await index.ensure_filled()
        rankings = tuple(entry.ranking for entry in index.entries())
        elements = await self._data.character_elements()
        if not query.strip():
            since = window_start(time_range, now=datetime.now(UTC))
            tallies = character_tallies(rankings, since=since)
            if element_filter is not None:
                tallies = tuple(
                    tally for tally in tallies
                    if elements.get(tally.name) == element_filter
                )
            unseen: tuple[str, ...] = ()
            if profession_filter is not None:
                tallies = by_profession(tallies, profession_filter)
                unseen = unseen_characters(
                    tallies,
                    await self._data.character_professions(),
                    profession=profession_filter,
                )
            image_path = await self._renderer().render_character_champions(
                tallies,
                board_count=len(rankings),
                query="角色排名",
                web_base_url=self._web_base_url,
                age_seconds=index.oldest_age_seconds(),
                element=element_filter,
                elements=elements,
                profession=profession_filter,
                teams=first_place_teams(rankings, since=since),
                usage=profession_usage(rankings, since=since),
                window_label=window_label(time_range),
                unseen=unseen,
            )
            return Outcome(image_path=image_path)
        resolution = resolve_character_name(query, roster_character_names(rankings))
        if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
            options = " / ".join(resolution.candidates)
            return Outcome(
                message=f"「{messages.shorten(query)}」可能是：{options}，请写全名。"
            )
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            return Outcome(message=messages.CHARACTER_NOT_IN_RECORDS)
        standings = character_standings(rankings, resolution.name)
        image_path = await self._renderer().render_character_standings(
            standings,
            query=query,
            web_base_url=self._web_base_url,
            age_seconds=index.oldest_age_seconds(),
            elements=elements,
        )
        return Outcome(image_path=image_path)

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
            options = " / ".join(resolution.candidates)
            return Outcome(
                message=f"「{resolution.query}」可能是：{options}，请写全名。"
            )
        character_key = next(
            entry.key for entry in entries if entry.name == resolution.name
        )
        stats = await self._data.get_character_boss_statistics(
            character_key,
            time_range=pending.stats_range,
            potential=pending.stats_potential,
        )
        image_path = await self._renderer().render_character_boss(
            stats, query=query, web_base_url=self._web_base_url
        )
        return Outcome(image_path=image_path)

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
        if pending.view is CandidateView.CHARACTER_STATS:
            stats = await self._data.get_character_statistics(
                boss_slug,
                time_range=pending.stats_range,
                potential=pending.stats_potential,
            )
            image_path = await renderer.render_character_stats(
                stats, query=query, web_base_url=self._web_base_url
            )
            return Outcome(image_path=image_path)

        ranking = await self._data.get_boss_ranking(boss_slug)
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
            image_path = await renderer.render_roster(
                ranking,
                query=query,
                ranking_limit=ranking_limit,
                web_base_url=self._web_base_url,
            )
            return Outcome(image_path=image_path)

        character_filter: tuple[str, ...] | None = None
        character_filter_scope = CharacterFilterScope.MAIN
        if pending.character_filter is not None:
            resolved = _resolve_filter_names(ranking, pending.character_filter)
            if isinstance(resolved, Outcome):
                return resolved
            character_filter = resolved
            if len(resolved) == 1:
                # No main-C records is the normal case for supports, so widen
                # the filter to the whole roster instead of answering
                # "nothing found".
                character_filter_scope = pick_character_filter_scope(
                    ranking, resolved[0]
                )
                if character_filter_scope is CharacterFilterScope.NONE:
                    return Outcome(
                        message=(
                            f"「{ranking.boss_name}」的公开排名里没有带"
                            f"「{resolved[0]}」的记录。"
                        )
                    )
            else:
                # Several names ask for teams fielding all of them; a team has
                # one main C, so this is a roster question by definition.
                character_filter_scope = CharacterFilterScope.ROSTER
                if not any(
                    all(
                        any(
                            entry.character_name == name
                            for entry in row.roster_entries
                        )
                        for name in resolved
                    )
                    for row in ranking.rows
                ):
                    return Outcome(
                        message=(
                            f"「{ranking.boss_name}」的公开排名里没有同时带上"
                            f"「{'、'.join(resolved)}」的记录。"
                        )
                    )
        elements = await self._data.character_elements()
        element_filter = pending.element_filter
        if element_filter is not None and not any(
            elements.get(row.character_name) == element_filter for row in ranking.rows
        ):
            return Outcome(
                message=(
                    f"「{ranking.boss_name}」的公开排名里没有主 C 为"
                    f"{element_filter}属性的记录。"
                )
            )
        image_path = await renderer.render_ranking(
            ranking,
            query=query,
            ranking_limit=ranking_limit,
            web_base_url=self._web_base_url,
            character_filter=character_filter,
            character_filter_scope=character_filter_scope,
            element_filter=element_filter,
            elements=elements,
        )
        return Outcome(image_path=image_path)

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
                refusal = _export_refusal(export)
                if refusal is None:
                    raise export
                self._logger.warning(
                    "ZmdLogBot API request failed: %s", export.code
                )
                return Outcome(message=refusal)
            if isinstance(export, BaseException):
                raise export
            image_path = await renderer.render_timeline(
                export,
                query=query,
                web_base_url=self._web_base_url,
                battle=None if isinstance(battle, BaseException) else battle,
            )
            return Outcome(image_path=image_path)
        if view in (CandidateView.LOADOUT, CandidateView.SKILLS):
            # Neither page reads the cast export, so neither pays for it.
            battle = await self._data.get_battle_detail(battle_id)
            if view is CandidateView.LOADOUT:
                if not battle.roster:
                    return Outcome(message=messages.NO_LOADOUT)
                image_path = await renderer.render_loadout(
                    battle,
                    query=query,
                    web_base_url=self._web_base_url,
                    suits=await self._equip_suits(),
                )
            else:
                if not battle.skill_stats:
                    return Outcome(message=messages.NO_SKILL_STATS)
                image_path = await renderer.render_skills(
                    battle, query=query, web_base_url=self._web_base_url
                )
            return Outcome(image_path=image_path)
        battle, export, note = await self._battle_with_export(battle_id)
        image_path = await renderer.render_battle(
            battle,
            query=query,
            web_base_url=self._web_base_url,
            export=export,
            export_note=note,
            suits=await self._equip_suits(),
        )
        return Outcome(image_path=image_path)

    async def _battle_with_export(
        self,
        battle_id: str,
    ) -> tuple[BattleDetailSummary, BattleExport | None, str | None]:
        """The battle card's two reads, fetched together.

        Serially, a slow or rate-limited export endpoint spent its whole
        15-second client budget in front of the detail, so the card the
        reader asked for waited on the section it can do without.
        """

        detail, extra = await asyncio.gather(
            self._data.get_battle_detail(battle_id),
            self._battle_export_for_card(battle_id),
            return_exceptions=True,
        )
        if isinstance(detail, BaseException):
            raise detail
        if isinstance(extra, BaseException):
            # _battle_export_for_card answers its own failures, so reaching
            # here is a bug rather than an outage; the card stands without.
            self._logger.warning(
                "ZmdLogBot battle export raised unexpectedly: %s",
                type(extra).__name__,
            )
            return detail, None, None
        export, note = extra
        return detail, export, note

    async def _battle_export_for_card(
        self,
        battle_id: str,
    ) -> tuple[BattleExport | None, str | None]:
        """The cast sequence for the battle card, or a one-line reason without.

        Best effort: the card must never fail because the export did. An old
        upload and a rate limit get a note the card can print; anything else
        is logged and the section is simply left out.
        """

        try:
            return await self._data.get_battle_export(battle_id), None
        except ZmdLogsAPIError as exc:
            self._logger.warning("ZmdLogBot battle export unavailable: %s", exc.code)
            return None, _export_refusal(exc)
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot battle export unavailable: %s", type(exc).__name__
            )
            return None, None

    async def _render_compare(
        self,
        battle_id_a: str,
        battle_id_b: str,
        *,
        query: str,
        rank_a: int | None = None,
        rank_b: int | None = None,
    ) -> Outcome:
        """Two battles side by side; both details are fetched concurrently."""

        if battle_id_a == battle_id_b:
            return Outcome(message=messages.COMPARE_SAME_BATTLE)
        renderer = self._renderer()
        first, second = await asyncio.gather(
            self._data.get_battle_detail(battle_id_a),
            self._data.get_battle_detail(battle_id_b),
        )
        if first.boss_name != second.boss_name:
            return Outcome(
                message=messages.COMPARE_CROSS_BOSS.format(
                    first=shorten(first.boss_name), second=shorten(second.boss_name)
                )
            )
        image_path = await renderer.render_compare(
            first,
            second,
            query=query,
            web_base_url=self._web_base_url,
            rank_a=rank_a,
            rank_b=rank_b,
            suits=await self._equip_suits(),
        )
        return Outcome(image_path=image_path)

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

    def _format_candidates(
        self,
        entry: PendingCandidates,
        *,
        note: str | None = None,
    ) -> str:
        return format_candidates(
            entry, ttl_seconds=self._candidates.ttl_seconds, note=note
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
    )


def looks_like_direct_slug(query: str) -> bool:
    return is_valid_boss_slug(query) and ("_" in query or "-" in query)


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


def _no_such_rank(ranking, rank: int) -> str:
    return (
        f"「{ranking.boss_name}」公开排名共 {len(ranking.rows)} 条，"
        f"没有第 {rank} 名。"
    )


def _resolve_filter_names(ranking, character_filter: str) -> tuple[str, ...] | Outcome:
    """Resolve every ``--角色`` name against the board; an Outcome is a refusal."""

    names: list[str] = []
    for wanted in character_filter.split():
        resolution = resolve_character_name(wanted, ranking_character_names(ranking))
        if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
            options = " / ".join(resolution.candidates)
            return Outcome(
                message=f"「{shorten(resolution.query)}」可能是：{options}，请写全名。"
            )
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            return Outcome(
                message=(
                    f"「{ranking.boss_name}」的公开排名里没有"
                    f"「{shorten(resolution.query)}」。"
                )
            )
        if resolution.name not in names:
            names.append(resolution.name)
    return tuple(names)


def _export_refusal(error: ZmdLogsAPIError) -> str | None:
    """The one-line reason the export endpoint gave, for the two known refusals."""

    if error.status_code == 422 and error.code == EXPORT_UNSUPPORTED:
        return messages.NO_TIMELINE
    if error.status_code == 429:
        return messages.TIMELINE_RATE_LIMITED
    return None


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


def _catalog_names(entries: tuple[CharacterCatalogEntry, ...]) -> tuple[str, ...]:
    return tuple(entry.name for entry in entries)

