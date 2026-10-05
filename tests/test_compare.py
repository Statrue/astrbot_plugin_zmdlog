"""Tests for the 0.9.0 战报对比 page: two battles side by side."""

import copy
import re
import unittest
from pathlib import Path

from core.candidates import CandidateStore, CandidateView, format_candidates
from core.matcher import MatchChoice, MatchLevel, MatchTarget, TargetType
from core.models import parse_battle_detail
from core.presentation import build_battle_build_page, build_compare_page, side_names
from core.render import WIDE_FRAME, TemplateRenderer, page_frame
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
        for payload in ("对比", "对比 罗丹 2 2", "对比 罗丹 0", "对比 罗丹 --页 3"):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)

    def test_me_stands_for_the_askers_record_against_one_rank(self) -> None:
        route = parse_zmdlog_payload("对比 罗丹 我")
        self.assertEqual(route.kind, RouteKind.COMPARE_QUERY)
        self.assertTrue(route.compare_self)
        self.assertEqual((route.query, route.compare_rank), ("罗丹", 1))
        route = parse_zmdlog_payload("对比 危境再现 罗丹 我 第3名 --口径 rDPS")
        self.assertEqual(
            (route.query, route.compare_rank, route.metric, route.compare_self),
            ("危境再现 罗丹", 3, "rdps", True),
        )
        self.assertFalse(parse_zmdlog_payload("对比 罗丹 3").compare_self)
        for payload in ("对比 我", "对比 我 3", "对比 罗丹 我 0", "对比 罗丹 我 1 2"):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)

    def test_ranks_on_a_board_take_the_metric_and_references_do_not(self) -> None:
        route = parse_zmdlog_payload("对比 罗丹 1 3 --口径 rdps")
        self.assertEqual((route.battle_rank, route.compare_rank), (1, 3))
        self.assertEqual(route.metric, "rdps")
        battle = parse_zmdlog_payload("战报 罗丹 2 --口径 rdps")
        self.assertEqual(
            (battle.kind, battle.query, battle.battle_rank, battle.metric),
            (RouteKind.BATTLE_QUERY, "罗丹", 2, "rdps"),
        )
        self.assertEqual(parse_zmdlog_payload("养成 罗丹 --口径 rdps").metric, "rdps")
        # A battle named outright is on both boards or neither; no metric.
        for payload in (
            "对比 btl_upload_aaaaaaaaaaaa btl_upload_bbbbbbbbbbbb --口径 rdps",
            "战报 btl_upload_aaaaaaaaaaaa --口径 rdps",
            "排轴 https://zmdlogs.com/battle/btl_upload_aaaaaaaaaaaa --口径 rdps",
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RouteParseError, "--口径"):
                    parse_zmdlog_payload(payload)

    def test_pick_list_names_the_compare_view_and_keeps_both_ranks(self) -> None:
        choice = MatchChoice(
            target=MatchTarget(
                target_type=TargetType.BOARD,
                key="a",
                name="榜单甲",
                dungeon_names=("副本",),
                boss_slugs=("a",),
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




def named(payload: dict, uploader: str, *, end_at: str | None = None) -> dict:
    """``payload`` uploaded by ``uploader``, fought at ``end_at`` if given."""

    payload["battle"]["uploaderNickname"] = uploader
    if end_at is not None:
        payload["battle"]["battleEndAt"] = end_at
    return payload


def build(page, name):
    return next(card for card in page.builds if card.character_name == name)


def lines(card):
    return {line.label: line for line in card.lines}


class ComparePageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first = parse_battle_detail(named(battle_detail_payload(), "shiki"))
        self.second = parse_battle_detail(named(second_battle_payload(), "KevNe"))
        self.page = build_compare_page(
            self.first,
            self.second,
            query="对比 罗丹 1 2",
            web_base_url=WEB,
            rank_a=1,
            rank_b=2,
        )

    def test_each_side_is_called_by_its_uploader_never_a_or_b(self) -> None:
        page = self.page
        self.assertEqual(page.header.target_type, "战报对比")
        self.assertEqual([side.name for side in page.sides], ["shiki", "KevNe"])
        self.assertEqual([side.headline for side in page.sides], ["shiki", "KevNe"])
        self.assertEqual(page.sides[0].rank_label, "第 1 名")
        self.assertEqual(page.sides[1].battle_id, "btl_upload_bbbbbbbbbbbb")
        # The gaps name the side that is ahead.
        self.assertEqual(page.time.better, "b")
        self.assertEqual(page.time.delta_label, "KevNe 快 2.83 秒")
        facts = {fact.label: fact for fact in page.facts}
        self.assertEqual(list(facts), ["总 DPS", "总伤害", "主 C", "主 C DPS"])
        self.assertEqual(facts["总 DPS"].better, "b")
        self.assertEqual(facts["总 DPS"].delta_label, "KevNe 高 26.2%")
        # Whole, as the battle's own 摘要 prints them.
        self.assertEqual(
            (facts["总 DPS"].a, facts["总 DPS"].b), ("110,061", "138,889")
        )
        self.assertEqual(facts["总伤害"].better, "b")
        self.assertEqual(facts["主 C"].a, "洛茜 · 88.4%")
        self.assertEqual(facts["主 C"].delta_label, "同一主 C")
        self.assertEqual(facts["主 C DPS"].delta_label, "相同")
        self.assertEqual(facts["主 C DPS"].better, "")

    def test_one_uploader_twice_is_told_apart(self) -> None:
        # By the rank each was picked by; the strip keeps the bare name,
        # since it prints the ranks beside it.
        same = parse_battle_detail(battle_detail_payload())
        again = parse_battle_detail(second_battle_payload())
        page = build_compare_page(
            same, again, query="q", web_base_url=WEB, rank_a=1, rank_b=2
        )
        self.assertEqual(
            [side.name for side in page.sides],
            ["测试账号（第 1 名）", "测试账号（第 2 名）"],
        )
        self.assertEqual([side.headline for side in page.sides], ["测试账号"] * 2)
        self.assertEqual(page.time.delta_label, "测试账号（第 2 名） 快 2.83 秒")
        self.assertEqual(
            side_names(same, again, rank_a=1, rank_b=4, metric="rdps"),
            ("测试账号（rDPS 第 1 名）", "测试账号（rDPS 第 4 名）"),
        )
        # Named outright: by when each was fought, else by its place.
        later = parse_battle_detail(
            named(
                second_battle_payload(),
                "测试账号",
                end_at="2026-07-14T09:05:00+08:00",
            )
        )
        page = build_compare_page(same, later, query="q", web_base_url=WEB)
        self.assertEqual(
            [side.name for side in page.sides],
            ["测试账号（07-13 22:00）", "测试账号（07-14 09:05）"],
        )
        self.assertEqual(page.sides[1].headline, "测试账号（07-14 09:05）")
        self.assertEqual(side_names(same, again), ("测试账号（左）", "测试账号（右）"))

    def test_ranks_off_the_rdps_board_say_so(self) -> None:
        # DPS by default, rDPS on request, never mixed: an rDPS rank must
        # not read as the DPS board's.
        page = build_compare_page(
            self.first,
            self.second,
            query="对比 罗丹 我 --口径 rdps",
            web_base_url=WEB,
            rank_a=1,
            rank_b=4,
            metric="rdps",
        )

        self.assertEqual(
            [side.rank_label for side in page.sides], ["rDPS 第 1 名", "rDPS 第 4 名"]
        )

    def test_the_strip_shows_each_team_main_c_first(self) -> None:
        self.assertEqual(
            [[face.character_name for face in side.team] for side in self.page.sides],
            [["洛茜", "卡缪"], ["洛茜", "黎风"]],
        )

    def test_dps_rows_are_the_union_in_the_first_battles_order(self) -> None:
        rows = {row.character_name: row for row in self.page.roster}
        self.assertEqual(list(rows), ["洛茜", "卡缪", "黎风"])
        self.assertEqual([row.lead for row in rows.values()], [True, False, False])
        self.assertIsNone(rows["卡缪"].b)
        self.assertIsNone(rows["黎风"].a)
        # Both sides' bars on one scale: the highest DPS of either fills it.
        self.assertEqual(rows["洛茜"].a.bar_percent, 100.0)
        self.assertEqual(rows["卡缪"].a.dps, "12,736")
        self.assertEqual(rows["卡缪"].a.bar_percent, 13.09)
        self.assertEqual(rows["黎风"].b.damage_share, "10.6%")

    def test_curves_share_one_axis_and_the_shorter_stops_early(self) -> None:
        curve = self.page.curve
        self.assertIsNotNone(curve)
        self.assertEqual(curve.end_a, 100.0)
        self.assertEqual(curve.end_b, 86.4)
        self.assertEqual(curve.ticks[0].label, "0s")
        self.assertIn(" ", curve.polyline_b)
        self.assertNotIn(".", curve.dps_a)

    def test_build_cards_follow_the_first_battles_build_order(self) -> None:
        # The first battle keeps the order its own 养成 page shows; the
        # second's characters the first did not field come after.
        own = build_battle_build_page(self.first, query="q", web_base_url=WEB)
        self.assertEqual(
            [card.character_name for card in self.page.builds],
            [*(item.character_name for item in own.characters), "黎风"],
        )
        luoxi = build(self.page, "洛茜")
        self.assertEqual(luoxi.only, "")
        self.assertEqual(luoxi.note, "1 项不同")
        self.assertEqual([line.label for line in luoxi.differences], ["武器"])
        weapon = lines(luoxi)["武器"]
        self.assertEqual(
            (weapon.a.name, weapon.b.name), ("宏愿 · 精炼 3", "宏愿 · 精炼 5")
        )
        self.assertIsNone(weapon.a.sub)
        self.assertTrue(weapon.a.icon_url.endswith("/wpn_sword_0021.png"))
        self.assertIn(("技能等级", "12 / 12 / 9 / 12"), luoxi.shared)
        self.assertIn(("护手", "点剑 · 护手"), luoxi.shared)
        # A character one side sat out is that side's alone, by name.
        kamiu = build(self.page, "卡缪")
        self.assertEqual((kamiu.only, kamiu.note), ("a", "仅 shiki 上场"))
        self.assertEqual(kamiu.differences, ())
        self.assertIsNone(lines(kamiu)["武器"].b)
        self.assertIsNone(kamiu.sources_b)
        self.assertEqual(kamiu.sources_a[0].share, "75.5%")
        self.assertEqual(build(self.page, "黎风").note, "仅 KevNe 上场")

    def test_a_build_alike_on_both_sides_says_so(self) -> None:
        page = build_compare_page(self.first, self.first, query="q", web_base_url=WEB)

        self.assertEqual(build(page, "洛茜").note, "养成一致")
        self.assertEqual(build(page, "洛茜").differences, ())

    def test_a_weapon_that_reads_the_same_says_which_levels_differ(self) -> None:
        # The 词条 and the 武器技能 are 养成's split; a level of one is a
        # difference even where the name and the 精炼 agree.
        payload = named(second_battle_payload(), "KevNe")
        for entry in payload["battle"]["roster"]:
            if entry["characterName"] == "洛茜":
                entry["weapon"]["weaponRefine"] = 3
                entry["weapon"]["skills"][2]["level"] = 5
        page = build_compare_page(
            self.first, parse_battle_detail(payload), query="q", web_base_url=WEB
        )

        weapon = lines(build(page, "洛茜"))["武器"]
        self.assertTrue(weapon.differs)
        self.assertEqual(weapon.a.name, "宏愿")
        self.assertEqual(weapon.a.sub, "词条 9/7 · 技能 9")
        self.assertEqual(weapon.b.sub, "词条 9/5 · 技能 9")

    def test_skill_levels_mark_the_slots_that_differ(self) -> None:
        payload = named(second_battle_payload(), "KevNe")
        for entry in payload["battle"]["roster"]:
            if entry["characterName"] == "洛茜":
                for skill in entry["skills"]:
                    if skill["level"] == 9:
                        skill["level"] = 10
        page = build_compare_page(
            self.first, parse_battle_detail(payload), query="q", web_base_url=WEB
        )

        levels = lines(build(page, "洛茜"))["技能等级"]
        self.assertTrue(levels.differs)
        self.assertEqual(
            [(slot.label, slot.level, slot.differs) for slot in levels.b.levels],
            [
                ("普攻", "12", False),
                ("战技", "12", False),
                ("连携", "10", True),
                ("终结", "12", False),
            ],
        )

    def test_identical_gear_is_not_flagged_when_upstream_renames_it(self) -> None:
        # Upstream's suitName is not a property of the item: the same item id
        # comes back under different suits in different battles. Comparing the
        # rendered label called identical gear different on every line.
        payload = second_battle_payload()
        for entry in payload["battle"]["roster"]:
            for equip in entry["equips"]:
                equip["suitName"] = None
                equip["pieceName"] = equip["itemId"]
        page = build_compare_page(
            self.first, parse_battle_detail(payload), query="q", web_base_url=WEB
        )
        gear = lines(build(page, "洛茜"))

        for label in ("护手", "护甲", "配件 1", "配件 2"):
            with self.subTest(label=label):
                self.assertFalse(gear[label].differs)
        # The page still prints what each side's upstream actually said.
        self.assertEqual(gear["护手"].a.text, "点剑 · 护手")
        self.assertEqual(gear["护手"].b.text, "护手（未收录）")

    def test_gear_that_reads_the_same_names_the_piece_and_its_enhancement(
        self,
    ) -> None:
        payload = second_battle_payload()
        for entry in payload["battle"]["roster"]:
            if entry["characterName"] == "洛茜":
                gloves = entry["equips"][0]
                gloves["itemId"] = "item_equip_t4_suit_phy01_hand_02"
                gloves["pieceName"] = "点剑护手·壹型"
                gloves["enhanceLevels"] = [{"index": 1, "level": 1}]
        page = build_compare_page(
            self.first, parse_battle_detail(payload), query="q", web_base_url=WEB
        )

        gloves = lines(build(page, "洛茜"))["护手"]
        self.assertTrue(gloves.differs)
        self.assertEqual((gloves.a.text, gloves.b.text), ("点剑 · 护手",) * 2)
        self.assertEqual((gloves.a.name, gloves.a.sub), ("点剑护手", "+3 / +3 / +2"))
        self.assertEqual((gloves.b.name, gloves.b.sub), ("点剑护手·壹型", "+1"))

    def test_the_same_two_accessories_in_either_order_compare_equal(self) -> None:
        # Both accessory slots hold the same kind of piece, and one player's
        # pair sits in slots 2/3 while another's sits in 3/2. Numbering the
        # lines by slot made a swapped pair read as two changes.
        payload = second_battle_payload()
        for entry in payload["battle"]["roster"]:
            accessories = [e for e in entry["equips"] if e["partName"] == "配件"]
            if len(accessories) != 2:
                continue
            accessories[0]["itemId"] = "item_equip_t4_suit_heal01_edc_03"
            accessories[0]["pieceName"] = "生物辅助护板"
            accessories[0]["suitName"] = "生物辅助"
        swapped = copy.deepcopy(payload)
        for entry in swapped["battle"]["roster"]:
            accessories = [e for e in entry["equips"] if e["partName"] == "配件"]
            if len(accessories) != 2:
                continue
            first, second = accessories
            first["slot"], second["slot"] = second["slot"], first["slot"]
        page = build_compare_page(
            parse_battle_detail(payload),
            parse_battle_detail(swapped),
            query="q",
            web_base_url=WEB,
        )
        gear = lines(build(page, "洛茜"))
        # The second is matched to the first rather than both being sorted,
        # so the first column still reads in the order the 养成 page shows
        # for that battle.
        own = build_battle_build_page(
            parse_battle_detail(payload), query="q", web_base_url=WEB
        )
        view = next(item for item in own.characters if item.character_name == "洛茜")
        self.assertEqual(
            [gear["配件 1"].a.text, gear["配件 2"].a.text],
            ["生物辅助 · 配件", "点剑 · 配件"],
        )
        self.assertEqual(
            [tile.label for tile in view.gear if tile and tile.part_name == "配件"],
            ["生物辅助护板", "点剑火石"],
        )
        self.assertEqual(gear["配件 1"].a.text, gear["配件 1"].b.text)
        self.assertEqual(gear["配件 2"].a.text, gear["配件 2"].b.text)
        for card in page.builds:
            with self.subTest(character=card.character_name):
                self.assertEqual(card.differences, ())

    def test_a_real_gear_change_is_still_flagged(self) -> None:
        payload = second_battle_payload()
        for entry in payload["battle"]["roster"]:
            if entry["characterName"] == "洛茜":
                entry["equips"][0]["itemId"] = "item_equip_t4_suit_heal01_hand_03"
                entry["equips"][0]["pieceName"] = "生物辅助手甲"
                entry["equips"][0]["suitName"] = "生物辅助"
        page = build_compare_page(
            self.first, parse_battle_detail(payload), query="q", web_base_url=WEB
        )
        gear = lines(build(page, "洛茜"))

        self.assertTrue(gear["护手"].differs)
        # Read differently, so each side prints its own short form.
        self.assertEqual(gear["护手"].b.name, "生物辅助 · 护手")
        self.assertIsNone(gear["护手"].b.sub)
        self.assertFalse(gear["护甲"].differs)

    def test_page_is_titled_after_the_shared_boss(self) -> None:
        page = build_compare_page(self.first, self.second, query="q", web_base_url=WEB)
        self.assertEqual(page.header.title, "“碾骨之拳”罗丹")
        self.assertEqual(page.header.subtitle, "危境再现·罗丹")
        self.assertIsNone(page.sides[0].rank_label)


def visible_text(html: str) -> str:
    """What a reader sees: the body's text, without its tags or drawings."""

    body = html[html.index("<body>") :]
    body = re.sub(r"<svg.*?</svg>", " ", body, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


class CompareTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])
        self.first = parse_battle_detail(named(battle_detail_payload(), "shiki"))
        self.second = parse_battle_detail(named(second_battle_payload(), "KevNe"))

    def render(self, **options) -> str:
        return self.renderer.render_compare(
            self.first, self.second, query="对比 罗丹 1 2", web_base_url=WEB, **options
        )

    def test_the_comparison_is_a_wide_page_on_the_new_shell(self) -> None:
        html = self.render(rank_a=1, rank_b=2)

        self.assertEqual(page_frame("compare"), WIDE_FRAME)
        self.assertIn("--zmd-frame-width: 960;", html)
        self.assertIn('id="zmd-root"', html)
        self.assertIn('class="zmd-main', html)
        self.assertIn(">VERSUS<", html)

    def test_the_page_draws_every_section_by_the_uploaders_names(self) -> None:
        html = self.render(rank_a=1, rank_b=2)
        text = visible_text(html)

        for section in ("对比摘要", "阵容与 DPS", "DPS 曲线"):
            self.assertIn(f"<strong>{section}</strong>", html)
        self.assertIn("shiki VS KevNe", text)
        self.assertIn("KevNe 快 2.83 秒", text)
        self.assertIn("第 1 名", text)
        self.assertIn("仅 shiki 上场", text)
        self.assertIn("仅 KevNe 上场", text)
        self.assertIn("未上场", text)
        self.assertIn("1 项不同", text)
        self.assertIn('class="b-line v-line is-a"', html)
        # The differing weapon on both sides, its icon beside it.
        self.assertIn("宏愿 · 精炼 3", text)
        self.assertIn("宏愿 · 精炼 5", text)
        self.assertIn('class="v-gear-icon"', html)
        # Never A / B: not on a tag, not in a gap, not over a column.
        self.assertIsNone(re.search(r"(?<![A-Za-z])[AB](?![A-Za-z])", text), text)

    def test_one_uploader_twice_reads_as_two_people(self) -> None:
        self.first = parse_battle_detail(battle_detail_payload())
        self.second = parse_battle_detail(second_battle_payload())
        text = visible_text(self.render(rank_a=1, rank_b=2))

        self.assertIn("测试账号（第 2 名） 快 2.83 秒", text)
        self.assertIn("仅 测试账号（第 1 名） 上场", text)

    def test_every_colour_on_the_page_is_a_token(self) -> None:
        css = (
            Path(__file__).parents[1] / "resources" / "compare" / "compare.css"
        ).read_text(encoding="utf-8")

        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css), [])


if __name__ == "__main__":
    unittest.main()
