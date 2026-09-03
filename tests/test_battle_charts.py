"""Tests for the 0.8.0 battle-card telemetry: DPS curve and buff coverage."""

import unittest
from pathlib import Path

from core.models import parse_battle_detail
from core.presentation import (
    build_battle_page,
    build_buff_band_view,
    build_dps_curve_view,
)
from core.render import TemplateRenderer
from core.telemetry import build_buff_coverage, build_dps_curve
from tests.helpers import battle_detail_payload, buff, damage_tick, effect

WEB = "https://zmdlogs.com"


def ordered_participants(battle):
    """The card's display order: highest DPS first."""

    return tuple(
        sorted(battle.participants, key=lambda item: item.dps, reverse=True)
    )


class TelemetryParseTests(unittest.TestCase):
    def test_damage_ticks_and_buffs_are_read_leniently(self) -> None:
        battle = parse_battle_detail(battle_detail_payload())

        self.assertEqual(
            [(point.at_ms, point.character_name, point.value)
             for point in battle.damage_points],
            [
                (1_000, "洛茜", 900_000),
                (4_000, "卡缪", 200_000),
                (11_000, "洛茜", 1_127_572),
                (20_833, "卡缪", 65_333),
            ],
        )
        # Seven buff rows survive; the string entry and the unknown state do not.
        self.assertEqual(len(battle.buffs), 7)
        first = battle.buffs[0]
        self.assertEqual((first.source_name, first.target_name), ("卡缪", "洛茜"))
        self.assertEqual((first.start_ms, first.duration_ms), (1_000, 9_000))
        self.assertEqual(
            [(item.zone, item.element, item.rate) for item in first.effects],
            [("atk", "all", 0.16)],
        )

    def test_missing_or_malformed_sections_parse_to_nothing(self) -> None:
        for value in (None, {}, "不是列表", [123, None]):
            with self.subTest(value=value):
                payload = battle_detail_payload()
                payload["timelineEvents"] = value
                payload["characterStates"] = value
                battle = parse_battle_detail(payload)
                self.assertEqual(battle.damage_points, ())
                self.assertEqual(battle.buffs, ())

    def test_a_battle_without_telemetry_still_parses(self) -> None:
        payload = battle_detail_payload()
        del payload["timelineEvents"]
        del payload["characterStates"]

        battle = parse_battle_detail(payload)

        self.assertEqual(battle.damage_points, ())
        self.assertEqual(battle.buffs, ())
        self.assertEqual(battle.total_damage, 2_292_905)


class DpsCurveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.battle = parse_battle_detail(battle_detail_payload())

    def test_every_line_ends_at_that_character_s_reported_dps(self) -> None:
        curve = build_dps_curve(
            self.battle.damage_points,
            duration_ms=self.battle.duration_ms,
            character_names=tuple(
                item.character_name for item in ordered_participants(self.battle)
            ),
        )

        reported = {
            item.character_name: (item.dps, item.total_damage)
            for item in self.battle.participants
        }
        for series in curve.series:
            dps, damage = reported[series.character_name]
            with self.subTest(character=series.character_name):
                self.assertEqual(series.total_damage, damage)
                self.assertAlmostEqual(series.final_dps, dps, places=2)
        self.assertAlmostEqual(
            curve.team.final_dps, self.battle.total_dps, places=2
        )
        # An early burst peaks well above the final average, and the peak must
        # include the team line or the axis would clip it.
        self.assertAlmostEqual(curve.peak_dps, 450_000.0, places=2)

    def test_buckets_are_cumulative_and_thinned_for_long_fights(self) -> None:
        points = tuple(
            damage_tick(index * 1_000, "洛茜", 1_000)
            for index in range(600)
        )
        parsed = parse_battle_detail(
            {**battle_detail_payload(), "timelineEvents": list(points)}
        )

        curve = build_dps_curve(
            parsed.damage_points,
            duration_ms=600_000,
            character_names=("洛茜",),
            max_points=150,
        )
        series = curve.series[0]

        self.assertEqual(series.total_damage, 600_000)
        self.assertLessEqual(len(series.points), 151)
        # Thinning keeps real samples: cumulative damage rises by 1000 a second,
        # so the average settles at 1000 DPS.
        self.assertAlmostEqual(series.points[-1].dps, 1_000.0, places=2)
        self.assertEqual(series.points[-1].at_ms, 600_000)

    def test_nothing_to_draw_returns_none(self) -> None:
        for points in ((), tuple(self.battle.damage_points[:1])):
            with self.subTest(count=len(points)):
                curve = build_dps_curve(
                    points,
                    duration_ms=1_000,
                    character_names=("洛茜",),
                )
                # One bucket cannot make a line; the view layer drops it too.
                self.assertTrue(curve is None or len(curve.team.points) == 1)


class BuffCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.battle = parse_battle_detail(battle_detail_payload())
        self.roster = tuple(
            entry.character_name for entry in self.battle.roster
        )

    def coverage(self, battle=None):
        target = battle or self.battle
        return build_buff_coverage(
            target.buffs,
            duration_ms=target.duration_ms,
            roster_names=tuple(
                entry.character_name for entry in target.roster
            ),
        )

    def test_rows_merge_variants_and_keep_uptime(self) -> None:
        coverage = self.coverage()

        self.assertEqual(len(coverage.rows), 2)
        attack, amp = coverage.rows
        # The caster and owner variants of one buff are one row, covering the
        # whole roster, with the refresh as a second span.
        self.assertEqual(attack.effect_label, "攻击 +16%")
        self.assertEqual(attack.name, "攻击提升")
        self.assertEqual(attack.source_name, "卡缪")
        self.assertTrue(attack.team_wide)
        self.assertEqual(attack.max_targets, 2)
        self.assertEqual(
            [(span.start_ms, span.end_ms) for span in attack.spans],
            [(1_000, 10_040), (12_000, 20_833)],
        )
        self.assertEqual(attack.covered_ms, 17_873)
        # A raw event key leaves the name empty; the effect already says it.
        self.assertEqual(amp.effect_label, "增幅灼热 +40%")
        self.assertEqual(amp.name, "")
        self.assertEqual(amp.zone, "amp")

    def test_zones_targets_and_durations_are_filtered(self) -> None:
        coverage = self.coverage()
        labels = [row.effect_label for row in coverage.rows]

        # speedup is not a damage zone, the flat buff has no duration, and the
        # non-roster target was never a row.
        self.assertNotIn("疾行", [row.name for row in coverage.rows])
        self.assertTrue(all("加速" not in label for label in labels))
        for row in coverage.rows:
            for span in row.spans:
                self.assertTrue(set(span.targets) <= set(self.roster))
                self.assertLessEqual(span.end_ms, self.battle.duration_ms)

    def test_rows_past_the_cap_are_counted_not_dropped_silently(self) -> None:
        rows = [
            buff(f"buff_{index}", f"增益{index}", "洛茜", "洛茜",
                 index * 100, 1_000 + index, [effect("atk", "all", 0.01 * (index + 1))])
            for index in range(16)
        ]
        payload = battle_detail_payload()
        payload["characterStates"] = [
            {"characterName": "洛茜", "buffsReceived": rows}
        ]

        coverage = self.coverage(parse_battle_detail(payload))

        self.assertEqual(len(coverage.rows), 12)
        self.assertEqual(coverage.hidden_rows, 4)
        # Kept by coverage, then shown in the order they landed.
        starts = [row.spans[0].start_ms for row in coverage.rows]
        self.assertEqual(starts, sorted(starts))

    def test_no_usable_buffs_returns_none(self) -> None:
        payload = battle_detail_payload()
        payload["characterStates"] = []

        self.assertIsNone(self.coverage(parse_battle_detail(payload)))


class ChartViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.battle = parse_battle_detail(battle_detail_payload())

    def test_curve_geometry_axis_and_colours(self) -> None:
        view = build_dps_curve_view(
            self.battle, ordered_participants(self.battle)
        )

        self.assertEqual(
            [item.character_name for item in view.series], ["洛茜", "卡缪"]
        )
        # Colour index follows the card's DPS order.
        self.assertEqual([item.colour_index for item in view.series], [1, 2])
        self.assertEqual(view.series[0].final_dps, "97,325.01")
        self.assertEqual(view.series[0].damage_share, "88.4%")
        self.assertEqual(view.team_dps, "110,061.2")
        self.assertEqual(view.bucket_label, "1 秒")
        # Axis top is the peak (450k) rounded up, printed high to low.
        self.assertEqual(view.axis_labels[0], "50万")
        self.assertEqual(view.axis_labels[-1], "0")
        # Second-based ticks, the same set the buff band draws underneath.
        self.assertEqual([tick.label for tick in view.ticks][:3], ["0s", "2s", "4s"])
        self.assertEqual(view.ticks[-1].label, "20s")
        band = build_buff_band_view(self.battle)
        self.assertEqual(
            [tick.top for tick in view.ticks], [tick.top for tick in band.ticks]
        )
        points = [
            tuple(float(value) for value in point.split(","))
            for point in view.series[0].polyline.split()
        ]
        # The first bucket ends before the first hit, so the line starts at 0.
        self.assertAlmostEqual(points[0][0], 4.8, places=1)
        self.assertAlmostEqual(points[0][1], 100.0, places=1)
        # By 2 s she has dealt 900k, an average of 450k of a 500k axis.
        self.assertAlmostEqual(points[1][0], 9.6, places=1)
        self.assertAlmostEqual(points[1][1], 10.0, places=1)
        for point in view.team_polyline.split():
            x, y = (float(value) for value in point.split(","))
            self.assertTrue(0 <= x <= 100 and 0 <= y <= 100)

    def test_buff_band_spans_ticks_and_labels(self) -> None:
        view = build_buff_band_view(self.battle)

        self.assertEqual(view.duration_label, "0:20.833")
        self.assertEqual(view.hidden_rows, 0)
        self.assertEqual(view.ticks[0].label, "0s")
        self.assertEqual(view.ticks[-1].label, "20s")
        self.assertTrue(all(0 <= tick.top <= 100 for tick in view.ticks))
        attack, amp = view.rows
        self.assertEqual(attack.zone, "atk")
        self.assertEqual(attack.target_label, "全队")
        self.assertEqual(attack.coverage_label, "86%")
        self.assertEqual(len(attack.spans), 2)
        self.assertAlmostEqual(attack.spans[0].left, 4.8, places=1)
        self.assertAlmostEqual(attack.spans[0].width, 43.4, places=1)
        self.assertEqual(attack.spans[0].target_label, "全队")
        self.assertEqual(amp.target_label, "洛茜")
        for row in view.rows:
            for span in row.spans:
                self.assertLessEqual(span.left + span.width, 100.01)

    def test_a_battle_without_telemetry_has_neither_chart(self) -> None:
        payload = battle_detail_payload()
        payload["timelineEvents"] = []
        payload["characterStates"] = []
        battle = parse_battle_detail(payload)

        page = build_battle_page(battle, query="q", web_base_url=WEB)

        self.assertIsNone(page.dps_curve)
        self.assertIsNone(page.buff_band)


class ChartTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])
        self.battle = parse_battle_detail(battle_detail_payload())

    def test_card_draws_both_charts(self) -> None:
        html = self.renderer.render_battle(self.battle, query="q", web_base_url=WEB)

        self.assertIn("<h2>DPS 曲线</h2>", html)
        self.assertIn('class="curve-line share-line--1"', html)
        self.assertIn("curve-line is-team", html)
        self.assertIn("<h2>增益覆盖</h2>", html)
        self.assertIn('class="buff-band"', html)
        self.assertIn("buff-span is-atk is-team", html)
        self.assertIn("buff-zone is-amp", html)
        self.assertIn(">攻击提升<", html)
        self.assertIn(">卡缪 → 全队<", html)
        # A raw-key buff prints its effect only, with no empty name element.
        self.assertNotIn("<b></b>", html)
        self.assertNotIn("buff_wpn_sword_0021_up", html)
        self.assertNotIn("ignored", html)

    def test_card_omits_the_sections_without_telemetry(self) -> None:
        payload = battle_detail_payload()
        payload["timelineEvents"] = []
        payload["characterStates"] = []

        html = self.renderer.render_battle(
            parse_battle_detail(payload), query="q", web_base_url=WEB
        )

        # The stylesheet names both sections in its comments, so assert on the
        # markup rather than on the words.
        self.assertNotIn("<h2>DPS 曲线</h2>", html)
        self.assertNotIn('class="buff-band"', html)
        self.assertNotIn('class="curve-panel"', html)
        # The card itself still renders.
        self.assertIn("战斗贡献", html)


if __name__ == "__main__":
    unittest.main()
