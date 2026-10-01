"""The ranking index: one place every board ranking is read from.

Time is a mutable list the index reads as its clock; a test moves it and
calls ``run_due``, the step the background loop repeats, instead of running
the loop. What is asserted is what upstream was asked for and when, and
what a reader got back.
"""

import asyncio
import copy
import unittest

from core.client import ZmdLogsClientError
from core.models import parse_boss_ranking, parse_hot_bosses
from core.ranking_index import IndexEntry, RankingIndex, account_rankings
from tests.helpers import CapturingLogger, hot_bosses_payload, ranking_payload_with_rows

SLUGS = ("dung01_group_bossrush02", "dung01_group_bossrush03")
FIVE = tuple(f"dung01_group_bossrush{n:02d}" for n in range(10, 15))
# The fixture ranking's first three battle ids, as hot-bosses would list them.
TOP3 = [f"btl_upload_{rank:012d}" for rank in (1, 2, 3)]
# Long enough that no ranking falls due by schedule while a test asks for one.
PACE = 600.0


def run(coro):
    return asyncio.run(coro)


def cards_payload(
    top_ids: dict[str, list[str]] | None = None, slugs: tuple[str, ...] = SLUGS
) -> list[dict]:
    """One card per slug; each lists the fixture's top three unless told not to.

    A card agreeing with the ranking held is what a quiet board looks like
    to the signal.
    """

    base = hot_bosses_payload()[0]
    cards = []
    for slug in slugs:
        card = copy.deepcopy(base)
        card["bossSlug"] = slug
        card["bossName"] = f"首领 {slug[-2:]}"
        card["topSpeedRuns"] = [
            {
                "battleId": battle_id,
                "durationMs": 20_000,
                "uploaderNickname": "someone",
                "characterName": "洛茜",
            }
            for battle_id in (top_ids or {}).get(slug, TOP3)
        ]
        cards.append(card)
    return cards


def ranking_for(slug: str, *, first_id: str | None = None, metric: str = "dps"):
    payload = ranking_payload_with_rows()
    payload["bossSlug"] = slug
    payload["metric"] = metric
    if first_id is not None:
        payload["rows"][0]["battleId"] = first_id
    return parse_boss_ranking(payload, metric=metric)


class FakeUpstream:
    def __init__(self, clock, slugs: tuple[str, ...] = SLUGS) -> None:
        self._clock = clock
        self.cards = parse_hot_bosses(cards_payload(slugs=slugs))
        # DPS board reads by slug; the rDPS board's reads are kept apart,
        # because most of what is asserted here is about the DPS index.
        self.ranking_calls: list[str] = []
        self.rdps_calls: list[str] = []
        # When each board read was asked for, either metric.
        self.read_times: list[float] = []
        self.board_calls = 0
        self.fail = False
        self.failing: set[str] = set()
        self.first_ids: dict[str, str] = {}

    async def fetch_ranking(self, slug: str, metric: str):
        (self.ranking_calls if metric == "dps" else self.rdps_calls).append(slug)
        self.read_times.append(self._clock())
        if self.fail or slug in self.failing:
            raise ZmdLogsClientError("offline")
        return ranking_for(slug, first_id=self.first_ids.get(slug), metric=metric)

    async def fetch_boards(self):
        self.board_calls += 1
        if self.fail:
            raise ZmdLogsClientError("offline")
        return self.cards

    @property
    def reads(self) -> int:
        return len(self.ranking_calls) + len(self.rdps_calls)


class RankingIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [1_000.0]
        self.logger = CapturingLogger()
        self.upstream = FakeUpstream(lambda: self.now[0])
        self.index = self._index(self.upstream)

    def _index(self, upstream: FakeUpstream, **kwargs) -> RankingIndex:
        return RankingIndex(
            fetch_ranking=upstream.fetch_ranking,
            fetch_boards=upstream.fetch_boards,
            logger=self.logger,
            clock=lambda: self.now[0],
            pace_seconds=PACE,
            **kwargs,
        )

    def _five_boards(self) -> tuple[FakeUpstream, RankingIndex]:
        upstream = FakeUpstream(lambda: self.now[0], slugs=FIVE)
        index = self._index(upstream)
        self._fill(index)
        return upstream, index

    def _fill(self, index: RankingIndex | None = None) -> None:
        index = index or self.index
        run(index.ensure_filled())
        run(index.ensure_filled("rdps"))

    def _run_due(self, index: RankingIndex | None = None) -> None:
        run((index or self.index).run_due())

    # --- reading ----------------------------------------------------------------

    def test_a_fill_reads_every_board_once(self) -> None:
        run(self.index.ensure_filled())

        self.assertTrue(self.index.complete)
        self.assertEqual(sorted(self.upstream.ranking_calls), sorted(SLUGS))
        self.assertEqual(len(self.index.entries()), 2)

    def test_a_held_copy_is_served_however_old_it_is(self) -> None:
        self._fill()
        self.now[0] += 10 * 3_600
        reads = self.upstream.reads

        plain = run(self.index.get(SLUGS[0]))
        asked = run(self.index.get(SLUGS[0], on_demand=True))
        rdps = run(self.index.get(SLUGS[1], metric="rdps", on_demand=True))

        self.assertEqual(plain.boss_slug, SLUGS[0])
        self.assertIs(asked, plain)
        self.assertEqual(rdps.metric, "rdps")
        self.assertEqual(self.upstream.reads, reads)

    def test_a_board_not_held_is_read_and_waited_for(self) -> None:
        ranking = run(self.index.get(SLUGS[1]))

        self.assertEqual(ranking.boss_slug, SLUGS[1])
        self.assertIs(self.index.entry(SLUGS[1]).ranking, ranking)

    def test_a_board_not_held_that_cannot_be_read_raises(self) -> None:
        self.upstream.fail = True

        with self.assertRaises(ZmdLogsClientError):
            run(self.index.get(SLUGS[0], on_demand=True))

    def test_upstream_failing_never_fails_a_board_held(self) -> None:
        self._fill()
        held = self.index.entry(SLUGS[0]).ranking
        self.upstream.fail = True
        self.now[0] += 3_600

        self.assertIs(run(self.index.get(SLUGS[0], on_demand=True)), held)

    def test_concurrent_reads_of_one_board_share_a_fetch(self) -> None:
        async def scenario():
            await asyncio.gather(
                self.index.get(SLUGS[0]),
                self.index.get(SLUGS[0]),
                self.index.get(SLUGS[0]),
            )

        run(scenario())

        self.assertEqual(self.upstream.ranking_calls.count(SLUGS[0]), 1)

    # --- asking for a board -------------------------------------------------------

    def test_a_board_asked_for_is_re_read_by_the_next_run(self) -> None:
        self._fill()
        self.now[0] += 180
        before = len(self.upstream.ranking_calls)

        run(self.index.get(SLUGS[0], on_demand=True))
        self.assertEqual(len(self.upstream.ranking_calls), before)
        self._run_due()

        self.assertEqual(self.upstream.ranking_calls[before:], [SLUGS[0]])
        self.assertEqual(self.upstream.rdps_calls, list(SLUGS))
        self.assertEqual(self.index.entry(SLUGS[0]).loaded_at, self.now[0])

    def test_a_board_checked_within_two_minutes_is_not_re_read(self) -> None:
        self._fill()
        self.now[0] += 119
        reads = self.upstream.reads

        run(self.index.get(SLUGS[0], on_demand=True))
        self._run_due()

        self.assertEqual(self.upstream.reads, reads)

    def test_asking_for_one_board_again_and_again_re_reads_it_once(self) -> None:
        self._fill()
        self.now[0] += 180
        before = len(self.upstream.ranking_calls)

        for _ in range(4):
            run(self.index.get(SLUGS[0], on_demand=True))
        self._run_due()
        # Just checked: asking again right after costs nothing either.
        run(self.index.get(SLUGS[0], on_demand=True))
        self._run_due()

        self.assertEqual(self.upstream.ranking_calls[before:], [SLUGS[0]])

    def test_four_boards_asked_for_are_re_read_at_once_and_a_fifth_waits(
        self,
    ) -> None:
        # The fill read all five boards and took no token for it.
        upstream, index = self._five_boards()
        self.now[0] += 180
        before = len(upstream.ranking_calls)

        for slug in FIVE:
            run(index.get(slug, on_demand=True))
        self._run_due(index)

        self.assertEqual(upstream.ranking_calls[before:], list(FIVE[:4]))
        self.assertEqual(index.next_wake(), self.now[0] + 20)
        self.now[0] += 19
        self._run_due(index)
        self.assertEqual(len(upstream.ranking_calls), before + 4)
        self.now[0] += 1
        self._run_due(index)
        self.assertEqual(upstream.ranking_calls[before:], list(FIVE))

    def test_a_board_asked_for_goes_before_the_ones_due(self) -> None:
        upstream, index = self._five_boards()
        # Every ranking is due: one pass is five boards at the pace.
        self.now[0] += 5 * PACE
        before = len(upstream.ranking_calls)

        run(index.get(FIVE[-1], on_demand=True))
        self._run_due(index)

        self.assertEqual(upstream.ranking_calls[before:], [FIVE[-1], *FIVE[:3]])

    # --- the schedule -------------------------------------------------------------

    def test_each_ranking_is_re_read_once_per_pass(self) -> None:
        self._fill()
        reads = self.upstream.reads

        self.now[0] += len(SLUGS) * PACE - 1
        self._run_due()
        self.assertEqual(self.upstream.reads, reads)
        self.now[0] += 1
        self._run_due()

        self.assertEqual(self.upstream.reads, reads + 2 * len(SLUGS))

    def test_a_newly_listed_board_is_read_without_a_token(self) -> None:
        self._fill()
        self.now[0] += 180
        for slug in SLUGS:
            for metric in ("dps", "rdps"):
                run(self.index.get(slug, metric=metric, on_demand=True))
        new = "dung01_group_bossrush09"
        self.upstream.cards = parse_hot_bosses(cards_payload(slugs=(*SLUGS, new)))
        reads = self.upstream.reads

        # The signal lists the new board; it is read on top of the four
        # re-reads the four tokens pay for.
        self._run_due()

        self.assertEqual(self.upstream.reads, reads + 6)
        self.assertIsNotNone(self.index.entry(new))
        self.assertIsNotNone(self.index.entry(new, "rdps"))

    # --- failures -----------------------------------------------------------------

    def test_a_failed_re_read_keeps_the_copy_and_is_tried_again_in_five_minutes(
        self,
    ) -> None:
        self._fill()
        held = self.index.entry(SLUGS[0]).ranking
        self.upstream.failing = {SLUGS[0]}
        self.now[0] += 180
        before = len(self.upstream.ranking_calls)

        run(self.index.get(SLUGS[0], on_demand=True))
        self._run_due()
        self.assertEqual(len(self.upstream.ranking_calls), before + 1)
        self.assertIs(run(self.index.get(SLUGS[0])), held)

        # Asking again does not bring the retry forward.
        self.now[0] += 299
        run(self.index.get(SLUGS[0], on_demand=True))
        self._run_due()
        self.assertEqual(len(self.upstream.ranking_calls), before + 1)
        self.now[0] += 1
        self._run_due()
        self.assertEqual(len(self.upstream.ranking_calls), before + 2)

    def test_reads_failing_in_a_row_back_the_schedule_off_to_ten_minutes(
        self,
    ) -> None:
        slugs = tuple(f"dung01_group_bossrush{n:02d}" for n in range(20, 32))
        upstream = FakeUpstream(lambda: self.now[0], slugs=slugs)
        index = self._index(upstream)
        self._fill(index)
        self.now[0] += len(slugs) * PACE
        upstream.fail = True
        upstream.read_times.clear()

        bursts: list[float] = []
        for _ in range(100):
            self.now[0] = max(self.now[0], index.next_wake())
            self._run_due(index)
            if upstream.read_times and upstream.read_times[-1] not in bursts:
                bursts.append(upstream.read_times[-1])
            if len(bursts) == 7:
                break

        gaps = [later - earlier for earlier, later in zip(bursts, bursts[1:])]
        self.assertEqual(gaps, [60, 120, 240, 480, 600, 600])

    def test_a_failing_board_logs_once_per_streak(self) -> None:
        self._fill()
        self.upstream.failing = {SLUGS[0]}

        self.now[0] += len(SLUGS) * PACE
        self._run_due()
        self.now[0] += 300
        self._run_due()

        # The board's DPS and rDPS rankings: two streaks, each logged once,
        # the rDPS one named as such.
        failures = [m for m in self.logger.messages if "could not read" in m]
        self.assertEqual(len(failures), 2)
        self.assertEqual(sum("(rdps)" in m for m in failures), 1)
        self.upstream.failing = set()
        self.now[0] += 300
        self._run_due()
        self.assertEqual(sum("again" in m for m in self.logger.messages), 2)

    # --- the top-three signal -----------------------------------------------------

    def _new_first_place(self, slug: str) -> None:
        """Upstream's top run on ``slug`` is now a battle the index has not seen."""

        self.upstream.first_ids[slug] = "btl_upload_new000000001"
        self.upstream.cards = parse_hot_bosses(
            cards_payload({slug: ["btl_upload_new000000001", *TOP3[1:]]})
        )

    def test_a_changed_top_three_is_re_read_with_its_rdps_board(self) -> None:
        self._fill()
        before = (len(self.upstream.ranking_calls), len(self.upstream.rdps_calls))
        self._new_first_place(SLUGS[1])

        self.now[0] += 60
        self._run_due()

        self.assertEqual(self.upstream.ranking_calls[before[0] :], [SLUGS[1]])
        self.assertEqual(self.upstream.rdps_calls[before[1] :], [SLUGS[1]])
        rows = self.index.entry(SLUGS[1]).ranking.rows
        self.assertEqual(rows[0].battle_id, "btl_upload_new000000001")

    def test_an_unchanged_top_three_costs_nothing_but_the_signal(self) -> None:
        self._fill()
        reads, boards = self.upstream.reads, self.upstream.board_calls

        self.now[0] += 60
        self._run_due()

        self.assertEqual(self.upstream.reads, reads)
        self.assertEqual(self.upstream.board_calls, boards + 1)

    def test_the_signal_is_read_once_a_minute(self) -> None:
        self._fill()
        boards = self.upstream.board_calls

        self.now[0] += 59
        self._run_due()
        self.assertEqual(self.upstream.board_calls, boards)
        self.now[0] += 1
        self._run_due()
        self.assertEqual(self.upstream.board_calls, boards + 1)
        self.assertEqual(self.index.next_wake(), self.now[0] + 60)

    def test_a_shorter_top_list_means_a_record_was_deleted(self) -> None:
        self._fill()
        before = len(self.upstream.ranking_calls)
        # The board now has fewer than three records; the index holds five.
        self.upstream.cards = parse_hot_bosses(
            cards_payload({slug: TOP3[:2] for slug in SLUGS})
        )

        self.now[0] += 60
        self._run_due()

        self.assertEqual(len(self.upstream.ranking_calls), before + 2)

    def test_a_persistent_disagreement_is_read_once_then_waited_out(self) -> None:
        self._fill()
        before = len(self.upstream.ranking_calls)
        # The card says two records, the ranking keeps saying five: a
        # server-side cache lag that would otherwise cost a re-read a minute.
        self.upstream.cards = parse_hot_bosses(
            cards_payload({slug: TOP3[:2] for slug in SLUGS})
        )

        for _ in range(3):
            self.now[0] += 60
            self._run_due()

        self.assertEqual(len(self.upstream.ranking_calls), before + len(SLUGS))
        self.assertEqual(
            sum("disagree" in message for message in self.logger.messages),
            len(SLUGS),
        )

    def test_a_board_that_left_the_list_is_dropped(self) -> None:
        self._fill()
        self.upstream.cards = parse_hot_bosses(cards_payload()[:1])

        self.now[0] += 60
        self._run_due()

        self.assertEqual(self.index.slugs, SLUGS[:1])
        self.assertIsNone(self.index.entry(SLUGS[1]))
        self.assertIsNone(self.index.entry(SLUGS[1], "rdps"))

    # --- filling ------------------------------------------------------------------

    def test_the_rdps_boards_fill_after_the_dps_ones_and_share_the_list(self) -> None:
        run(self.index.ensure_filled())
        self.assertFalse(self.index.is_complete("rdps"))
        self.assertEqual(self.index.missing("rdps"), len(SLUGS))

        run(self.index.ensure_filled("rdps"))

        self.assertTrue(self.index.is_complete("rdps"))
        self.assertEqual(sorted(self.upstream.rdps_calls), sorted(SLUGS))
        # The board list was read once, by the DPS fill.
        self.assertEqual(self.upstream.board_calls, 1)
        self.assertEqual(self.index.entry(SLUGS[0], "rdps").ranking.metric, "rdps")
        self.assertEqual(len(self.index.entries("rdps")), 2)
        # The DPS index is untouched by the rDPS fill.
        self.assertEqual(sorted(self.upstream.ranking_calls), sorted(SLUGS))

    def test_a_board_a_fill_could_not_read_is_tried_again_later(self) -> None:
        self.upstream.failing = {SLUGS[1]}
        self._fill()
        self.assertEqual(self.index.missing(), 1)
        # A page waiting on the whole index does not read it again at once.
        run(self.index.ensure_filled())
        self.assertEqual(self.upstream.ranking_calls.count(SLUGS[1]), 1)
        self.upstream.failing = set()

        self.now[0] += 299
        self._run_due()
        self.assertEqual(self.index.missing(), 1)
        self.now[0] += 1
        self._run_due()

        self.assertTrue(self.index.complete)
        self.assertTrue(self.index.is_complete("rdps"))

    def test_waiting_for_a_fill_is_bounded_and_the_fill_goes_on(self) -> None:
        gate = asyncio.Event()

        async def slow_boards():
            await gate.wait()
            return await self.upstream.fetch_boards()

        index = RankingIndex(
            fetch_ranking=self.upstream.fetch_ranking,
            fetch_boards=slow_boards,
            logger=self.logger,
            clock=lambda: self.now[0],
        )

        async def scenario():
            first = await index.wait_filled(timeout=0.01)
            gate.set()
            second = await index.wait_filled(timeout=1.0)
            return first, second

        self.assertEqual(run(scenario()), (False, True))
        self.assertTrue(index.complete)

    # --- the background loop ------------------------------------------------------

    def test_a_board_asked_for_wakes_the_loop(self) -> None:
        async def settle():
            for _ in range(50):
                await asyncio.sleep(0)

        async def scenario():
            self.index.start()
            await settle()  # the fill, then a sleep until the next signal
            self.now[0] += 180
            before = len(self.upstream.ranking_calls)
            await self.index.get(SLUGS[0], on_demand=True)
            await settle()
            calls = self.upstream.ranking_calls[before:]
            await self.index.stop()
            return calls

        self.assertEqual(run(scenario()), [SLUGS[0]])

    def test_start_and_stop_leave_no_task_behind(self) -> None:
        async def scenario():
            self.index.start()
            self.index.start()
            # Two turns: one for the loop to begin its fill, one for the
            # fill to read the board list.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await self.index.stop()
            return [task for task in asyncio.all_tasks() if not task.done()]

        pending = run(scenario())

        self.assertEqual(self.upstream.board_calls, 1)
        # Only the scenario itself was still running: the loop, the fill and
        # every fetch in flight were cancelled and awaited.
        self.assertEqual(len(pending), 1)


class AccountRankingsTests(unittest.TestCase):
    def test_the_best_row_per_board_becomes_the_accounts_ranking(self) -> None:
        first = ranking_for(SLUGS[0])
        second = ranking_for(SLUGS[1])
        account_id = first.rows[2].account_id
        entries = (IndexEntry(first, 0.0), IndexEntry(second, 0.0))

        derived = account_rankings(entries, account_id)

        self.assertIsNotNone(derived)
        self.assertEqual(derived.account_id, account_id)
        self.assertEqual(
            derived.account_display_name, first.rows[2].account_display_name
        )
        by_slug = {entry.boss_slug: entry for entry in derived.rankings}
        expected = min(
            (row.rank for row in first.rows if row.account_id == account_id)
        )
        self.assertEqual(by_slug[SLUGS[0]].rank, expected)
        self.assertEqual(by_slug[SLUGS[0]].total_dps, first.rows[2].dps)

    def test_an_account_with_no_row_anywhere_is_none(self) -> None:
        entries = (IndexEntry(ranking_for(SLUGS[0]), 0.0),)

        self.assertIsNone(account_rankings(entries, "usr_nobody"))


if __name__ == "__main__":
    unittest.main()
