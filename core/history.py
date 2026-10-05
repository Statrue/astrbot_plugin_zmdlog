"""Rank traces: the shape of the 趋势 data, and the pure work on it.

A trace is one account's rank on one board over time, a point stored only
when the rank moved. This module holds that model, its pruning, the window
arithmetic of the 趋势 page and the account tool, and the file format;
:mod:`core.rank_trend` keeps every account's traces and explains what that
costs. Nothing here talks to the network or holds state.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .timestamps import parse_timestamp

HISTORY_VERSION = 1
TREND_RETENTION_DAYS = 90
MAX_POINT_AGE_SECONDS = TREND_RETENTION_DAYS * 86_400.0
# Why 500 and not the 120 the watched-account trace had: core/rank_trend.
MAX_POINTS_PER_BOARD = 500
# What ``all`` means on a trace: the whole of what is kept.
ALL_TREND_LABEL = f"近 {TREND_RETENTION_DAYS} 天"


@dataclass(frozen=True, slots=True)
class RankPoint:
    checked_at: str
    rank: int


@dataclass(frozen=True, slots=True)
class BoardHistory:
    boss_slug: str
    boss_name: str
    dungeon_name: str
    points: tuple[RankPoint, ...]


@dataclass(frozen=True, slots=True)
class AccountHistory:
    account_id: str
    display_name: str
    boards: tuple[BoardHistory, ...]

    def board(self, boss_slug: str) -> BoardHistory | None:
        for board in self.boards:
            if board.boss_slug == boss_slug:
                return board
        return None


def prune_points(
    points: tuple[RankPoint, ...],
    *,
    now: str,
    max_points: int = MAX_POINTS_PER_BOARD,
    max_age_seconds: float = MAX_POINT_AGE_SECONDS,
) -> tuple[RankPoint, ...]:
    """Drop points older than the window or beyond the cap, newest kept.

    The most recent point always survives: it is the current rank, and a
    board whose rank never moved would otherwise lose its only point.
    """

    current = parse_timestamp(now)
    kept: list[RankPoint] = []
    for index, point in enumerate(points):
        if index == len(points) - 1:
            kept.append(point)
            continue
        stamp = parse_timestamp(point.checked_at)
        if current is not None and stamp is not None:
            if (current - stamp).total_seconds() > max_age_seconds:
                continue
        kept.append(point)
    if len(kept) > max_points:
        kept = kept[-max_points:]
    return tuple(kept)


def append_point(
    points: tuple[RankPoint, ...],
    point: RankPoint,
    *,
    max_points: int = MAX_POINTS_PER_BOARD,
    max_age_seconds: float = MAX_POINT_AGE_SECONDS,
) -> tuple[RankPoint, ...]:
    """``points`` with ``point`` appended, pruned from the old end only.

    :func:`prune_points` parses every stamp; a busy board's read appends a
    point to a couple of hundred traces, so this one stops at the first
    point still inside the window — points are appended in time order.
    """

    kept = (*points, point)
    start = max(0, len(kept) - max_points)
    now = parse_timestamp(point.checked_at)
    if now is not None:
        while start < len(kept) - 1:
            stamp = parse_timestamp(kept[start].checked_at)
            if stamp is None or (now - stamp).total_seconds() <= max_age_seconds:
                break
            start += 1
    return kept[start:]


def trend_points(
    board: BoardHistory,
    *,
    start: datetime | None,
) -> tuple[RankPoint, ...]:
    """Points at or after ``start`` plus the last one before it, pinned to it.

    The pinned point is what makes a line start at the left edge with the rank
    the account actually held then, rather than at its first move inside the
    window. Points whose stamp cannot be parsed are ignored.
    """

    dated = [
        (stamp, point)
        for point in board.points
        if (stamp := parse_timestamp(point.checked_at)) is not None
    ]
    if start is None:
        return tuple(point for _, point in dated)
    before = [point for stamp, point in dated if stamp < start]
    within = tuple(point for stamp, point in dated if stamp >= start)
    if not before:
        return within
    carried = RankPoint(checked_at=start.isoformat(), rank=before[-1].rank)
    return (carried, *within)


def window_label(time_range: str) -> str:
    """The Chinese label of a ``7d`` / ``14d`` / ``30d`` window; empty for ``all``."""

    days = {"7d": 7, "14d": 14, "30d": 30}.get(time_range)
    return "" if days is None else f"近 {days} 天"


def window_start(time_range: str, *, now: datetime) -> datetime | None:
    """The left edge of a ``7d`` / ``14d`` / ``30d`` window; None for ``all``."""

    days = {"7d": 7, "14d": 14, "30d": 30}.get(time_range)
    return None if days is None else now - timedelta(days=days)


def trend_window_start(time_range: str, *, now: datetime) -> datetime:
    """The left edge of a trace window; ``all`` is the 90 days kept.

    Not :func:`window_start`'s ``None``: each board keeps its newest point
    however old it is, so an unbounded window could start a line before
    the 90 days the page says it shows.
    """

    start = window_start(time_range, now=now)
    return start if start is not None else now - timedelta(days=TREND_RETENTION_DAYS)


def parse_history_payload(payload: Any) -> dict[str, AccountHistory]:
    """Read the stored history, dropping anything malformed instead of raising."""

    if not isinstance(payload, dict):
        return {}
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    history: dict[str, AccountHistory] = {}
    # A read stamps every point it records with one string; sharing it again
    # after a load keeps 145,000 points from carrying 145,000 copies.
    stamps: dict[str, str] = {}
    for account_id, entry in accounts.items():
        if not isinstance(account_id, str) or not account_id:
            continue
        if not isinstance(entry, dict):
            continue
        raw_boards = entry.get("boards")
        if not isinstance(raw_boards, dict):
            continue
        boards: list[BoardHistory] = []
        for boss_slug, raw_board in raw_boards.items():
            if not isinstance(boss_slug, str) or not isinstance(raw_board, dict):
                continue
            raw_points = raw_board.get("points")
            if not isinstance(raw_points, list):
                continue
            points: list[RankPoint] = []
            for raw_point in raw_points:
                if not isinstance(raw_point, list) or len(raw_point) != 2:
                    continue
                stamp, rank = raw_point
                if not isinstance(stamp, str) or not stamp:
                    continue
                if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
                    continue
                stamp = stamps.setdefault(stamp, stamp)
                points.append(RankPoint(checked_at=stamp, rank=rank))
            if not points:
                continue
            boards.append(
                BoardHistory(
                    boss_slug=boss_slug,
                    boss_name=_text(raw_board.get("bossName")) or boss_slug,
                    dungeon_name=_text(raw_board.get("dungeonName")),
                    points=tuple(points),
                )
            )
        history[account_id] = AccountHistory(
            account_id=account_id,
            display_name=_text(entry.get("displayName")) or account_id,
            boards=tuple(boards),
        )
    return history


def history_payload(history: dict[str, AccountHistory]) -> dict[str, Any]:
    return {
        "version": HISTORY_VERSION,
        "accounts": {
            account_id: {
                "displayName": entry.display_name,
                "boards": {
                    board.boss_slug: {
                        "bossName": board.boss_name,
                        "dungeonName": board.dungeon_name,
                        "points": [
                            [point.checked_at, point.rank] for point in board.points
                        ],
                    }
                    for board in entry.boards
                },
            }
            for account_id, entry in history.items()
        },
    }


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
