"""What the LLM tools do, without any of the AstrBot they run inside.

Each method resolves what the model asked for, draws the page zmdlog would
have drawn for that question, and returns both: the picture carries the
numbers, the text carries what they are. The split is deliberate — a model
handed numbers as text restates them wrongly, and a picture cannot be
reasoned about — so the tool descriptions tell the model the figures are in
the image and its own job is to explain them.

Nothing here posts a candidate list. A command can afford to ask "did you
mean one of these five"; a tool call cannot wait for an answer, so an
ambiguous name is reported as such, with the options, and the model asks.

There are four tools, one per subject — board, battle, character, account —
never one per feature. AstrBot sends every active tool's schema with every
LLM request, and a model choosing among overlapping tools picks the wrong
one; a new view of a subject is a parameter or extra lines in its text.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import facts, messages
from .candidates import MAX_CANDIDATES
from .characters import (
    CharacterFilterScope,
    CharacterResolutionStatus,
    pick_character_filter_scope,
    ranking_character_names,
    resolve_character_name,
)
from .client import (
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsClientError,
    searchable_nickname,
)
from .datasource import ZmdLogsDataSource
from .elements import ELEMENTS, normalize_element
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
from .models import BossRanking, HotBossCard
from .professions import PROFESSIONS, normalize_profession
from .rank_watch import RankWatcher
from .render import LongImageRenderer
from .routing import parse_potential_text, parse_range_text
from .settings import PluginSettings
from .standings import (
    account_tallies,
    account_tally,
    by_profession,
    character_standings,
    character_tallies,
    first_place_teams,
    profession_usage,
    roster_character_names,
    unseen_characters,
)

BoardMatcher = Callable[[tuple[HotBossCard, ...]], RankingMatcher]

# What people say when they mean every board at once.
_EVERY_BOARD = frozenset(
    {
        "全部", "所有", "全部榜单", "所有榜单", "全部副本", "所有副本",
        "所有首领", "全部首领", "all",
    }
)


@dataclass(frozen=True, slots=True)
class ToolAnswer:
    """What one tool call produced.

    ``text`` always says something, including when the answer is that the
    data does not cover the question. ``image_path`` is set only when zmdlog
    has a page for this question and it rendered.
    """

    text: str
    image_path: str | None = None

    def noted(self, note: str) -> "ToolAnswer":
        """The same answer with a one-line note in front, when there is one."""

        if not note:
            return self
        return ToolAnswer(note + "\n" + self.text, self.image_path)


class ToolService:
    """Answer the questions the LLM tools accept."""

    def __init__(
        self,
        *,
        client: ZmdLogsClient,
        data: ZmdLogsDataSource,
        renderer: Callable[[], LongImageRenderer],
        board_matcher: BoardMatcher,
        settings: PluginSettings,
        logger: LogSink,
        watcher: RankWatcher | None = None,
    ) -> None:
        self._client = client
        self._data = data
        self._renderer = renderer
        self._board_matcher = board_matcher
        self._web_base_url = settings.web_base_url
        self._logger = logger
        # The rank watch's trace is the only rank history there is; the
        # account tool reads it when it has one.
        self._watcher = watcher

    # --- boards -----------------------------------------------------------------

    async def board(
        self,
        keyword: str,
        *,
        character: str = "",
        limit: int = facts.DEFAULT_ROW_LIMIT,
        element: str = "",
        time_range: str = "",
        profession: str = "",
    ) -> ToolAnswer:
        if not keyword.strip():
            return await self._records(time_range)
        span, range_note = _time_range(time_range)
        wanted = self._element(element)
        if isinstance(wanted, ToolAnswer):
            return wanted
        role = self._profession(profession)
        if isinstance(role, ToolAnswer):
            return role
        if _means_every_board(keyword):
            return await self._boards_overview(keyword)
        target = await self._resolve_target(keyword)
        if isinstance(target, ToolAnswer):
            return target
        if not isinstance(target, str):
            choice, cards = target
            return await self._dungeon_overview(keyword, choice, cards)
        ranking = await self._data.get_boss_ranking(target)
        elements = await self._data.character_elements()
        name, scope = "", CharacterFilterScope.ROSTER
        if character.strip():
            resolved = self._resolve_character(ranking, character)
            if isinstance(resolved, ToolAnswer):
                return resolved
            name = resolved
            # Main-C rows when there are any, the whole roster otherwise —
            # the same choice the command makes, so page and text agree.
            scope = pick_character_filter_scope(ranking, name)
        if wanted:
            if not elements:
                return ToolAnswer(
                    "角色属性目录暂时读不到，无法按属性筛选，请稍后再试。"
                )
            if not any(
                elements.get(row.character_name) == wanted for row in ranking.rows
            ):
                return ToolAnswer(
                    f"「{ranking.boss_name}」的公开记录里没有主C 为{wanted}属性的队伍。"
                )
        if role and not any(
            normalize_profession(row.character_profession or "") == role
            for row in ranking.rows
        ):
            return ToolAnswer(
                f"「{ranking.boss_name}」的公开记录里没有主C 为{role}的队伍。"
            )
        since = window_start(span, now=datetime.now(UTC))
        events = ()
        if since is not None:
            events = tuple(
                event
                for event in self._data.event_log.recent(since=since)
                if event.boss_slug == ranking.boss_slug
            )
        limit = max(1, min(limit, facts.MAX_ROW_LIMIT))
        text = facts.format_board_ranking(
            ranking,
            limit=limit,
            character=name or None,
            character_scope=scope,
            element=wanted or None,
            elements=elements,
            profession=role or None,
            since=since,
            window_label=window_label(span),
            events=events,
        )
        image = await self._render(
            lambda renderer: renderer.render_ranking(
                ranking,
                query=keyword,
                ranking_limit=max(limit, facts.DEFAULT_ROW_LIMIT),
                web_base_url=self._web_base_url,
                character_filter=(name,) if name else None,
                character_filter_scope=scope,
                element_filter=wanted or None,
                profession_filter=role or None,
                elements=elements,
            )
        )
        return ToolAnswer(text, image).noted(range_note)

    async def _records(self, time_range: str) -> ToolAnswer:
        """New records, first places changing hands and board activity."""

        span, range_note = _time_range(time_range)
        if span == "all":
            span = "7d"
        index = self._data.ranking_index
        await index.ensure_filled()
        rankings = tuple(entry.ranking for entry in index.entries())
        since = window_start(span, now=datetime.now(UTC))
        log = self._data.event_log
        events = log.recent(since=since)
        activity = board_activity(rankings, since=since)
        label = window_label(span)
        age = index.oldest_age_seconds()
        text = facts.format_records(
            events,
            activity,
            window_label=label,
            age_seconds=age,
            log_since=log.oldest_seen_at(),
        )
        image = await self._render(
            lambda renderer: renderer.render_records(
                events,
                activity,
                query="新纪录",
                window_label=label,
                age_seconds=age,
                log_since=log.oldest_seen_at(),
            )
        )
        return ToolAnswer(text, image).noted(range_note)

    async def _boards_overview(self, keyword: str) -> ToolAnswer:
        """Every board's first place, from the board list itself."""

        cards = await self._data.list_hot_bosses()
        text = facts.format_boards_overview(
            cards, title="全部公开榜单", runs_per_board=1
        )
        image = await self._render(
            lambda renderer: renderer.render_all_top3(
                cards, query=keyword, web_base_url=self._web_base_url
            )
        )
        return ToolAnswer(text, image)

    async def _dungeon_overview(
        self,
        keyword: str,
        choice: MatchChoice,
        cards: tuple[HotBossCard, ...],
    ) -> ToolAnswer:
        """The top three of every board of one dungeon or phase."""

        text = facts.format_boards_overview(
            cards, title=choice.target.name, runs_per_board=3
        )
        image = await self._render(
            lambda renderer: renderer.render_dungeon_top3(
                choice, cards, query=keyword, web_base_url=self._web_base_url
            )
        )
        return ToolAnswer(text, image)

    # --- battles ----------------------------------------------------------------

    async def battle(self, reference: str, compare_with: str = "") -> ToolAnswer:
        """One battle, or two side by side when a second reference is given."""

        if compare_with.strip():
            return await self.compare(reference, compare_with)
        battle_id = self._battle_reference(reference)
        if battle_id is None:
            return ToolAnswer(
                "需要一个 battleId 或战报链接。想按名次找，"
                "先用榜单工具拿到那一名的 battleId。"
            )
        # Three upstream reads, each with its own latency, go out together;
        # awaiting them in turn put the optional two in front of the payload.
        detail, export, suits = await asyncio.gather(
            self._data.get_battle_detail(battle_id),
            self._battle_export(battle_id),
            self._equip_suits(),
            return_exceptions=True,
        )
        if isinstance(detail, BaseException):
            if isinstance(detail, ZmdLogsAPIError) and detail.status_code == 404:
                return ToolAnswer(messages.BATTLE_NOT_FOUND)
            raise detail
        text = facts.format_battle(detail, export=export, suits=suits)
        image = await self._render(
            lambda renderer: renderer.render_battle(
                detail,
                query=battle_id,
                web_base_url=self._web_base_url,
                export=export,
                export_note=None,
                suits=suits,
            )
        )
        return ToolAnswer(text, image)

    async def compare(self, first: str, second: str) -> ToolAnswer:
        left, right = self._battle_reference(first), self._battle_reference(second)
        if left is None or right is None:
            return ToolAnswer(
                "对比需要两个 battleId 或战报链接。想按名次对比，"
                "先用榜单工具拿到那两名的 battleId。"
            )
        if left == right:
            return ToolAnswer(messages.COMPARE_SAME_BATTLE)
        detail_a, detail_b, suits = await asyncio.gather(
            self._data.get_battle_detail(left),
            self._data.get_battle_detail(right),
            self._equip_suits(),
            return_exceptions=True,
        )
        for outcome in (detail_a, detail_b):
            if isinstance(outcome, ZmdLogsAPIError) and outcome.status_code == 404:
                return ToolAnswer("其中一场战报不存在、未公开或已删除。")
            if isinstance(outcome, BaseException):
                raise outcome
        if isinstance(suits, BaseException):
            suits = {}
        text = facts.format_battle_comparison(detail_a, detail_b, suits=suits)
        if detail_a.boss_name != detail_b.boss_name:
            return ToolAnswer(text)
        image = await self._render(
            lambda renderer: renderer.render_compare(
                detail_a,
                detail_b,
                query=f"{left} vs {right}",
                web_base_url=self._web_base_url,
                suits=suits,
            )
        )
        return ToolAnswer(text, image)

    # --- characters and accounts --------------------------------------------------

    async def character(
        self,
        name: str,
        board: str = "",
        element: str = "",
        time_range: str = "",
        profession: str = "",
        potential: str = "",
    ) -> ToolAnswer:
        span, range_note = _time_range(time_range)
        wanted_potential = self._potential(potential)
        if isinstance(wanted_potential, ToolAnswer):
            return wanted_potential
        if board.strip():
            if not name.strip():
                answer = await self._board_statistics(board, span, wanted_potential)
            else:
                answer = await self._character_on_board(
                    name, board, span, wanted_potential
                )
            return answer.noted(range_note)
        if not name.strip():
            wanted = self._element(element)
            if isinstance(wanted, ToolAnswer):
                return wanted
            role = self._profession(profession)
            if isinstance(role, ToolAnswer):
                return role
            answer = await self._champions(
                wanted or None, profession=role or None, span=span
            )
            return answer.noted(range_note)
        # Standings come from the ranking index and cover every rarity; the
        # DPS distribution needs a six-star key and is added when there is one.
        index = self._data.ranking_index
        await index.ensure_filled()
        rankings = tuple(entry.ranking for entry in index.entries())
        resolution = resolve_character_name(name, roster_character_names(rankings))
        if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
            return ToolAnswer(
                messages.ambiguous_character(name, resolution.candidates)
            )
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            answer = await self._character_distribution_only(
                name, span, wanted_potential
            )
            return answer.noted(range_note)
        standings = character_standings(rankings, resolution.name)
        age = index.oldest_age_seconds()
        key = await self._catalog_key(resolution.name)
        # The distribution is an upstream read of several seconds and the
        # render about one; they need nothing from each other, so they overlap.
        elements = await self._data.character_elements()
        stats, image = await asyncio.gather(
            self._boss_statistics(key, span, wanted_potential),
            self._render(
                lambda renderer: renderer.render_character_standings(
                    standings,
                    query=resolution.name,
                    web_base_url=self._web_base_url,
                    age_seconds=age,
                    elements=elements,
                )
            ),
        )
        parts = [
            facts.format_character_standings(standings, age_seconds=age),
            facts.format_character_partners(rankings, resolution.name),
        ]
        if stats is not None:
            parts.append(facts.format_character_boards(stats))
        return ToolAnswer(facts.join_sections(*parts), image).noted(range_note)

    async def _champions(
        self,
        element: str | None = None,
        *,
        profession: str | None = None,
        span: str = "all",
    ) -> ToolAnswer:
        """Every character's first places over all boards, most first.

        ``element`` keeps only the characters of that element, which is how
        "物理队有什么冠军" is answered: the board, restricted to them.
        ``profession`` restricts it to one class and lists every member,
        zeros included, which is how "谁是冠军最少的突击" is answered.
        ``span`` (7d / 14d / 30d) narrows every board to a window.
        """

        index = self._data.ranking_index
        await index.ensure_filled()
        rankings = tuple(entry.ranking for entry in index.entries())
        elements = await self._data.character_elements()
        professions = await self._data.character_professions()
        since = window_start(span, now=datetime.now(UTC))
        tallies = character_tallies(rankings, since=since)
        if element is not None:
            tallies = tuple(t for t in tallies if elements.get(t.name) == element)
        if profession is not None:
            tallies = by_profession(tallies, profession)
        # Who the catalog knows and no record fields; not worked out after an
        # element cut, which would call every member of another element unseen.
        unseen = (
            unseen_characters(tallies, professions, profession=profession)
            if element is None
            else ()
        )
        teams = first_place_teams(rankings, since=since)
        usage = profession_usage(rankings, since=since)
        label = window_label(span)
        age = index.oldest_age_seconds()
        text = facts.format_character_tallies(
            tallies,
            board_count=len(rankings),
            limit=15,
            age_seconds=age,
            element=element,
            profession=profession,
            teams=teams,
            usage=usage,
            window_label=label,
            unseen=unseen,
        )
        image = await self._render(
            lambda renderer: renderer.render_character_champions(
                tallies,
                board_count=len(rankings),
                query="角色排名",
                web_base_url=self._web_base_url,
                age_seconds=age,
                element=element,
                elements=elements,
                profession=profession,
                teams=teams,
                usage=usage,
                window_label=label,
                unseen=unseen,
            )
        )
        return ToolAnswer(text, image)

    @staticmethod
    def _element(text: str) -> str | ToolAnswer:
        """The catalog label for an element the model typed; "" for none."""

        if not text.strip():
            return ""
        label = normalize_element(text)
        if label is None:
            return ToolAnswer(
                f"「{shorten(text)}」不是属性，属性只有：{'、'.join(ELEMENTS)}。"
            )
        return label

    @staticmethod
    def _profession(text: str) -> str | ToolAnswer:
        """The records' label for a profession the model typed; "" for none."""

        if not text.strip():
            return ""
        label = normalize_profession(text)
        if label is None:
            return ToolAnswer(
                f"「{shorten(text)}」不是职业，职业只有：{'、'.join(PROFESSIONS)}"
                "（术师也认）。"
            )
        return label

    @staticmethod
    def _potential(text: str) -> str | ToolAnswer:
        """``0`` / ``1-5`` / ``all`` for what the model typed; "" means all."""

        if not text.strip():
            return "all"
        value = parse_potential_text(text)
        if value is None:
            return ToolAnswer(
                f"「{shorten(text)}」不是可用的潜能档：只分 0（零潜）、1-5（有潜能，"
                "合并统计）和 all，ZMDLogs 不按具体潜能层数拆分。"
            )
        return value

    async def _boss_statistics(self, key: str | None, span: str, potential: str):
        """The six-star distribution, or nothing; the standings stand without it."""

        if key is None:
            return None
        try:
            return await self._data.get_character_boss_statistics(
                key, time_range=span, potential=potential
            )
        except ZmdLogsClientError:
            return None

    async def _board_statistics(
        self, board: str, span: str, potential: str
    ) -> ToolAnswer:
        """Every six-star's distribution on one board, or over all of them."""

        if _means_every_board(board):
            slug: str | None = None
            query = "角色统计"
        else:
            target = await self._resolve_target(board)
            if isinstance(target, ToolAnswer):
                return target
            if not isinstance(target, str):
                choice, _cards = target
                return ToolAnswer(
                    f"「{shorten(board)}」是副本，角色统计要按具体榜单看：请指明"
                    f"「{choice.target.name}」下的一个榜单。"
                )
            slug, query = target, board
        stats = await self._data.get_character_statistics(
            slug, time_range=span, potential=potential
        )
        text = facts.format_character_statistics(stats)
        image = await self._render(
            lambda renderer: renderer.render_character_stats(
                stats, query=query, web_base_url=self._web_base_url
            )
        )
        return ToolAnswer(text, image)

    async def _character_on_board(
        self, name: str, board: str, span: str, potential: str
    ) -> ToolAnswer:
        catalog_name = await self._resolve_six_star(name)
        if isinstance(catalog_name, ToolAnswer):
            return catalog_name
        if catalog_name is None:
            # Not a six-star, so there is no distribution; the records
            # fielding it on that board are what the question is about.
            return await self.board(board, character=name)
        target = await self._resolve_target(board)
        if isinstance(target, ToolAnswer):
            return target
        if not isinstance(target, str):
            choice, _cards = target
            return ToolAnswer(
                f"「{shorten(board)}」是副本，角色统计要按具体榜单看：请指明"
                f"「{choice.target.name}」下的一个榜单。"
            )
        stats = await self._data.get_character_statistics(
            target, time_range=span, potential=potential
        )
        text = facts.format_character_statistics(stats, character=catalog_name)
        image = await self._render(
            lambda renderer: renderer.render_character_stats(
                stats, query=board, web_base_url=self._web_base_url
            )
        )
        return ToolAnswer(text, image)

    async def _character_distribution_only(
        self, name: str, span: str, potential: str
    ) -> ToolAnswer:
        """A six-star with no public record yet: the distribution page alone."""

        catalog_name = await self._resolve_six_star(name)
        if isinstance(catalog_name, ToolAnswer):
            return catalog_name
        if catalog_name is None:
            return await self._not_a_character(name)
        key = await self._catalog_key(catalog_name)
        stats = await self._data.get_character_boss_statistics(
            key, time_range=span, potential=potential
        )
        text = facts.format_character_boards(stats)
        image = await self._render(
            lambda renderer: renderer.render_character_boss(
                stats, query=catalog_name, web_base_url=self._web_base_url
            )
        )
        return ToolAnswer(text, image)

    async def _not_a_character(self, name: str) -> ToolAnswer:
        """Why a name is not a character: a boss name, or simply unknown."""

        try:
            cards = await self._data.list_hot_bosses()
        except ZmdLogsClientError:
            cards = ()
        if cards:
            match = self._board_matcher(cards).match(
                name, allowed_types=BOARD_QUERY_TARGETS
            )
            choice = match.selected
            if (
                match.status is MatchStatus.MATCHED
                and choice is not None
                and choice.level <= MatchLevel.PREFIX_SUFFIX
            ):
                # Nothing in any roster or the catalog, and a board's name
                # starts or ends with it: it is the board.
                return ToolAnswer(
                    f"「{shorten(name)}」是榜单或副本（{choice.target.name}），"
                    "不是角色；它的记录请用榜单工具查。"
                )
        return ToolAnswer(
            f"公开记录里没有「{shorten(name)}」出场，"
            "角色统计也只覆盖六星干员，可能是名字不对。"
        )

    async def _resolve_six_star(self, name: str) -> str | None | ToolAnswer:
        """The catalog name for ``name``; None when the catalog has no such name."""

        entries = await self._data.get_character_catalog()
        resolution = resolve_character_name(name, tuple(e.name for e in entries))
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            # A name the catalog lacks may be a character added since it was
            # read. One bounded refresh settles it either way.
            entries = await self._data.get_character_catalog(refresh=True)
            resolution = resolve_character_name(name, tuple(e.name for e in entries))
        if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
            return ToolAnswer(
                messages.ambiguous_character(name, resolution.candidates)
            )
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            return None
        return resolution.name

    async def _catalog_key(self, name: str) -> str | None:
        entries = await self._data.get_character_catalog()
        return next((e.key for e in entries if e.name == name), None)

    async def account(self, query: str, time_range: str = "") -> ToolAnswer:
        if not query.strip():
            return await self._player_champions(time_range)
        span, range_note = _time_range(time_range)
        try:
            account_id = parse_account_reference(
                query, web_base_url=self._web_base_url
            )
        except PublicReferenceError:
            account_id = None
        if account_id is None:
            resolved = await self._search_account(query)
            if isinstance(resolved, ToolAnswer):
                return resolved
            account_id = resolved
        try:
            rankings = await self._data.get_public_user_rankings(account_id)
        except ZmdLogsAPIError as exc:
            if exc.status_code == 404:
                return ToolAnswer(messages.ACCOUNT_NOT_FOUND)
            raise
        # Habits come from whatever the index already holds; never wait for it.
        held = tuple(entry.ranking for entry in self._data.ranking_index.entries())
        habits = account_tally(held, account_id) if held else None
        since = window_start(span, now=datetime.now(UTC))
        label = window_label(span)
        parts = [
            facts.format_account(
                rankings, habits=habits, since=since, window_label=label
            )
        ]
        if self._watcher is not None:
            history = self._watcher.history_for(account_id)
            if history is not None and history.boards:
                parts.append(
                    facts.format_account_trend(
                        history,
                        since=since,
                        window_label=label,
                        last_checked=self._watcher.last_checked(account_id),
                    )
                )
            else:
                parts.append(facts.NO_RANK_HISTORY)
        rows, listed = await self._data.index_rows_for(
            row.battle_id for row in rankings.rankings
        )
        elements = await self._data.character_elements()
        image = await self._render(
            lambda renderer: renderer.render_account(
                rankings,
                query=query,
                web_base_url=self._web_base_url,
                rows_by_battle=rows,
                listed_boards=listed,
                elements=elements,
            )
        )
        return ToolAnswer(facts.join_sections(*parts), image).noted(range_note)

    async def _search_account(self, query: str) -> str | ToolAnswer:
        """One public account id for a nickname, or the reason there is not one.

        An exact nickname wins over longer ones that merely contain it, as
        the smart command already decides; anything still ambiguous is
        listed for the model to ask about.
        """

        nickname = searchable_nickname(query)
        if nickname is None:
            return ToolAnswer(messages.ACCOUNT_REFERENCE_NEEDED)
        search = await self._client.search_public_accounts(
            nickname, limit=MAX_CANDIDATES
        )
        hits = search.accounts
        if not hits:
            return ToolAnswer(f"没有找到昵称包含「{shorten(nickname)}」的公开账号。")
        folded = fold_text(nickname)
        exact = [hit for hit in hits if fold_text(hit.account_display_name) == folded]
        if len(exact) == 1:
            return exact[0].account_id
        if len(hits) == 1 and not search.has_more:
            return hits[0].account_id
        names = "、".join(hit.account_display_name for hit in hits)
        more = "（还有更多同名结果未列出）" if search.has_more else ""
        return ToolAnswer(
            f"「{shorten(nickname)}」匹配到多个公开账号：{names}{more}。"
            "请让对方给出完整昵称或 accountId。"
        )

    async def _player_champions(self, time_range: str) -> ToolAnswer:
        """Which public accounts uploaded the most first places."""

        span, range_note = _time_range(time_range)
        index = self._data.ranking_index
        await index.ensure_filled()
        rankings = tuple(entry.ranking for entry in index.entries())
        since = window_start(span, now=datetime.now(UTC))
        tallies = account_tallies(rankings, since=since)
        label = window_label(span)
        age = index.oldest_age_seconds()
        text = facts.format_account_tallies(
            tallies,
            board_count=len(rankings),
            limit=15,
            age_seconds=age,
            window_label=label,
        )
        image = await self._render(
            lambda renderer: renderer.render_player_champions(
                tallies,
                board_count=len(rankings),
                query="玩家冠军榜",
                age_seconds=age,
                window_label=label,
            )
        )
        return ToolAnswer(text, image).noted(range_note)

    # --- shared -------------------------------------------------------------------

    async def _battle_export(self, battle_id: str):
        """The cast list, or nothing; the card renders without it."""

        try:
            return await self._data.get_battle_export(battle_id)
        except ZmdLogsClientError:
            return None

    async def _equip_suits(self) -> dict[str, str]:
        """The gear catalog, or nothing; a page renders without it."""

        try:
            return await self._data.get_equip_suits()
        except ZmdLogsClientError:
            return {}

    async def _resolve_target(
        self, keyword: str
    ) -> str | tuple[MatchChoice, tuple[HotBossCard, ...]] | ToolAnswer:
        """A board slug, a dungeon with its cards, or the reason there is neither."""

        cards = await self._data.list_hot_bosses()
        matcher = self._board_matcher(cards)
        match = matcher.match(keyword, allowed_types=BOARD_QUERY_TARGETS)
        if match.status is MatchStatus.NOT_FOUND:
            return ToolAnswer(
                f"没有找到与「{shorten(keyword)}」匹配的榜单或副本。"
            )
        if match.status is MatchStatus.AMBIGUOUS:
            best = match.candidates[0] if match.candidates else None
            if (
                best is not None
                and best.level is MatchLevel.SIMILARITY
                and best.score < matcher.fuzzy_threshold
            ):
                # Several weak guesses are not options; they are noise.
                return ToolAnswer(
                    f"没有找到与「{shorten(keyword)}」匹配的榜单或副本。"
                )
            names = "、".join(choice.target.name for choice in match.candidates[:5])
            return ToolAnswer(
                f"「{shorten(keyword)}」可能指：{names}。请说得更具体一些。"
            )
        choice = match.selected
        if choice is None:
            return ToolAnswer(f"没有找到与「{shorten(keyword)}」匹配的榜单。")
        if (
            choice.level is MatchLevel.SIMILARITY
            and choice.score < matcher.fuzzy_threshold
        ):
            # A command shows the closest board and lets the reader judge from
            # the page. A tool has no reader in the loop: the model would take
            # a guess as the answer and talk about the wrong boss.
            return ToolAnswer(
                f"「{shorten(keyword)}」没有可靠匹配的榜单，"
                f"最接近的是「{choice.target.name}」但不像。请确认名字。"
            )
        if choice.target.target_type is TargetType.BOARD:
            return choice.target.key
        boards = matcher.expand_to_boards((choice,))
        if len(boards) == 1:
            return boards[0].target.key
        slugs = {entry.target.key for entry in boards}
        selected = tuple(card for card in cards if card.boss_slug in slugs)
        return choice, selected

    @staticmethod
    def _resolve_character(ranking: BossRanking, name: str) -> str | ToolAnswer:
        """The board's spelling of a character the model typed, or why not."""

        resolution = resolve_character_name(name, ranking_character_names(ranking))
        if resolution.status is CharacterResolutionStatus.MATCHED:
            return resolution.name
        if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
            return ToolAnswer(messages.ambiguous_character(name, resolution.candidates))
        return ToolAnswer(
            f"「{ranking.boss_name}」的公开记录里没有"
            f"「{shorten(name)}」这个角色，可能是名字不对。"
        )

    def _battle_reference(self, value: str) -> str | None:
        try:
            return parse_battle_reference(value, web_base_url=self._web_base_url)
        except PublicReferenceError:
            return None

    async def _render(self, draw) -> str | None:
        """Draw the page for this answer; None when it cannot be drawn.

        A tool that lost its picture still has its text, and text is the part
        the model needs, so a render failure is logged and swallowed.
        """

        try:
            return await draw(self._renderer())
        except Exception as exc:
            self._logger.warning(
                "ZmdLogBot tool could not render its page: %s", type(exc).__name__
            )
            return None


def _means_every_board(keyword: str) -> bool:
    return "".join(keyword.split()).casefold() in _EVERY_BOARD


def _time_range(text: str) -> tuple[str, str]:
    """A model's range as the option spells it, and a note when it was not understood.

    Anything unreadable means all time — but says so, because a model that
    asked for a week and got everything would present it as the week.
    """

    if not text.strip():
        return "all", ""
    value = parse_range_text(text)
    if value is not None:
        return value, ""
    return "all", (
        f"（范围「{shorten(text)}」没看懂，按全部时间算；可写 7d、14d、30d。）"
    )
