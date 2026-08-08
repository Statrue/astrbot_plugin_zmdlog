"""Command routing primitives for the ``zmdlog`` entry point."""

from dataclasses import dataclass
from enum import Enum


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


def parse_zmdlog_payload(payload: str) -> RouteRequest:
    """Resolve the text following ``zmdlog`` into a stable route.

    This layer only decides which feature owns the request. Matching board,
    dungeon, scope, and (in a later version) account candidates belongs to the
    unified matcher rather than the command handler.
    """

    normalized = " ".join(payload.split())
    if not normalized or normalized.casefold() == "help":
        return RouteRequest(RouteKind.HELP)

    command, separator, remainder = normalized.partition(" ")
    if command == "榜单":
        if not separator:
            return RouteRequest(RouteKind.ALL_RANKINGS)
        return RouteRequest(RouteKind.RANKING_QUERY, remainder)

    return RouteRequest(RouteKind.SMART_QUERY, normalized)
