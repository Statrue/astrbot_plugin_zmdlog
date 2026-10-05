"""Command routing primitives for the ``zmdlog`` entry point."""

import re
from dataclasses import dataclass, replace
from enum import Enum

from .elements import normalize_element
from .metrics import DEFAULT_METRIC, parse_metric_text
from .professions import normalize_profession

# A board ranking is read a page of ten rows at a time (``--页 N``), or as
# one long picture of its first thirty (``--页 全部``); past thirty the
# picture points at the site instead of growing.
RANKING_PAGE_SIZE = 10
MAX_RANKING_ROWS = 30
# ``--页 全部``: the one page value that is not a number.
ALL_PAGES = "all"
# A page number never needs more digits than this; int() raises a plain
# ValueError past ~4300 of them, so a longer one is refused before conversion.
_MAX_PAGE_DIGITS = 3
# A team has four slots, so more names than that can never all be in one.
MAX_CHARACTER_FILTERS = 4

STATS_RANGES = ("7d", "14d", "30d", "all")
STATS_POTENTIALS = ("0", "1-5", "all")
DEFAULT_STATS_RANGE = "all"
DEFAULT_STATS_POTENTIAL = "all"
# The trend page reads a local trace, so a month is a sensible default view.
DEFAULT_TREND_RANGE = "30d"

_RANKS_START_AT_ONE = "战报名次从 1 开始。"
_BATTLE_RANK_RE = re.compile(r"^(?:第|#)?([0-9]{1,3})(?:名)?$")

# Every spelling of a window that people and models actually type; the
# LLM tools read the same table, so the two never drift apart again.
_RANGE_ALIASES = {
    "7天": "7d", "7日": "7d", "一周": "7d", "1周": "7d", "一星期": "7d",
    "本周": "7d", "这周": "7d", "最近一周": "7d", "近一周": "7d", "week": "7d",
    "14天": "14d", "14日": "14d", "两周": "14d", "2周": "14d", "两星期": "14d",
    "半个月": "14d", "最近两周": "14d", "近两周": "14d",
    "30天": "30d", "30日": "30d", "一个月": "30d", "1个月": "30d", "本月": "30d",
    "最近一个月": "30d", "近一个月": "30d", "month": "30d",
    "全部": "all", "所有": "all", "不限": "all", "全部时间": "all",
}
_RANGE_DAYS_RE = re.compile(r"^(?:最近|近|过去)?(\d{1,3})\s*(?:天|日|d)$")


def parse_potential_text(text: str) -> str | None:
    """``0`` / ``1-5`` / ``all`` for any accepted spelling; None otherwise."""

    value = "".join(text.split())
    folded = value.casefold()
    if folded in STATS_POTENTIALS:
        return folded
    return _POTENTIAL_ALIASES.get(value)


def parse_range_text(text: str) -> str | None:
    """``7d`` / ``14d`` / ``30d`` / ``all`` for any accepted spelling; None otherwise.

    Shared by the ``--范围`` option and the tools' ``range`` argument.
    """

    value = "".join(text.split())
    folded = value.casefold()
    if folded in STATS_RANGES:
        return folded
    if value in _RANGE_ALIASES:
        return _RANGE_ALIASES[value]
    match = _RANGE_DAYS_RE.match(folded)
    if match is not None and f"{int(match.group(1))}d" in STATS_RANGES:
        return f"{int(match.group(1))}d"
    return None
_POTENTIAL_ALIASES = {
    "0潜": "0",
    "零": "0",
    "零潜": "0",
    "1-5潜": "1-5",
    "1~5": "1-5",
    "1～5": "1-5",
    "全部": "all",
    "所有": "all",
}

