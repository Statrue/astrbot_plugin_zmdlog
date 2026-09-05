"""The ranking index: one place every board ranking is read from."""

import asyncio
import copy
import logging
import unittest

from core.client import ZmdLogsClientError
from core.models import parse_boss_ranking, parse_hot_bosses
from core.ranking_index import (
    STALE_FALLBACK_MAX_AGE_SECONDS,
    RankingIndex,
)
from tests.helpers import hot_bosses_payload, ranking_payload_with_rows

SLUGS = ("dung01_group_bossrush02", "dung01_group_bossrush03")
# The fixture ranking's first three battle ids, as hot-bosses would list them.
TOP3 = [f"btl_upload_{rank:012d}" for rank in (1, 2, 3)]


def run(coro):
    return asyncio.run(coro)


def cards_payload(top_ids: dict[str, list[str]] | None = None) -> list[dict]:
    """Two boards; ``top_ids`` sets each board's top runs' battle ids."""

    base = hot_bosses_payload()[0]
    cards = []
    for slug in SLUGS:
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
            for battle_id in (top_ids or {}).get(slug, [])
        ]
        cards.append(card)
    return cards


def ranking_for(slug: str, *, first_id: str | None = None):
    payload = ranking_payload_with_rows()
    payload["bossSlug"] = slug
    if first_id is not None:
        payload["rows"][0]["battleId"] = first_id
    return parse_boss_ranking(payload)


class FakeUpstream:
    def __init__(self) -> None:
        self.cards = parse_hot_bosses(cards_payload())
        self.ranking_calls: list[str] = []
        self.board_calls = 0
        self.fail = False
        self.first_ids: dict[str, str] = {}

    async def fetch_ranking(self, slug: str):
        self.ranking_calls.append(slug)
        if self.fail:
            raise ZmdLogsClientError("offline")
        return ranking_for(slug, first_id=self.first_ids.get(slug))

    async def fetch_boards(self):
        self.board_calls += 1
        if self.fail:
            raise ZmdLogsClientError("offline")
        return self.cards


