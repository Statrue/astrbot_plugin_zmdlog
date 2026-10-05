"""Every board's ranking, resident in memory and kept fresh in the background.

Upstream has no way to ask "where does a team with X stand on each board":
the only source is each board's full ranking, 49 reads of 0.7 s each. Read
on demand that is a 13-second answer; read once and refreshed in a trickle
it is an instant one. So the index holds all of them (about 5 MB parsed) and
is the single place board rankings are read from.

Every board has two rankings, DPS and rDPS, and the index holds both: the
DPS one is what every page reads unless asked otherwise and what the rank
trend and the account page read only; the rDPS one lists the records whose
upload could compute team contribution (a few percent, as of 2026-09-16),
in the same clear-time order. The DPS boards are filled first, so a DPS
page waits for 49 reads, not 98.

**Readers never wait for upstream once a ranking is held.** The copy is
served however long it has been held, because whether it is current depends
on upstream, not on its age. Only a ranking not held yet — the plugin just
loaded, or the site just listed the board — is read while the reader waits.
A reader that says someone is waiting on that one board (a board page, the
board tool) also has a copy last checked over two minutes ago re-read in the
background: a player who just uploaded asks for that board, and asking
again shows the new rank.

**A ranking is re-read about as often as it changes.** Its next re-read comes
``CHANGE_FACTOR`` × (time since it last changed) after its last check,
between five minutes and six hours. A change is a re-read whose battle ids
differ from the copy's, in content or order: a new record, a record moving,
a record deleted. A busy tower is read every few minutes on a launch day,
and every board drifts to one read in six hours at the end of a version,
under one rule. A ranking read for the first time — at load, or on a newly
listed board — last changed when its newest record was fought (a date in the
future, from a device clock running fast, counts as now), so a reload starts
every board at its real pace instead of taking all of them for boards that
just changed. An empty ranking counts as unchanged for the longest interval:
most rDPS rankings hold no record, and their first one, if it reaches the
DPS top three, is caught by the signal anyway.

**The top-three signal.** One hot-bosses request lists every board's top
three; a board whose top three differ from the copy is re-read at once, its
rDPS ranking with it. It is read every minute, and every five once the top
threes have not changed for two hours; a change brings it straight back to
every minute. A card that still disagrees after the re-read (a server-side
cache lag) is waited out until it changes again.

**The budget.** Every background re-read — due, asked for or signalled —
takes a token from a bucket of four that gains one every twenty seconds:
three a minute on average, whatever people ask about. Asking only changes
which board goes first: rankings asked for or signalled go first, in the
order they came, then the ones due, most overdue first. Neither a first
read, which a reader waits for anyway, nor the signal's own request takes a
token. A read that fails keeps the copy and is due again after the shortest
interval; four failures in a row are an outage, not a bad board, and the
whole schedule then backs off, doubling up to ten minutes.

**What it costs**, simulated on the server's record-event log (its last
1000 new records, 2026-09-08 to 10-01; 13 full days, all at the end of a
version): about 1268 re-reads a day, 34% fewer than the server's 1931 at
its configured pace of 90 seconds and 78% fewer than the 5760 at the old
default of 30; the quietest day costs about 392. A new record below the
top three is found in about 6 minutes at the median, against 34 before
(p90 2.6 hours, never more than six); one in the top three, within five
minutes, by the signal.

**What was rejected, and why.**

- Fixed polling: 98% of its re-reads found nothing. Upstream offers no
  conditional request and no paging (UPSTREAM.md), so each of them was the
  whole ranking, 215 KB for the busiest board.
- AIMD (halve the interval on a change, grow it otherwise): records arrive
  in bursts, and halving step by step reached a burst late — a median of
  63 minutes to find a record, against 6 for this rule.
- Waiting a bounded time for a fresh read: every query still downloads the
  whole ranking, and waits up to a second more.
- A 60-second expiry: it measures how long a copy was held, not whether
  upstream changed. It is what this replaced.
- Keeping the index on disk: a reload only happens at a deploy, and the
  first-read rule above already restarts every board at its pace.

``CHANGE_FACTOR`` is provisional. The data held no launch day, when uploads
peak. Once the next version has been live through its first busy days,
replay that period's ``record-events.json`` (in the plugin data directory)
through the refresh simulation, compare reads a day and the time to find a
record for factors around 0.1, and only then change it. The simulation is
a local script, ``docs/debug/simulate_refresh.py``, kept out of the
repository.
"""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from .logs import LogSink
from .metrics import METRIC_DPS, METRICS
from .models import (
    BossRanking,
    BossRankingRow,
    HotBossCard,
    PublicUserRanking,
    PublicUserRankings,
)
from .timestamps import later_or_same, parse_timestamp