# Canonical option name -> accepted spellings (case-insensitive).
_OPTION_SPELLINGS: dict[str, tuple[str, ...]] = {
    "page": ("--页", "--page", "-p"),
    "character": ("--角色", "--char", "--character"),
    "range": ("--范围", "--range"),
    "potential": ("--潜能", "--potential"),
    "element": ("--属性", "--element"),
    "profession": ("--职业", "--profession"),
    "metric": ("--口径", "--metric"),
    "board": ("--榜单", "--board"),
}
_OPTION_BY_SPELLING = {
    spelling.casefold(): name
    for name, spellings in _OPTION_SPELLINGS.items()
    for spelling in spellings
}
_OPTION_LABEL = {
    "page": "--页",
    "character": "--角色",
    "range": "--范围",
    "potential": "--潜能",
    "element": "--属性",
    "profession": "--职业",
    "metric": "--口径",
    "board": "--榜单",
}
# Rejection text names where the option DOES work, not the current route —
# "--潜能 不适用于榜单查询" reads like the option belongs somewhere unknown.
OPTION_USAGE = {
    "page": "--页 仅适用于具体榜单查询，例如：罗丹 --页 2，或 罗丹 --页 全部。",
    "character": (
        "--角色 仅适用于具体榜单查询，例如：罗丹 --角色 黎风，"
        "或 罗丹 --角色 黎风 洛茜（同时带上两人的队伍）。"
    ),
    "range": (
        "--范围 仅适用于角色统计、角色档案、名次趋势、新纪录和不带名字的角色排名、"
        "玩家排名，例如：角色统计 罗丹 --范围 7d。"
    ),
    "potential": "--潜能 仅适用于角色统计，例如：角色统计 罗丹 --潜能 0。",
    "element": (
        "--属性 仅适用于具体榜单查询和不带角色名的角色排名，"
        "例如：罗丹 --属性 物理，或 角色排名 --属性 物理。"
    ),
    "profession": "--职业 仅适用于不带角色名的角色排名，例如：角色排名 --职业 突击。",
    "metric": (
        "--口径 仅适用于具体榜单、阵容、角色统计、角色排名、玩家排名、新纪录，"
        "以及按名次查的战报和对比，例如：罗丹 --口径 rdps、战报 罗丹 1 --口径 rdps。"
    ),
    "board": "--榜单 仅适用于角色档案，例如：角色档案 莱万汀 --榜单 罗丹。",
}


# Options a single dash introduces; every other option is spelled ``--``.
_SHORT_OPTIONS = frozenset(
    spelling
    for spellings in _OPTION_SPELLINGS.values()
    for spelling in spellings
    if not spelling.startswith("--")
)
# ``--top N`` drew the first N rows until the ranking was paged (1.3.0).
_REMOVED_TOP = (
    "--top 已取消：具体榜单改为分页，每页 10 条，例如：罗丹 --页 2，"
    "或 罗丹 --页 全部 一次看前 30 条；阵容固定统计前 10 名。"
)


class RouteParseError(ValueError):
    """Raised when a public command option is malformed or unsupported."""


# Commands that take "<battleId, link or board keyword [rank]>"; the kind
# decides which page of that battle is drawn.
_BATTLE_STYLE_COMMANDS: dict[str, "RouteKind"] = {}


class RouteKind(str, Enum):
    """A route resolved before any API request or fuzzy matching happens."""

    HELP = "help"
    # 榜单 alone: every dungeon, as a pick list.
    DUNGEON_LIST = "dungeon_list"
    RANKING_QUERY = "ranking_query"
    ACCOUNT_QUERY = "account_query"
    BATTLE_QUERY = "battle_query"
    # Same argument shape as 战报; they differ only in which page is drawn.
    LOADOUT_QUERY = "loadout_query"
    SKILL_QUERY = "skill_query"
    TIMELINE_QUERY = "timeline_query"
    COMPARE_QUERY = "compare_query"
    SMART_QUERY = "smart_query"
    CHARACTER_STATS = "character_stats"
    # Where the teams fielding one character stand on every board.
    CHARACTER_STANDINGS = "character_standings"
    # 角色档案: how one character is built and how fast it clears.
    CHARACTER_PROFILE = "character_profile"
    # Which public accounts uploaded the most first places.
    PLAYER_CHAMPIONS = "player_champions"
    # New records and first places changing hands, from the index's re-reads.
    RECORDS_QUERY = "records_query"
    ROSTER_QUERY = "roster_query"
    ALIAS_LIST = "alias_list"
    ALIAS_ADD = "alias_add"
    ALIAS_REMOVE = "alias_remove"
    # 关注 / 取关: a chat's board watch, its notice whitelist.
    WATCH_LIST = "watch_list"
    WATCH_ADD = "watch_add"
    WATCH_REMOVE = "watch_remove"
    WATCH_ALL = "watch_all"
    UNWATCH_ALL = "unwatch_all"
    TREND_QUERY = "trend_query"
    # 绑定 <绑定码> / 解绑 / 主账号: one user's verified site accounts.
    BIND = "bind"
    UNBIND = "unbind"
    PRIMARY_ACCOUNT = "primary_account"
    # 我的: the sender's bound account, drawn as the account page.
    MY_ACCOUNT = "my_account"


