"""What one read of a board changed, for everything that keeps a record of it.

The ranking index re-reads every board on its own schedule and hands each
read to the data source's refresh callback: the copy it held, if any, and
the copy it fetched. That callback is the one place a board change is
discovered. The event log reads new records off it (:mod:`core.events`);
this module turns the same read into what the other consumers record, so
the rules live in one pure function that touches neither AstrBot nor the
network and can be driven with two hand-built boards.

**The rank trend** of every account on the board: one point per account
whose best-row rank differs from the last point its trend holds for this
board, and each account's current nickname. Comparing against the trend,
not against the copy the index held, is the whole design:

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

**The 顶屁股通告 entries** (:class:`NoticeEntry`) of the chats that watch
the board: one per new record that entered its top N. Unlike the trend,
they compare the read with the board as it stood before it — the copy the
index held, or, on the first read after a start, the top N the board watch
kept on disk (:class:`BoardSnapshot`). Having both states is what lets a
notice say who pushed whom down.

* **A new record is a battle id the baseline did not have**, and that the
  event log never announced as new: upstream drops a record below 60% of
  the median and lists it again when the median moves, and a record that
  comes back is not an upload (:meth:`core.events.EventLog.announced`).
  A deletion adds no battle id, so the rows it moves up are no news.
* **A snapshot holds the top N only**, so a record not in it may have risen
  from below N when one above was deleted. Such a record ranks below every
  snapshot record still on the board, so one ranked above any of them must
  be new, and only that one counts: a new record that lands below all of
  them is missed rather than an old one announced. A snapshot of a board
  that had N records or fewer holds all of them and knows every record
  (``whole``). This only matters on the first read after a start; the
  index's copy is the whole board.
* **No baseline, no news.** A first read with no snapshot, or one older
  than the snapshot age (max(3 × interval, 1 h), ``core/settings``), is a
  silent new start: replaying hours of a board that moved while the bot
  was down is old news.
* **Several new records in one read are one entry each, best first.** The
  board just before the first is the read with every new record taken out
  (so a deletion in the same read is already applied); each record is put
  back in rank order, and what it pushed is counted on the board as it
  stood just before and just after it. Taken best first, each enters at
  the rank it holds in the read.
* **Pushed** are the accounts whose best record was within the top N just
  before the new one and one place lower after it — every account at or
  below the new record's place, the uploader's own excepted (their best
  improved or did not move). Past N is 跌出前 N.
* **新冠军** is a new record at #1 whose account is not the one that held
  #1 just before it: first place changed hands. A holder beating their own
  #1 is a plain new record; a board's very first record is a new champion,
  since the place had no holder.

Only DPS reads count: the rDPS boards rank a few percent of the records by
the same clear time, so their ranks are a second, thinner ranking that a
trend line or a notice must never mix in (``core/metrics``). An rDPS read
gives ``None``; the event log still reads it, through its own path.

What one entry holds is fixed by the page that draws a chat's batch of them
(``presentation.build_notice_page``); when and to whom they are sent is
``core/rank_watch``.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from .metrics import METRIC_DPS
from .models import BossRanking, BossRankingRow
from .timestamps import later_or_same, parse_timestamp
from .watchlist import WatchList


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
class PushedAccount:
    """An account a new record moved down from inside the top N.

    ``record`` is its best record on the board (the row the face and name
    are read from); ``before`` and ``after`` its rank either side of the new
    record. ``after`` past N is the 跌出前 N mark.
    """

    record: BossRankingRow
    before: int
    after: int


@dataclass(frozen=True, slots=True)
class NoticeEntry:
    """One new record that entered a board's top N: a 顶屁股通告 entry.

    ``rank`` is the place it entered at, which is the place to print even
    when a later record sits above it by now; ``record`` supplies who, the
    main C's face, 用时, DPS and, on a contract board, the score.
    ``champion`` is 新冠军: first place changed hands with it. ``pushed`` is
    every account it moved down within the top N, best first. ``seen_at``
    is the read that found it, an ISO stamp.
    """

    boss_slug: str
    boss_name: str
    dungeon_name: str
    seen_at: str
    rank: int
    record: BossRankingRow
    pushed: tuple[PushedAccount, ...] = ()
    champion: bool = False


@dataclass(frozen=True, slots=True)
class BoardSnapshot:
    """A board's top N as the board watch last saw it: a restart's baseline.

    ``battle_ids`` are the top records in rank order; ``whole`` says they
    are every record the board had. ``checked_at`` is when the watch last
    vouched for them, an ISO stamp.
    """

    battle_ids: tuple[str, ...]
    checked_at: str
    whole: bool = False


@dataclass(frozen=True, slots=True)
class NoticeWatch:
    """What the board watch brings to a read: who watches, N, its snapshot.

    ``snapshot`` is the one kept for this board, compared only when the
    index held no copy; one older than ``snapshot_max_age_seconds`` is a
    silent new start.
    """

    watchlist: WatchList
    top_n: int
    snapshot: BoardSnapshot | None = None
    snapshot_max_age_seconds: float = 3600.0


@dataclass(frozen=True, slots=True)
class BoardChanges:
    """What one DPS read of a board changed."""

    boss_slug: str
    boss_name: str
    dungeon_name: str
    # When the read was compared: the stamp of its points and entries, and
    # the trend's 最近检查 for this board.
    seen_at: str
    trend_points: tuple[TrendPoint, ...] = ()
    # Every account on the board, moved or not: a rename shows up on a read
    # where the rank stays put.
    nicknames: Mapping[str, Nickname] = field(default_factory=dict)
    # Chat origin → its entries, for every chat watching the board; empty
    # when nothing entered the top N or nobody watches it.
    notices: Mapping[str, tuple[NoticeEntry, ...]] = field(default_factory=dict)
    # The read's top N, for the board watch to keep; None without a watch.
    snapshot: BoardSnapshot | None = None


def board_changes(
    previous: BossRanking | None,
    current: BossRanking,
    *,
    seen_at: str,
    last_ranks: Mapping[str, int],
    watch: NoticeWatch | None = None,
    announced: Collection[str] = frozenset(),
) -> BoardChanges | None:
    """The changes one read of a board brings; ``None`` for an rDPS read.

    ``previous`` is the copy the index held (``None`` on its first read of
    the board since start-up), ``current`` the copy it fetched, and
    ``last_ranks`` each account's rank at the last point of its trend on
    this board. ``watch`` is the board watch's side of the read, without
    which there are no notices; ``announced`` the battle ids the event log
    has already announced as new DPS records. The trend points come out in
    rank order, the entries best first.
    """

    if current.metric != METRIC_DPS:
        return None
    accounts = _accounts(current.rows)
    notices: dict[str, tuple[NoticeEntry, ...]] = {}
    snapshot = None
    if watch is not None:
        snapshot = snapshot_of(current, top_n=watch.top_n, checked_at=seen_at)
        notices = _notices(
            previous, current, watch=watch, seen_at=seen_at, announced=announced
        )
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
        notices=notices,
        snapshot=snapshot,
    )


def snapshot_of(
    ranking: BossRanking, *, top_n: int, checked_at: str
) -> BoardSnapshot:
    """``ranking``'s top ``top_n`` as a snapshot stamped ``checked_at``."""

    return BoardSnapshot(
        battle_ids=tuple(row.battle_id for row in ranking.rows[:top_n]),
        checked_at=checked_at,
        whole=len(ranking.rows) <= top_n,
    )


