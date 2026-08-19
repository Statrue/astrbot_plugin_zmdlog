"""Command routing primitives for the ``zmdlog`` entry point."""

from dataclasses import dataclass
from enum import Enum

DEFAULT_RANKING_TOP = 10
MIN_RANKING_TOP = 1
MAX_RANKING_TOP = 30

STATS_RANGES = ("7d", "14d", "30d", "all")
STATS_POTENTIALS = ("0", "1-5", "all")
DEFAULT_STATS_RANGE = "all"
DEFAULT_STATS_POTENTIAL = "all"

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


class RouteParseError(ValueError):
    """Raised when a public command option is malformed or unsupported."""


class RouteKind(str, Enum):
    """A route resolved before any API request or fuzzy matching happens."""

    HELP = "help"
    ALL_RANKINGS = "all_rankings"
    RANKING_QUERY = "ranking_query"
    ACCOUNT_QUERY = "account_query"
    BATTLE_QUERY = "battle_query"
    SMART_QUERY = "smart_query"
    CHARACTER_STATS = "character_stats"
    ROSTER_QUERY = "roster_query"
    ALIAS_LIST = "alias_list"
    ALIAS_ADD = "alias_add"
    ALIAS_REMOVE = "alias_remove"


@dataclass(frozen=True, slots=True)
class RouteOptions:
    """Trailing ``--name value`` options parsed from the payload."""

    ranking_top: int | None = None
    character_filter: str | None = None
    stats_range: str = DEFAULT_STATS_RANGE
    stats_potential: str = DEFAULT_STATS_POTENTIAL
    present: frozenset[str] = frozenset()

    def reject_except(self, *allowed: str, context: str) -> None:
        """Raise when an option outside ``allowed`` was given."""

        for name in ("top", "character", "range", "potential"):
            if name in self.present and name not in allowed:
                raise RouteParseError(
                    f"{_OPTION_LABEL[name]} {context}"
                )


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """Normalized input passed from the AstrBot handler to later services."""

    kind: RouteKind
    query: str = ""
    ranking_top: int | None = None
    character_filter: str | None = None
    stats_range: str = DEFAULT_STATS_RANGE
    stats_potential: str = DEFAULT_STATS_POTENTIAL

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
        options.reject_except(context="仅适用于具体榜单查询。")
        return RouteRequest(RouteKind.HELP)

    command, separator, remainder = normalized.partition(" ")
    if command == "榜单":
        if not separator:
            options.reject_except(context="仅适用于具体榜单查询。")
            return RouteRequest(RouteKind.ALL_RANKINGS)
        options.reject_except(
            "top", "character", context="仅适用于具体榜单查询。"
        )
        return RouteRequest(
            RouteKind.RANKING_QUERY,
            remainder,
            ranking_top=options.ranking_top,
            character_filter=options.character_filter,
        )

    if command in {"账号", "账户"}:
        options.reject_except(context="不适用于账号查询。")
        if not separator or not remainder:
            raise RouteParseError("请提供 accountId 或 ZMDLogs 账号主页链接。")
        return RouteRequest(RouteKind.ACCOUNT_QUERY, remainder)

    if command == "战报":
        options.reject_except(context="不适用于战报查询。")
        if not separator or not remainder:
            raise RouteParseError("请提供 battleId 或 ZMDLogs 战报链接。")
        return RouteRequest(RouteKind.BATTLE_QUERY, remainder)

    if command == "角色":
        options.reject_except(
            "range", "potential", context="不适用于角色统计。"
        )
        return RouteRequest(
            RouteKind.CHARACTER_STATS,
            remainder,
            stats_range=options.stats_range,
            stats_potential=options.stats_potential,
        )

    if command == "阵容":
        options.reject_except("top", context="不适用于阵容查询。")
        if not separator or not remainder:
            raise RouteParseError("请提供榜单关键词。")
        return RouteRequest(
            RouteKind.ROSTER_QUERY,
            remainder,
            ranking_top=options.ranking_top,
        )

    if command == "别名":
        options.reject_except(context="不适用于别名管理。")
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

    options.reject_except("top", "character", context="仅适用于具体榜单查询。")
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


def _parse_top(raw_top: str) -> int:
    if not raw_top.isascii() or not raw_top.isdecimal():
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
    raise RouteParseError(f"{label} 只支持 {' / '.join(choices)}。")