ALIAS_ROUTES = frozenset(
    {RouteKind.ALIAS_LIST, RouteKind.ALIAS_ADD, RouteKind.ALIAS_REMOVE}
)
WATCH_ROUTES = frozenset(
    {
        RouteKind.WATCH_LIST,
        RouteKind.WATCH_ADD,
        RouteKind.WATCH_REMOVE,
        RouteKind.WATCH_ALL,
        RouteKind.UNWATCH_ALL,
    }
)
BINDING_ROUTES = frozenset(
    {RouteKind.BIND, RouteKind.UNBIND, RouteKind.PRIMARY_ACCOUNT}
)
# 关注 全部 / 取关 全部: every board the site lists, now and later. One
# spelling: 全部榜单 is an ordinary keyword, matched like any other.
ALL_BOARDS_WORDS = frozenset({"全部"})
# The configuration actions — 别名, 关注, 绑定 — as against the queries,
# which draw a page. They answer in text, and a tapped callback button never
# runs one: its data is whatever the tapping client sends.
CONFIGURATION_ROUTES = ALIAS_ROUTES | WATCH_ROUTES | BINDING_ROUTES


_BATTLE_STYLE_COMMANDS.update(
    {
        "战报": RouteKind.BATTLE_QUERY,
        "配装": RouteKind.LOADOUT_QUERY,
        "装备": RouteKind.LOADOUT_QUERY,
        "技能": RouteKind.SKILL_QUERY,
        "技能统计": RouteKind.SKILL_QUERY,
        "技能轴": RouteKind.TIMELINE_QUERY,
        "排轴": RouteKind.TIMELINE_QUERY,
        "时间轴": RouteKind.TIMELINE_QUERY,
    }
)


@dataclass(frozen=True, slots=True)
class RouteOptions:
    """Trailing ``--name value`` options parsed from the payload."""

    # A page number from 1, or ``ALL_PAGES``; None when not given.
    ranking_page: int | str | None = None
    character_filter: str | None = None
    element_filter: str | None = None
    profession_filter: str | None = None
    stats_range: str = DEFAULT_STATS_RANGE
    stats_potential: str = DEFAULT_STATS_POTENTIAL
    metric: str = DEFAULT_METRIC
    board_query: str | None = None
    present: frozenset[str] = frozenset()

    def reject_except(self, *allowed: str) -> None:
        """Raise when an option outside ``allowed`` was given."""

        for name in (
            "page", "character", "range", "potential", "element", "profession",
            "metric", "board",
        ):
            if name in self.present and name not in allowed:
                raise RouteParseError(OPTION_USAGE[name])


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """Normalized input passed from the AstrBot handler to later services."""

    kind: RouteKind
    query: str = ""
    # The board ranking's page: a number from 1 or ``ALL_PAGES``; None is
    # the first page, as typed without ``--页``.
    ranking_page: int | str | None = None
    character_filter: str | None = None
    # A catalog element label (物理 …); rows whose main C has it.
    element_filter: str | None = None
    # A profession label (突击 …); only 角色排名 without a name takes it.
    profession_filter: str | None = None
    stats_range: str = DEFAULT_STATS_RANGE
    stats_potential: str = DEFAULT_STATS_POTENTIAL
    # ``dps`` unless ``--口径 rdps`` asked for the team-contribution board.
    metric: str = DEFAULT_METRIC
    battle_rank: int = 1
    # 对比 only: the second battle reference, or the second rank on the same
    # board when the query is a board keyword.
    compare_target: str | None = None
    compare_rank: int | None = None
    # 对比 <榜单> 我 [名次]: one side is the asker's own best record on the
    # board, the other the record at ``compare_rank``.
    compare_self: bool = False
    # 角色档案 only: the board keyword ``--榜单`` cut the profile to.
    board_query: str | None = None