# A ranking's next re-read comes this share of the time since it last
# changed after its last check, within the two bounds. Provisional: the
# module docstring says how to check it.
CHANGE_FACTOR = 0.1
MIN_INTERVAL_SECONDS = 5 * 60.0
MAX_INTERVAL_SECONDS = 6 * 3600.0
# The top-three signal: every minute, and every five once no board's top
# three has changed for two hours.
SIGNAL_SECONDS = 60.0
SLOW_SIGNAL_SECONDS = 5 * 60.0
SIGNAL_QUIET_SECONDS = 2 * 3600.0
FILL_CONCURRENCY = 4
# A board asked for is re-read only when its copy was checked longer ago
# than this; a re-read sooner could only return the copy just served.
ON_DEMAND_AFTER_SECONDS = 120.0
# Every background re-read takes a token: four at once, one more every
# twenty seconds, three a minute on average.
TOKEN_CAPACITY = 4
TOKEN_SECONDS = 20.0
# A ranking whose read failed keeps its copy and is tried again this late.
RETRY_SECONDS = MIN_INTERVAL_SECONDS
# This many failed reads in a row is an outage: the schedule pauses for
# base × 2ⁿ, capped, so an outage costs a handful of requests an hour.
OUTAGE_AFTER_FAILURES = 4
OUTAGE_BACKOFF_BASE_SECONDS = 30.0
MAX_OUTAGE_BACKOFF_SECONDS = 600.0
# How long a command or tool waits for a fill in progress before answering
# that the index is still being built.
INDEX_WAIT_SECONDS = 30.0
_TOP_SIGNAL_RUNS = 3
# A run of the loop that raised is not retried sooner than this.
_CYCLE_RETRY_SECONDS = 30.0
_EPSILON = 1e-9

FetchRanking = Callable[[str, str], Awaitable[BossRanking]]
FetchBoards = Callable[[], Awaitable[tuple[HotBossCard, ...]]]
# A ranking: (slug, metric).
Key = tuple[str, str]


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One board's ranking and when it was last read."""

    ranking: BossRanking
    loaded_at: float


