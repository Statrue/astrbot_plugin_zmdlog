"""Every board's ranking, resident in memory and kept fresh in the background.

Upstream has no way to ask "where does a team with X stand on each board":
the only source is each board's full ranking, 49 reads of 0.7 s each. Read
on demand that is a 13-second answer; read once and refreshed in a trickle
it is an instant one. So the index holds all of them (about 5 MB parsed) and
is the single place board rankings are read from — a caller says how old a
ranking it can accept, and the index fetches only when its copy is older.

Three things keep it fresh. A round-robin re-read of the oldest board every
``pace_seconds`` bounds the age of every board (49 boards at 30 s is a full
pass every 25 minutes). The hot-bosses response, one request for every
board's top three, is read every ``signal_seconds`` and a board whose top
three changed is re-read at once, so the changes people care about most are
never older than that. And a caller wanting fresher data than the index
has simply gets a fetch, which also updates the index.

The steady-state cost is therefore three requests a minute per running
bot, whether or not anyone asks anything; a fill that keeps failing backs
off instead of hammering an endpoint that just refused.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from .client import ZmdLogsClientError
from .logs import LogSink
from .models import (
    BossRanking,
    BossRankingRow,
    HotBossCard,
    PublicUserRanking,
    PublicUserRankings,
)
from .timestamps import later_or_same

DEFAULT_PACE_SECONDS = 30.0
# One hot-bosses read a minute: what a single keyword query a minute would
# cost anyway, and it keeps the query cache warm as a side effect.
DEFAULT_SIGNAL_SECONDS = 60.0
FILL_CONCURRENCY = 4
# A fill stops after this many board reads failed in a row: an upstream
# that refuses one board is refusing the next forty-eight too.
FILL_ABORT_AFTER_FAILURES = 4
# Between fills that left the index incomplete the loop waits pace × 2ⁿ,
# capped here, so an outage costs a handful of requests an hour, not 150.
MAX_FILL_BACKOFF_SECONDS = 600.0
# How long a command or tool waits for a fill in progress before answering
# that the index is still being built.
INDEX_WAIT_SECONDS = 30.0
# A board the index could not re-read keeps serving its last copy for this
# long; past it a reader asking for fresh data gets the error instead of a
# ranking that may be hours behind.
STALE_FALLBACK_MAX_AGE_SECONDS = 30 * 60.0
_TOP_SIGNAL_RUNS = 3

FetchRanking = Callable[[str], Awaitable[BossRanking]]
FetchBoards = Callable[[], Awaitable[tuple[HotBossCard, ...]]]


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One board's ranking and when it was read."""

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
        pace_seconds: float = DEFAULT_PACE_SECONDS,
        signal_seconds: float = DEFAULT_SIGNAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        on_refresh: Callable[[BossRanking, BossRanking], None] | None = None,
    ) -> None:
        self._fetch_ranking = fetch_ranking
        self._fetch_boards = fetch_boards
        self._logger = logger
        # Called with (copy held, copy fetched) after every re-read of a board
        # that was already held: the event log reads the difference.
        self._on_refresh = on_refresh
        self._pace = pace_seconds
        self._signal = signal_seconds
        # The loop checks the signal clock once per pace, so a read can land
        # this long after the previous one; a value put into a cache must
        # stay fresh at least that long or readers see a gap.
        self.signal_period_seconds = signal_seconds + pace_seconds
        self._clock = clock
        self._entries: dict[str, IndexEntry] = {}
        self._slugs: tuple[str, ...] = ()
        self._inflight: dict[str, asyncio.Task[BossRanking]] = {}
        self._failing: set[str] = set()
        # Fills in a row that left boards missing; drives the loop's backoff.
        self._fill_failures = 0
        self._signal_failing = False
        self._cycle_failing = False
        # Boards whose card and ranking disagreed even after a re-read, keyed
        # to the card's top ids: re-read again only when the card changes.
        self._disagreeing: dict[str, tuple[str, ...]] = {}
        self._fill_task: asyncio.Task[None] | None = None
        self._loop_task: asyncio.Task[None] | None = None

    # --- reading --------------------------------------------------------------

    async def get(self, boss_slug: str, *, max_age: float | None) -> BossRanking:
        """The board's ranking, fetched if the copy held is older than ``max_age``.

        ``None`` accepts any copy the index has. A fetch that fails falls back
        to the copy held when it is not too old (``STALE_FALLBACK_MAX_AGE``);
        with no usable copy the error propagates.
        """

        entry = self._entries.get(boss_slug)
        if entry is not None and (
            max_age is None or self._clock() - entry.loaded_at <= max_age
        ):
            return entry.ranking
        try:
            return await self._refresh(boss_slug)
        except ZmdLogsClientError:
            if (
                entry is not None
                and self._clock() - entry.loaded_at <= STALE_FALLBACK_MAX_AGE_SECONDS
            ):
                self._logger.warning(
                    "ZmdLogBot is serving a board ranking from the index after "
                    "a refresh failure."
                )
                return entry.ranking
            raise

    def entry(self, boss_slug: str) -> IndexEntry | None:
        return self._entries.get(boss_slug)

    def entries(self) -> tuple[IndexEntry, ...]:
        """Every ranking held, in board order."""

        return tuple(
            self._entries[slug] for slug in self._slugs if slug in self._entries
        )

    @property
    def slugs(self) -> tuple[str, ...]:
        return self._slugs

    @property
    def complete(self) -> bool:
        """True once every known board has a ranking in the index."""

        return bool(self._slugs) and all(slug in self._entries for slug in self._slugs)

    @property
    def missing_count(self) -> int:
        """Boards the board list names that the index has not read yet."""

        return sum(1 for slug in self._slugs if slug not in self._entries)

    def oldest_age_seconds(self) -> float | None:
        """How far behind the oldest ranking held is; None with nothing held."""

        held = self.entries()
        if not held:
            return None
        return self._clock() - min(entry.loaded_at for entry in held)

    # --- filling and refreshing ---------------------------------------------------

    async def ensure_filled(self) -> None:
        """Fill the index if it is not complete, sharing a fill in progress."""

        if self.complete:
            return
        if self._fill_task is None or self._fill_task.done():
            self._fill_task = asyncio.create_task(self._fill())
        await asyncio.shield(self._fill_task)

    async def wait_filled(self, timeout: float = INDEX_WAIT_SECONDS) -> bool:
        """``ensure_filled`` with a bound: False when the fill is still running.

        The fill itself carries on in the background (it is shielded), so a
        caller that gives up only stops waiting, and asks again later.
        """

        try:
            await asyncio.wait_for(self.ensure_filled(), timeout)
        except TimeoutError:
            return False
        return True

    async def _fill(self) -> None:
        cards = await self._fetch_boards()
        self._set_boards(cards)
        await self._refresh_many(
            [slug for slug in self._slugs if slug not in self._entries]
        )
        self._fill_failures = 0 if self.complete else self._fill_failures + 1

    async def _refresh_many(self, slugs: list[str]) -> None:
        semaphore = asyncio.Semaphore(FILL_CONCURRENCY)
        failures = 0

        async def one(slug: str) -> None:
            nonlocal failures
            async with semaphore:
                if failures >= FILL_ABORT_AFTER_FAILURES:
                    return
                if await self._refresh_quietly(slug):
                    failures = 0
                else:
                    failures += 1

        await asyncio.gather(*(one(slug) for slug in slugs))

    async def _refresh(self, boss_slug: str) -> BossRanking:
        """Read one board from upstream, merging concurrent requests for it."""

        task = self._inflight.get(boss_slug)
        if task is None:
            task = asyncio.create_task(self._fetch_ranking(boss_slug))
            self._inflight[boss_slug] = task
            task.add_done_callback(
                lambda done, slug=boss_slug: self._inflight.pop(slug, None)
            )
        ranking = await asyncio.shield(task)
        previous = self._entries.get(boss_slug)
        self._entries[boss_slug] = IndexEntry(ranking, self._clock())
        if previous is not None and self._on_refresh is not None:
            try:
                self._on_refresh(previous.ranking, ranking)
            except Exception as exc:
                self._logger.warning(
                    "ZmdLogBot could not record board changes: %s",
                    type(exc).__name__,
                )
        if boss_slug in self._failing:
            self._failing.discard(boss_slug)
            self._logger.info("ZmdLogBot ranking index reads %s again.", boss_slug)
        return ranking

    async def _refresh_quietly(self, boss_slug: str) -> bool:
        """A background refresh: log the first failure of a streak, never raise."""

        try:
            await self._refresh(boss_slug)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if boss_slug not in self._failing:
                self._failing.add(boss_slug)
                self._logger.warning(
                    "ZmdLogBot ranking index could not read %s: %s",
                    boss_slug,
                    type(exc).__name__,
                )
            return False
        return True

    async def apply_signal(self, cards: tuple[HotBossCard, ...]) -> None:
        """Re-read every board whose top runs differ from the copy held.

        A new record entering the top three is what people ask about first,
        and this catches it at the cost of the one hot-bosses request the
        caller already made; a record landing lower waits for the round-robin.
        """

        self._set_boards(cards)
        tops = {
            card.boss_slug: tuple(
                run.battle_id for run in card.top_speed_runs[:_TOP_SIGNAL_RUNS]
            )
            for card in cards
        }
        changed = []
        for card in cards:
            if not self._top_changed(card):
                self._disagreeing.pop(card.boss_slug, None)
            elif self._disagreeing.get(card.boss_slug) != tops[card.boss_slug]:
                changed.append(card.boss_slug)
        if not changed:
            return
        await self._refresh_many(changed)
        by_slug = {card.boss_slug: card for card in cards}
        for slug in changed:
            if slug in self._entries and self._top_changed(by_slug[slug]):
                # Card and ranking still disagree after a fresh read (a
                # server-side cache lag): wait for the card to change rather
                # than re-reading this board every signal, unlogged, forever.
                if slug not in self._disagreeing:
                    self._logger.warning(
                        "ZmdLogBot ranking index: the board list and the "
                        "ranking of %s disagree; waiting for the list to change.",
                        slug,
                    )
                self._disagreeing[slug] = tops[slug]

    async def refresh_oldest(self) -> None:
        """Re-read the board whose copy is oldest, or one not held at all."""

        if not self._slugs:
            return
        missing = [slug for slug in self._slugs if slug not in self._entries]
        if missing:
            await self._refresh_quietly(missing[0])
            return
        oldest = min(self._slugs, key=lambda slug: self._entries[slug].loaded_at)
        await self._refresh_quietly(oldest)

    def _set_boards(self, cards: tuple[HotBossCard, ...]) -> None:
        self._slugs = tuple(card.boss_slug for card in cards)
        for slug in list(self._entries):
            if slug not in self._slugs:
                # A board that left the index is not queryable any more; its
                # ranking would only ever be memory.
                del self._entries[slug]

    def _top_changed(self, card: HotBossCard) -> bool:
        """Does the card's top three disagree with the ranking held?

        The card lists min(3, records) runs and the ranking every record, so
        the first ``len(top)`` held ids must match, and a card shorter than
        three with more rows held means records were deleted.
        """

        entry = self._entries.get(card.boss_slug)
        if entry is None:
            return True
        top = tuple(run.battle_id for run in card.top_speed_runs[:_TOP_SIGNAL_RUNS])
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
        """Cancel the loop, a fill and every fetch in flight, and wait for them.

        The fetches are shielded from a cancelled reader, so they are only
        ever ended here; a client closed under a still-running fetch would
        fail it after the fact, with nobody left to read the result.
        """

        tasks = [
            task
            for task in (self._loop_task, self._fill_task, *self._inflight.values())
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = None
        self._fill_task = None
        self._inflight.clear()

    def _delay(self) -> float:
        """Seconds to the next iteration: the pace, backed off while fills fail."""

        if self.complete or self._fill_failures == 0:
            return self._pace
        return min(self._pace * 2**self._fill_failures, MAX_FILL_BACKOFF_SECONDS)

    async def _loop(self) -> None:
        try:
            await self.ensure_filled()
        except Exception as exc:
            self._logger.warning(
                "ZmdLogBot ranking index could not fill: %s", type(exc).__name__
            )
        next_signal = self._clock() + self._signal
        while True:
            await asyncio.sleep(self._delay())
            try:
                # The signal and the round-robin are independent budgets:
                # one hot-bosses read a minute, and one board read per pace.
                if self._clock() >= next_signal:
                    next_signal = self._clock() + self._signal
                    await self._read_signal()
                if not self.complete:
                    await self.ensure_filled()
                else:
                    await self.refresh_oldest()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._cycle_failing:
                    self._cycle_failing = True
                    self._logger.warning(
                        "ZmdLogBot ranking index cycle failed: %s",
                        type(exc).__name__,
                    )
            else:
                if self._cycle_failing:
                    self._cycle_failing = False
                    self._logger.info("ZmdLogBot ranking index cycles again.")

    async def _read_signal(self) -> None:
        """One hot-bosses read; a failing streak is logged once, not per minute."""

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
        await self.apply_signal(cards)


def account_rankings(
    entries: tuple[IndexEntry, ...],
    account_id: str,
) -> PublicUserRankings | None:
    """One account's best record on every board, read off the index.

    The same answer as ``users/{id}/rankings`` — a board ranking lists every
    record, so the account's rank there is the rank of its best row — but
    for a whole watch list it costs no request at all. ``None`` when the
    account has no row anywhere, which the caller answers with the endpoint.
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
    so the roster avatars, professions and main C that the user endpoint
    leaves out can be read off the index without a request. Ids the index
    does not hold — a retired board, or a fill still in progress — are
    simply absent from the result and the caller falls back.
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
