"""暴击期望: the per-hit crit maths, the lenient parse, the 摘要 and the tool line."""

import asyncio
import logging
import math
import unittest
from pathlib import Path

from core.crit import CritHit, CritRoll, CritTotals, build_crit_expectation
from core.matcher import AliasConfig, MatcherCache
from core.models import parse_battle_detail
from core.presentation import build_crit_bell
from core.recipes import prepare_battle
from core.render import TemplateRenderer
from core.settings import PluginSettings
from core.toolbox import ToolService
from tests.helpers import battle_detail_payload, damage_tick
from tests.test_compare import second_battle_payload
from tests.test_tools import FakeData, FakeRenderer

WEB = "https://zmdlogs.com"


def verified(**overrides) -> dict:
    """A ``hitContext`` as a v57 upload records it, with fields overridden."""

    critical = {
        "version": 1,
        "status": "verified",
        "isCritical": True,
        "critRate": 0.05,
        "critDamageBonus": 0.5,
        "uncappedDamage": 900_000,
        "damageCap": 1_477_030,
        "source": "bound_entity_attributes_and_exact_damage_unit",
    }
    critical.update(overrides)
    return {"critical": {k: v for k, v in critical.items() if v is not ...}}


def crit_battle_payload(*, cover_last: bool = True) -> dict:
    """The fixture fight as a v57 upload: every hit carries its crit roll.

    洛茜's 900,000 crit at +50% rolled 600,000 without it (a crit adds
    300,000 at 5%); 卡缪's 200,000 would have crit for 300,000 (adds 100,000
    at 5%); the other two hits could not crit. Expected: 1,992,905 fixed
    plus 15,000 + 5,000 = 2,012,905 against 2,292,905 dealt, +13.91%.
    ``cover_last=False`` leaves 卡缪's 65,333 unrecorded, as an older client
    part of the way through would.
    """

    payload = battle_detail_payload()
    rolls = {
        1_000: verified(isCritical=True, critRate=0.05, uncappedDamage=...),
        4_000: verified(isCritical=False, critRate=0.05, uncappedDamage=...),
        11_000: verified(isCritical=False, critRate=0.0, uncappedDamage=...),
        20_833: verified(isCritical=False, critRate=0.0, uncappedDamage=...)
        if cover_last
        else None,
    }
    for event in payload["timelineEvents"]:
        if event.get("laneType") == "skill" and event.get("tsMsFromStart") in rolls:
            event["hitContext"] = rolls[event["tsMsFromStart"]]
    return payload


def hit(
    damage: int,
    *,
    crit: bool,
    rate: float,
    bonus: float = 0.5,
    uncapped: float | None = None,
    cap: float | None = None,
    character: str | None = "洛茜",
) -> CritHit:
    return CritHit(
        damage=damage,
        roll=CritRoll(
            is_critical=crit,
            rate=rate,
            damage_bonus=bonus,
            uncapped_damage=uncapped,
            damage_cap=cap,
        ),
        character=character,
    )