def snapshot_is_fresh(
    snapshot: BoardSnapshot, *, now: str, max_age_seconds: float
) -> bool:
    """Whether ``snapshot`` is recent enough to compare a read against.

    A stamp that does not read, or one in the future, is not: either way
    nothing says how long the bot was away.
    """

    baseline = parse_timestamp(snapshot.checked_at)
    current = parse_timestamp(now)
    if baseline is None or current is None:
        return False
    return 0 <= (current - baseline).total_seconds() <= max_age_seconds


def _notices(
    previous: BossRanking | None,
    current: BossRanking,
    *,
    watch: NoticeWatch,
    seen_at: str,
    announced: Collection[str],
) -> dict[str, tuple[NoticeEntry, ...]]:
    origins = tuple(
        origin
        for origin, chat in watch.watchlist.chats
        if chat.covers(current.boss_slug)
    )
    if not origins:
        return {}
    if previous is not None:
        known = tuple(row.battle_id for row in previous.rows)
        whole = True
    else:
        held = watch.snapshot
        if held is None or not snapshot_is_fresh(
            held, now=seen_at, max_age_seconds=watch.snapshot_max_age_seconds
        ):
            return {}
        known, whole = held.battle_ids, held.whole
    new = _new_records(current.rows, known, whole=whole, announced=announced)
    entries = _entries(current, new, top_n=watch.top_n, seen_at=seen_at)
    if not entries:
        return {}
    return {origin: entries for origin in origins}


