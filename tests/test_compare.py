"""Tests for the 0.9.0 战报对比 page: two battles side by side."""

import copy
import unittest
from pathlib import Path

from core.candidates import CandidateStore, CandidateView, format_candidates
from core.matcher import MatchChoice, MatchLevel, MatchTarget, TargetType
from core.models import parse_battle_detail
from core.presentation import build_compare_page
from core.render import TemplateRenderer
from core.routing import RouteKind, RouteParseError, parse_zmdlog_payload
from tests.helpers import battle_detail_payload

WEB = "https://zmdlogs.com"


def second_battle_payload() -> dict:
    """The fixture fight, replayed faster by a slightly different team."""

    payload = copy.deepcopy(battle_detail_payload())
    battle = payload["battle"]
    battle["id"] = "btl_upload_bbbbbbbbbbbb"
    battle["durationMs"] = 18_000
    battle["totalDamage"] = 2_500_000
    battle["totalDps"] = 138_888.89
    # 卡缪 sits out; 黎风 takes the slot with the same gear.
    for entry in battle["roster"]:
        if entry["characterName"] == "卡缪":
            entry["characterName"] = "黎风"
            entry["characterKey"] = "chr_0001_lifeng"
        elif entry["characterName"] == "洛茜" and entry.get("weapon"):
            entry["weapon"]["weaponRefine"] = 5
    for participant in payload["participants"]:
        if participant["characterName"] == "卡缪":
            participant["characterName"] = "黎风"
            participant["characterKey"] = "chr_0001_lifeng"
    for event in payload.get("timelineEvents", []):
        if event.get("sourceCharacterName") == "卡缪":
            event["sourceCharacterName"] = "黎风"
    return payload


class CompareRoutingTests(unittest.TestCase):
    def test_keyword_forms_read_ranks_off_the_tail(self) -> None:
        route = parse_zmdlog_payload("对比 罗丹")
        self.assertEqual(route.kind, RouteKind.COMPARE_QUERY)
        self.assertEqual(
            (route.query, route.battle_rank, route.compare_rank), ("罗丹", 1, 2)
        )
        self.assertIsNone(route.compare_target)

        route = parse_zmdlog_payload("比较 罗丹 3")
        self.assertEqual((route.battle_rank, route.compare_rank), (1, 3))
        route = parse_zmdlog_payload("对比 危境再现 罗丹 2 第5名")
        self.assertEqual(
            (route.query, route.battle_rank, route.compare_rank),
            ("危境再现 罗丹", 2, 5),
        )

    def test_two_references_skip_the_board(self) -> None:
        route = parse_zmdlog_payload(
            "对比 btl_upload_aaaaaaaaaaaa https://zmdlogs.com/battle/btl_upload_bbbbbbbbbbbb"
        )
        self.assertEqual(route.kind, RouteKind.COMPARE_QUERY)
        self.assertEqual(route.query, "btl_upload_aaaaaaaaaaaa")
        self.assertEqual(
            route.compare_target, "https://zmdlogs.com/battle/btl_upload_bbbbbbbbbbbb"
        )
        self.assertIsNone(route.compare_rank)

    def test_malformed_forms_are_rejected_with_usage(self) -> None:
        for payload in ("对比", "对比 罗丹 2 2", "对比 罗丹 0", "对比 罗丹 --top 3"):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)

    def test_pick_list_names_the_compare_view_and_keeps_both_ranks(self) -> None:
        choice = MatchChoice(
            target=MatchTarget(
                target_type=TargetType.BOARD,
                key="a",
                name="榜单甲",
                dungeon_names=("副本",),
                boss_slugs=("a",),
                query_text="榜",
            ),
            level=MatchLevel.NORMALIZED_EXACT,
            score=1.0,
            matched_text="榜",
        )
        store = CandidateStore()

        entry = store.remember(
            "榜",
            (choice, choice),
            view=CandidateView.COMPARE,
            battle_rank=2,
            compare_rank=5,
        )

        self.assertIn("战报对比匹配到 2 个榜单", format_candidates(entry))
        self.assertEqual((entry.battle_rank, entry.compare_rank), (2, 5))


class ComparePageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first = parse_battle_detail(battle_detail_payload())
        self.second = parse_battle_detail(second_battle_payload())
        self.page = build_compare_page(
            self.first,
            self.second,
            query="对比 罗丹 1 2",
            web_base_url=WEB,
            rank_a=1,
            rank_b=2,
        )

    def test_facts_mark_the_better_side(self) -> None:
        page = self.page
        self.assertEqual(page.header.target_type, "战报对比")
        self.assertEqual([side.label for side in page.sides], ["A", "B"])
        self.assertEqual(page.sides[0].rank_label, "第 1 名")
        self.assertEqual(page.sides[1].battle_id, "btl_upload_bbbbbbbbbbbb")
        facts = {fact.label: fact for fact in page.facts}
        self.assertEqual(facts["通关时间"].better, "b")
        self.assertEqual(facts["通关时间"].delta_label, "B 快 2.83 秒")
        self.assertEqual(facts["总 DPS"].better, "b")
        self.assertTrue(facts["总 DPS"].delta_label.startswith("B 高 "))
        self.assertEqual(facts["总伤害"].better, "b")
        self.assertEqual(facts["主 C"].delta_label, "同一主 C")
        self.assertEqual(facts["主 C DPS"].delta_label, "相同")
        self.assertEqual(facts["主 C DPS"].better, "")

    def test_roster_is_the_union_in_a_order(self) -> None:
        rows = {row.character_name: row for row in self.page.roster}
        self.assertEqual(
            [row.character_name for row in self.page.roster], ["洛茜", "卡缪", "黎风"]
        )
        self.assertIsNotNone(rows["洛茜"].a)
        self.assertIsNotNone(rows["洛茜"].b)
        self.assertIsNone(rows["卡缪"].b)
        self.assertIsNone(rows["黎风"].a)

    def test_curves_share_one_axis_and_the_shorter_stops_early(self) -> None:
        curve = self.page.curve
        self.assertIsNotNone(curve)
        self.assertEqual(curve.end_a, 100.0)
        self.assertEqual(curve.end_b, 86.4)
        self.assertEqual(curve.ticks[0].label, "0s")
        self.assertTrue(curve.polyline_a.endswith(",") is False)
        self.assertIn(" ", curve.polyline_b)

    def test_gear_lines_flag_only_real_differences(self) -> None:
        rows = {row.character_name: row for row in self.page.loadouts}
        luoxi = rows["洛茜"]
        self.assertEqual(luoxi.only, "")
        lines = {line.label: line for line in luoxi.lines}
        self.assertTrue(lines["武器"].differs)
        self.assertIn("精炼 3", lines["武器"].a)
        self.assertIn("精炼 5", lines["武器"].b)
        self.assertFalse(lines["护手"].differs)
        self.assertFalse(lines["技能等级"].differs)
        self.assertEqual(rows["卡缪"].only, "a")
        self.assertEqual(rows["黎风"].only, "b")
        kamiu_lines = {line.label: line for line in rows["卡缪"].lines}
        self.assertEqual(kamiu_lines["武器"].b, "—")
        self.assertFalse(kamiu_lines["武器"].differs)
        self.assertEqual(rows["卡缪"].skills_b, ())

    def test_page_is_titled_after_the_shared_boss(self) -> None:
        page = build_compare_page(self.first, self.second, query="q", web_base_url=WEB)
        self.assertEqual(page.header.title, "“碾骨之拳”罗丹")
        self.assertEqual(page.header.subtitle, "危境再现·罗丹")
        self.assertIsNone(page.sides[0].rank_label)


class CompareTemplateTests(unittest.TestCase):
    def test_compare_page_renders(self) -> None:
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])
        first = parse_battle_detail(battle_detail_payload())
        second = parse_battle_detail(second_battle_payload())

        html = renderer.render_compare(
            first, second, query="对比 罗丹 1 2", web_base_url=WEB, rank_a=1, rank_b=2
        )

        self.assertIn("<h2>对比摘要</h2>", html)
        self.assertIn("B 快 2.83 秒", html)
        self.assertIn('class="cmp-side is-a"', html)
        self.assertIn("第 1 名 · ", html)
        self.assertIn("仅 A 上场", html)
        self.assertIn("仅 B 上场", html)
        self.assertIn('class="curve-line cmp-line-a"', html)
        self.assertIn('class="cmp-gear-line is-diff"', html)
        self.assertIn("未上场", html)
        self.assertIn("btl_upload_bbbbbbbbbbbb", html)


if __name__ == "__main__":
    unittest.main()
