"""What the LLM tools do, without any of the AstrBot they run inside.

Each method resolves what the model asked for, draws the page zmdlog would
have drawn for that question, and returns both: the picture carries the
numbers, the text carries what they are. The split is deliberate — a model
handed numbers as text restates them wrongly, and a picture cannot be
reasoned about — so the tool descriptions tell the model the figures are in
the image and its own job is to explain them. The one answer with facts and
no picture is the board tool's 全部: there is no page of every board since
榜单 became a pick list, and the text is one line per board. The one set of
figures in text alone is the character tool's two build lines, which head
every answer about one character: they come from its 角色档案, a page that
answer did not draw.

Nothing here posts a candidate list. A command can afford to ask "did you
mean one of these five"; a tool call cannot wait for an answer, so an
ambiguous name is reported as such, with the options, and the model asks.

There are four tools, one per subject — board, battle, character, account —
never one per feature. AstrBot sends every active tool's schema with every
LLM request, and a model choosing among overlapping tools picks the wrong
one; a new view of a subject is a parameter or extra lines in its text.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import facts, messages
from .candidates import MAX_CANDIDATES
from .characters import (
    CharacterResolutionStatus,
    resolve_character_name,
    resolve_standing_names,
    split_character_names,
)
from .client import (
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsClientError,
    searchable_nickname,
)
from .datasource import ZmdLogsDataSource
from .elements import ELEMENTS, normalize_element
from .history import trend_window_start, window_label, window_start
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
from .metrics import METRIC_DPS, is_rdps, parse_metric_text
from .models import HotBossCard
from .professions import PROFESSIONS, normalize_profession
from .rank_trend import RankTrend
from .recipes import (
    IndexSnapshot,
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
from .routing import parse_potential_text, parse_range_text
from .settings import PluginSettings
from .standings import account_tally

BoardMatcher = Callable[[tuple[HotBossCard, ...]], RankingMatcher]

# What people say when they mean every board at once.
_EVERY_BOARD = frozenset(
    {
        "全部", "所有", "全部榜单", "所有榜单", "全部副本", "所有副本",
        "所有首领", "全部首领", "all",
    }
)
# What the model may write for the character tool's 角色档案 view.
_PROFILE_VIEW = frozenset({"档案", "角色档案", "profile"})
# How long an answer about one character waits for its build lines once it is
# otherwise ready. A cold all-time profile takes 2–5 s and overlaps the
# answer's own reads; one slower than this goes without, and its read still
# fills the cache for the next question.
SUMMARY_GRACE_SECONDS = 3.0


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

    def led_by(self, section: str) -> "ToolAnswer":
        """The same answer with one more section on top, when there is one."""

        if not section:
            return self
        return ToolAnswer(facts.join_sections(section, self.text), self.image_path)


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
        trend: RankTrend | None = None,
        summary_grace_seconds: float = SUMMARY_GRACE_SECONDS,
    ) -> None:
        self._client = client
        self._data = data
        self._renderer = renderer
        self._board_matcher = board_matcher
        self._web_base_url = settings.web_base_url
        self._logger = logger
        # Every account's rank trace; the account tool reads it when it has one.
        self._trend = trend
        self._summary_grace_seconds = summary_grace_seconds

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
        metric: str = "",
    ) -> ToolAnswer:
        wanted_metric = self._metric(metric)
        if isinstance(wanted_metric, ToolAnswer):
            return wanted_metric
        if not keyword.strip():
            return await self._records(time_range, wanted_metric)
        span, range_note = _time_range(time_range)
        wanted = self._element(element)
        if isinstance(wanted, ToolAnswer):
            return wanted
        role = self._profession(profession)
        if isinstance(role, ToolAnswer):
            return role
        if _means_every_board(keyword):
            answer = await self._boards_overview()
            return answer.noted(_overview_metric_note(wanted_metric))
        target = await self._resolve_target(keyword)
        if isinstance(target, ToolAnswer):
            return target
        if not isinstance(target, str):
            choice, cards = target
            answer = await self._dungeon_overview(keyword, choice, cards)
            return answer.noted(_overview_metric_note(wanted_metric))
        ranking = await self._data.get_boss_ranking(
            target, metric=wanted_metric, on_demand=True
        )
        limit = max(1, min(limit, facts.MAX_ROW_LIMIT))
        recipe = await prepare_ranking(
            self._data,
            ranking,
            query=keyword,
            web_base_url=self._web_base_url,
            ranking_limit=max(limit, facts.DEFAULT_ROW_LIMIT),
            character_filter=character or None,
            element_filter=wanted or None,
            profession_filter=role or None,
        )
        if isinstance(recipe, str):
            return ToolAnswer(recipe)
        since = window_start(span, now=datetime.now(UTC))
        events = ()
        if since is not None:
            events = tuple(
                event
                for event in self._data.event_log.recent(since=since)
                if event.boss_slug == ranking.boss_slug
            )
        text = facts.format_board_ranking(
            ranking,
            limit=limit,
            character=recipe.character_filter,
            character_scope=recipe.character_filter_scope,
            element=wanted or None,
            elements=recipe.elements,
            profession=role or None,
            since=since,
            window_label=window_label(span),
            events=events,
        )
        image = await self._render(recipe.draw)
        return ToolAnswer(text, image).noted(range_note)

    async def _records(
        self, time_range: str, metric: str = METRIC_DPS
    ) -> ToolAnswer:
        """New records, first places changing hands and board activity."""

        span, range_note = _time_range(time_range)
        snapshot = await self._index(metric)
        if isinstance(snapshot, ToolAnswer):
            return snapshot
        recipe = prepare_records(self._data, snapshot, time_range=span)
        text = facts.format_records(
            recipe.events,
            recipe.activity,
            window_label=recipe.window_label,
            log_since=recipe.log_since,
            metric=recipe.metric,
        )
        image = await self._render(recipe.draw)
        return ToolAnswer(text, image).noted(range_note)

    async def _boards_overview(self) -> ToolAnswer:
        """Every board's first place, from the board list itself.

        Text alone: the page of every board's top three is gone (榜单 is a
        pick list now), and a picture of forty-nine boards was never one
        the model could point at anyway.
        """

        text = facts.format_boards_overview(
            await self._data.list_hot_bosses(),
            title="全部公开榜单",
            runs_per_board=1,
        )
        return ToolAnswer(text)

    async def _dungeon_overview(
        self,
        keyword: str,
        choice: MatchChoice,
        cards: tuple[HotBossCard, ...],
    ) -> ToolAnswer:
        """The top three of every board of one dungeon or phase."""

        recipe = prepare_dungeon_overview(
            choice, cards, query=keyword, web_base_url=self._web_base_url
        )
        text = facts.format_boards_overview(
            recipe.cards, title=choice.target.name, runs_per_board=3
        )
        image = await self._render(recipe.draw)
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
        try:
            recipe = await prepare_battle(
                self._data, battle_id, query=battle_id, web_base_url=self._web_base_url
            )
        except ZmdLogsAPIError as exc:
            if exc.status_code == 404:
                return ToolAnswer(messages.BATTLE_NOT_FOUND)
            raise
        text = facts.format_battle(
            recipe.battle, export=recipe.export, suits=recipe.suits, crit=recipe.crit
        )
        image = await self._render(recipe.draw)
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
        try:
            recipe = await prepare_compare(
                self._data,
                left,
                right,
                query=f"{left} vs {right}",
                web_base_url=self._web_base_url,
            )
        except ZmdLogsAPIError as exc:
            if exc.status_code == 404:
                return ToolAnswer("其中一场战报不存在、未公开或已删除。")
            raise
        text = facts.format_battle_comparison(
            recipe.first, recipe.second, suits=recipe.suits
        )
        if recipe.refusal is not None:
            # Two bosses have two rotations: the differences are still named,
            # the page that would compare nothing is not drawn.
            return ToolAnswer(text)
        image = await self._render(recipe.draw)
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
        metric: str = "",
        view: str = "",
    ) -> ToolAnswer:
        span, range_note = _time_range(time_range)
        wanted_view = self._view(view)
        if isinstance(wanted_view, ToolAnswer):
            return wanted_view
        if wanted_view:
            answer = await self._character_profile(name, board, span)
            return answer.noted(_profile_options_note(potential, metric)).noted(
                range_note
            )
        wanted_potential = self._potential(potential)
        if isinstance(wanted_potential, ToolAnswer):
            return wanted_potential
        wanted_metric = self._metric(metric)
        if isinstance(wanted_metric, ToolAnswer):
            return wanted_metric
        question = self._character_question(
            name,
            board,
            element=element,
            span=span,
            profession=profession,
            potential=wanted_potential,
            metric=wanted_metric,
        )
        if len(split_character_names(name)) != 1:
            return (await question).noted(range_note)
        answer = await self._with_summary(question, name, span)
        return answer.noted(range_note)

    async def _with_summary(
        self, question: Awaitable[ToolAnswer], name: str, span: str
    ) -> ToolAnswer:
        """``question``'s answer headed by the two build lines of ``name``.

        On top because a long reply is cut from the end. The profile read
        starts with the question and overlaps it; once the answer is in, the
        read gets ``summary_grace_seconds`` more, so a slow or unreachable
        profile delays an answer by that much at most. An answer that drew no
        page goes without: it is a refusal (a board that matched nothing, an
        ambiguous name), and two lines of builds under "which board did you
        mean" would be answered instead of the question. A page that failed
        to draw loses them too; they are never a condition of the answer.
        """

        summary = asyncio.ensure_future(self._profile_summary(name, span))
        try:
            answer = await question
        except BaseException:
            summary.cancel()
            raise
        if answer.image_path is None:
            summary.cancel()
            return answer
        try:
            lines = await asyncio.wait_for(summary, self._summary_grace_seconds)
        except TimeoutError:
            # The data source shields its read, so it still fills the cache.
            return answer
        return answer.led_by(lines)

    async def _character_question(
        self,
        name: str,
        board: str,
        *,
        element: str,
        span: str,
        profession: str,
        potential: str,
        metric: str,
    ) -> ToolAnswer:
        """Every question the character tool takes but its build summary."""

        if board.strip():
            if not name.strip():
                return await self._board_statistics(board, span, potential, metric)
            return await self._character_on_board(name, board, span, potential, metric)
        if not name.strip():
            wanted = self._element(element)
            if isinstance(wanted, ToolAnswer):
                return wanted
            role = self._profession(profession)
            if isinstance(role, ToolAnswer):
                return role
            return await self._champions(
                wanted or None,
                profession=role or None,
                span=span,
                metric=metric,
            )
        # Standings come from the ranking index and cover every rarity; the
        # DPS distribution needs a six-star key and is added when there is one.
        snapshot = await self._index(metric)
        if isinstance(snapshot, ToolAnswer):
            return snapshot
        if len(split_character_names(name)) > 1:
            # Several names: the teams fielding all of them, standings only —
            # a team has no DPS distribution and no single set of partners.
            return await self._team_standings(name, snapshot)
        resolution = resolve_character_name(name, snapshot.fielded)
        if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
            return ToolAnswer(
                messages.ambiguous_character(name, resolution.candidates)
            )
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            return await self._character_distribution_only(
                name, span, potential, metric
            )
        recipe = await prepare_standings(
            self._data,
            snapshot,
            (resolution.name,),
            query=resolution.name,
            web_base_url=self._web_base_url,
        )
        if isinstance(recipe, str):
            return ToolAnswer(recipe)
        key = await self._catalog_key(resolution.name)
        # The distribution is an upstream read of several seconds and the
        # render about one; they need nothing from each other, so they overlap.
        stats, image = await asyncio.gather(
            self._boss_statistics(key, span, potential, metric),
            self._render(recipe.draw),
        )
        parts = [
            facts.format_character_standings(
                recipe.standings, metric=recipe.metric
            ),
            facts.format_character_partners(snapshot.rankings, resolution.name),
        ]
        if stats is not None:
            parts.append(facts.format_character_boards(stats))
        return ToolAnswer(facts.join_sections(*parts), image).noted(
            _index_note(recipe.missing_count)
        )

    async def _team_standings(
        self, text: str, snapshot: IndexSnapshot
    ) -> ToolAnswer:
        """Where the teams fielding every named character stand, 角色排名 A B."""

        names = resolve_standing_names(text, snapshot.fielded)
        if isinstance(names, str):
            return ToolAnswer(names)
        recipe = await prepare_standings(
            self._data,
            snapshot,
            names,
            query=" ".join(names),
            web_base_url=self._web_base_url,
        )
        if isinstance(recipe, str):
            return ToolAnswer(recipe)
        image = await self._render(recipe.draw)
        text = facts.format_character_standings(
            recipe.standings, metric=recipe.metric
        )
        return ToolAnswer(text, image).noted(_index_note(recipe.missing_count))

    async def _champions(
        self,
        element: str | None = None,
        *,
        profession: str | None = None,
        span: str = "all",
        metric: str = METRIC_DPS,
    ) -> ToolAnswer:
        """Every character's first places over all boards, most first.

        ``element`` keeps only the characters of that element, which is how
        "物理队有什么冠军" is answered: the board, restricted to them.
        ``profession`` restricts it to one class and lists every member,
        zeros included, which is how "谁是冠军最少的突击" is answered.
        ``span`` (7d / 14d / 30d) narrows every board to a window; ``metric``
        counts the rDPS boards instead of the DPS ones.
        """

        snapshot = await self._index(metric)
        if isinstance(snapshot, ToolAnswer):
            return snapshot
        recipe = await prepare_champions(
            self._data,
            snapshot,
            web_base_url=self._web_base_url,
            element=element,
            profession=profession,
            time_range=span,
        )
        text = facts.format_character_tallies(
            recipe.tallies,
            board_count=recipe.board_count,
            limit=15,
            element=recipe.element,
            profession=recipe.profession,
            teams=recipe.teams,
            usage=recipe.usage,
            window_label=recipe.window_label,
            unseen=recipe.unseen,
            metric=recipe.metric,
        )
        image = await self._render(recipe.draw)
        return ToolAnswer(text, image).noted(_index_note(recipe.missing_count))

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
    def _metric(text: str) -> str | ToolAnswer:
        """``dps`` / ``rdps`` for what the model typed; "" means DPS."""

        if not text.strip():
            return METRIC_DPS
        value = parse_metric_text(text)
        if value is None:
            return ToolAnswer(
                f"「{shorten(text)}」不是可用的口径：只有 dps（直伤，默认）和 "
                "rdps（团队贡献）两种。"
            )
        return value

    @staticmethod
    def _view(text: str) -> str | ToolAnswer:
        """``档案`` for the 角色档案 view; "" for the default answer."""

        if not text.strip():
            return ""
        if "".join(text.split()).casefold() in _PROFILE_VIEW:
            return "档案"
        return ToolAnswer(
            f"「{shorten(text)}」不是可用的 view：只有 档案（角色档案：养成、武器、"
            "装备、队友占比和各榜通关名次）；不填就是默认的名次与分布。"
        )

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

    async def _boss_statistics(
        self, key: str | None, span: str, potential: str, metric: str = METRIC_DPS
    ):
        """The six-star distribution, or nothing; the standings stand without it."""

        if key is None:
            return None
        try:
            return await self._data.get_character_boss_statistics(
                key, time_range=span, potential=potential, metric=metric
            )
        except ZmdLogsClientError:
            return None

    async def _board_statistics(
        self, board: str, span: str, potential: str, metric: str = METRIC_DPS
    ) -> ToolAnswer:
        """Every six-star's distribution on one board, or over all of them."""

        if _means_every_board(board):
            slug: str | None = None
            query = "角色统计"
        else:
            target = await self._board_slug(board, "角色统计")
            if isinstance(target, ToolAnswer):
                return target
            slug, query = target, board
        recipe = await prepare_character_stats(
            self._data,
            slug,
            time_range=span,
            potential=potential,
            query=query,
            web_base_url=self._web_base_url,
            metric=metric,
        )
        text = facts.format_character_statistics(recipe.stats)
        image = await self._render(recipe.draw)
        return ToolAnswer(text, image)

    async def _character_on_board(
        self,
        name: str,
        board: str,
        span: str,
        potential: str,
        metric: str = METRIC_DPS,
    ) -> ToolAnswer:
        catalog_name = await self._resolve_six_star(name)
        if isinstance(catalog_name, ToolAnswer):
            return catalog_name
        if catalog_name is None:
            # Not a six-star, so there is no distribution; the records
            # fielding it on that board are what the question is about.
            return await self.board(board, character=name, metric=metric)
        target = await self._board_slug(board, "角色统计")
        if isinstance(target, ToolAnswer):
            return target
        recipe = await prepare_character_stats(
            self._data,
            target,
            time_range=span,
            potential=potential,
            query=board,
            web_base_url=self._web_base_url,
            metric=metric,
        )
        text = facts.format_character_statistics(recipe.stats, character=catalog_name)
        image = await self._render(recipe.draw)
        return ToolAnswer(text, image)

    async def _character_distribution_only(
        self, name: str, span: str, potential: str, metric: str = METRIC_DPS
    ) -> ToolAnswer:
        """A six-star with no public record yet: the distribution page alone."""

        catalog_name = await self._resolve_six_star(name)
        if isinstance(catalog_name, ToolAnswer):
            return catalog_name
        if catalog_name is None:
            return await self._not_a_character(name)
        key = await self._catalog_key(catalog_name)
        recipe = await prepare_character_boss(
            self._data,
            key,
            time_range=span,
            potential=potential,
            query=catalog_name,
            web_base_url=self._web_base_url,
            metric=metric,
        )
        text = facts.format_character_boards(recipe.stats)
        image = await self._render(recipe.draw)
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

    async def _character_profile(self, name: str, board: str, span: str) -> ToolAnswer:
        """角色档案: the page the command draws, with its shares and 通关名次 as text.

        ``board`` cuts it to one board, as ``--榜单`` does; a dungeon is
        refused rather than listed, since a tool call cannot wait for a pick.
        """

        if len(split_character_names(name)) != 1:
            return ToolAnswer("角色档案需要且只能填一个角色名。")
        character = await find_profile_character(self._data, name)
        if isinstance(character, str):
            return ToolAnswer(character)
        boss_slug = None
        if board.strip() and not _means_every_board(board):
            boss_slug = await self._board_slug(board, "角色档案")
            if isinstance(boss_slug, ToolAnswer):
                return boss_slug
        recipe = await prepare_character_profile(
            self._data,
            character,
            time_range=span,
            query=name,
            web_base_url=self._web_base_url,
            boss_slug=boss_slug,
        )
        if isinstance(recipe, str):
            return ToolAnswer(recipe)
        text = facts.format_character_profile(recipe.profile, character=character.name)
        image = await self._render(recipe.draw)
        return ToolAnswer(text, image)

    async def _profile_summary(self, name: str, span: str) -> str:
        """The two build lines of the character ``name``; "" when there are none.

        An addition to the answer, never a condition of it: a name the
        game-data catalog cannot settle, a profile that cannot be read and a
        window without records all leave the answer as it would have been.
        The profile is the one 角色档案 draws, so it is read through the
        same cache.
        """

        try:
            character = await find_profile_character(self._data, name)
            if isinstance(character, str):
                return ""
            profile = await self._data.get_character_profile(
                character.key, time_range=span
            )
            return facts.format_profile_summary(profile, character=character.name)
        except ZmdLogsClientError:
            return ""
        except Exception as exc:
            self._logger.warning(
                "ZmdLogBot tool could not add the build summary: %s",
                type(exc).__name__,
            )
            return ""

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
            recipe = await prepare_account(
                self._data, account_id, query=query, web_base_url=self._web_base_url
            )
        except ZmdLogsAPIError as exc:
            if exc.status_code == 404:
                return ToolAnswer(messages.ACCOUNT_NOT_FOUND)
            raise
        rankings = recipe.account
        # Habits come from whatever the index already holds; never wait for it.
        held = tuple(entry.ranking for entry in self._data.ranking_index.entries())
        habits = account_tally(held, account_id) if held else None
        now = datetime.now(UTC)
        since = window_start(span, now=now)
        label = window_label(span)
        parts = [
            facts.format_account(
                rankings,
                habits=habits,
                since=since,
                window_label=label,
                rows_by_battle=recipe.rows_by_battle,
            )
        ]
        history = None if self._trend is None else self._trend.history_for(account_id)
        if history is not None and history.boards:
            parts.append(
                facts.format_account_trend(
                    history,
                    since=trend_window_start(span, now=now),
                    window_label=label,
                    last_checked=self._trend.last_checked(account_id),
                )
            )
        elif self._trend is not None:
            parts.append(facts.NO_RANK_TREND)
        image = await self._render(recipe.draw)
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
        snapshot = await self._index()
        if isinstance(snapshot, ToolAnswer):
            return snapshot
        recipe = prepare_player_champions(snapshot, time_range=span)
        text = facts.format_account_tallies(
            recipe.tallies,
            board_count=recipe.board_count,
            limit=15,
            window_label=recipe.window_label,
        )
        image = await self._render(recipe.draw)
        return (
            ToolAnswer(text, image)
            .noted(_index_note(recipe.missing_count))
            .noted(range_note)
        )

    # --- shared -------------------------------------------------------------------

    async def _index(self, metric: str = METRIC_DPS) -> IndexSnapshot | ToolAnswer:
        """The ranking index for ``metric``, or the answer for one still filling."""

        snapshot = await index_snapshot(self._data, metric=metric)
        if snapshot is None:
            return ToolAnswer(messages.INDEX_FILLING)
        return snapshot

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
        return choice, cards

    async def _board_slug(self, board: str, page: str) -> str | ToolAnswer:
        """The one board ``board`` names, or the reply saying why it names none.

        ``page`` exists per board, so a dungeon is refused with its name for
        the model to ask about: a tool call cannot wait for a pick.
        """

        target = await self._resolve_target(board)
        if isinstance(target, (str, ToolAnswer)):
            return target
        choice, _cards = target
        return ToolAnswer(
            f"「{shorten(board)}」是副本，{page}要按具体榜单看：请指明"
            f"「{choice.target.name}」下的一个榜单。"
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
            # A tool's picture goes out as a plain image, so only the file
            # matters; the scale is for the command path's markdown picture.
            return (await draw(self._renderer())).path
        except Exception as exc:
            self._logger.warning(
                "ZmdLogBot tool could not render its page: %s", type(exc).__name__
            )
            return None


def _index_note(missing: int) -> str:
    """One line when the index is missing boards, so counts are not taken as whole."""

    if not missing:
        return ""
    return f"（榜单索引有 {missing} 个榜没读到，以下未计入它们。）"


def _profile_options_note(potential: str, metric: str) -> str:
    """角色档案 counts clears by time; a metric or a 潜能 tier given it goes unused.

    Only one that would have changed another answer is named: a model that
    writes the default ``dps`` has asked for nothing.
    """

    unused = []
    if metric.strip() and parse_metric_text(metric) != METRIC_DPS:
        unused.append("metric")
    if potential.strip() and parse_potential_text(potential) != "all":
        unused.append("potential")
    if not unused:
        return ""
    return (
        "（角色档案按通关用时统计，不分口径也不按潜能档筛选，"
        f"{'、'.join(unused)} 没有用上。）"
    )


def _means_every_board(keyword: str) -> bool:
    return "".join(keyword.split()).casefold() in _EVERY_BOARD


def _overview_metric_note(metric: str) -> str:
    """The board list has no rDPS reading; an overview asked for one says so."""

    if not is_rdps(metric):
        return ""
    return "（榜单总览来自榜单列表，只有 DPS 口径；rDPS 请查具体榜单。）"


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