class CritExpectationMathTests(unittest.TestCase):
    def test_stochastic_hits_add_to_fixed_mean_and_variance(self) -> None:
        # A crit of 150 at +50% rolled 100 without it, so a crit adds 50:
        # fixed 100, mean 50·0.5 = 25, variance 50²·0.5·0.5 = 625.
        # A non-crit 200 at +100% would have crit for 400, adding 200:
        # fixed 200, mean 200·0.2 = 40, variance 200²·0.2·0.8 = 6400.
        result = build_crit_expectation(
            (
                hit(150, crit=True, rate=0.5, bonus=0.5),
                hit(200, crit=False, rate=0.2, bonus=1.0),
            ),
            duration_ms=2_000,
        )

        team = result.team
        self.assertEqual(team.actual_damage, 350)
        self.assertAlmostEqual(team.expected_damage, 365.0)
        self.assertAlmostEqual(team.standard_deviation, math.sqrt(7025))
        self.assertAlmostEqual(team.expected_dps, 182.5)
        self.assertAlmostEqual(team.relative_difference, -15 / 365)
        self.assertEqual(team.coverage, 1.0)
        self.assertEqual((team.analysed_hits, team.discarded_hits), (2, 0))

    def test_certain_rolls_are_fixed_and_add_no_variance(self) -> None:
        # Rate 0: the 100 it dealt is all it could have dealt. Rate 1: the
        # 300 crit (200 base, +50%) was certain.
        result = build_crit_expectation(
            (
                hit(100, crit=False, rate=0.0),
                hit(300, crit=True, rate=1.0, bonus=0.5),
            ),
            duration_ms=1_000,
        )

        self.assertAlmostEqual(result.team.expected_damage, 400.0)
        self.assertEqual(result.team.standard_deviation, 0.0)
        self.assertEqual(result.team.relative_difference, 0.0)

    def test_the_cap_bounds_both_outcomes(self) -> None:
        # Capped crit: 1500 uncapped at +100% is 750 base; a crit is capped
        # at 1000, so it adds 250 — fixed 750, mean 125, variance 15625.
        # Capped miss: 1000 is already the cap, a crit adds nothing.
        result = build_crit_expectation(
            (
                hit(1_000, crit=True, rate=0.5, bonus=1.0, uncapped=1_500, cap=1_000),
                hit(1_000, crit=False, rate=0.5, bonus=1.0, uncapped=1_000, cap=1_000),
            ),
            duration_ms=1_000,
        )

        self.assertAlmostEqual(result.team.expected_damage, 1_875.0)
        self.assertAlmostEqual(result.team.standard_deviation, 125.0)
        self.assertEqual(result.team.actual_damage, 2_000)

    def test_inconsistent_hits_are_dropped_and_lower_the_coverage(self) -> None:
        good = hit(100, crit=False, rate=0.0)
        inconsistent = {
            "rate above 1": hit(100, crit=False, rate=1.2),
            "rate below 0": hit(100, crit=False, rate=-0.1),
            "crit at rate 0": hit(100, crit=True, rate=0.0),
            "miss at rate 1": hit(100, crit=False, rate=1.0),
            "uncapped below the damage": hit(100, crit=False, rate=0.05, uncapped=99),
            "cap below the damage": hit(100, crit=False, rate=0.05, cap=99),
            "a bonus that would divide by zero": hit(
                100, crit=True, rate=0.05, bonus=-1.0
            ),
        }
        for reason, bad in inconsistent.items():
            with self.subTest(reason):
                result = build_crit_expectation((good, bad), duration_ms=1_000)

                self.assertEqual(result.team.actual_damage, 100)
                self.assertAlmostEqual(result.team.expected_damage, 100.0)
                self.assertEqual(result.team.coverage, 0.5)
                self.assertEqual(
                    (result.team.analysed_hits, result.team.discarded_hits), (1, 1)
                )

    def test_unrecorded_hits_count_only_toward_the_coverage(self) -> None:
        result = build_crit_expectation(
            (
                hit(300, crit=False, rate=0.0),
                CritHit(damage=100, roll=None, character="洛茜"),
                # Zero-damage hits are not selected at all, as on the site.
                CritHit(damage=0, roll=None, character="洛茜"),
            ),
            duration_ms=1_000,
        )

        self.assertEqual(result.team.actual_damage, 300)
        self.assertEqual(result.team.coverage, 0.75)
        self.assertEqual(result.team.discarded_hits, 0)

    def test_each_character_gets_a_row_and_the_team_sums_them(self) -> None:
        result = build_crit_expectation(
            (
                hit(100, crit=False, rate=0.0, character="卡缪"),
                hit(300, crit=True, rate=1.0, character="洛茜"),
                CritHit(damage=100, roll=None, character="洛茜"),
                # Nobody recorded as the dealer: the team still counts it.
                hit(50, crit=False, rate=0.0, character=None),
                # Nothing of 伊冯's could be analysed, so she gets no row.
                CritHit(damage=500, roll=None, character="伊冯"),
            ),
            duration_ms=2_000,
        )

        self.assertEqual(
            [
                (row.character, row.actual_damage, row.expected_dps, row.coverage)
                for row in result.characters
            ],
            [("洛茜", 300, 150.0, 0.75), ("卡缪", 100, 50.0, 1.0)],
        )
        self.assertIsNone(result.team.character)
        self.assertEqual(result.team.actual_damage, 450)
        self.assertEqual(result.team.coverage, 450 / 1_050)

    def test_nothing_analysable_is_no_result(self) -> None:
        self.assertIsNone(build_crit_expectation((), duration_ms=1_000))
        self.assertIsNone(
            build_crit_expectation(
                (
                    CritHit(damage=100, roll=None),
                    hit(100, crit=True, rate=0.0),
                ),
                duration_ms=1_000,
            )
        )


