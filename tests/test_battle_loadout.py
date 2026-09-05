"""Tests for the 0.6.0 battle loadout (配装) and skill statistics (技能) pages."""

import unittest
from pathlib import Path

from core.candidates import CandidateStore, CandidateView, format_candidates
from core.loadout import (
    SkillCategory,
    group_skill_damage,
    is_raw_item_name,
    skill_category,
    skill_display_name,
    skill_level_summary,
    suit_catalog_id,
    weapon_skill_levels,
)
from core.matcher import MatchChoice, MatchLevel, MatchTarget, TargetType
from core.models import (
    BattleSkillStat,
    ModelValidationError,
    parse_battle_detail,
)
from core.presentation import (
    _format_stat_value,
    build_battle_page,
    build_loadout_page,
    build_skill_page,
)
from core.render import TemplateRenderer
from core.routing import RouteKind, RouteParseError, parse_zmdlog_payload
from tests.helpers import battle_detail_payload


class BattleDetailModelTests(unittest.TestCase):
    def test_roster_loadout_and_skill_stats_are_parsed(self) -> None:
        battle = parse_battle_detail(battle_detail_payload())

        self.assertEqual(len(battle.roster), 2)
        first = battle.roster[0]
        self.assertEqual(first.slot, 1)
        self.assertEqual(first.character_element, "fire")
        self.assertEqual(first.character_level, 90)
        self.assertEqual(first.character_potential, 5)
        self.assertEqual(first.weapon.name, "宏愿")
        self.assertEqual(first.weapon.refine, 3)
        self.assertEqual(len(first.weapon.skills), 3)
        self.assertEqual(first.weapon.skills[0].potential_level, 5)
        self.assertEqual(len(first.equips), 4)
        self.assertEqual(first.equips[0].enhance_levels, ((1, 3), (2, 3), (3, 2)))
        self.assertEqual(
            [stat.name for stat in first.equips[0].stats],
            ["防御力", "力量", "物理伤害提升", "Main"],
        )
        self.assertEqual(first.equips[0].stats[0].slot, "main")
        self.assertIsNone(first.equips[0].stats[0].level)
        self.assertEqual(len(first.skills), 8)
        self.assertEqual(len(battle.skill_stats), 9)
        self.assertEqual(
            battle.skill_stats[0].skill_key, "chr_0028_wulfa_ultimate_skill"
        )
        self.assertEqual(battle.skill_stats[0].avg_damage, 600_000.0)

    def test_untyped_gear_lists_drop_junk_instead_of_failing(self) -> None:
        battle = parse_battle_detail(battle_detail_payload())
        body = battle.roster[0].equips[1]

        # A string value, an empty name and a bare string are all skipped.
        self.assertEqual(body.enhance_levels, ((1, 3),))
        self.assertEqual(body.stats, ())

    def test_typed_roster_fields_stay_strict(self) -> None:
        payload = battle_detail_payload()
        payload["battle"]["roster"][0]["slot"] = "1"
        with self.assertRaises(ModelValidationError):
            parse_battle_detail(payload)

        payload = battle_detail_payload()
        payload["roleSkillStats"][0]["castCount"] = -1
        with self.assertRaises(ModelValidationError):
            parse_battle_detail(payload)

        payload = battle_detail_payload()
        payload["battle"]["roster"][0]["weapon"]["weaponName"] = None
        with self.assertRaises(ModelValidationError):
            parse_battle_detail(payload)

    def test_older_payloads_without_loadout_data_still_parse(self) -> None:
        payload = battle_detail_payload()
        del payload["roleSkillStats"]
        payload["battle"]["roster"] = [
            {"slot": 1, "characterName": "洛茜", "accountDisplayName": "测试账号"}
        ]

        battle = parse_battle_detail(payload)

        self.assertEqual(battle.skill_stats, ())
        self.assertIsNone(battle.roster[0].weapon)
        self.assertEqual(battle.roster[0].equips, ())
        self.assertEqual(battle.uploader_display_name, "测试账号")


