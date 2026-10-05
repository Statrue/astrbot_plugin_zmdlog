"""Every account's rank trend on every board, kept: the raw material of 趋势.

Every DPS read the ranking index makes is compared, in
:mod:`core.board_changes`, against the last point each account's trend
holds for that board; a rank that moved becomes a point here. Nothing is
stored for an account whose rank stays put, so a point is a real
observation of a move, and "no change" is one too. Nothing here talks to
the network; the trace model and its pure helpers are :mod:`core.history`.

**What it costs.** A new record at rank *r* moves every account below it
down one place, so the points come from the busy boards' lower halves. The
server's record-event log (577 new DPS records in 23 days, 2026-09-08 to
10-01, 41 boards) replays to about 1,600 points a day, about 145,000 in 90
days: at most about 5 MB as compact JSON (written in 60 ms) and 12 MB in
memory, loaded in 0.2 s. The replay counts every row as its own account,
so the real figures are lower. Indented, the file would be 14 MB, so this
one is written compact.

**Kept 90 days, and each board's newest point always** — it is the current
rank, and a board whose rank never moved would otherwise lose its only
point. The 120-point cap per board that the watched-account trace had is
raised to ``history.MAX_POINTS_PER_BOARD``: replayed over 90 days, 120 would cut a
quarter of all traces short of the window — the lower halves of the busiest
towers, some to under a month — while 500 binds nowhere at that pace and
still bounds a launch-week burst.

**Written at most every ``SAVE_INTERVAL_SECONDS``.** A busy board's re-read
can add a couple of hundred points, and the index re-reads boards about once
a minute, so rewriting a 5 MB file per read would be wasteful. A change is
kept in memory and saved by the first read at least that long after the
last save, and at shutdown. A crash loses at most that window, and mostly
harmlessly: the next start compares against the last point on disk, so a
rank that moved meanwhile is recorded again, only a few minutes late; only
a move that was undone inside the window is gone.

**One file, the old one.** ``rank-history.json`` already held the watched
accounts' traces in this very shape, written by the rank watch's poll; the
trend now writes it from board reads instead, so those records are simply
read on, and the first board read after the upgrade joins them up without a
false point. A missing file and a corrupt one stay apart, as everywhere
(``core/persistence``): the corrupt one is set aside, not overwritten.

**Nicknames follow the newest upload.** A board read names every account
on it, moved or not, by the nickname of its newest record there. That is
an old name on a board the account has not played since its rename, so a
name only replaces one from a later upload. The upload times are held in
memory, not in the file: for a minute after a start the first boards read
may set an older name, and the fill corrects it.
"""

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType

from .board_changes import BoardChanges, Nickname
from .client import MIN_ACCOUNT_SEARCH_LENGTH
from .history import (
    AccountHistory,
    BoardHistory,
    RankPoint,
    append_point,
    history_payload,
    parse_history_payload,
    prune_points,
)
from .matcher import fold_text
from .persistence import JsonStore
from .timestamps import later_or_same, parse_timestamp

SAVE_INTERVAL_SECONDS = 300.0
# Points older than the window are dropped as a board's trace grows; a trace
# that stopped growing is pruned by this sweep over everything, at most daily.
PRUNE_INTERVAL_SECONDS = 86_400.0