class RankingIndex:
    """All board rankings, read through one place."""

    def __init__(
        self,
        *,
        fetch_ranking: FetchRanking,
        fetch_boards: FetchBoards,
        logger: LogSink,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        on_refresh: Callable[[BossRanking | None, BossRanking], None] | None = None,
    ) -> None:
        # Called with (slug, metric); the ranking it returns carries the metric.
        self._fetch_ranking = fetch_ranking
        self._fetch_boards = fetch_boards
        self._logger = logger
        # Called with (copy held, copy fetched) after every read of a board,
        # the held copy ``None`` on its first read since start-up: the event
        # log reads the difference, the rank trend the copy fetched.
        self._on_refresh = on_refresh
        self._clock = clock
        # Epoch seconds: what a record's battle end is compared with.
        self._wall_clock = wall_clock
        # One map of slug → entry per metric; the DPS one is ``_entries``.
        self._held: dict[str, dict[str, IndexEntry]] = {
            metric: {} for metric in METRICS
        }
        self._slugs: tuple[str, ...] = ()
        self._inflight: dict[Key, asyncio.Task[BossRanking]] = {}
        self._failing: set[Key] = set()
        # Rankings someone asked for or the signal saw change, in the order
        # they came; each is re-read ahead of the ones merely due.
        self._urgent: dict[Key, None] = {}
        # A ranking whose last read failed is not read again before this.
        self._retry_at: dict[Key, float] = {}
        # When each ranking last changed, on ``clock``.
        self._changed_at: dict[Key, float] = {}
        # The card's top runs that sent a board to be re-read, checked
        # against the ranking that read brings back.
        self._signal_tops: dict[str, tuple[str, ...]] = {}
        # Boards whose card and ranking disagreed even after a re-read, keyed
        # to the card's top ids: re-read again only when the card changes.
        self._disagreeing: dict[str, tuple[str, ...]] = {}
        # Each board's top runs as the last signal listed them; the signal
        # slows down once none has changed for SIGNAL_QUIET_SECONDS.
        self._card_tops: dict[str, tuple[str, ...]] = {}
        now = clock()
        self._tokens = float(TOKEN_CAPACITY)
        self._tokens_at = now
        self._failures_in_row = 0
        self._outage_rounds = 0
        self._resume_at = -math.inf
        # The loop begins with a fill, which reads the board list itself.
        self._next_signal = now + SIGNAL_SECONDS
        self._signal_changed_at = now
        self._signal_failing = False
        self._cycle_failing = False
        self._fill_tasks: dict[str, asyncio.Task[None]] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    @property
    def _entries(self) -> dict[str, IndexEntry]:
        """The DPS rankings held, by slug."""

        return self._held[METRIC_DPS]

    # --- reading --------------------------------------------------------------

    async def get(
        self,
        boss_slug: str,
        *,
        metric: str = METRIC_DPS,
        on_demand: bool = False,
    ) -> BossRanking:
        """The board's ranking: the copy held, or a read waited for if none is.

        ``on_demand`` says someone is waiting on this one board: a copy last
        checked over ``ON_DEMAND_AFTER_SECONDS`` ago is then re-read in the
        background, so the next reader sees what changed. With no copy the
        read's error propagates.
        """

        entry = self._held[metric].get(boss_slug)
        if entry is None:
            return await self._refresh(boss_slug, metric)
        if on_demand:
            self._ask((boss_slug, metric), entry)
        return entry.ranking

    def _ask(self, key: Key, entry: IndexEntry) -> None:
        """Queue a re-read for a reader, unless one is pending or pointless."""

        now = self._clock()
        if (
            now - entry.loaded_at < ON_DEMAND_AFTER_SECONDS
            or now < self._retry_at.get(key, -math.inf)
            or key in self._urgent
            or key in self._inflight
        ):
            return
        self._urgent[key] = None
        self._wake.set()

    def entry(self, boss_slug: str, metric: str = METRIC_DPS) -> IndexEntry | None:
        return self._held[metric].get(boss_slug)

    def entries(self, metric: str = METRIC_DPS) -> tuple[IndexEntry, ...]:
        """Every ranking of ``metric`` held, in board order."""

        held = self._held[metric]
        return tuple(held[slug] for slug in self._slugs if slug in held)

    @property
    def slugs(self) -> tuple[str, ...]:
        return self._slugs

    def is_complete(self, metric: str = METRIC_DPS) -> bool:
        """True once every known board has a ranking of ``metric`` in the index."""

        held = self._held[metric]
        return bool(self._slugs) and all(slug in held for slug in self._slugs)

    @property
    def complete(self) -> bool:
        """True once every known board has its DPS ranking in the index."""

        return self.is_complete(METRIC_DPS)

    def missing(self, metric: str = METRIC_DPS) -> int:
        """Boards the board list names whose ``metric`` ranking is not held yet."""

        held = self._held[metric]
        return sum(1 for slug in self._slugs if slug not in held)

    @property
    def missing_count(self) -> int:
        """Boards the board list names that the index has not read yet (DPS)."""

        return self.missing(METRIC_DPS)

    # --- filling ----------------------------------------------------------------

    async def ensure_filled(self, metric: str = METRIC_DPS) -> None:
        """Fill the index for ``metric`` if incomplete, sharing a fill in progress."""

        if self.is_complete(metric):
            return
        task = self._fill_tasks.get(metric)
        if task is None or task.done():
            task = asyncio.create_task(self._fill(metric))
            self._fill_tasks[metric] = task
        await asyncio.shield(task)

    async def wait_filled(
        self, timeout: float = INDEX_WAIT_SECONDS, metric: str = METRIC_DPS
    ) -> bool:
        """``ensure_filled`` with a bound: False when the fill is still running.

        The fill itself carries on in the background (it is shielded), so a
        caller that gives up only stops waiting, and asks again later.
        """

        try:
            await asyncio.wait_for(self.ensure_filled(metric), timeout)
        except TimeoutError:
            return False
        return True

    async def _fill(self, metric: str) -> None:
        # The board list is read only when nothing has read it yet: the
        # signal keeps it current, and a page waiting on an index with one
        # board missing would otherwise cost a hot-bosses read every time.
        if not self._slugs:
            cards = await self._fetch_boards()
            self._set_boards(cards)
        # A board whose read just failed is left to its retry: a page waiting
        # on the whole index would otherwise read it again every time.
        await self._read_many(self._first_reads(metric))

    def _first_reads(self, metric: str) -> list[Key]:
        """Listed rankings of ``metric`` not held yet, once any retry has come."""

        now = self._clock()
        held = self._held[metric]
        return [
            (slug, metric)
            for slug in self._slugs
            if slug not in held
            and self._retry_at.get((slug, metric), -math.inf) <= now
        ]

    # --- reading upstream ---------------------------------------------------------

    async def _read_many(self, keys: list[Key]) -> None:
        """Read in the background, a few at a time, until an outage pauses it."""

        semaphore = asyncio.Semaphore(FILL_CONCURRENCY)

        async def one(key: Key) -> None:
            async with semaphore:
                if not self._paused():
                    await self._read_quietly(key)

        await asyncio.gather(*(one(key) for key in keys))

    async def _read_quietly(self, key: Key) -> None:
        """A background read: log the first failure of a streak, never raise."""

        try:
            await self._refresh(*key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if key not in self._failing:
                self._failing.add(key)
                self._logger.warning(
                    "ZmdLogBot ranking index could not read %s: %s",
                    _board_label(key),
                    type(exc).__name__,
                )

    async def _refresh(self, boss_slug: str, metric: str) -> BossRanking:
        """Read one ranking from upstream, merging concurrent requests for it."""

        key = (boss_slug, metric)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_and_hold(key))
            self._inflight[key] = task
            task.add_done_callback(lambda done, key=key: self._read_done(key, done))
        return await asyncio.shield(task)

    def _read_done(self, key: Key, task: asyncio.Task[BossRanking]) -> None:
        self._inflight.pop(key, None)
        if not task.cancelled():
            # Every reader may have stopped waiting; the failure was already
            # accounted for, so it is not left for asyncio to report.
            task.exception()

    async def _fetch_and_hold(self, key: Key) -> BossRanking:
        """One upstream read, and what it means for the schedule, settled once."""

        slug, metric = key
        try:
            ranking = await self._fetch_ranking(slug, metric)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._urgent.pop(key, None)
            self._retry_at[key] = self._clock() + RETRY_SECONDS
            self._note_failure()
            raise
        self._hold(key, ranking)
        return ranking

    def _hold(self, key: Key, ranking: BossRanking) -> None:
        slug, metric = key
        held = self._held[metric]
        previous = held.get(slug)
        now = self._clock()
        held[slug] = IndexEntry(ranking, now)
        if previous is None:
            self._changed_at[key] = self._first_change(ranking, now)
        elif _battle_ids(previous.ranking) != _battle_ids(ranking):
            self._changed_at[key] = now
        self._urgent.pop(key, None)
        self._retry_at.pop(key, None)
        self._note_success()
        if self._on_refresh is not None:
            try:
                self._on_refresh(
                    None if previous is None else previous.ranking, ranking
                )
            except Exception as exc:
                self._logger.warning(
                    "ZmdLogBot could not record board changes: %s",
                    type(exc).__name__,
                )
        if metric == METRIC_DPS:
            self._check_signal(slug)
        if key in self._failing:
            self._failing.discard(key)
            self._logger.info(
                "ZmdLogBot ranking index reads %s again.", _board_label(key)
            )

    def _first_change(self, ranking: BossRanking, now: float) -> float:
        """When a ranking read for the first time last changed, on ``clock``.

        When its newest record was fought, never later than now; an empty
        ranking has not changed for as long as the schedule cares about.
        """

        if not ranking.rows:
            return -math.inf
        fought = [
            stamp.timestamp()
            for row in ranking.rows
            if (stamp := parse_timestamp(row.battle_end_at)) is not None
        ]
        if not fought:
            return now
        return now - max(self._wall_clock() - max(fought), 0.0)

    def _note_failure(self) -> None:
        self._failures_in_row += 1
        now = self._clock()
        if self._failures_in_row >= OUTAGE_AFTER_FAILURES and now >= self._resume_at:
            self._outage_rounds += 1
            self._resume_at = now + min(
                OUTAGE_BACKOFF_BASE_SECONDS * 2**self._outage_rounds,
                MAX_OUTAGE_BACKOFF_SECONDS,
            )

    def _note_success(self) -> None:
        self._failures_in_row = 0
        self._outage_rounds = 0
        self._resume_at = -math.inf

    def _paused(self) -> bool:
        return self._clock() < self._resume_at

    # --- the schedule -------------------------------------------------------------

    async def run_due(self) -> None:
        """Do everything due now: the signal, first reads, and paid re-reads.

        This is the step the background loop repeats whenever ``next_wake``
        comes or a reader asks for a board.
        """

        if self._clock() >= self._next_signal:
            await self._read_signal()
        for metric in METRICS:
            if self._paused():
                return
            # First reads take no token: a reader would wait for them anyway.
            await self._read_many(self._first_reads(metric))
        if self._paused():
            return
        now = self._clock()
        paid = []
        for key in self._wanted(now):
            if not self._take_token(now):
                break
            paid.append(key)
        await self._read_many(paid)

    def next_wake(self) -> float:
        """When ``run_due`` next has something to do, on the index's clock.

        The earliest of the next signal, the next first read, and the next
        re-read due with a token there to pay for it — the last two never
        before an outage's pause ends. A board asked for wakes the loop
        sooner.
        """

        now = self._clock()
        work = [
            self._retry_at.get((slug, metric), now)
            for metric in METRICS
            for slug in self._slugs
            if slug not in self._held[metric] and (slug, metric) not in self._inflight
        ]
        due = min((self._due_at(key) for key in self._held_keys()), default=None)
        if due is not None:
            work.append(max(due, self._token_ready_at(now)))
        wake = self._next_signal
        if work:
            wake = min(wake, max(min(work), self._resume_at))
        return wake

    def _held_keys(self) -> list[Key]:
        """Every listed ranking held and not being read, DPS boards first."""

        return [
            (slug, metric)
            for metric in METRICS
            for slug in self._slugs
            if slug in self._held[metric] and (slug, metric) not in self._inflight
        ]

    def _due_at(self, key: Key) -> float:
        """When a held ranking is next to be re-read.

        After a failure, at its retry; asked for or signalled, at once;
        otherwise ``CHANGE_FACTOR`` of the time it had gone unchanged when
        last checked, after that check, within the bounds.
        """

        retry = self._retry_at.get(key)
        if retry is not None:
            return retry
        if key in self._urgent:
            return -math.inf
        slug, metric = key
        checked = self._held[metric][slug].loaded_at
        unchanged = checked - self._changed_at.get(key, checked)
        return checked + min(
            max(CHANGE_FACTOR * unchanged, MIN_INTERVAL_SECONDS),
            MAX_INTERVAL_SECONDS,
        )

    def _wanted(self, now: float) -> list[Key]:
        """Held rankings due now: asked-for ones as they came, then oldest due."""

        urgent = {key: order for order, key in enumerate(self._urgent)}
        ready = [
            (key not in urgent, urgent.get(key, 0), due, position, key)
            for position, key in enumerate(self._held_keys())
            if (due := self._due_at(key)) <= now
        ]
        return [key for *_, key in sorted(ready)]

    def _tokens_now(self, now: float) -> float:
        return min(
            TOKEN_CAPACITY, self._tokens + (now - self._tokens_at) / TOKEN_SECONDS
        )

    def _take_token(self, now: float) -> bool:
        tokens = self._tokens_now(now)
        if tokens < 1 - _EPSILON:
            return False
        self._tokens, self._tokens_at = tokens - 1, now
        return True

    def _token_ready_at(self, now: float) -> float:
        tokens = self._tokens_now(now)
        if tokens >= 1 - _EPSILON:
            return now
        return now + (1 - tokens) * TOKEN_SECONDS

    # --- the top-three signal -----------------------------------------------------

    async def _read_signal(self) -> None:
        """One hot-bosses read; a failing streak is logged once, not per read."""

        started = self._clock()
        self._next_signal = started + self._signal_period(started)
        try:
            cards = await self._fetch_boards()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._signal_failing:
                self._signal_failing = True
                self._logger.warning(
                    "ZmdLogBot ranking index could not read the board list: %s",
                    type(exc).__name__,
                )
            return
        if self._signal_failing:
            self._signal_failing = False
            self._logger.info("ZmdLogBot ranking index reads the board list again.")
        self._apply_signal(cards)
        self._next_signal = started + self._signal_period(started)

    def _signal_period(self, at: float) -> float:
        if at - self._signal_changed_at >= SIGNAL_QUIET_SECONDS:
            return SLOW_SIGNAL_SECONDS
        return SIGNAL_SECONDS

    def _apply_signal(self, cards: tuple[HotBossCard, ...]) -> None:
        """Queue every held board whose top runs differ from its card.

        A new record entering the top three is what people ask about first,
        and this catches it at the cost of the one hot-bosses request just
        made; a record landing lower waits for the schedule. The card reflects
        the DPS board; a changed board's rDPS ranking is re-read along with
        it, since a new record may have entered both. A board not held yet is
        left to the first reads.
        """

        self._set_boards(cards)
        for card in cards:
            slug = card.boss_slug
            top = _top_ids(card)
            listed = self._card_tops.get(slug)
            self._card_tops[slug] = top
            if listed is not None and listed != top:
                self._signal_changed_at = self._clock()
            if slug not in self._entries:
                continue
            if not self._top_changed(slug, top):
                self._disagreeing.pop(slug, None)
                self._signal_tops.pop(slug, None)
            elif self._disagreeing.get(slug) != top:
                self._signal_tops[slug] = top
                for metric in METRICS:
                    if slug in self._held[metric]:
                        self._urgent.setdefault((slug, metric), None)

    def _check_signal(self, slug: str) -> None:
        """After a signalled re-read, note a card the ranking still disagrees with."""

        top = self._signal_tops.pop(slug, None)
        if top is None or not self._top_changed(slug, top):
            return
        # Card and ranking still disagree after a fresh read (a server-side
        # cache lag): wait for the card to change rather than re-reading this
        # board every signal, unlogged, forever.
        if slug not in self._disagreeing:
            self._logger.warning(
                "ZmdLogBot ranking index: the board list and the ranking of %s "
                "disagree; waiting for the list to change.",
                slug,
            )
        self._disagreeing[slug] = top

    def _set_boards(self, cards: tuple[HotBossCard, ...]) -> None:
        self._slugs = tuple(card.boss_slug for card in cards)
        listed = set(self._slugs)
        for held in self._held.values():
            for slug in list(held):
                if slug not in listed:
                    # A board that left the index is not queryable any more;
                    # its ranking would only ever be memory.
                    del held[slug]
        for by_key in (self._urgent, self._retry_at, self._changed_at):
            for key in [key for key in by_key if key[0] not in listed]:
                del by_key[key]
        self._failing = {key for key in self._failing if key[0] in listed}
        for by_slug in (self._card_tops, self._signal_tops, self._disagreeing):
            for slug in [slug for slug in by_slug if slug not in listed]:
                del by_slug[slug]

    def _top_changed(self, slug: str, top: tuple[str, ...]) -> bool:
        """Do a card's top runs disagree with the DPS ranking held?

        The card lists min(3, records) runs and the ranking every record, so
        the first ``len(top)`` held ids must match, and a card shorter than
        three with more rows held means records were deleted.
        """

        entry = self._entries.get(slug)
        if entry is None:
            return True
        held = tuple(row.battle_id for row in entry.ranking.rows[:_TOP_SIGNAL_RUNS])
        if len(top) < _TOP_SIGNAL_RUNS and len(held) > len(top):
            return True
        return held[: len(top)] != top

    # --- the background loop --------------------------------------------------------

    def start(self) -> None:
        """Begin filling and refreshing in the background; safe to call twice."""

        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the loop, the fills and every fetch in flight, and wait for them.

        The fetches are shielded from a cancelled reader, so they are only
        ever ended here; a client closed under a still-running fetch would
        fail it after the fact, with nobody left to read the result.
        """

        tasks = [
            task
            for task in (
                self._loop_task,
                *self._fill_tasks.values(),
                *self._inflight.values(),
            )
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = None
        self._fill_tasks.clear()
        self._inflight.clear()

    async def _loop(self) -> None:
        for metric in METRICS:
            try:
                await self.ensure_filled(metric)
            except Exception as exc:
                self._logger.warning(
                    "ZmdLogBot ranking index could not fill: %s", type(exc).__name__
                )
        while True:
            delay = self.next_wake() - self._clock()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except TimeoutError:
                    pass
            self._wake.clear()
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._cycle_failing:
                    self._cycle_failing = True
                    self._logger.warning(
                        "ZmdLogBot ranking index cycle failed: %s",
                        type(exc).__name__,
                    )
                await asyncio.sleep(_CYCLE_RETRY_SECONDS)
            else:
                if self._cycle_failing:
                    self._cycle_failing = False
                    self._logger.info("ZmdLogBot ranking index cycles again.")


def _top_ids(card: HotBossCard) -> tuple[str, ...]:
    return tuple(run.battle_id for run in card.top_speed_runs[:_TOP_SIGNAL_RUNS])


def _battle_ids(ranking: BossRanking) -> tuple[str, ...]:
    """A ranking's records in order: what a change is measured on."""

    return tuple(row.battle_id for row in ranking.rows)


