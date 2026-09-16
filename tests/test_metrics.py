"""rDPS: the second ranking of every board, drawn only on request."""

import copy
import unittest
from datetime import UTC, datetime
from pathlib import Path

from core.events import NEW_RECORD, EventLog, diff_rankings
from core.metrics import metric_label, parse_metric_text
from core.models import parse_battle_detail, parse_boss_ranking
from core.persistence import JsonStore
from core.presentation import build_ranking_page
from core.presentation.common import format_number
from core.render import TemplateRenderer
from tests.helpers import battle_detail_payload, ranking_payload_with_rows

WEB = "https://zmdlogs.com"


def rdps_payload() -> dict:
    """The fixture board's rDPS ranking: two eligible records, re-ranked.

    Like upstream's, it keeps the clear-time order, and the support that
    carried the team's rDPS becomes the main C of the first row.
    """

    payload = copy.deepcopy(ranking_payload_with_rows())
    payload["metric"] = "rdps"
    kept = [copy.deepcopy(payload["rows"][0]), copy.deepcopy(payload["rows"][2])]
    kept[0]["characterName"] = "卡缪"
    kept[0]["characterProfession"] = "术士"
    for rank, row in enumerate(kept, start=1):
        row["rank"] = rank
    payload["rows"] = kept
    return payload


class SpellingTests(unittest.TestCase):
    def test_the_table_reads_both_readings_and_nothing_else(self) -> None:
        for text, expected in (
            ("dps", "dps"), ("DPS", "dps"), ("直伤", "dps"),
            ("rdps", "rdps"), ("rDPS", "rdps"), ("团队贡献", "rdps"),
            (" R-DPS ", "rdps"),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_metric_text(text), expected)
        self.assertIsNone(parse_metric_text("xdps"))
        self.assertEqual((metric_label("dps"), metric_label("rdps")), ("DPS", "rDPS"))


class RankingPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dps = parse_boss_ranking(ranking_payload_with_rows())
        self.rdps = parse_boss_ranking(rdps_payload(), metric="rdps")
        self.dps_rows = {row.battle_id: row for row in self.dps.rows}

    def test_the_rdps_page_points_every_row_at_the_dps_board(self) -> None:
        page = build_ranking_page(
            self.rdps,
            query="三位一体",
            web_base_url=WEB,
            dps_rows=self.dps_rows,
            dps_row_count=len(self.dps.rows),
        )

        self.assertEqual(page.header.metric, "rdps")
        self.assertEqual(page.metric_label, "rDPS")
        self.assertIn("只收录可计算 rDPS 的记录 2 条", page.rdps_note)
        self.assertIn(f"DPS 榜 {len(self.dps.rows)} 条", page.rdps_note)
        first, second = page.rows
        # The first rDPS row is DPS #1 with another main C; the second is
        # DPS #3 with the same one, so only the first names a DPS main C.
        self.assertEqual((first.rank, first.dps_rank), (1, 1))
        self.assertEqual(first.dps_character_name, "黎风")
        self.assertEqual((second.rank, second.dps_rank), (2, 3))
        self.assertIsNone(second.dps_character_name)
        # The column shows the row's rDPS, and the bar is relative to it.
        self.assertEqual(first.dps, format_number(self.rdps.rows[0].rdps))

    def test_the_dps_page_is_unchanged(self) -> None:
        page = build_ranking_page(self.dps, query="三位一体", web_base_url=WEB)

        self.assertEqual(page.header.metric, "dps")
        self.assertEqual(page.rdps_note, "")
        self.assertTrue(all(row.dps_rank is None for row in page.rows))
        self.assertEqual(page.header.footer_note, "公开榜单 · DPS 口径")

    def test_the_template_carries_the_badge_and_the_cross_reference(self) -> None:
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])

        html = renderer.render_ranking(
            self.rdps,
            query="三位一体",
            web_base_url=WEB,
            dps_rows=self.dps_rows,
            dps_row_count=len(self.dps.rows),
        )
        plain = renderer.render_ranking(self.dps, query="三位一体", web_base_url=WEB)

        self.assertIn("rDPS 口径", html)
        self.assertIn("DPS 榜 #1 · DPS 口径主 C 黎风", html)
        self.assertIn("DPS 榜 #3", html)
        self.assertIn("相对榜首 rDPS", html)
        self.assertNotIn("rDPS 口径", plain)
        self.assertNotIn("DPS 榜 #", plain)


class BattleCardTests(unittest.TestCase):
    def test_only_a_record_on_the_rdps_board_gets_the_mark(self) -> None:
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])
        payload = battle_detail_payload()
        plain = parse_battle_detail(payload)
        payload["battle"]["rdpsRankingEligible"] = True
        eligible = parse_battle_detail(payload)
        payload["battle"]["rdpsRankingEligible"] = "yes"
        odd = parse_battle_detail(payload)

        self.assertIsNone(plain.rdps_ranking_eligible)
        self.assertTrue(eligible.rdps_ranking_eligible)
        # Anything but a boolean is read as unknown, never as an error.
        self.assertIsNone(odd.rdps_ranking_eligible)
        self.assertIn(
            "rDPS 榜记录",
            renderer.render_battle(eligible, query="x", web_base_url=WEB),
        )
        self.assertNotIn(
            "rDPS 榜记录", renderer.render_battle(plain, query="x", web_base_url=WEB)
        )


class EventMetricTests(unittest.TestCase):
    def test_events_carry_their_ranking_and_a_record_is_new_once_per_ranking(
        self,
    ) -> None:
        store = JsonStore(None, label="events", warn=lambda *a: None)
        now = datetime(2026, 9, 16, tzinfo=UTC)
        log = EventLog(store, clock=lambda: now, stamp=lambda: "2026-09-16T00:00:00Z")
        dps_before = parse_boss_ranking(ranking_payload_with_rows())
        rdps_before = parse_boss_ranking(rdps_payload(), metric="rdps")
        # The same new record enters both boards.
        dps_after = copy.deepcopy(ranking_payload_with_rows())
        new = copy.deepcopy(dps_after["rows"][-1])
        new["battleId"] = "btl_upload_both00000001"
        dps_after["rows"].append(new)
        rdps_after = rdps_payload()
        rdps_after["rows"].append({**new, "rank": 3})

        log.record(dps_before, parse_boss_ranking(dps_after))
        log.record(rdps_before, parse_boss_ranking(rdps_after, metric="rdps"))
        log.record(dps_before, parse_boss_ranking(dps_after))

        metrics = [(e.kind, e.metric) for e in log.events]
        self.assertEqual(metrics, [(NEW_RECORD, "dps"), (NEW_RECORD, "rdps")])
        self.assertEqual([e.metric for e in log.recent(metric="rdps")], ["rdps"])
        self.assertEqual(len(log.recent()), 2)
        # A DPS board's events say so themselves.
        fresh = diff_rankings(dps_before, parse_boss_ranking(dps_after), seen_at="t")
        self.assertEqual(fresh[0].metric, "dps")


if __name__ == "__main__":
    unittest.main()