class SkillNamingTests(unittest.TestCase):
    def test_display_names_follow_the_site_rules(self) -> None:
        self.assertEqual(
            skill_display_name(
                "cryst triggered physical break",
                "buff_common_cryst_triggered_physical_break",
            ),
            "寒冷击破触发",
        )
        self.assertEqual(
            skill_display_name("whatever", "chr_0031_kamiu_combo_skill"), "连携技"
        )
        # Raw keys lose the character prefix and read as words.
        raw_key = "chr_0028_wulfa_skill_3090"
        self.assertEqual(skill_display_name(raw_key, raw_key), "skill 3090")
        self.assertEqual(
            skill_display_name("绯红刃舞", "chr_0028_wulfa_attack1"), "绯红刃舞"
        )
        self.assertEqual(
            skill_display_name("burning status", "buff_x"), "burning status"
        )
        self.assertEqual(skill_display_name("", "buff_x"), "buff_x")
        self.assertEqual(skill_display_name("  ", None), "未命名技能")
        self.assertEqual(
            skill_display_name(
                "ultimate / skill / 派生", "chr_0035_liino_ultimate_skill_projhit"
            ),
            "ultimate / skill / 派生",
        )

    def test_categories_mirror_the_site_and_bucket_engine_sources(self) -> None:
        cases = {
            ("终结技", "chr_x_ultimate_skill2"): SkillCategory.ULTIMATE,
            ("A5", "chr_x_attack5"): SkillCategory.NORMAL,
            ("绯红刃舞", "chr_x_attack1"): SkillCategory.NORMAL,
            ("噪点", "chr_x_power_attack"): SkillCategory.HEAVY,
            ("噪点", "chr_x_plunging_attack_end"): SkillCategory.HEAVY,
            ("战技", None): SkillCategory.SKILL,
            ("构成序列", "chr_x_normal_skill"): SkillCategory.SKILL,
            ("连携·潮汐", "chr_x_combo_skill"): SkillCategory.COMBO,
            ("燃烧", "buff_common_fire_natural_triggered"): SkillCategory.MECHANIC,
            ("套装", "buff_equipsuit_phy01"): SkillCategory.SUIT,
            ("武器", "buff_wpn_sword"): SkillCategory.WEAPON,
            ("skill 640", "chr_x_skill_640"): SkillCategory.OTHER,
            ("未知", None): SkillCategory.OTHER,
        }
        for (name, key), expected in cases.items():
            with self.subTest(name=name, key=key):
                self.assertIs(skill_category(name, key), expected)

    def test_normal_attack_chain_is_folded_into_one_row(self) -> None:
        battle = parse_battle_detail(battle_detail_payload())

        groups = group_skill_damage(battle.skill_stats)

        self.assertEqual([group.character_name for group in groups], ["洛茜", "卡缪"])
        luoxi = groups[0]
        self.assertEqual(luoxi.total_damage, 2_027_572)
        totals = [row.total_damage for row in luoxi.rows]
        self.assertEqual(totals, sorted(totals, reverse=True))
        self.assertEqual(luoxi.rows[0].name, "终结技")
        merged = next(row for row in luoxi.rows if row.merged_count > 1)
        self.assertEqual(merged.name, "普攻 · 绯红刃舞")
        self.assertIs(merged.category, SkillCategory.NORMAL)
        self.assertEqual(merged.cast_count, 48)
        self.assertEqual(merged.total_damage, 500_000)
        self.assertEqual(merged.max_damage, 20_000)
        self.assertEqual(merged.merged_count, 3)
        self.assertAlmostEqual(merged.avg_damage, 500_000 / 48)
        # Segments named A1/A2 have no shared real name to carry over.
        self.assertEqual(
            [row.name for row in groups[1].rows], ["连携技", "普攻（各段合并）"]
        )

    def test_same_name_rows_fold_together(self) -> None:
        stats = (
            BattleSkillStat("诀", "终结技", 1, 100, 100.0, 100, "chr_x_ultimate_skill"),
            BattleSkillStat("诀", "终结技", 1, 50, 50.0, 50, "chr_x_ultimate_skill2"),
            BattleSkillStat("诀", "战技", 2, 30, 15.0, 20, "chr_x_normal_skill"),
        )

        (group,) = group_skill_damage(stats)

        self.assertEqual(
            [
                (
                    row.name,
                    row.cast_count,
                    row.total_damage,
                    row.max_damage,
                    row.merged_count,
                )
                for row in group.rows
            ],
            [("终结技", 2, 150, 100, 2), ("战技", 2, 30, 20, 1)],
        )
        self.assertEqual(group.rows[0].avg_damage, 75.0)
        self.assertEqual(group_skill_damage(()), ())