def ranking_page_count(row_count: int) -> int:
    """The pages ``row_count`` ranking rows fill; no rows is still one page."""

    return max(1, -(-row_count // RANKING_PAGE_SIZE))


def parse_zmdlog_payload(payload: str) -> RouteRequest:
    """Resolve the text following ``zmdlog`` into a stable route.

    This layer only decides which feature owns the request. Matching boards,
    dungeons and scopes belongs to the matcher, and nicknames to the account
    search, never to the command handler.
    """

    normalized, options = _extract_options(" ".join(payload.split()))
    if not normalized or normalized.casefold() == "help":
        options.reject_except()
        return RouteRequest(RouteKind.HELP)

    command, separator, remainder = normalized.partition(" ")
    if command == "榜单":
        if not separator:
            options.reject_except()
            return RouteRequest(RouteKind.DUNGEON_LIST)
        options.reject_except("page", "character", "element", "metric")
        return RouteRequest(
            RouteKind.RANKING_QUERY,
            remainder,
            ranking_page=options.ranking_page,
            character_filter=options.character_filter,
            element_filter=options.element_filter,
            metric=options.metric,
        )

    if command in {"账号", "账户"}:
        options.reject_except()
        if not separator or not remainder:
            raise RouteParseError("请提供 accountId 或 ZMDLogs 账号主页链接。")
        return RouteRequest(RouteKind.ACCOUNT_QUERY, remainder)

    if command in {"对比", "比较"}:
        options.reject_except("metric")
        route = _parse_compare(remainder)
        if route.compare_target is not None:
            _reject_metric_on_references(options)
        return replace(route, metric=options.metric)

    battle_kind = _BATTLE_STYLE_COMMANDS.get(command)
    if battle_kind is not None:
        options.reject_except("metric")
        if not separator or not remainder:
            raise RouteParseError(
                "请提供 battleId、战报链接，或榜单关键词"
                f"（可加名次，如：{command} 罗丹 3）。"
            )
        query, battle_rank = _split_battle_rank(remainder)
        if _REFERENCE_RE.match(query):
            _reject_metric_on_references(options)
        return RouteRequest(
            battle_kind, query, battle_rank=battle_rank, metric=options.metric
        )

    if command in {"趋势", "名次趋势"}:
        options.reject_except("range")
        if not separator or not remainder:
            raise RouteParseError(
                "请提供公开昵称、accountId 或 ZMDLogs 账号主页链接，例如：趋势 CPU 0。"
            )
        return RouteRequest(
            RouteKind.TREND_QUERY,
            remainder,
            stats_range=(
                options.stats_range
                if "range" in options.present
                else DEFAULT_TREND_RANGE
            ),
        )

    if command in {"新纪录", "新记录", "最近纪录"}:
        options.reject_except("range", "metric")
        if remainder:
            raise RouteParseError(
                "新纪录不接参数，只能加 --范围，例如：新纪录 --范围 30d。"
            )
        if "range" in options.present and options.stats_range == "all":
            raise RouteParseError(
                "新纪录的 --范围 只支持 7d / 14d / 30d，例如：新纪录 --范围 30d。"
            )
        return RouteRequest(
            RouteKind.RECORDS_QUERY,
            stats_range=(
                options.stats_range if "range" in options.present else "7d"
            ),
            metric=options.metric,
        )

    if command in {"玩家排名", "玩家榜", "玩家冠军榜"}:
        # The same shape as 角色排名: bare is the board of everyone, a name
        # is that one player, which is what 账号 draws.
        options.reject_except("range", "metric")
        if remainder:
            if "range" in options.present or "metric" in options.present:
                raise RouteParseError(
                    "--范围 和 --口径 只在不带昵称的玩家排名里用，"
                    "例如：玩家排名 --范围 7d。"
                )
            return RouteRequest(RouteKind.ACCOUNT_QUERY, remainder)
        return RouteRequest(
            RouteKind.PLAYER_CHAMPIONS,
            stats_range=options.stats_range,
            metric=options.metric,
        )

    if command in {"角色排名", "角色榜"}:
        # Without a name: every character's first places over all boards,
        # optionally only the characters of one element.
        options.reject_except("element", "range", "profession", "metric")
        if remainder and (
            options.element_filter is not None
            or options.profession_filter is not None
            or "range" in options.present
        ):
            raise RouteParseError(
                "--属性、--职业 和 --范围 只在不带角色名的角色排名里用，"
                "例如：角色排名 --属性 物理，角色排名 --职业 突击，"
                "或 角色排名 --范围 7d。"
            )
        return RouteRequest(
            RouteKind.CHARACTER_STANDINGS,
            remainder,
            element_filter=options.element_filter,
            profession_filter=options.profession_filter,
            stats_range=options.stats_range,
            metric=options.metric,
        )

    if command in {"角色统计", "角色"}:
        options.reject_except("range", "potential", "metric")
        return RouteRequest(
            RouteKind.CHARACTER_STATS,
            remainder,
            stats_range=options.stats_range,
            stats_potential=options.stats_potential,
            metric=options.metric,
        )

    if command == "角色档案":
        # Upstream ignores metric and potential here, so neither is passed
        # on as if it did something.
        options.reject_except("range", "board")
        if not remainder:
            raise RouteParseError("请提供角色名，例如：角色档案 莱万汀。")
        return RouteRequest(
            RouteKind.CHARACTER_PROFILE,
            remainder,
            stats_range=options.stats_range,
            board_query=options.board_query,
        )

    if command == "阵容":
        options.reject_except("metric")
        if not separator or not remainder:
            raise RouteParseError("请提供榜单关键词。")
        return RouteRequest(
            RouteKind.ROSTER_QUERY,
            remainder,
            metric=options.metric,
        )

    if command == "别名":
        options.reject_except()
        if not remainder:
            return RouteRequest(RouteKind.ALIAS_LIST)
        action, _, rest = remainder.partition(" ")
        if action in {"添加", "增加", "add"}:
            if len(rest.split()) < 2:
                raise RouteParseError(
                    "用法：别名 添加 <榜单或副本> <别名1> [别名2 ...]"
                )
            return RouteRequest(RouteKind.ALIAS_ADD, rest)
        if action in {"删除", "移除", "del", "remove"}:
            if not rest:
                raise RouteParseError("用法：别名 删除 <别名>")
            return RouteRequest(RouteKind.ALIAS_REMOVE, rest)
        raise RouteParseError("别名子命令只支持：添加 / 删除，或不带参数查看列表。")

    if command in {"关注", "盯"}:
        options.reject_except()
        if not remainder:
            return RouteRequest(RouteKind.WATCH_LIST)
        if remainder in ALL_BOARDS_WORDS:
            return RouteRequest(RouteKind.WATCH_ALL)
        return RouteRequest(RouteKind.WATCH_ADD, remainder)

    if command in {"取关", "取消关注", "不盯"}:
        options.reject_except()
        if not remainder:
            raise RouteParseError(
                "用法：取关 <序号或榜单关键词> 或 取关 全部，序号见 关注 列表。"
            )
        if remainder in ALL_BOARDS_WORDS:
            return RouteRequest(RouteKind.UNWATCH_ALL)
        return RouteRequest(RouteKind.WATCH_REMOVE, remainder)

    if command in {"绑定", "绑定账号"}:
        # The argument is a binding code or nothing; the service says which.
        options.reject_except()
        return RouteRequest(RouteKind.BIND, remainder)

    if command in {"解绑", "解除绑定", "取消绑定"}:
        options.reject_except()
        return RouteRequest(RouteKind.UNBIND, remainder)

    if command == "主账号":
        options.reject_except()
        return RouteRequest(RouteKind.PRIMARY_ACCOUNT, remainder)

    if command == "我的":
        options.reject_except()
        return RouteRequest(RouteKind.MY_ACCOUNT, remainder)

    options.reject_except("page", "character", "element", "metric")
    return RouteRequest(
        RouteKind.SMART_QUERY,
        normalized,
        ranking_page=options.ranking_page,
        character_filter=options.character_filter,
        element_filter=options.element_filter,
        metric=options.metric,
    )


def _extract_options(payload: str) -> tuple[str, RouteOptions]:
    """Split trailing ``--name value`` pairs off the free-text query."""

    tokens = payload.split()
    positions = [
        index for index, token in enumerate(tokens) if _is_option_token(token)
    ]
    if not positions:
        return payload, RouteOptions()

    first = positions[0]
    values: dict[str, str] = {}
    index = first
    last_label = ""
    while index < len(tokens):
        token = tokens[index]
        name = _OPTION_BY_SPELLING.get(token.casefold())
        if name is None:
            if token.casefold() == "--top":
                raise RouteParseError(_REMOVED_TOP)
            if token.startswith("--"):
                raise RouteParseError(f"不支持的选项 {token}。")
            raise RouteParseError(f"{last_label} 参数必须放在查询末尾。")
        label = last_label = _OPTION_LABEL[name]
        if name in values:
            raise RouteParseError(f"{label} 参数只能填写一次。")
        if index + 1 >= len(tokens) or _is_option_token(tokens[index + 1]):
            raise RouteParseError(f"{label} 后需要填写取值。")
        if name in ("character", "board"):
            # Both take every word up to the next option: ``--角色 黎风 洛茜``
            # asks for teams fielding both, and a board keyword is as many
            # words as one typed after zmdlog (``--榜单 白刃穿水 残酷``).
            end = index + 1
            while end < len(tokens) and not _is_option_token(tokens[end]):
                end += 1
            words = tokens[index + 1 : end]
            if name == "character":
                words = list(dict.fromkeys(words))
                if len(words) > MAX_CHARACTER_FILTERS:
                    raise RouteParseError(
                        f"--角色 最多写 {MAX_CHARACTER_FILTERS} 个角色。"
                    )
            values[name] = " ".join(words)
            index = end
            continue
        values[name] = tokens[index + 1]
        index += 2

    query = " ".join(tokens[:first])
    element_filter = None
    if "element" in values:
        element_filter = normalize_element(values["element"])
        if element_filter is None:
            raise RouteParseError("--属性 只能填 物理、灼热、寒冷、自然、电磁。")
    profession_filter = None
    if "profession" in values:
        profession_filter = normalize_profession(values["profession"])
        if profession_filter is None:
            raise RouteParseError(
                "--职业 只能填 先锋、近卫、重装、术士、突击、辅助（术师也认）。"
            )
    metric = DEFAULT_METRIC
    if "metric" in values:
        parsed_metric = parse_metric_text(values["metric"])
        if parsed_metric is None:
            raise RouteParseError("--口径 只能填 dps 或 rdps。")
        metric = parsed_metric
    return query, RouteOptions(
        ranking_page=_parse_page(values["page"]) if "page" in values else None,
        character_filter=values.get("character"),
        element_filter=element_filter,
        profession_filter=profession_filter,
        metric=metric,
        board_query=values.get("board"),
        stats_range=(
            _parse_range(values["range"])
            if "range" in values
            else DEFAULT_STATS_RANGE
        ),
        stats_potential=(
            _parse_choice(
                values["potential"], STATS_POTENTIALS, _POTENTIAL_ALIASES, "--潜能"
            )
            if "potential" in values
            else DEFAULT_STATS_POTENTIAL
        ),
        present=frozenset(values),
    )


def _is_option_token(token: str) -> bool:
    return token.startswith("--") or token.casefold() in _SHORT_OPTIONS


_COMPARE_USAGE = (
    "用法：对比 <榜单关键词> [名次A] [名次B]（默认第 1 名对第 2 名），"
    "对比 <榜单关键词> 我 [名次]（自己的最好记录对第 N 名，默认第 1 名），"
    "或 对比 <battleId或链接> <battleId或链接>。"
)
_REFERENCE_RE = re.compile(r"^(?:https?://\S+|btl_[A-Za-z0-9_-]+)$")
# 对比 <榜单> 我: the asker's own record is one side.
_SELF_MARKER = "我"


def _reject_metric_on_references(options: RouteOptions) -> None:
    """A battle named outright has no rank to read off either board."""

    if "metric" in options.present:
        raise RouteParseError(OPTION_USAGE["metric"])


def _parse_compare(remainder: str) -> RouteRequest:
    """``对比 A B`` for two references, else ``对比 <榜单> [名次] [名次]``.

    Ranks are read off the tail: one rank means "the leader against that
    rank", none means the top two. Two references are recognised by shape
    (link or battle id), so a board keyword can never be mistaken for one.
    ``对比 <榜单> 我 [名次]`` puts the asker's own record against one rank,
    the first by default; who the asker is, is the query service's to say.
    """

    tokens = remainder.split()
    if not tokens:
        raise RouteParseError(_COMPARE_USAGE)
    if _SELF_MARKER in tokens:
        return _parse_compare_self(tokens)
    if len(tokens) == 1 and _REFERENCE_RE.match(tokens[0]):
        # One battle id is not a board keyword; it would otherwise go
        # upstream as one and come back as "no such board".
        raise RouteParseError(_COMPARE_USAGE)
    if len(tokens) == 2 and all(_REFERENCE_RE.match(token) for token in tokens):
        return RouteRequest(
            RouteKind.COMPARE_QUERY, tokens[0], compare_target=tokens[1]
        )
    ranks: list[int] = []
    while tokens and len(ranks) < 2:
        match = _BATTLE_RANK_RE.match(tokens[-1])
        if match is None:
            break
        ranks.insert(0, int(match.group(1)))
        tokens.pop()
    if not tokens:
        raise RouteParseError(_COMPARE_USAGE)
    if any(rank < 1 for rank in ranks):
        raise RouteParseError(_RANKS_START_AT_ONE)
    if len(ranks) == 2:
        first, second = ranks
    elif len(ranks) == 1:
        first, second = 1, ranks[0]
    else:
        first, second = 1, 2
    if first == second:
        raise RouteParseError("两个名次相同，没有可比的。")
    return RouteRequest(
        RouteKind.COMPARE_QUERY,
        " ".join(tokens),
        battle_rank=first,
        compare_rank=second,
    )


def _parse_compare_self(tokens: list[str]) -> RouteRequest:
    """``<榜单> 我 [名次]``: the marker is last but for one optional rank."""

    rank = 1
    if tokens[-1] != _SELF_MARKER:
        match = _BATTLE_RANK_RE.match(tokens[-1])
        if match is None or len(tokens) < 2 or tokens[-2] != _SELF_MARKER:
            raise RouteParseError(_COMPARE_USAGE)
        rank = int(match.group(1))
        tokens = tokens[:-1]
    board = tokens[:-1]
    if not board:
        raise RouteParseError(_COMPARE_USAGE)
    if rank < 1:
        raise RouteParseError(_RANKS_START_AT_ONE)
    return RouteRequest(
        RouteKind.COMPARE_QUERY,
        " ".join(board),
        compare_rank=rank,
        compare_self=True,
    )


def _split_battle_rank(remainder: str) -> tuple[str, int]:
    """Split a trailing rank token off ``战报 <榜单> [名次]``.

    Single-token arguments (battle ids, URLs, plain board names) are left
    untouched so ``战报 btl_xxx`` keeps its exact reference semantics.
    """

    tokens = remainder.split()
    if len(tokens) < 2:
        return remainder, 1
    match = _BATTLE_RANK_RE.match(tokens[-1])
    if match is None:
        return remainder, 1
    rank = int(match.group(1))
    if rank < 1:
        raise RouteParseError(_RANKS_START_AT_ONE)
    return " ".join(tokens[:-1]), rank


def _parse_page(raw: str) -> int | str:
    """A page number from 1, or ``ALL_PAGES`` for 全部 / all.

    Whether the page exists depends on the board and its filters, so the
    upper bound is checked once the rows are known, not here.
    """

    if raw == "全部" or raw.casefold() == ALL_PAGES:
        return ALL_PAGES
    if (
        not raw.isascii()
        or not raw.isdecimal()
        or len(raw) > _MAX_PAGE_DIGITS
        or int(raw) < 1
    ):
        raise RouteParseError("--页 只能填页码（1、2、3…）或 全部。")
    return int(raw)


def _parse_range(raw: str) -> str:
    value = parse_range_text(raw)
    if value is None:
        raise RouteParseError(f"--范围 只支持 {' / '.join(STATS_RANGES)}。")
    return value


def _parse_choice(
    raw: str,
    choices: tuple[str, ...],
    aliases: dict[str, str],
    label: str,
) -> str:
    folded = raw.casefold()
    if folded in choices:
        return folded
    if raw in aliases:
        return aliases[raw]
    if label == "--潜能":
        raise RouteParseError(
            "--潜能 只支持 0（零潜）/ 1-5（有潜能，合并统计）/ all，"
            "ZMDLogs 不按具体潜能层数拆分。"
        )
    raise RouteParseError(f"{label} 只支持 {' / '.join(choices)}。")