class CritParseTests(unittest.TestCase):
    def parse(self, *contexts):
        payload = battle_detail_payload()
        payload["timelineEvents"] = [
            {**damage_tick(1_000 + index, "洛茜", 900_000), "hitContext": context}
            for index, context in enumerate(contexts)
        ]
        return parse_battle_detail(payload).damage_points

    def test_a_verified_roll_is_read_onto_its_hit(self) -> None:
        (point,) = self.parse(verified())

        self.assertEqual(point.value, 900_000)
        self.assertEqual(
            point.crit_roll,
            CritRoll(
                is_critical=True,
                rate=0.05,
                damage_bonus=0.5,
                uncapped_damage=900_000,
                damage_cap=1_477_030,
            ),
        )

    def test_optional_bounds_may_be_null_or_absent(self) -> None:
        points = self.parse(
            verified(damageCap=None), verified(uncappedDamage=..., damageCap=...)
        )

        self.assertEqual(
            [(p.crit_roll.uncapped_damage, p.crit_roll.damage_cap) for p in points],
            [(900_000, None), (None, None)],
        )

    def test_unusable_rolls_leave_the_hit_uncovered_not_the_battle_broken(self):
        unusable = {
            "unavailable": {
                "critical": {
                    "version": 1,
                    "status": "unavailable",
                    "reasons": ["part_damage_roll_relation_unavailable"],
                }
            },
            "a newer version": verified(version=2),
            "no crit flag": verified(isCritical=...),
            "no rate": verified(critRate=...),
            "no bonus": verified(critDamageBonus=...),
            "flag as text": verified(isCritical="true"),
            "rate as text": verified(critRate="0.05"),
            "bonus as a flag": verified(critDamageBonus=True),
            "rate not finite": verified(critRate=float("nan")),
            "cap as text": verified(damageCap="1477030"),
            "no critical at all": {},
            "a list for a context": [],
            "an old upload": None,
        }
        points = self.parse(*unusable.values())

        self.assertEqual(len(points), len(unusable))
        for reason, point in zip(unusable, points, strict=True):
            with self.subTest(reason):
                self.assertIsNone(point.crit_roll)


class _HtmlRenderer:
    """The template renderer behind the async face a recipe draws with."""

    def __init__(self) -> None:
        self.inner = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])

    async def render_battle(self, *args, **kwargs) -> str:
        return self.inner.render_battle(*args, **kwargs)


def card_html(payload: dict) -> str:
    """The 摘要 as the command draws it, recipe and all."""

    battle_id = payload["battle"]["id"]
    data = FakeData(battles={battle_id: parse_battle_detail(payload)})

    async def draw() -> str:
        recipe = await prepare_battle(
            data, battle_id, query=battle_id, web_base_url=WEB
        )
        return await recipe.draw(_HtmlRenderer())

    return asyncio.run(draw())


def crit_section(html: str) -> str:
    """The 摘要's 暴击期望 section, up to the curve after it."""

    return html[
        html.index("<strong>暴击期望</strong>"):html.index("<strong>DPS 曲线</strong>")
    ]


class CritSummaryTests(unittest.TestCase):
    """暴击期望 on the 摘要: the verdict, three figures and the bell."""

    def test_the_section_sits_between_the_shares_and_the_curve(self) -> None:
        html = card_html(crit_battle_payload())

        shares, crit, curve = (
            html.index(f"<strong>{title}</strong>")
            for title in ("伤害构成", "暴击期望", "DPS 曲线")
        )
        self.assertLess(shares, crit)
        self.assertLess(crit, curve)

    def test_a_lucky_run_says_how_far_above_on_yellow(self) -> None:
        section = crit_section(card_html(crit_battle_payload()))

        # 2,292,905 dealt against 2,012,905 expected over 20.833 s.
        self.assertIn('class="b-sum-luck is-up"', section)
        self.assertIn("<span>高于期望</span>", section)
        self.assertIn("<b>13.91%</b>", section)
        for label, value in (
            ("实际总伤", "2,292,905"),
            ("期望总伤", "2,012,905"),
            ("期望 DPS", "96,621"),
        ):
            self.assertIn(f"<dt>{label}</dt><dd>{value}</dd>", section)
        self.assertNotIn("已覆盖", section)
        # The bell, both marks on it, and a damage ruler of six ticks.
        self.assertIn('class="b-dist-mark is-expected"', section)
        self.assertIn('class="b-dist-mark is-actual"', section)
        ruler = section[section.index('class="b-ruler b-dist-ruler"'):]
        self.assertEqual(ruler.count("万</span>"), 6)

    def test_an_unlucky_run_says_how_far_below(self) -> None:
        payload = crit_battle_payload()
        for event in payload["timelineEvents"]:
            if event.get("hitContext") and event.get("tsMsFromStart") == 1_000:
                # 洛茜's 900,000 did not crit: a crit would have added 450,000.
                event["hitContext"] = verified(isCritical=False, critRate=0.05)

        section = crit_section(card_html(payload))

        self.assertIn('class="b-sum-luck"', section)
        self.assertIn("<span>低于期望</span>", section)
        # Half a deviation apart: the two labels lean away from each other.
        self.assertIn('class="b-dist-plot is-close is-below"', section)
        # 2,292,905 against 2,292,905 + 22,500 + 5,000.
        self.assertIn("<b>1.19%</b>", section)

    def test_partial_coverage_is_stated(self) -> None:
        section = crit_section(card_html(crit_battle_payload(cover_last=False)))

        self.assertIn("已覆盖 97.1% 伤害", section)

    def test_an_older_upload_has_no_section(self) -> None:
        html = card_html(battle_detail_payload())

        # The stylesheet names the section in comments; assert on markup.
        self.assertNotIn("<strong>暴击期望</strong>", html)
        self.assertNotIn('class="b-dist-plot', html)
        self.assertIn("<strong>DPS 曲线</strong>", html)