def _new_records(
    rows: tuple[BossRankingRow, ...],
    known: tuple[str, ...],
    *,
    whole: bool,
    announced: Collection[str],
) -> frozenset[str]:
    """The battle ids on ``rows`` that are uploads the baseline lacked."""

    known_ids = frozenset(known)
    # Off a partial baseline, the lowest place a known record holds now: a
    # record above it cannot have risen from below the baseline's top N.
    lowest_known = max(
        (place for place, row in enumerate(rows) if row.battle_id in known_ids),
        default=-1,
    )
    return frozenset(
        row.battle_id
        for place, row in enumerate(rows)
        if row.battle_id not in known_ids
        and row.battle_id not in announced
        and (whole or place < lowest_known)
    )


def _entries(
    current: BossRanking,
    new: frozenset[str],
    *,
    top_n: int,
    seen_at: str,
) -> tuple[NoticeEntry, ...]:
    """Put the new records back best first; one entry each inside the top N."""

    if not new:
        return ()
    board = [row for row in current.rows if row.battle_id not in new]
    entries: list[NoticeEntry] = []
    for place, row in enumerate(current.rows):
        if row.battle_id not in new:
            continue
        # Every record above it is back already, so it enters at its rank.
        before = board
        board = [*board[:place], row, *board[place:]]
        rank = place + 1
        if rank > top_n:
            continue
        entries.append(
            NoticeEntry(
                boss_slug=current.boss_slug,
                boss_name=current.boss_name,
                dungeon_name=current.dungeon_name,
                seen_at=seen_at,
                rank=rank,
                record=row,
                pushed=_pushed(before, board, row, top_n=top_n),
                champion=rank == 1
                and (not before or before[0].account_id != row.account_id),
            )
        )
    return tuple(entries)


def _pushed(
    before: list[BossRankingRow],
    after: list[BossRankingRow],
    record: BossRankingRow,
    *,
    top_n: int,
) -> tuple[PushedAccount, ...]:
    """The accounts ``record`` moved down from inside the top N, best first."""

    now = _best_places(after)
    pushed = []
    for account_id, (place, _) in _best_places(before).items():
        if place >= top_n or account_id == record.account_id:
            continue
        later, row = now[account_id]
        if later > place:
            pushed.append(PushedAccount(record=row, before=place + 1, after=later + 1))
    return tuple(pushed)


def _best_places(
    rows: list[BossRankingRow],
) -> dict[str, tuple[int, BossRankingRow]]:
    """Each account's best place on ``rows`` (from 0) and its record there,
    best first."""

    best: dict[str, tuple[int, BossRankingRow]] = {}
    for place, row in enumerate(rows):
        if row.account_id and row.account_id not in best:
            best[row.account_id] = (place, row)
    return best


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
