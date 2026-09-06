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

from . import facts
from .characters import CharacterResolutionStatus, resolve_character_name
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
    MatchLevel,
    MatchStatus,
    RankingMatcher,
    TargetType,
)
from .messages import shorten
from .models import BossRanking, HotBossCard
from .professions import PROFESSIONS, normalize_profession
from .render import LongImageRenderer
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



@dataclass(frozen=True, slots=True)
class ToolAnswer:
    """What one tool call produced.

    ``text`` always says something, including when the answer is that the
    data does not cover the question. ``image_path`` is set only when zmdlog
    has a page for this question and it rendered.
    """

    text: str
    image_path: str | None = None


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
    ) -> None:
        self._client = client
        self._data = data
        self._renderer = renderer
        self._board_matcher = board_matcher
        self._web_base_url = settings.web_base_url
        self._logger = logger

    # --- boards -----------------------------------------------------------------

    async def board(
        self,
        keyword: str,
        *,
        character: str = "",
        limit: int = facts.DEFAULT_ROW_LIMIT,
        element: str = "",
        time_range: str = "",
    ) -> ToolAnswer:
        if not keyword.strip():
            return await self._records(time_range)
        wanted = self._element(element)
        if isinstance(wanted, ToolAnswer):
            return wanted
        slug = await self._resolve_board(keyword)
        if isinstance(slug, ToolAnswer):
            return slug
        ranking = await self._data.get_boss_ranking(slug)
        elements = await self._elements()
        name = ""
        if character:
            name = self._resolve_character(ranking, character)
            if name is None:
                return ToolAnswer(
                    f"「{ranking.boss_name}」的公开记录里没有"
                    f"「{shorten(character)}」这个角色，可能是名字不对。"
                )
        if wanted and not any(
            elements.get(row.character_name) == wanted for row in ranking.rows
        ):
            return ToolAnswer(
                f"「{ranking.boss_name}」的公开记录里没有主C 为{wanted}属性的队伍。"
            )
        text = facts.format_board_ranking(
            ranking,
            limit=limit,
            character=name or None,
            element=wanted or None,
            elements=elements,
        )
        image = await self._render(
            lambda renderer: renderer.render_ranking(
                ranking,
                query=keyword,
                ranking_limit=max(limit, 10),
                web_base_url=self._web_base_url,
                character_filter=(name,) if name else None,
                element_filter=wanted or None,
                elements=elements,
            )
        )
        return ToolAnswer(text, image)

    async def _records(self, time_range: str) -> ToolAnswer:
        """New records, first places changing hands and board activity."""

        span = _time_range(time_range)
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
                return ToolAnswer("这场战报不存在、未公开或已删除。")
            raise detail
        text = facts.format_battle(detail, export=export)
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
            return ToolAnswer("两边是同一场战报，没有可比的。")
        details = await asyncio.gather(
            self._data.get_battle_detail(left),
            self._data.get_battle_detail(right),
            return_exceptions=True,
        )
        for outcome in details:
            if isinstance(outcome, ZmdLogsAPIError) and outcome.status_code == 404:
                return ToolAnswer("其中一场战报不存在、未公开或已删除。")
            if isinstance(outcome, BaseException):
                raise outcome
        detail_a, detail_b = details
        text = facts.format_battle_comparison(detail_a, detail_b)
        if detail_a.boss_name != detail_b.boss_name:
            return ToolAnswer(text)
        image = await self._render(
            lambda renderer: renderer.render_compare(
                detail_a,
                detail_b,
                query=f"{left} vs {right}",
                web_base_url=self._web_base_url,
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
    ) -> ToolAnswer:
        if board:
            return await self._character_on_board(name, board)
        if not name.strip():
            wanted = self._element(element)
            if isinstance(wanted, ToolAnswer):
                return wanted
            role = self._profession(profession)
            if isinstance(role, ToolAnswer):
                return role
            return await self._champions(
                wanted or None, profession=role or None, time_range=time_range
            )
        # Standings come from the ranking index and cover every rarity; the
        # DPS distribution needs a six-star key and is added when there is one.
        index = self._data.ranking_index
        await index.ensure_filled()
        rankings = tuple(entry.ranking for entry in index.entries())
        resolution = resolve_character_name(name, roster_character_names(rankings))
        if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
            return ToolAnswer(
                f"「{shorten(name)}」可能是：{' / '.join(resolution.candidates)}，"
                "请用全名再问一次。"
            )
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            return await self._character_distribution_only(name)
        standings = character_standings(rankings, resolution.name)
        age = index.oldest_age_seconds()
        key = await self._catalog_key(resolution.name)
        # The distribution is an upstream read of several seconds and the
        # render about one; they need nothing from each other, so they overlap.
        elements = await self._elements()
        stats, image = await asyncio.gather(
            self._boss_statistics(key),
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
        parts = [facts.format_character_standings(standings, age_seconds=age)]
        if stats is not None:
            parts.append(facts.format_character_boards(stats))
        return ToolAnswer(facts.join_sections(*parts), image)

    async def _champions(
        self,
        element: str | None = None,
        *,
        profession: str | None = None,
        time_range: str = "",
    ) -> ToolAnswer:
        """Every character's first places over all boards, most first.

        ``element`` keeps only the characters of that element, which is how
        "物理队有什么冠军" is answered: the board, restricted to them.
        ``profession`` restricts it to one class and lists every member,
        zeros included, which is how "谁是冠军最少的突击" is answered.
        ``time_range`` (7d / 14d / 30d) narrows every board to a window.
        """

        span = _time_range(time_range)
        index = self._data.ranking_index
        await index.ensure_filled()
        rankings = tuple(entry.ranking for entry in index.entries())
        elements = await self._elements()
        professions = await self._professions()
        since = window_start(span, now=datetime.now(UTC))
        tallies = character_tallies(rankings, since=since)
        if element is not None:
            tallies = tuple(t for t in tallies if elements.get(t.name) == element)
        if profession is not None:
            tallies = by_profession(tallies, profession)
        # Who the catalog knows and no record fields; not worked out for an
        # element alone, which the catalog dict here does not carry.
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

    async def _elements(self) -> dict[str, str]:
        """Name to element label; empty when the catalog is unreachable."""

        try:
            types = await self._data.get_character_types()
        except ZmdLogsClientError:
            return {}
        return {name: entry.element for name, entry in types.items()}

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

    async def _professions(self) -> dict[str, str]:
        """Name to profession as the catalog spells it; empty when unreachable."""

        try:
            types = await self._data.get_character_types()
        except ZmdLogsClientError:
            return {}
        return {
            name: entry.profession for name, entry in types.items() if entry.profession
        }

    async def _boss_statistics(self, key: str | None):
        """The six-star distribution, or nothing; the standings stand without it."""

        if key is None:
            return None
        try:
            return await self._data.get_character_boss_statistics(
                key, time_range="all", potential="all"
            )
        except ZmdLogsClientError:
            return None

    async def _character_on_board(self, name: str, board: str) -> ToolAnswer:
        resolution = await self._resolve_six_star(name)
        if isinstance(resolution, ToolAnswer):
            return resolution
        slug = await self._resolve_board(board)
        if isinstance(slug, ToolAnswer):
            return slug
        stats = await self._data.get_character_statistics(
            slug, time_range="all", potential="all"
        )
        text = facts.format_character_statistics(stats, character=resolution)
        image = await self._render(
            lambda renderer: renderer.render_character_stats(
                stats, query=board, web_base_url=self._web_base_url
            )
        )
        return ToolAnswer(text, image)

    async def _character_distribution_only(self, name: str) -> ToolAnswer:
        """A six-star with no public record yet: the distribution page alone."""

        resolution = await self._resolve_six_star(name)
        if isinstance(resolution, ToolAnswer):
            return resolution
        key = await self._catalog_key(resolution)
        stats = await self._data.get_character_boss_statistics(
            key, time_range="all", potential="all"
        )
        text = facts.format_character_boards(stats)
        image = await self._render(
            lambda renderer: renderer.render_character_boss(
                stats, query=resolution, web_base_url=self._web_base_url
            )
        )
        return ToolAnswer(text, image)

    async def _resolve_six_star(self, name: str) -> str | ToolAnswer:
        """The catalog name for ``name``, or the reason there is none."""

        entries = await self._data.get_character_catalog()
        resolution = resolve_character_name(name, tuple(e.name for e in entries))
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            # A name the catalog lacks may be a character added since it was
            # read. One bounded refresh settles it either way.
            entries = await self._data.get_character_catalog(refresh=True)
            resolution = resolve_character_name(name, tuple(e.name for e in entries))
        if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
            return ToolAnswer(
                f"「{shorten(name)}」可能是：{' / '.join(resolution.candidates)}，"
                "请用全名再问一次。"
            )
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            return ToolAnswer(
                f"公开记录里没有「{shorten(name)}」出场，"
                "角色统计也只覆盖六星干员，可能是名字不对。"
            )
        return resolution.name

    async def _catalog_key(self, name: str) -> str | None:
        entries = await self._data.get_character_catalog()
        return next((e.key for e in entries if e.name == name), None)

    async def account(self, query: str, time_range: str = "") -> ToolAnswer:
        if not query.strip():
            return await self._player_champions(time_range)
        try:
            account_id = parse_account_reference(
                query, web_base_url=self._web_base_url
            )
        except PublicReferenceError:
            account_id = None
        if account_id is None:
            nickname = searchable_nickname(query)
            if nickname is None:
                return ToolAnswer(
                    "请给出公开昵称（至少 2 个字符）、accountId 或主页链接。"
                )
            search = await self._client.search_public_accounts(nickname, limit=5)
            if not search.accounts:
                return ToolAnswer(
                    f"没有找到昵称包含「{shorten(nickname)}」的公开账号。"
                )
            if len(search.accounts) > 1:
                names = "、".join(
                    hit.account_display_name for hit in search.accounts
                )
                return ToolAnswer(
                    f"「{shorten(nickname)}」匹配到多个公开账号：{names}。"
                    "请让对方说得更完整一些。"
                )
            account_id = search.accounts[0].account_id
        try:
            rankings = await self._data.get_public_user_rankings(account_id)
        except ZmdLogsAPIError as exc:
            if exc.status_code == 404:
                return ToolAnswer("没有这个公开账号，或它暂无公开榜单记录。")
            raise
        # Habits come from whatever the index already holds; never wait for it.
        held = tuple(entry.ranking for entry in self._data.ranking_index.entries())
        habits = account_tally(held, account_id) if held else None
        text = facts.format_account(rankings, habits=habits)
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
        return ToolAnswer(text, image)

    async def _player_champions(self, time_range: str) -> ToolAnswer:
        """Which public accounts uploaded the most first places."""

        span = _time_range(time_range)
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
        return ToolAnswer(text, image)

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

    async def _resolve_board(self, keyword: str) -> str | ToolAnswer:
        """One board slug for ``keyword``, or the reason there is not one."""

        cards = await self._data.list_hot_bosses()
        matcher = self._board_matcher(cards)
        match = matcher.match(keyword, allowed_types=BOARD_QUERY_TARGETS)
        if match.status is MatchStatus.NOT_FOUND:
            return ToolAnswer(
                f"没有找到与「{shorten(keyword)}」匹配的榜单或副本。"
            )
        if match.status is MatchStatus.AMBIGUOUS:
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
        if len(boards) > 1:
            names = "、".join(entry.target.name for entry in boards[:5])
            return ToolAnswer(
                f"「{shorten(keyword)}」是副本，包含多个榜单：{names}。"
                "请指明其中一个。"
            )
        return boards[0].target.key

    @staticmethod
    def _resolve_character(ranking: BossRanking, name: str) -> str | None:
        names = tuple(
            entry.character_name
            for row in ranking.rows
            for entry in row.roster_entries
        )
        resolution = resolve_character_name(name, names)
        if resolution.status is CharacterResolutionStatus.MATCHED:
            return resolution.name
        return None

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


_TIME_RANGES = {"7d": "7d", "14d": "14d", "30d": "30d", "all": "all", "": "all"}
_TIME_RANGE_ALIASES = {
    "7天": "7d", "一周": "7d", "week": "7d", "14天": "14d", "两周": "14d",
    "30天": "30d", "一个月": "30d", "month": "30d", "全部": "all", "所有": "all",
}


def _time_range(text: str) -> str:
    """A model's range as the option spells it; anything odd means all time."""

    value = text.strip().casefold()
    return _TIME_RANGES.get(value) or _TIME_RANGE_ALIASES.get(value) or "all"

