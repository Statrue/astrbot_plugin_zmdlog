"""What one read of a board changed, for everything that keeps a record of it.

The ranking index re-reads every board on its own schedule and hands each
read to the data source's refresh callback: the copy it held, if any, and
the copy it fetched. That callback is the one place a board change is
discovered. The event log reads new records off it (:mod:`core.events`);
this module turns the same read into what the other consumers record, so
the rules live in one pure function that touches neither AstrBot nor the
network and can be driven with two hand-built boards.

Today that is the rank trend of every account on the board: one point per
account whose best-row rank differs from the last point its trend holds
for this board, and each account's current nickname. Comparing against
the trend, not against the copy the index held, is the whole design:

* **A restart joins up.** The index lives in memory, so after a reload the
  first read of a board has no previous copy, but the trend file still
  holds every account's last rank there. An account whose rank did not
  move while the bot was down records nothing; one that did records its
  new rank once.
* **A board not read yet costs nothing.** No read, no comparison, no point:
  an index still filling cannot fake a change.
* **An account that vanished records nothing.** Upstream drops a record
  below 60% of the board's median, or its owner deletes it; the trace keeps
  its last point and resumes if the record comes back.
* **An account seen for the first time starts its trace** with the rank it
  holds — the left edge of its line.

Only DPS reads count: the rDPS boards rank a few percent of the records by
the same clear time, so their ranks are a second, thinner ranking that a
trend line must never mix in (``core/metrics``). An rDPS read gives
``None``; the event log still reads it, through its own path.

The input is the whole before/after pair even though the trend reads only
``current``: the rank-change notices (#53) are the next consumer, and they
need ``previous`` to say who pushed whom down. They extend
:class:`BoardChanges` with their entries and :func:`board_changes` with
keyword-only inputs; the positional pair and ``seen_at`` stay as they are.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from .metrics import METRIC_DPS
from .models import BossRanking, BossRankingRow
from .timestamps import later_or_same


@dataclass(frozen=True, slots=True)
class TrendPoint:
    """One account's rank on the board read, recorded because it changed."""

    account_id: str
    rank: int


@dataclass(frozen=True, slots=True)
class Nickname:
    """The nickname an account's newest record on the board carries."""

    name: str
    # That record's battle end: a nickname from a later upload wins.
    as_of: str


@dataclass(frozen=True, slots=True)
class BoardChanges:
    """What one DPS read of a board changed."""

    boss_slug: str
    boss_name: str
    dungeon_name: str
    # When the read was compared: the stamp of its points, and the trend's
    # 最近检查 for this board.
    seen_at: str
    trend_points: tuple[TrendPoint, ...] = ()
    # Every account on the board, moved or not: a rename shows up on a read
    # where the rank stays put.
    nicknames: Mapping[str, Nickname] = field(default_factory=dict)


def board_changes(
    previous: BossRanking | None,
    current: BossRanking,
    *,
    seen_at: str,
    last_ranks: Mapping[str, int],
) -> BoardChanges | None:
    """The changes one read of a board brings; ``None`` for an rDPS read.

    ``previous`` is the copy the index held (``None`` on its first read of
    the board since start-up), ``current`` the copy it fetched, and
    ``last_ranks`` each account's rank at the last point of its trend on
    this board. The trend points come out in rank order. ``previous`` is not
    read yet; the module docstring says who will.
    """

    if current.metric != METRIC_DPS:
        return None
    accounts = _accounts(current.rows)
    return BoardChanges(
        boss_slug=current.boss_slug,
        boss_name=current.boss_name,
        dungeon_name=current.dungeon_name,
        seen_at=seen_at,
        trend_points=tuple(
            TrendPoint(account_id=account_id, rank=best.rank)
            for account_id, best, _ in accounts
            if last_ranks.get(account_id) != best.rank
        ),
        nicknames={
            account_id: Nickname(newest.account_display_name, newest.battle_end_at)
            for account_id, _, newest in accounts
        },
    )


def _accounts(
    rows: tuple[BossRankingRow, ...],
) -> list[tuple[str, BossRankingRow, BossRankingRow]]:
    """Each account on the board: its best row and its newest, best first.

    An account's rank on a board is the rank of its best record, as
    ``users/{id}/rankings`` and the account page count it; its nickname is
    the one its most recent upload carries, since nicknames change.
    """

    best: dict[str, BossRankingRow] = {}
    newest: dict[str, BossRankingRow] = {}
    for row in rows:
        account_id = row.account_id
        if not account_id:
            continue
        held = best.get(account_id)
        if held is None or row.rank < held.rank:
            best[account_id] = row
        latest = newest.get(account_id)
        if latest is None or later_or_same(row.battle_end_at, latest.battle_end_at):
            newest[account_id] = row
    ordered = sorted(best.items(), key=lambda item: item[1].rank)
    return [(account_id, row, newest[account_id]) for account_id, row in ordered]
