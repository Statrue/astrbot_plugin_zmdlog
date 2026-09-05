"""Command routing primitives for the ``zmdlog`` entry point."""

import re
from dataclasses import dataclass
from enum import Enum

DEFAULT_RANKING_TOP = 10
MIN_RANKING_TOP = 1
MAX_RANKING_TOP = 30
# A team has four slots, so more names than that can never all be in one.
MAX_CHARACTER_FILTERS = 4

STATS_RANGES = ("7d", "14d", "30d", "all")
STATS_POTENTIALS = ("0", "1-5", "all")
DEFAULT_STATS_RANGE = "all"
DEFAULT_STATS_POTENTIAL = "all"
# The trend page reads a local trace, so a month is a sensible default view.
DEFAULT_TREND_RANGE = "30d"

_BATTLE_RANK_RE = re.compile(r"^(?:第|#)?([0-9]{1,3})(?:名)?$")

_RANGE_ALIASES = {
    "7天": "7d",
    "7日": "7d",
    "14天": "14d",
    "14日": "14d",
    "30天": "30d",
    "30日": "30d",
    "全部": "all",
    "所有": "all",
}
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
    "top": ("--top",),
    "character": ("--角色", "--char", "--character"),
    "range": ("--范围", "--range"),
    "potential": ("--潜能", "--potential"),
}
_OPTION_BY_SPELLING = {
    spelling.casefold(): name
    for name, spellings in _OPTION_SPELLINGS.items()
    for spelling in spellings
}
_OPTION_LABEL = {
    "top": "--top",
    "character": "--角色",
    "range": "--范围",
    "potential": "--潜能",
}
# Rejection text names where the option DOES work, not the current route —
# "--潜能 不适用于榜单查询" reads like the option belongs somewhere unknown.
_OPTION_USAGE = {
    "top": "--top 仅适用于具体榜单和阵容查询。",
    "character": (
        "--角色 仅适用于具体榜单查询，例如：罗丹 --角色 黎风，"
        "或 罗丹 --角色 黎风 洛茜（同时带上两人的队伍）。"
    ),
    "range": "--范围 仅适用于角色统计和名次趋势，例如：角色统计 罗丹 --范围 7d。",
    "potential": "--潜能 仅适用于角色统计，例如：角色统计 罗丹 --潜能 0。",
}


class RouteParseError(ValueError):
    """Raised when a public command option is malformed or unsupported."""


# Commands that take "<battleId, link or board keyword [rank]>"; the kind
# decides which page of that battle is drawn.
_BATTLE_STYLE_COMMANDS: dict[str, "RouteKind"] = {}


class RouteKind(str, Enum):
    """A route resolved before any API request or fuzzy matching happens."""

    HELP = "help"
    ALL_RANKINGS = "all_rankings"
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
    ROSTER_QUERY = "roster_query"
    ALIAS_LIST = "alias_list"
    ALIAS_ADD = "alias_add"
    ALIAS_REMOVE = "alias_remove"
    WATCH_LIST = "watch_list"
    WATCH_ADD = "watch_add"
    WATCH_REMOVE = "watch_remove"
    WATCH_BOARD_ADD = "watch_board_add"
    WATCH_BOARD_REMOVE = "watch_board_remove"
    TREND_QUERY = "trend_query"


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

    ranking_top: int | None = None
    character_filter: str | None = None
    stats_range: str = DEFAULT_STATS_RANGE
    stats_potential: str = DEFAULT_STATS_POTENTIAL
    present: frozenset[str] = frozenset()

    def reject_except(self, *allowed: str) -> None:
        """Raise when an option outside ``allowed`` was given."""

        for name in ("top", "character", "range", "potential"):
            if name in self.present and name not in allowed:
                raise RouteParseError(_OPTION_USAGE[name])


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """Normalized input passed from the AstrBot handler to later services."""

    kind: RouteKind
    query: str = ""
    ranking_top: int | None = None
    character_filter: str | None = None
    stats_range: str = DEFAULT_STATS_RANGE
    stats_potential: str = DEFAULT_STATS_POTENTIAL
    battle_rank: int = 1
    # 对比 only: the second battle reference, or the second rank on the same
    # board when the query is a board keyword.
    compare_target: str | None = None
    compare_rank: int | None = None

    @property
    def ranking_limit(self) -> int:
        """Return the requested concrete-ranking size or its default."""

        return (
            self.ranking_top
            if self.ranking_top is not None
            else DEFAULT_RANKING_TOP
        )


