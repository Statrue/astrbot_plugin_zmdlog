"""Command routing primitives for the ``zmdlog`` entry point."""

from dataclasses import dataclass
from enum import Enum

DEFAULT_RANKING_TOP = 10
MIN_RANKING_TOP = 1
MAX_RANKING_TOP = 30


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
    ALIAS_LIST = "alias_list"
    ALIAS_ADD = "alias_add"
    ALIAS_REMOVE = "alias_remove"


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """Normalized input passed from the AstrBot handler to later services."""

    kind: RouteKind
    query: str = ""
    ranking_top: int | None = None

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

    normalized, ranking_top = _extract_ranking_top(" ".join(payload.split()))
    if not normalized or normalized.casefold() == "help":
        if ranking_top is not None:
            raise RouteParseError("--top 仅适用于具体榜单查询。")
        return RouteRequest(RouteKind.HELP)

    command, separator, remainder = normalized.partition(" ")
    if command == "榜单":
        if not separator:
            if ranking_top is not None:
                raise RouteParseError("--top 仅适用于具体榜单查询。")
            return RouteRequest(RouteKind.ALL_RANKINGS)
        return RouteRequest(
            RouteKind.RANKING_QUERY,
            remainder,
            ranking_top=ranking_top,
        )

    if command in {"账号", "账户"}:
        if ranking_top is not None:
            raise RouteParseError("--top 不适用于账号查询。")
        if not separator or not remainder:
            raise RouteParseError("请提供 accountId 或 ZMDLogs 账号主页链接。")
        return RouteRequest(RouteKind.ACCOUNT_QUERY, remainder)

    if command == "战报":
        if ranking_top is not None:
            raise RouteParseError("--top 不适用于战报查询。")
        if not separator or not remainder:
            raise RouteParseError("请提供 battleId 或 ZMDLogs 战报链接。")
        return RouteRequest(RouteKind.BATTLE_QUERY, remainder)

    if command == "别名":
        if ranking_top is not None:
            raise RouteParseError("--top 不适用于别名管理。")
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

    return RouteRequest(
        RouteKind.SMART_QUERY,
        normalized,
        ranking_top=ranking_top,
    )


def _extract_ranking_top(payload: str) -> tuple[str, int | None]:
    tokens = payload.split()
    positions = [
        index
        for index, token in enumerate(tokens)
        if token.casefold() == "--top"
    ]
    if not positions:
        return payload, None
    if len(positions) > 1:
        raise RouteParseError("--top 参数只能填写一次。")

    position = positions[0]
    if position == len(tokens) - 1:
        raise RouteParseError("--top 后需要填写 1–30 的整数。")
    if position != len(tokens) - 2:
        raise RouteParseError("--top 参数必须放在查询末尾。")

    raw_top = tokens[position + 1]
    if not raw_top.isascii() or not raw_top.isdecimal():
        raise RouteParseError("--top 只支持 1–30 的整数。")
    ranking_top = int(raw_top)
    if not MIN_RANKING_TOP <= ranking_top <= MAX_RANKING_TOP:
        raise RouteParseError("--top 只支持 1–30 的整数。")

    return " ".join(tokens[:position]), ranking_top
