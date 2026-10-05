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

    def test_the_rdps_page_draws_the_rdps_board_and_nothing_more(self) -> None:
        # The V2 page says only how many rDPS records there are: no note on
        # what the board leaves out, no cross-reference to the DPS board
        # (the prototype's ruling, "说明要精简").
        page = build_ranking_page(self.rdps, query="三位一体", web_base_url=WEB)

        self.assertEqual(page.header.metric, "rdps")
        self.assertEqual(page.metric_label, "rDPS")
        self.assertEqual(page.record_count, len(self.rdps.rows))
        # The figure is the row's rDPS.
        self.assertEqual(
            page.rows[0].dps, format_number(round(self.rdps.rows[0].rdps))
        )

    def test_the_dps_page_is_unchanged(self) -> None:
        page = build_ranking_page(self.dps, query="三位一体", web_base_url=WEB)

        self.assertEqual(page.header.metric, "dps")
        self.assertEqual(page.metric_label, "DPS")
        self.assertEqual(page.header.footer_note, "公开榜单 · DPS 口径")
        self.assertEqual(page.rows[0].dps, format_number(round(self.dps.rows[0].dps)))

    def test_the_template_names_its_metric(self) -> None:
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])

        html = renderer.render_ranking(self.rdps, query="三位一体", web_base_url=WEB)
        plain = renderer.render_ranking(self.dps, query="三位一体", web_base_url=WEB)

        self.assertIn("<span>rDPS 口径</span>", html)
        self.assertIn(f"<b>{len(self.rdps.rows)}</b> 条公开记录", html)
        self.assertNotIn("DPS 榜", html)
        self.assertNotIn("rDPS", plain)


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