def parse_zmdlog_payload(payload: str) -> RouteRequest:
    """Resolve the text following ``zmdlog`` into a stable route.

    This layer only decides which feature owns the request. Matching board,
    dungeon, scope, and (in a later version) account candidates belongs to the
    unified matcher rather than the command handler.
    """

    normalized, options = _extract_options(" ".join(payload.split()))
    if not normalized or normalized.casefold() == "help":
        options.reject_except()
        return RouteRequest(RouteKind.HELP)

    command, separator, remainder = normalized.partition(" ")
    if command == "榜单":
        if not separator:
            options.reject_except()
            return RouteRequest(RouteKind.ALL_RANKINGS)
        options.reject_except("top", "character")
        return RouteRequest(
            RouteKind.RANKING_QUERY,
            remainder,
            ranking_top=options.ranking_top,
            character_filter=options.character_filter,
        )

    if command in {"账号", "账户"}:
        options.reject_except()
        if not separator or not remainder:
            raise RouteParseError("请提供 accountId 或 ZMDLogs 账号主页链接。")
        return RouteRequest(RouteKind.ACCOUNT_QUERY, remainder)

    if command in {"对比", "比较"}:
        options.reject_except()
        return _parse_compare(remainder)

    battle_kind = _BATTLE_STYLE_COMMANDS.get(command)
    if battle_kind is not None:
        options.reject_except()
        if not separator or not remainder:
            raise RouteParseError(
                "请提供 battleId、战报链接，或榜单关键词"
                f"（可加名次，如：{command} 罗丹 3）。"
            )
        query, battle_rank = _split_battle_rank(remainder)
        return RouteRequest(battle_kind, query, battle_rank=battle_rank)

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

    if command in {"角色排名", "角色榜"}:
        options.reject_except()
        if not separator or not remainder:
            raise RouteParseError("请提供角色名，例如：角色排名 提弗洛斯。")
        return RouteRequest(RouteKind.CHARACTER_STANDINGS, remainder)

    if command in {"角色统计", "角色"}:
        options.reject_except("range", "potential")
        return RouteRequest(
            RouteKind.CHARACTER_STATS,
            remainder,
            stats_range=options.stats_range,
            stats_potential=options.stats_potential,
        )

    if command == "阵容":
        options.reject_except("top")
        if not separator or not remainder:
            raise RouteParseError("请提供榜单关键词。")
        return RouteRequest(
            RouteKind.ROSTER_QUERY,
            remainder,
            ranking_top=options.ranking_top,
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
        board_query = _board_watch_argument(remainder)
        if board_query is not None:
            if not board_query:
                raise RouteParseError("用法：关注 榜单 <榜单关键词>")
            return RouteRequest(RouteKind.WATCH_BOARD_ADD, board_query)
        return RouteRequest(RouteKind.WATCH_ADD, remainder)

    if command in {"取关", "取消关注", "不盯"}:
        options.reject_except()
        if not remainder:
            raise RouteParseError(
                "用法：取关 <序号或昵称> 或 取关 榜单 <序号或榜单关键词>，"
                "序号见 关注 列表。"
            )
        board_query = _board_watch_argument(remainder)
        if board_query is not None:
            if not board_query:
                raise RouteParseError("用法：取关 榜单 <序号或榜单关键词>")
            return RouteRequest(RouteKind.WATCH_BOARD_REMOVE, board_query)
        return RouteRequest(RouteKind.WATCH_REMOVE, remainder)

    options.reject_except("top", "character")
    return RouteRequest(
        RouteKind.SMART_QUERY,
        normalized,
        ranking_top=options.ranking_top,
        character_filter=options.character_filter,
    )


def _extract_options(payload: str) -> tuple[str, RouteOptions]:
    """Split trailing ``--name value`` pairs off the free-text query."""

    tokens = payload.split()
    positions = [
        index
        for index, token in enumerate(tokens)
        if token.startswith("--")
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
            if token.startswith("--"):
                raise RouteParseError(f"不支持的选项 {token}。")
            raise RouteParseError(f"{last_label} 参数必须放在查询末尾。")
        label = last_label = _OPTION_LABEL[name]
        if name in values:
            raise RouteParseError(f"{label} 参数只能填写一次。")
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
            raise RouteParseError(f"{label} 后需要填写取值。")
        if name == "character":
            # --角色 takes every name up to the next option: ``--角色 黎风 洛茜``
            # asks for teams fielding both.
            end = index + 1
            while end < len(tokens) and not tokens[end].startswith("--"):
                end += 1
            names = tuple(dict.fromkeys(tokens[index + 1 : end]))
            if len(names) > MAX_CHARACTER_FILTERS:
                raise RouteParseError(
                    f"--角色 最多写 {MAX_CHARACTER_FILTERS} 个角色。"
                )
            values[name] = " ".join(names)
            index = end
            continue
        values[name] = tokens[index + 1]
        index += 2

    query = " ".join(tokens[:first])
    return query, RouteOptions(
        ranking_top=_parse_top(values["top"]) if "top" in values else None,
        character_filter=values.get("character"),
        stats_range=(
            _parse_choice(values["range"], STATS_RANGES, _RANGE_ALIASES, "--范围")
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


def _board_watch_argument(remainder: str) -> str | None:
    """The text after a leading 榜单 marker; None when an account is meant."""

    head, _, rest = remainder.partition(" ")
    if head != "榜单":
        return None
    return rest.strip()


_COMPARE_USAGE = (
    "用法：对比 <榜单关键词> [名次A] [名次B]（默认第 1 名对第 2 名），"
    "或 对比 <battleId或链接> <battleId或链接>。"
)
_REFERENCE_RE = re.compile(r"^(?:https?://\S+|btl_[A-Za-z0-9_-]+)$")


def _parse_compare(remainder: str) -> RouteRequest:
    """``对比 A B`` for two references, else ``对比 <榜单> [名次] [名次]``.

    Ranks are read off the tail: one rank means "the leader against that
    rank", none means the top two. Two references are recognised by shape
    (link or battle id), so a board keyword can never be mistaken for one.
    """

    tokens = remainder.split()
    if not tokens:
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
        raise RouteParseError("战报名次从 1 开始。")
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
        raise RouteParseError("战报名次从 1 开始。")
    return " ".join(tokens[:-1]), rank


def _parse_top(raw_top: str) -> int:
    # int() raises a plain ValueError past ~4300 digits; the maximum is two
    # digits, so anything longer is rejected before conversion.
    if not raw_top.isascii() or not raw_top.isdecimal() or len(raw_top) > 3:
        raise RouteParseError("--top 只支持 1–30 的整数。")
    ranking_top = int(raw_top)
    if not MIN_RANKING_TOP <= ranking_top <= MAX_RANKING_TOP:
        raise RouteParseError("--top 只支持 1–30 的整数。")
    return ranking_top


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
