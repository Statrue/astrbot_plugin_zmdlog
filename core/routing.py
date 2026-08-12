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
    SMART_QUERY = "smart_query"


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