class CapturingLogger(logging.Logger):
    def __init__(self) -> None:
        super().__init__("test")
        self.messages: list[str] = []

    def handle(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class RankingIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = FakeUpstream()
        self.now = [1_000.0]
        self.logger = CapturingLogger()
        self.index = RankingIndex(
            fetch_ranking=self.upstream.fetch_ranking,
            fetch_boards=self.upstream.fetch_boards,
            logger=self.logger,
            clock=lambda: self.now[0],
        )

    def test_a_fill_reads_every_board_once(self) -> None:
        run(self.index.ensure_filled())

        self.assertTrue(self.index.complete)
        self.assertEqual(sorted(self.upstream.ranking_calls), sorted(SLUGS))
        self.assertEqual(len(self.index.entries()), 2)
        self.assertEqual(self.index.oldest_age_seconds(), 0.0)

    def test_a_reader_accepting_any_age_never_fetches(self) -> None:
        run(self.index.ensure_filled())
        self.now[0] += 3_600

        ranking = run(self.index.get(SLUGS[0], max_age=None))

        self.assertEqual(ranking.boss_slug, SLUGS[0])
        self.assertEqual(self.upstream.ranking_calls.count(SLUGS[0]), 1)

    def test_a_reader_wanting_fresher_data_gets_a_fetch_that_updates_the_index(
        self,
    ) -> None:
        run(self.index.ensure_filled())
        self.now[0] += 90

        run(self.index.get(SLUGS[0], max_age=60))

        self.assertEqual(self.upstream.ranking_calls.count(SLUGS[0]), 2)
        self.assertEqual(self.index.entry(SLUGS[0]).loaded_at, self.now[0])

    def test_a_board_not_held_is_fetched_on_first_read(self) -> None:
        ranking = run(self.index.get(SLUGS[1], max_age=60))

        self.assertEqual(ranking.boss_slug, SLUGS[1])
        self.assertIsNotNone(self.index.entry(SLUGS[1]))

    def test_a_failed_fetch_falls_back_to_a_recent_copy_only(self) -> None:
        run(self.index.ensure_filled())
        self.upstream.fail = True

        self.now[0] += 120
        ranking = run(self.index.get(SLUGS[0], max_age=60))
        self.assertEqual(ranking.boss_slug, SLUGS[0])
        self.assertTrue(any("refresh failure" in m for m in self.logger.messages))

        self.now[0] += STALE_FALLBACK_MAX_AGE_SECONDS
        with self.assertRaises(ZmdLogsClientError):
            run(self.index.get(SLUGS[0], max_age=60))

    def test_the_round_robin_re_reads_the_oldest_board(self) -> None:
        run(self.index.ensure_filled())
        self.now[0] += 10
        run(self.index.get(SLUGS[0], max_age=0))  # SLUGS[0] is now the newer copy
        self.now[0] += 10

        run(self.index.refresh_oldest())

        self.assertEqual(self.upstream.ranking_calls[-1], SLUGS[1])
        self.assertEqual(self.index.entry(SLUGS[1]).loaded_at, self.now[0])

    def test_a_changed_top_three_triggers_an_immediate_re_read(self) -> None:
        run(self.index.ensure_filled())
        calls_before = len(self.upstream.ranking_calls)
        # Upstream's top run on board 1 is now a battle the index has not seen.
        self.upstream.first_ids[SLUGS[1]] = "btl_upload_new000000001"
        cards = parse_hot_bosses(
            cards_payload(
                {
                    SLUGS[0]: TOP3,
                    SLUGS[1]: ["btl_upload_new000000001", *TOP3[1:]],
                }
            )
        )

        run(self.index.apply_signal(cards))

        self.assertEqual(self.upstream.ranking_calls[calls_before:], [SLUGS[1]])
        rows = self.index.entry(SLUGS[1]).ranking.rows
        self.assertEqual(rows[0].battle_id, "btl_upload_new000000001")

    def test_an_unchanged_top_three_costs_nothing(self) -> None:
        run(self.index.ensure_filled())
        calls_before = len(self.upstream.ranking_calls)
        cards = parse_hot_bosses(cards_payload({slug: TOP3 for slug in SLUGS}))

        run(self.index.apply_signal(cards))

        self.assertEqual(len(self.upstream.ranking_calls), calls_before)

    def test_a_shorter_top_list_means_a_record_was_deleted(self) -> None:
        run(self.index.ensure_filled())
        calls_before = len(self.upstream.ranking_calls)
        # The board now has fewer than three records; the index holds ten.
        cards = parse_hot_bosses(cards_payload({slug: TOP3[:2] for slug in SLUGS}))

        run(self.index.apply_signal(cards))

        self.assertEqual(len(self.upstream.ranking_calls), calls_before + 2)

    def test_a_board_that_left_the_index_is_dropped(self) -> None:
        run(self.index.ensure_filled())
        cards = parse_hot_bosses(cards_payload()[:1])

        run(self.index.apply_signal(cards))

        self.assertEqual(self.index.slugs, SLUGS[:1])
        self.assertIsNone(self.index.entry(SLUGS[1]))

    def test_a_failing_board_logs_once_per_streak(self) -> None:
        run(self.index.ensure_filled())
        self.upstream.fail = True

        run(self.index.refresh_oldest())
        run(self.index.refresh_oldest())

        failures = [m for m in self.logger.messages if "could not read" in m]
        self.assertEqual(len(failures), 1)

    def test_concurrent_reads_of_one_board_share_a_fetch(self) -> None:
        async def scenario():
            await asyncio.gather(
                self.index.get(SLUGS[0], max_age=60),
                self.index.get(SLUGS[0], max_age=60),
                self.index.get(SLUGS[0], max_age=60),
            )

        run(scenario())

        self.assertEqual(self.upstream.ranking_calls.count(SLUGS[0]), 1)

    def test_start_and_stop_leave_no_task_behind(self) -> None:
        async def scenario():
            self.index.start()
            self.index.start()
            await asyncio.sleep(0)
            await self.index.stop()

        run(scenario())

        self.assertEqual(self.upstream.board_calls, 1)


if __name__ == "__main__":
    unittest.main()
