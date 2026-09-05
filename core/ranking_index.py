"""Every board's ranking, resident in memory and kept fresh in the background.

Upstream has no way to ask "where does a team with X stand on each board":
the only source is each board's full ranking, 49 reads of 0.7 s each. Read
on demand that is a 13-second answer; read once and refreshed in a trickle
it is an instant one. So the index holds all of them (about 5 MB parsed) and
is the single place board rankings are read from — a caller says how old a
ranking it can accept, and the index fetches only when its copy is older.

Three things keep it fresh. A round-robin re-read of the oldest board every
``pace_seconds`` bounds the age of every board. The hot-bosses response, one
request for every board's top three, is read every ``signal_seconds`` and a
board whose top three changed is re-read at once, so the changes people
care about most are never older than that. And a caller wanting fresher
data than the index has simply gets a fetch, which also updates the index.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .client import ZmdLogsClientError
from .logs import LogSink
from .models import (
    BossRanking,
    HotBossCard,
    PublicUserRanking,
    PublicUserRankings,
)

DEFAULT_PACE_SECONDS = 20.0
DEFAULT_SIGNAL_SECONDS = 120.0
FILL_CONCURRENCY = 4
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
    ) -> None:
        self._fetch_ranking = fetch_ranking
        self._fetch_boards = fetch_boards
        self._logger = logger
        self._pace = pace_seconds
        self._signal = signal_seconds
        self._clock = clock
        self._entries: dict[str, IndexEntry] = {}
        self._slugs: tuple[str, ...] = ()
        self._inflight: dict[str, asyncio.Task[BossRanking]] = {}
        self._failing: set[str] = set()
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

    async def _fill(self) -> None:
        cards = await self._fetch_boards()
        self._set_boards(cards)
        await self._refresh_many(
            [slug for slug in self._slugs if slug not in self._entries]
        )

    async def _refresh_many(self, slugs: list[str]) -> None:
        semaphore = asyncio.Semaphore(FILL_CONCURRENCY)

        async def one(slug: str) -> None:
            async with semaphore:
                await self._refresh_quietly(slug)

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
        self._entries[boss_slug] = IndexEntry(ranking, self._clock())
        if boss_slug in self._failing:
            self._failing.discard(boss_slug)
            self._logger.info("ZmdLogBot ranking index reads %s again.", boss_slug)
        return ranking

    async def _refresh_quietly(self, boss_slug: str) -> None:
        """A background refresh: log the first failure of a streak, never raise."""

        try:
            await self._refresh(boss_slug)
        except Exception as exc:
            if boss_slug not in self._failing:
                self._failing.add(boss_slug)
                self._logger.warning(
                    "ZmdLogBot ranking index could not read %s: %s",
                    boss_slug,
                    type(exc).__name__,
                )

    async def apply_signal(self, cards: tuple[HotBossCard, ...]) -> None:
        """Re-read every board whose top runs differ from the copy held.

        A new record entering the top three is what people ask about first,
        and this catches it at the cost of the one hot-bosses request the
        caller already made; a record landing lower waits for the round-robin.
        """

        self._set_boards(cards)
        changed = [card.boss_slug for card in cards if self._top_changed(card)]
        if changed:
            await self._refresh_many(changed)

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
        for task in (self._loop_task, self._fill_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._loop_task = None
        self._fill_task = None

    async def _loop(self) -> None:
        try:
            await self.ensure_filled()
        except Exception as exc:
            self._logger.warning(
                "ZmdLogBot ranking index could not fill: %s", type(exc).__name__
            )
        next_signal = self._clock() + self._signal
        while True:
            await asyncio.sleep(self._pace)
            try:
                if self._clock() >= next_signal:
                    next_signal = self._clock() + self._signal
                    await self.apply_signal(await self._fetch_boards())
                elif not self.complete:
                    await self.ensure_filled()
                else:
                    await self.refresh_oldest()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning(
                    "ZmdLogBot ranking index cycle failed: %s", type(exc).__name__
                )


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
            if row.battle_end_at >= newest:
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