def _board_label(key: tuple[str, str]) -> str:
    """``slug`` for the DPS board, ``slug (rdps)`` for the other, in log lines."""

    slug, metric = key
    return slug if metric == METRIC_DPS else f"{slug} ({metric})"


def account_rankings(
    entries: tuple[IndexEntry, ...],
    account_id: str,
) -> PublicUserRankings | None:
    """One account's best record on every board, read off the index.

    The same answer as ``users/{id}/rankings`` on 全部榜单 — a board ranking
    lists every record, so the account's rank there is the rank of its best
    row — but it costs no request: an account page is read off boards
    already held. ``None`` when the account has no row
    anywhere, which the caller answers with the endpoint.
    """

    rankings: list[PublicUserRanking] = []
    display_name = ""
    newest = ""
    for entry in entries:
        ranking = entry.ranking
        mine = [row for row in ranking.rows if row.account_id == account_id]
        if not mine:
            continue
        best = min(mine, key=lambda row: row.rank)
        for row in mine:
            # Nicknames change; the most recent upload carries the current one.
            if not newest or later_or_same(row.battle_end_at, newest):
                newest = row.battle_end_at
                display_name = row.account_display_name
        rankings.append(
            PublicUserRanking(
                boss_slug=ranking.boss_slug,
                boss_name=ranking.boss_name,
                dungeon_name=ranking.dungeon_name,
                battle_id=best.battle_id,
                rank=best.rank,
                score_percent=best.score_percent,
                duration_ms=best.duration_ms,
                total_dps=best.dps,
                battle_end_at=best.battle_end_at,
                roster_summary=best.roster_summary,
                contract_tag_score=best.contract_tag_score,
                contract_tags=best.contract_tags,
                roster_entries=best.roster_entries,
            )
        )
    if not rankings:
        return None
    return PublicUserRankings(
        account_id=account_id,
        account_display_name=display_name,
        rankings=tuple(rankings),
    )


def rows_by_battle(
    entries: tuple[IndexEntry, ...],
    battle_ids: Iterable[str],
) -> dict[str, BossRankingRow]:
    """The held ranking row of every battle id the index knows.

    An account's best record on a board is a row of that board's ranking,
    so the main C that the user endpoint leaves out, and the roster an
    older response lacks, can be read off the index without a request. Ids
    the index does not hold — a board outside 全部榜单, or a fill still in
    progress — are simply absent from the result and the caller falls back.
    """

    wanted = set(battle_ids)
    found: dict[str, BossRankingRow] = {}
    for entry in entries:
        if not wanted:
            break
        for row in entry.ranking.rows:
            if row.battle_id in wanted:
                found[row.battle_id] = row
                wanted.discard(row.battle_id)
    return found