def team_totals(actual: int, *, spread: float = 10_000) -> CritTotals:
    return CritTotals(
        character=None,
        actual_damage=actual,
        expected_damage=1_000_000,
        standard_deviation=spread,
        expected_dps=50_000,
        coverage=1.0,
        analysed_hits=100,
        discarded_hits=0,
    )


class CritBellTests(unittest.TestCase):
    """The bell's geometry: four deviations either side of the expectation."""

    def test_the_expectation_is_the_middle_and_one_deviation_is_hatched(self) -> None:
        bell = build_crit_bell(team_totals(980_000))

        self.assertEqual(bell.expected_left, 50.0)
        # Two deviations below, on an axis eight deviations wide.
        self.assertEqual(bell.actual_left, 25.0)
        self.assertEqual((bell.spread_left, bell.spread_width), (37.5, 25.0))
        self.assertEqual(
            bell.ticks,
            (
                (0.0, "96万"),
                (20.0, "97.6万"),
                (40.0, "99.2万"),
                (60.0, "100.8万"),
                (80.0, "102.4万"),
                (100.0, "104万"),
            ),
        )
        self.assertTrue(bell.actual_below)
        self.assertFalse(bell.marks_close)

    def test_marks_close_together_lean_their_labels_apart(self) -> None:
        bell = build_crit_bell(team_totals(1_005_000))

        self.assertTrue(bell.marks_close)
        self.assertFalse(bell.actual_below)

    def test_a_far_run_widens_the_axis_to_keep_it_inside(self) -> None:
        bell = build_crit_bell(team_totals(1_100_000))

        self.assertLess(bell.actual_left, 100)
        self.assertLess(bell.expected_left, 50)

    def test_no_spread_draws_no_bell(self) -> None:
        self.assertIsNone(build_crit_bell(team_totals(1_000_000, spread=0)))


class CritToolTextTests(unittest.TestCase):
    def service(self, **battles) -> ToolService:
        self.renderer = FakeRenderer()
        matchers = MatcherCache()
        return ToolService(
            client=None,
            data=FakeData(
                battles={
                    battle_id: parse_battle_detail(payload)
                    for battle_id, payload in battles.items()
                }
            ),
            renderer=lambda: self.renderer,
            board_matcher=lambda cards: matchers.matcher_for(
                cards, AliasConfig.empty()
            ),
            settings=PluginSettings(web_base_url=WEB),
            logger=logging.getLogger("test"),
        )

    def battle_text(self, payload: dict) -> str:
        service = self.service(btl_upload_abcdef123456=payload)
        return asyncio.run(service.battle("btl_upload_abcdef123456")).text

    def test_the_battle_answer_names_actual_expected_and_the_deviation(self) -> None:
        text = self.battle_text(crit_battle_payload())

        self.assertIn("暴击期望：实际 2,292,905 / 期望 2,012,905（偏差 +13.91%）", text)
        self.assertNotIn("已覆盖", text)

    def test_partial_coverage_is_stated_on_the_line(self) -> None:
        text = self.battle_text(crit_battle_payload(cover_last=False))

        # 2,227,572 of 2,292,905 is 97.15%, rounded down so it never reads 100%.
        self.assertIn(
            "暴击期望：实际 2,227,572 / 期望 1,947,572"
            "（偏差 +14.38%，已覆盖 97.1% 伤害）",
            text,
        )

    def test_an_older_upload_gets_no_line(self) -> None:
        self.assertNotIn("暴击期望", self.battle_text(battle_detail_payload()))

    def test_the_card_is_drawn_with_the_same_result(self) -> None:
        self.battle_text(crit_battle_payload())

        crit = self.renderer.kwargs["battle"]["crit"]
        self.assertEqual(crit.team.actual_damage, 2_292_905)
        self.assertAlmostEqual(crit.team.expected_damage, 2_012_905)

    def test_a_comparison_adds_nothing(self) -> None:
        second = second_battle_payload()
        for event in second["timelineEvents"]:
            event["hitContext"] = verified(isCritical=False, critRate=0.0)
        service = self.service(
            btl_upload_abcdef123456=crit_battle_payload(),
            btl_upload_bbbbbbbbbbbb=second,
        )

        answer = asyncio.run(
            service.battle(
                "btl_upload_abcdef123456", compare_with="btl_upload_bbbbbbbbbbbb"
            )
        )

        self.assertNotIn("暴击期望", answer.text)
        self.assertNotIn("crit", self.renderer.kwargs["compare"])


if __name__ == "__main__":
    unittest.main()
