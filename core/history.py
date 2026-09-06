"""Rank history of watched accounts: the raw material of the 趋势 page.

The rank watch already reads every watched account each cycle; this keeps a
compact trace of what it saw. A point is stored only when a board rank differs
from the last stored one, so an account that never moves costs one point per
board. Nothing here talks to the network.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from .models import PublicUserRankings
from .timestamps import parse_timestamp

HISTORY_VERSION = 1
MAX_POINTS_PER_BOARD = 120
MAX_POINT_AGE_SECONDS = 90 * 86_400.0


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


def record_rankings(
    history: dict[str, AccountHistory],
    account: PublicUserRankings,
    *,
    checked_at: str,
    max_points: int = MAX_POINTS_PER_BOARD,
    max_age_seconds: float = MAX_POINT_AGE_SECONDS,
) -> tuple[dict[str, AccountHistory], bool]:
    """Append the ranks that moved; return the new map and whether it changed.

    A board the account is seen on for the first time starts its trace with
    the current rank. A board that vanished from the response keeps its last
    points: the record may be back next cycle and a gap says more than a hole.
    """

    existing = history.get(account.account_id)
    boards = (
        {board.boss_slug: board for board in existing.boards}
        if existing is not None
        else {}
    )
    changed = existing is None or existing.display_name != account.account_display_name
    for entry in account.rankings:
        board = boards.get(entry.boss_slug)
        point = RankPoint(checked_at=checked_at, rank=entry.rank)
        if board is None:
            boards[entry.boss_slug] = BoardHistory(
                boss_slug=entry.boss_slug,
                boss_name=entry.boss_name,
                dungeon_name=entry.dungeon_name,
                points=(point,),
            )
            changed = True
            continue
        last = board.points[-1] if board.points else None
        renamed = (
            board.boss_name != entry.boss_name
            or board.dungeon_name != entry.dungeon_name
        )
        moved = last is None or last.rank != entry.rank
        if not moved and not renamed:
            continue
        points = board.points + ((point,) if moved else ())
        boards[entry.boss_slug] = replace(
            board,
            boss_name=entry.boss_name,
            dungeon_name=entry.dungeon_name,
            points=prune_points(
                points,
                now=checked_at,
                max_points=max_points,
                max_age_seconds=max_age_seconds,
            ),
        )
        changed = True
    if not changed:
        return history, False
    updated = dict(history)
    updated[account.account_id] = AccountHistory(
        account_id=account.account_id,
        display_name=account.account_display_name,
        boards=tuple(boards.values()),
    )
    return updated, True


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


def parse_history_payload(payload: Any) -> dict[str, AccountHistory]:
    """Read the stored history, dropping anything malformed instead of raising."""

    if not isinstance(payload, dict):
        return {}
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    history: dict[str, AccountHistory] = {}
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