class GearHelperTests(unittest.TestCase):
    def test_item_ids_resolve_to_the_catalog_suit_or_to_nothing(self) -> None:
        # ``_suit_`` pieces name the catalog entry to look up.
        self.assertEqual(
            suit_catalog_id("item_equip_t4_suit_fire_natr01_hand_04"),
            "suit_fire_natr01",
        )
        self.assertEqual(
            suit_catalog_id("item_equip_t4_suit_spellburst_hand_01"),
            "suit_spellburst",
        )
        self.assertEqual(
            suit_catalog_id("item_equip_t3_suit_usp01_edc_03"), "suit_usp01"
        )
        # A ``_parts_`` piece belongs to no suit, so there is nothing to look
        # up and no name to guess at.
        self.assertIsNone(suit_catalog_id("item_equip_t4_parts_wuling01_body_02"))
        self.assertIsNone(suit_catalog_id("something_else"))
        self.assertIsNone(suit_catalog_id(None))
        self.assertTrue(
            is_raw_item_name(
                "item_equip_t4_suit_phy01_body_02", "item_equip_t4_suit_phy01_body_02"
            )
        )
        self.assertTrue(is_raw_item_name("", None))
        self.assertFalse(
            is_raw_item_name("点剑护手", "item_equip_t4_suit_phy01_hand_01")
        )

    def test_skill_and_weapon_levels(self) -> None:
        battle = parse_battle_detail(battle_detail_payload())
        first, second = battle.roster

        # ``normal_skill_combo`` must not be mistaken for the 战技 entry.
        self.assertEqual(
            [(level.label, level.level) for level in skill_level_summary(first)],
            [("普攻", 12), ("战技", 12), ("连携", 9), ("终结", 12)],
        )
        self.assertEqual(weapon_skill_levels(first.weapon), (9, (9, 7)))
        self.assertEqual(skill_level_summary(second), ())
        self.assertEqual(weapon_skill_levels(second.weapon), (None, ()))


class LoadoutPresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.battle = parse_battle_detail(battle_detail_payload())

    def test_missing_icons_are_derived_from_ids_but_never_invented(self) -> None:
        payload = battle_detail_payload()
        roster = payload["battle"]["roster"][0]
        roster["weapon"]["iconUrl"] = None
        roster["equips"][0]["iconUrl"] = None
        roster["equips"][1]["itemId"] = None
        roster["equips"][2]["iconUrl"] = None
        roster["equips"][2]["itemId"] = "../not an id"
        roster["equips"][3]["iconUrl"] = "/images/equip/iconbig/explicit.png"

        page = build_loadout_page(
            parse_battle_detail(payload), query="q", web_base_url="https://zmdlogs.com"
        )
        luoxi = page.loadouts[0]

        self.assertEqual(
            luoxi.weapon.icon_url,
            "https://zmdlogs.com/images/weapon/icon/wpn_sword_0021.png",
        )
        hand, body, edc, other = luoxi.equips
        self.assertEqual(
            hand.icon_url,
            "https://zmdlogs.com/images/equip/iconbig/item_equip_t4_suit_phy01_hand_01.png",
        )
        self.assertIsNone(body.icon_url)
        self.assertIsNone(edc.icon_url)
        self.assertEqual(
            other.icon_url, "https://zmdlogs.com/images/equip/iconbig/explicit.png"
        )

    def test_loadout_page_formats_gear(self) -> None:
        page = build_loadout_page(
            self.battle, query="配装 罗丹", web_base_url="https://zmdlogs.com"
        )

        self.assertEqual(page.header.target_type, "战报配装")
        self.assertTrue(page.stat_lines_available)
        luoxi, kamiu = page.loadouts
        self.assertEqual(luoxi.level_label, "Lv.90")
        self.assertEqual(luoxi.potential_label, "潜能 5")
        self.assertEqual(luoxi.element, "灼热")
        self.assertEqual(luoxi.damage_share, "88.4%")
        self.assertEqual(luoxi.weapon.refine_label, "精炼 3")
        self.assertEqual(luoxi.weapon.level_label, "Lv.90")
        self.assertEqual(luoxi.weapon.skill_label, "武器技能 9 · 词条 9 / 7")
        self.assertEqual(
            luoxi.weapon.icon_url,
            "https://zmdlogs.com/images/weapon/icon/wpn_sword_0021.png",
        )
        hand, body, edc, _ = luoxi.equips
        self.assertEqual(hand.piece_label, "点剑护手")
        self.assertEqual(hand.suit_label, "点剑")
        self.assertEqual(hand.enhance_label, "强化 +3 / +3 / +2")
        self.assertEqual(
            [(stat.name, stat.value, stat.is_main) for stat in hand.stats],
            [
                ("防御力", "30", True),
                ("力量", "61", False),
                ("物理伤害提升", "14.9%", False),
                ("主能力", "26.9%", False),
            ],
        )
        self.assertEqual(hand.compact_label, "点剑 · 护手")
        # Upstream named neither the piece nor its suit, and nothing is
        # guessed from a sibling any more.
        self.assertEqual(body.piece_label, "名称未收录")
        self.assertIsNone(body.suit_label)
        self.assertEqual(body.compact_label, "护甲（未收录）")
        # Upstream gave no icon for the unknown piece; the path is derived.
        self.assertEqual(
            body.icon_url,
            "https://zmdlogs.com/images/equip/iconbig/item_equip_t4_suit_phy01_body_02.png",
        )
        self.assertEqual(body.enhance_label, "强化 +3")
        self.assertIsNone(edc.enhance_label)
        self.assertEqual(edc.stats, ())
        self.assertEqual(
            [(level.label, level.level) for level in luoxi.skill_levels],
            [("普攻", 12), ("战技", 12), ("连携", 9), ("终结", 12)],
        )
        self.assertEqual(
            [row.name for row in luoxi.top_skills],
            ["终结技", "普攻 · 绯红刃舞", "战技"],
        )
        self.assertEqual(kamiu.potential_label, "潜能 0")
        self.assertIsNone(kamiu.element)
        self.assertEqual(kamiu.equips, ())
        self.assertIsNone(kamiu.weapon.skill_label)
        self.assertIsNone(kamiu.weapon.level_label)
        self.assertEqual(kamiu.weapon.refine_label, "精炼 6")

    def test_the_catalog_names_a_suit_upstream_left_blank(self) -> None:
        # The same piece, once without the catalog and once with it. The
        # catalog is keyed by the id the item carries, so it answers for
        # every battle rather than only the ones a named sibling appears in.
        page = build_loadout_page(
            self.battle,
            query="配装 罗丹",
            web_base_url="https://zmdlogs.com",
            suits={"suit_phy01": "点剑"},
        )
        _, body, _, _ = page.loadouts[0].equips

        self.assertEqual(body.piece_label, "名称未收录")
        self.assertEqual(body.suit_label, "点剑")
        self.assertEqual(body.compact_label, "点剑 · 护甲")

    def test_the_catalog_overrules_what_the_upload_called_the_suit(self) -> None:
        # 险关 has been seen labelled 长息, the name of a different suit, in
        # a real upload. The catalog wins.
        payload = battle_detail_payload()
        equip = payload["battle"]["roster"][0]["equips"][0]
        equip["itemId"] = "item_equip_t4_suit_spellburst_hand_01"
        equip["suitName"] = "长息"
        page = build_loadout_page(
            parse_battle_detail(payload),
            query="q",
            web_base_url="https://zmdlogs.com",
            suits={"suit_spellburst": "险关"},
        )

        self.assertEqual(page.loadouts[0].equips[0].suit_label, "险关")

    def test_a_standalone_piece_keeps_what_the_upload_called_it(self) -> None:
        # ``_parts_`` pieces belong to no suit, so the catalog has nothing to
        # say and upstream's own label is all there is.
        payload = battle_detail_payload()
        equip = payload["battle"]["roster"][0]["equips"][0]
        equip["itemId"] = "item_equip_t4_parts_wuling01_hand_01"
        equip["suitName"] = "独立装备"
        page = build_loadout_page(
            parse_battle_detail(payload),
            query="q",
            web_base_url="https://zmdlogs.com",
            suits={"suit_wuling01": "不该被用到"},
        )

        self.assertEqual(page.loadouts[0].equips[0].suit_label, "独立装备")

    def test_skill_page_shares_and_ordering(self) -> None:
        page = build_skill_page(
            self.battle, query="技能 罗丹", web_base_url="https://zmdlogs.com"
        )

        self.assertEqual(page.header.target_type, "技能统计")
        self.assertTrue(page.has_merged_rows)
        luoxi, kamiu = page.groups
        self.assertEqual(luoxi.total_damage, "2,027,572")
        self.assertEqual(luoxi.team_share, "88.4%")
        self.assertEqual(luoxi.profession, "近卫")
        self.assertEqual(
            luoxi.character_avatar_url,
            "https://zmdlogs.com/images/character/luoxi.png",
        )
        top = luoxi.rows[0]
        self.assertEqual(
            (
                top.category,
                top.name,
                top.cast_count,
                top.total_damage,
                top.share,
                top.avg_damage,
                top.max_damage,
                top.merged,
            ),
            ("终结技", "终结技", 2, "1,200,000", "59.2%", "600,000", "700,000", False),
        )
        merged = next(row for row in luoxi.rows if row.merged)
        self.assertEqual(merged.name, "普攻 · 绯红刃舞")
        self.assertEqual(luoxi.hidden_count, 0)
        self.assertEqual(kamiu.rows[0].category, "连携")
        self.assertEqual(kamiu.team_share, "11.6%")

    def test_battle_card_carries_compact_loadouts(self) -> None:
        page = build_battle_page(
            self.battle, query="q", web_base_url="https://zmdlogs.com"
        )

        self.assertTrue(page.skill_stats_available)
        self.assertEqual(
            [load.character_name for load in page.loadouts], ["洛茜", "卡缪"]
        )
        self.assertEqual(len(page.loadouts[0].top_skills), 3)
        self.assertEqual(page.loadouts[1].top_skills[0].name, "连携技")

    def test_stat_values_read_as_percentages_or_plain_numbers(self) -> None:
        self.assertEqual(_format_stat_value(0.15), "15%")
        self.assertEqual(_format_stat_value(0.269123), "26.9%")
        self.assertEqual(_format_stat_value(24.5), "24.5")
        self.assertEqual(_format_stat_value(113.0), "113")
        self.assertEqual(_format_stat_value(0), "0")


class LoadoutTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])
        self.battle = parse_battle_detail(battle_detail_payload())

    def test_loadout_page_renders_gear_lines_and_icons(self) -> None:
        html = self.renderer.render_loadout(
            self.battle, query="配装 罗丹", web_base_url="https://zmdlogs.com"
        )

        self.assertIn("点剑护手", html)
        self.assertIn("强化 +3 / +3 / +2", html)
        self.assertIn("物理伤害提升", html)
        self.assertIn("<b>14.9%</b>", html)
        self.assertIn("名称未收录", html)
        self.assertIn("武器技能 9 · 词条 9 / 7", html)
        self.assertIn(
            "https://zmdlogs.com/images/weapon/icon/wpn_sword_0021.png", html
        )
        self.assertIn(
            "https://zmdlogs.com/images/equip/iconbig/item_equip_t4_suit_phy01_hand_01.png",
            html,
        )
        # Raw item ids are never printed as names.
        # The raw id may only appear inside the derived icon URL, never as text.
        self.assertNotIn(">item_equip_t4_suit_phy01_body_02", html)
        self.assertIn("iconbig/item_equip_t4_suit_phy01_body_02.png", html)
        self.assertIn("本场未记录装备", html)
        self.assertIn("潜能 0", html)

    def test_skills_page_renders_rows(self) -> None:
        html = self.renderer.render_skills(
            self.battle, query="技能 罗丹", web_base_url="https://zmdlogs.com"
        )

        self.assertIn("普攻 · 绯红刃舞", html)
        self.assertIn("1,200,000", html)
        self.assertIn("59.2%", html)
        self.assertIn("<i>合并</i>", html)
        self.assertIn("burning status", html)
        self.assertIn("skill 3090", html)
        self.assertIn("占全队 88.4%", html)

    def test_battle_card_gains_the_loadout_section(self) -> None:
        html = self.renderer.render_battle(
            self.battle, query="战报 罗丹", web_base_url="https://zmdlogs.com"
        )

        self.assertIn("阵容与配装", html)
        self.assertIn("点剑 · 护手", html)
        self.assertIn("精炼 3", html)
        self.assertIn("普攻 12 · 战技 12 · 连携 9 · 终结 12", html)
        self.assertIn("终结技", html)

    def test_pages_without_loadout_data_still_render(self) -> None:
        payload = battle_detail_payload()
        payload["battle"]["roster"] = []
        payload["roleSkillStats"] = []
        battle = parse_battle_detail(payload)

        html = self.renderer.render_battle(
            battle, query="q", web_base_url="https://zmdlogs.com"
        )
        self.assertNotIn("阵容与配装", html)
        html = self.renderer.render_loadout(
            battle, query="q", web_base_url="https://zmdlogs.com"
        )
        self.assertIn("这份战报没有记录阵容", html)
        html = self.renderer.render_skills(
            battle, query="q", web_base_url="https://zmdlogs.com"
        )
        self.assertIn("这份战报没有技能统计数据", html)