class RankTrend:
    """Every account's rank trace, in memory and in one compact JSON file."""

    def __init__(
        self,
        store: JsonStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        save_interval_seconds: float = SAVE_INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._clock = clock
        self._now = now
        self._save_interval = save_interval_seconds
        self._dirty = False
        self._saved_at = -math.inf
        self._pruned_at = -math.inf
        self._history: dict[str, AccountHistory] = parse_history_payload(
            store.load()
        )
        self._prune_all()
        # Board slug → account → the rank of its last point there: what every
        # read is compared against, kept so a read never walks the whole map.
        self._last_rank_by_board: dict[str, dict[str, int]] = {}
        for account in self._history.values():
            for board in account.boards:
                self._last_rank_by_board.setdefault(board.boss_slug, {})[
                    account.account_id
                ] = board.points[-1].rank
        # Board slug → when it was last compared. In memory only: the index's
        # first reads refill it within a minute of a start.
        self._checked_at_by_board: dict[str, str] = {}
        # Account → the battle end of the upload its stored nickname came
        # from, as far as this process has seen.
        self._name_as_of: dict[str, str] = {}

    # --- reads -----------------------------------------------------------------

    def history_for(self, account_id: str) -> AccountHistory | None:
        return self._history.get(account_id)

    def by_name(self, query: str) -> tuple[AccountHistory, ...]:
        """Accounts whose recorded nickname is ``query``, else contains it.

        One pass over every account (a couple of thousand nicknames folded,
        under a millisecond), so a 趋势 by nickname needs no upstream search.
        """

        stripped = query.strip()
        if len(stripped) < MIN_ACCOUNT_SEARCH_LENGTH:
            return ()
        folded = fold_text(stripped)
        if not folded:
            return ()
        entries = tuple(self._history.values())
        exact = tuple(
            entry for entry in entries if fold_text(entry.display_name) == folded
        )
        if exact:
            return exact
        return tuple(
            entry for entry in entries if folded in fold_text(entry.display_name)
        )

    def last_checked(self, account_id: str) -> str | None:
        """The latest comparison of any board this account has a trace on."""

        account = self._history.get(account_id)
        if account is None:
            return None
        stamps = [
            stamp
            for board in account.boards
            if (stamp := self._checked_at_by_board.get(board.boss_slug)) is not None
        ]
        return max(stamps, key=_stamp_order) if stamps else None

    def last_ranks(self, boss_slug: str) -> Mapping[str, int]:
        """Each account's rank at the last point of its trace on a board."""

        return MappingProxyType(self._last_rank_by_board.get(boss_slug, {}))

    # --- writes ----------------------------------------------------------------

    def apply(self, changes: BoardChanges) -> None:
        """Record one board read's points and names; save if the interval allows."""

        self._checked_at_by_board[changes.boss_slug] = changes.seen_at
        for point in changes.trend_points:
            self._add(changes, point.account_id, point.rank)
        for account_id, nickname in changes.nicknames.items():
            self._rename(account_id, nickname)
        if self._clock() - self._saved_at >= self._save_interval:
            self.flush()

    def flush(self) -> None:
        """Write what is not on disk yet; a failed write is retried later."""

        if not self._dirty:
            return
        self._saved_at = self._clock()
        if self._saved_at - self._pruned_at >= PRUNE_INTERVAL_SECONDS:
            self._prune_all()
        if self._store.save(history_payload(self._history)):
            self._dirty = False

    def _add(self, changes: BoardChanges, account_id: str, rank: int) -> None:
        account = self._history.get(account_id)
        boards = (
            {board.boss_slug: board for board in account.boards}
            if account is not None
            else {}
        )
        point = RankPoint(checked_at=changes.seen_at, rank=rank)
        held = boards.get(changes.boss_slug)
        boards[changes.boss_slug] = BoardHistory(
            boss_slug=changes.boss_slug,
            boss_name=changes.boss_name,
            dungeon_name=changes.dungeon_name,
            points=(point,) if held is None else append_point(held.points, point),
        )
        self._history[account_id] = AccountHistory(
            account_id=account_id,
            # A new account is named by the rename pass that follows.
            display_name=account.display_name if account is not None else account_id,
            boards=tuple(boards.values()),
        )
        self._last_rank_by_board.setdefault(changes.boss_slug, {})[account_id] = rank
        self._dirty = True

    def _rename(self, account_id: str, nickname: Nickname) -> None:
        """Take ``nickname`` unless the stored one came from a later upload."""

        account = self._history.get(account_id)
        if account is None or not nickname.name:
            return
        seen = self._name_as_of.get(account_id)
        if seen is not None and not later_or_same(nickname.as_of, seen):
            return
        self._name_as_of[account_id] = nickname.as_of
        if account.display_name != nickname.name:
            self._history[account_id] = replace(account, display_name=nickname.name)
            self._dirty = True

    def _prune_all(self) -> None:
        """Drop every point past the window, each board's newest kept."""

        self._pruned_at = self._clock()
        now = self._now().isoformat()
        changed = False
        pruned: dict[str, AccountHistory] = {}
        for account_id, account in self._history.items():
            boards = []
            for board in account.boards:
                points = prune_points(board.points, now=now)
                if points != board.points:
                    changed = True
                    board = replace(board, points=points)
                boards.append(board)
            pruned[account_id] = replace(account, boards=tuple(boards))
        if changed:
            self._history = pruned
            self._dirty = True


def _stamp_order(stamp: str) -> float:
    parsed = parse_timestamp(stamp)
    return parsed.timestamp() if parsed is not None else -math.inf