class BattleStyleRouteTests(unittest.TestCase):
    def test_loadout_and_skill_commands_share_the_battle_shape(self) -> None:
        route = parse_zmdlog_payload("配装 罗丹 3")
        self.assertEqual(route.kind, RouteKind.LOADOUT_QUERY)
        self.assertEqual(route.query, "罗丹")
        self.assertEqual(route.battle_rank, 3)

        route = parse_zmdlog_payload("装备 btl_upload_abcdef123456")
        self.assertEqual(route.kind, RouteKind.LOADOUT_QUERY)
        self.assertEqual(route.query, "btl_upload_abcdef123456")
        self.assertEqual(route.battle_rank, 1)

        route = parse_zmdlog_payload(
            "技能 https://zmdlogs.com/battle/btl_upload_abcdef123456"
        )
        self.assertEqual(route.kind, RouteKind.SKILL_QUERY)

        route = parse_zmdlog_payload("技能统计 罗丹 第2名")
        self.assertEqual(route.kind, RouteKind.SKILL_QUERY)
        self.assertEqual(route.battle_rank, 2)

        self.assertEqual(parse_zmdlog_payload("战报 罗丹").kind, RouteKind.BATTLE_QUERY)

    def test_missing_argument_and_options_are_rejected(self) -> None:
        with self.assertRaisesRegex(RouteParseError, "配装 罗丹 3"):
            parse_zmdlog_payload("配装")
        for payload in ("技能", "配装 罗丹 --top 3", "技能 罗丹 --角色 黎风"):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)


class BattleCandidateViewTests(unittest.TestCase):
    @staticmethod
    def _board(slug: str, name: str) -> MatchChoice:
        return MatchChoice(
            target=MatchTarget(
                target_type=TargetType.BOARD,
                key=slug,
                name=name,
                dungeon_names=("测试副本",),
                boss_slugs=(slug,),
                query_text="测",
            ),
            level=MatchLevel.NORMALIZED_EXACT,
            score=1.0,
            matched_text="测",
        )

    def test_pick_lists_name_the_page_they_will_draw(self) -> None:
        store = CandidateStore()
        choices = (self._board("a", "榜单甲"), self._board("b", "榜单乙"))

        loadout = store.remember(
            "测", choices, view=CandidateView.LOADOUT, battle_rank=2
        )
        skills = store.remember("测", choices, view=CandidateView.SKILLS)

        self.assertIn("配装查询匹配到 2 个榜单", format_candidates(loadout))
        self.assertIn("技能统计查询匹配到 2 个榜单", format_candidates(skills))
        entry, choice = store.resolve(loadout.code, "2")
        self.assertIs(entry.view, CandidateView.LOADOUT)
        self.assertEqual(entry.battle_rank, 2)
        self.assertEqual(choice.target.key, "b")


if __name__ == "__main__":
    unittest.main()
