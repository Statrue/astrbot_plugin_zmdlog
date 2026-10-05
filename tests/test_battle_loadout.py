"""Tests for a battle's roster, its gear lines and the names its pages print."""

import unittest
from dataclasses import replace
from pathlib import Path

from core.candidates import CandidateStore, CandidateView, format_candidates
from core.facts import format_battle
from core.loadout import (
    SkillCategory,
    battle_suit_ids,
    group_skill_damage,
    is_raw_item_name,
    skill_category,
    skill_display_name,
    skill_level_summary,
    suit_catalog_id,
)
from core.matcher import MatchChoice, MatchLevel, MatchTarget, TargetType
from core.models import (
    BattleSkillStat,
    ModelValidationError,
    parse_battle_detail,
)
from core.presentation import (
    _format_stat_value,
    build_battle_build_page,
    build_battle_data_page,
    build_compare_page,
)
from core.presentation.battle import _equip_view
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

    def test_the_game_skill_name_is_read_leniently(self) -> None:
        payload = battle_detail_payload()
        stats = payload["roleSkillStats"]
        stats[0]["displayGroupName"] = " 狼之怒 "
        stats[1]["displayGroupName"] = None
        stats[2]["displayGroupName"] = 7
        stats[3]["displayGroupName"] = "  "

        battle = parse_battle_detail(payload)

        # Absent (the fixture's other rows), null, wrong-typed and blank alike
        # leave the row to the cleaned name; none of them fails the battle.
        self.assertEqual(
            [stat.display_group_name for stat in battle.skill_stats],
            ["狼之怒"] + [None] * 8,
        )

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
        # A bare numbered sub-skill has no words to read; it is at least Chinese.
        raw_key = "chr_0028_wulfa_skill_3090"
        self.assertEqual(skill_display_name(raw_key, raw_key), "技能 3090")
        self.assertEqual(
            skill_display_name("绯红刃舞", "chr_0028_wulfa_attack1"), "绯红刃舞"
        )
        # Upstream's word-joined buff names read through the same vocabulary
        # even when the key has no shape to parse.
        self.assertEqual(skill_display_name("burning status", "buff_x"), "燃烧状态")
        self.assertEqual(skill_display_name("", "buff_x"), "buff_x")
        self.assertEqual(skill_display_name("  ", None), "未命名技能")
        self.assertEqual(
            skill_display_name(
                "ultimate / skill / 派生", "chr_0035_liino_ultimate_skill_projhit"
            ),
            "终结技 · 派生",
        )

    def test_the_game_name_comes_before_the_cleaned_one(self) -> None:
        combo = ("锥心之棘", "chr_0033_camille_combo_skill")
        # The loose combo-by-key rule no longer hides the game's name.
        self.assertEqual(
            skill_display_name(*combo, official_name="锥心之棘"), "锥心之棘"
        )
        self.assertEqual(
            skill_display_name(
                "重击", "chr_0032_lizhiyan_power_attack", official_name="重火力截击"
            ),
            "重火力截击",
        )
        # The game names the skill, not the sub-hit: the part that tells
        # 梨诺's five 终结技 rows apart stays, under the skill's own name.
        self.assertEqual(
            skill_display_name(
                "ultimate / skill / 派生 / l",
                "chr_0035_liino_ultimate_skill_projhit_l",
                official_name="晨星的协奏曲",
            ),
            "晨星的协奏曲 · 派生（左）",
        )
        for missing in (None, "", "  "):
            with self.subTest(official_name=missing):
                self.assertEqual(
                    skill_display_name(*combo, official_name=missing), "连携技"
                )
        self.assertIs(skill_category(*combo), SkillCategory.COMBO)

    def test_raw_keys_read_as_the_moves_they_are(self) -> None:
        # Every shape the public boards showed with English left in it, and
        # what the segments mean. Upstream translates ``normal`` on its own
        # and leaves ``skill``, which is how a 战技 sub-hit read as 普攻.
        cases = {
            ("普攻 / skill / 派生 / hit", "chr_0035_liino_normal_skill_projhit_hit"): (
                "战技 · 派生命中"
            ),
            (
                "普攻 / skill / delay / 伤害",
                "buff_chr_0033_camille_normal_skill_delay_damage",
            ): "战技 · 延迟伤害",
            (
                "普攻 / skill / attack2 / 派生",
                "chr_0034_typhoea_normal_skill_attack2_projhit",
            ): "战技 · 二段派生",
            ("普攻 / skill / 派生 / 02", "chr_0035_liino_normal_skill_projhit_02"): (
                "战技 · 派生 2"
            ),
            ("连携 / persistentdamage", "chr_0034_typhoea_combo_persistentdamage"): (
                "连携技 · 持续伤害"
            ),
            ("combo skillfloating", "chr_0034_typhoea_combo_skillfloating"): (
                "连携技 · 浮空"
            ),
            (
                "ultimate / skill / 派生 / 伤害 / 02",
                "chr_0035_liino_ultimate_skill_projhit_damage_02",
            ): "终结技 · 派生伤害 2",
            (
                "ultimate / skill / soundwave / 派生",
                "chr_0035_liino_ultimate_skill_soundwave_projhit",
            ): "终结技 · 声波派生",
            (
                "ultimate / skill / 派生 / l",
                "chr_0035_liino_ultimate_skill_projhit_l",
            ): "终结技 · 派生（左）",
            ("power / 攻击 / 派生", "chr_0034_typhoea_power_attack_projhit"): (
                "重击 · 派生"
            ),
            (
                "floating / attack1 / 01 / 派生",
                "chr_0034_typhoea_floating_attack1_01_projhit",
            ): "浮空 A1-01 派生",
            # A single element ``triggered`` is that element being applied.
            ("natural triggered", "buff_common_natural_triggered"): "自然附着",
            ("cryst triggered fx", "buff_common_cryst_triggered_fx"): "寒冷附着特效",
            ("fire triggered start", "buff_common_fire_triggered_start"): (
                "灼热附着起手"
            ),
            ("weakness triggered", "buff_common_weakness_triggered"): "脆弱触发",
            # parser_core's own reaction names, plus a trailing character token
            # on the same-element burst (提弗洛斯's own 自然爆发).
            (
                "natural natural triggered typhoea",
                "buff_common_natural_natural_triggered_typhoea",
            ): "自然爆发",
            ("fire natural triggered", "buff_common_fire_natural_triggered"): "燃烧",
            ("skill 640", "chr_0031_mifu_skill_640"): "技能 640",
            # No English word at all, but still upstream's segment join.
            ("连携 / 02 / 派生", "chr_0034_typhoea_combo_02_projhit"): (
                "连携技 · 派生 2"
            ),
        }
        for (name, key), expected in cases.items():
            with self.subTest(key=key):
                self.assertEqual(skill_display_name(name, key), expected)

    def test_the_dictionary_names_what_no_rule_can(self) -> None:
        # Entries the user identified in play; they beat every rule, and the
        # family before the dot still buckets the row.
        cases = {
            "buff_common_burning_status": ("燃烧", SkillCategory.MECHANIC),
            "chr_0033_camille_skill_213": ("战技 · 衔火血翼", SkillCategory.SKILL),
            "chr_0032_lizhiyan_skill_3782": ("连携技 · 战术分身", SkillCategory.COMBO),
            "chr_0032_lizhiyan_skill_3423": ("连携技 · 战术分身", SkillCategory.COMBO),
        }
        for key, (name, category) in cases.items():
            with self.subTest(key=key):
                self.assertEqual(skill_display_name(key, key), name)
                self.assertIs(skill_category(key, key), category)

    def test_names_the_game_gave_are_kept_and_only_their_english_read(self) -> None:
        # A name whose shape is not upstream's token join of the key carries
        # text of its own; the key must not overwrite it.
        self.assertEqual(
            skill_display_name(
                "塞什卡的秘传 / phantom", "buff_chr_0026_lastrite_normal_skill_phantom"
            ),
            "塞什卡的秘传 / 幻影",
        )
        self.assertEqual(
            skill_display_name("河水 / water / gene", "chr_x_normal_skill_water_gene"),
            "河水 / water / gene",
        )
        # Words the vocabulary does not know stay as they are, in place.
        self.assertEqual(
            skill_display_name(
                "poise can be breaking attacked",
                "buff_common_poise_can_be_breaking_attacked",
            ),
            "poise can be breaking attacked",
        )
        # The dictionary layer beats every rule: this key has a name now.
        self.assertEqual(
            skill_display_name(
                "buff_chr_0030_zhuangfy_sword_triggerd",
                "buff_chr_0030_zhuangfy_sword_triggerd",
            ),
            "青霆剑",
        )
        self.assertEqual(
            skill_display_name(
                "buff_chr_0030_zhuangfy_other_triggerd",
                "buff_chr_0030_zhuangfy_other_triggerd",
            ),
            "other 触发",
        )

    def test_categories_mirror_the_site_and_bucket_engine_sources(self) -> None:
        cases = {
            ("终结技", "chr_x_ultimate_skill2"): SkillCategory.ULTIMATE,
            ("A5", "chr_x_attack5"): SkillCategory.NORMAL,
            # Sub-hits of the basic chain, whichever way the key spells it.
            ("A5 派生", "chr_0016_laevat_attack_5_projhit"): SkillCategory.NORMAL,
            (
                "A4-2 派生（格挡）",
                "chr_0016_laevat_attack_4_2_projhit_blocked",
            ): SkillCategory.NORMAL,
            ("绯红刃舞", "chr_x_attack1"): SkillCategory.NORMAL,
            ("噪点", "chr_x_power_attack"): SkillCategory.HEAVY,
            ("噪点", "chr_x_plunging_attack_end"): SkillCategory.HEAVY,
            ("战技", None): SkillCategory.SKILL,
            ("构成序列", "chr_x_normal_skill"): SkillCategory.SKILL,
            ("连携·潮汐", "chr_x_combo_skill"): SkillCategory.COMBO,
            ("燃烧", "buff_common_fire_natural_triggered"): SkillCategory.MECHANIC,
            # The family buckets a row, not the whole label.
            (
                "连携 / persistentdamage",
                "chr_0034_typhoea_combo_persistentdamage",
            ): SkillCategory.COMBO,
            (
                "普攻 / skill / 派生",
                "chr_0035_liino_normal_skill_projhit",
            ): SkillCategory.SKILL,
            ("连携 / 02 / 派生", "chr_0034_typhoea_combo_02_projhit"): (
                SkillCategory.COMBO
            ),
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

    def test_game_names_relabel_rows_without_regrouping_them(self) -> None:
        def stat(key: str, name: str, total: int, official: str | None = None):
            row = BattleSkillStat("梨诺", name, 1, total, float(total), total, key)
            return replace(row, display_group_name=official)

        # 梨诺 as a v46 upload carries her: upstream names the skill on every
        # row of it except the projectile and mechanism ones.
        named = (
            stat(
                "chr_0035_liino_ultimate_skill_projhit",
                "ultimate / skill / 派生",
                900,
                "晨星的协奏曲",
            ),
            stat(
                "chr_0035_liino_ultimate_skill_projhit_l",
                "ultimate / skill / 派生 / l",
                800,
                "晨星的协奏曲",
            ),
            stat(
                "chr_0035_liino_ultimate_skill_projhit_r",
                "ultimate / skill / 派生 / r",
                700,
                "晨星的协奏曲",
            ),
            stat("chr_0035_liino_combo_skill", "悦心音调", 600, "悦心音调"),
            stat(
                "chr_0035_liino_combo_skill_abilityrange",
                "chr_0035_liino_combo_skill_abilityrange",
                500,
            ),
            stat("chr_0035_liino_attack1", "A1", 350, "怦然星动"),
            stat("chr_0035_liino_attack2", "A2", 250, "怦然星动"),
            stat("chr_0035_liino_attack3_projhit", "A3 派生", 150),
            stat("buff_common_burning_status", "burning status", 100),
        )
        unnamed = tuple(replace(row, display_group_name=None) for row in named)

        (before,) = group_skill_damage(unnamed)
        (after,) = group_skill_damage(named)

        def shape(group):
            return [
                (row.category, row.cast_count, row.total_damage, row.merged_count)
                for row in group.rows
            ]

        self.assertEqual(shape(after), shape(before))
        self.assertEqual(
            [row.name for row in before.rows],
            [
                "连携技",
                "终结技 · 派生",
                "终结技 · 派生（左）",
                "普攻（各段合并）",
                "终结技 · 派生（右）",
                "燃烧",
            ],
        )
        self.assertEqual(
            [row.name for row in after.rows],
            [
                "悦心音调",
                "晨星的协奏曲 · 派生",
                "晨星的协奏曲 · 派生（左）",
                "普攻 · 怦然星动",
                "晨星的协奏曲 · 派生（右）",
                "燃烧",
            ],
        )

    def test_a_game_name_two_rows_would_share_leaves_both_as_they_were(self) -> None:
        def stat(key: str, name: str, total: int):
            row = BattleSkillStat("莱万汀", name, 1, total, float(total), total, key)
            return replace(row, display_group_name="焚灭")

        # Two rows of one skill the game calls 焚灭: under that one name they
        # would read as one row, so each keeps the name it had.
        (group,) = group_skill_damage(
            (
                stat("chr_0016_laevat_normal_skill", "焚灭", 300),
                stat("chr_0016_laevat_normal_skill_ember", "焚灭·余烬", 100),
                stat(
                    "chr_0016_laevat_normal_skill_abilityentity",
                    "chr_0016_laevat_normal_skill_abilityentity",
                    50,
                ),
            )
        )

        self.assertEqual(
            [(row.name, row.merged_count) for row in group.rows],
            [("焚灭", 1), ("焚灭·余烬", 1), ("焚灭 · 实体", 1)],
        )

    def test_a_game_name_another_category_already_prints_is_not_taken(
        self,
    ) -> None:
        def stat(key: str, name: str, total: int):
            row = BattleSkillStat("庄方宜", name, 1, total, float(total), total, key)
            return replace(row, display_group_name="惊霆诀")

        # The 青霆剑 strikes are a mechanic row the game files under the
        # 战技 that leaves them behind; 对比 prints no category chip, so
        # renamed they would read as a second 惊霆诀.
        (group,) = group_skill_damage(
            (
                stat("chr_0030_zhuangfy_normal_skill", "惊霆诀", 600),
                stat(
                    "buff_chr_0030_zhuangfy_sword_triggerd",
                    "buff_chr_0030_zhuangfy_sword_triggerd",
                    100,
                ),
            )
        )

        self.assertEqual([row.name for row in group.rows], ["惊霆诀", "青霆剑"])


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

    def test_a_battle_names_the_suits_its_pieces_belong_to(self) -> None:
        # What the catalog read is asked to cover: every suit id once, in the
        # order the pieces appear, standalone parts left out.
        battle = parse_battle_detail(battle_detail_payload())

        ids = battle_suit_ids(battle, battle)

        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(suit.startswith("suit_") for suit in ids))
        self.assertEqual(
            set(ids),
            {
                suit_catalog_id(equip.item_id)
                for entry in battle.roster
                for equip in entry.equips
                if suit_catalog_id(equip.item_id)
            },
        )

    def test_skill_and_weapon_levels(self) -> None:
        battle = parse_battle_detail(battle_detail_payload())
        first, second = battle.roster

        # ``normal_skill_combo`` must not be mistaken for the 战技 entry.
        self.assertEqual(
            [(level.label, level.level) for level in skill_level_summary(first)],
            [("普攻", 12), ("战技", 12), ("连携", 9), ("终结", 12)],
        )
        self.assertEqual(skill_level_summary(second), ())


WEB = "https://zmdlogs.com"


def gear_of(battle, suits=None, *, slot: int = 0):
    """One roster entry's gear as 养成 and 对比 read it, in upload order."""

    entry = sorted(battle.roster, key=lambda item: item.slot)[slot]
    return [
        _equip_view(equip, suits or {}, web_base_url=WEB)
        for equip in sorted(entry.equips, key=lambda item: item.slot)
    ]


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

        battle = parse_battle_detail(payload)
        page = build_battle_build_page(battle, query="q", web_base_url=WEB)

        self.assertEqual(
            page.characters[0].weapon.icon_url,
            "https://zmdlogs.com/images/weapon/icon/wpn_sword_0021.png",
        )
        hand, body, edc, other = gear_of(battle)
        self.assertEqual(
            hand.icon_url,
            "https://zmdlogs.com/images/equip/iconbig/item_equip_t4_suit_phy01_hand_01.png",
        )
        self.assertIsNone(body.icon_url)
        self.assertIsNone(edc.icon_url)
        self.assertEqual(
            other.icon_url, "https://zmdlogs.com/images/equip/iconbig/explicit.png"
        )

    def test_a_piece_of_gear_is_formatted_once_for_every_page(self) -> None:
        # The view 养成's tiles and 对比's lines both read
        # (test_battle_build, test_compare).
        hand, body, edc, _ = gear_of(self.battle)
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
        self.assertEqual(gear_of(self.battle, slot=1), [])

    def test_attribute_type_numbers_print_the_stat_they_stand_for(self) -> None:
        # 险关手甲 as btl_upload_c5cf4810aa09 (parser v57) recorded it, then
        # 涉渊护手's two other numbers from the same battle: the stat names are
        # the game's attribute enum by number, beside pieces that still carry
        # Chinese names. ``_0`` is the enum's unnamed zero (seen on v48–v57),
        # ``_999`` any number not yet seen, and ``PhySpellUp`` the enum's
        # English name in place of either.
        def stat(slot: str, name: str, value: float) -> dict:
            level = None if slot == "main" else 3
            return {"slot": slot, "name": name, "value": value, "level": level}

        payload = battle_detail_payload()
        payload["battle"]["roster"][0]["equips"][0]["stats"] = [
            stat("main", "attribute_type_3", 42.0),
            stat("sub1", "attribute_type_40", 84.0),
            stat("sub2", "attribute_type_42", 55.0),
            stat("sub3", "attribute_type_87", 44.85),
            stat("sub1", "attribute_type_41", 111.0),
            stat("sub2", "attribute_type_44", 0.278571),
            stat("sub3", "attribute_type_0", 0.299),
            stat("sub3", "attribute_type_999", 12.0),
            stat("sub3", "PhySpellUp", 12.0),
        ]
        battle = parse_battle_detail(payload)
        web = "https://zmdlogs.com"

        build = build_battle_build_page(battle, query="q", web_base_url=web)
        card = build_compare_page(battle, battle, query="q", web_base_url=web)

        # The piece is the fixture's 护手, second on 养成 after the 护甲.
        self.assertEqual(
            [(line.name, line.value) for line in build.characters[0].gear[1].stats],
            [
                ("防御力", "42"),
                ("敏捷", "84"),
                ("意志", "55"),
                ("源石技艺强度", "44.9"),
                ("智识", "111"),
                ("终结技充能效率", "27.9%"),
                ("名称未收录", "29.9%"),
                ("名称未收录", "12"),
                ("名称未收录", "12"),
            ],
        )
        # 对比's gear lines read the same view; no raw spelling survives
        # anywhere on either page.
        for page in (build, card):
            self.assertNotIn("attribute_type", repr(page))
            self.assertNotIn("PhySpellUp", repr(page))

    def test_the_catalog_names_a_suit_upstream_left_blank(self) -> None:
        # The same piece, once without the catalog and once with it. The
        # catalog is keyed by the id the item carries, so it answers for
        # every battle rather than only the ones a named sibling appears in.
        _, body, _, _ = gear_of(self.battle, {"suit_phy01": "点剑"})

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
        gear = gear_of(parse_battle_detail(payload), {"suit_spellburst": "险关"})

        self.assertEqual(gear[0].suit_label, "险关")

    def test_a_standalone_piece_keeps_what_the_upload_called_it(self) -> None:
        # ``_parts_`` pieces belong to no suit, so the catalog has nothing to
        # say and upstream's own label is all there is.
        payload = battle_detail_payload()
        equip = payload["battle"]["roster"][0]["equips"][0]
        equip["itemId"] = "item_equip_t4_parts_wuling01_hand_01"
        equip["suitName"] = "独立装备"
        gear = gear_of(parse_battle_detail(payload), {"suit_wuling01": "不该被用到"})

        self.assertEqual(gear[0].suit_label, "独立装备")

    def test_a_comparison_names_each_characters_heaviest_sources(self) -> None:
        page = build_compare_page(
            self.battle, self.battle, query="q", web_base_url=WEB
        )

        luoxi, kamiu = page.builds
        self.assertEqual(
            [row.name for row in luoxi.sources_a],
            ["终结技", "普攻 · 绯红刃舞", "战技"],
        )
        self.assertEqual(kamiu.sources_b[0].name, "连携技")

    def test_every_surface_prints_the_game_name(self) -> None:
        payload = battle_detail_payload()
        payload["roleSkillStats"][7]["displayGroupName"] = "连携·潮汐"
        battle = parse_battle_detail(payload)
        web = "https://zmdlogs.com"

        data = build_battle_data_page(battle, query="q", web_base_url=web)
        card = build_compare_page(battle, battle, query="q", web_base_url=web)

        self.assertEqual(data.skill_groups[1].rows[0].name, "连携·潮汐")
        # 对比's 伤害来源 on each build card.
        self.assertEqual(card.builds[1].sources_a[0].name, "连携·潮汐")
        self.assertIn("卡缪：连携·潮汐 76%", format_battle(battle))

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

    def test_the_build_page_renders_gear_lines_and_icons(self) -> None:
        html = self.renderer.render_battle_build(
            self.battle, query="养成 罗丹", web_base_url="https://zmdlogs.com"
        )

        self.assertIn("点剑护手", html)
        self.assertIn("+3 / +3 / +2", html)
        self.assertIn("物理伤害提升", html)
        self.assertIn("<b>14.9%</b>", html)
        self.assertIn("名称未收录", html)
        self.assertIn("力量提升·大", html)
        self.assertIn("攻击提升·大", html)
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
        # 卡缪 recorded no gear: four empty places, never a blank card.
        self.assertIn("<strong>未记录</strong>", html)

    def test_pages_without_loadout_data_still_render(self) -> None:
        payload = battle_detail_payload()
        payload["battle"]["roster"] = []
        payload["roleSkillStats"] = []
        battle = parse_battle_detail(payload)

        html = self.renderer.render_battle(
            battle, query="q", web_base_url="https://zmdlogs.com"
        )
        self.assertIn("<strong>伤害构成</strong>", html)


class BattleStyleRouteTests(unittest.TestCase):
    def test_build_and_data_commands_share_the_battle_shape(self) -> None:
        route = parse_zmdlog_payload("养成 罗丹 3")
        self.assertEqual(route.kind, RouteKind.BUILD_QUERY)
        self.assertEqual(route.query, "罗丹")
        self.assertEqual(route.battle_rank, 3)

        route = parse_zmdlog_payload("养成 罗丹 --口径 rdps")
        self.assertEqual(route.kind, RouteKind.BUILD_QUERY)
        self.assertEqual((route.query, route.metric), ("罗丹", "rdps"))

        # 配装 and 装备 are no commands any more: an ordinary keyword.
        for payload in ("配装 罗丹 3", "装备 btl_upload_abcdef123456"):
            with self.subTest(payload=payload):
                route = parse_zmdlog_payload(payload)
                self.assertEqual(route.kind, RouteKind.SMART_QUERY)

        route = parse_zmdlog_payload(
            "数据 https://zmdlogs.com/battle/btl_upload_abcdef123456"
        )
        self.assertEqual(route.kind, RouteKind.DATA_QUERY)

        route = parse_zmdlog_payload("数据 罗丹 第2名")
        self.assertEqual(route.kind, RouteKind.DATA_QUERY)
        self.assertEqual(route.battle_rank, 2)

        self.assertEqual(parse_zmdlog_payload("战报 罗丹").kind, RouteKind.BATTLE_QUERY)

    def test_missing_argument_and_options_are_rejected(self) -> None:
        with self.assertRaisesRegex(RouteParseError, "养成 罗丹 3"):
            parse_zmdlog_payload("养成")
        for payload in ("数据", "养成 罗丹 --页 3", "数据 罗丹 --角色 黎风"):
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
            ),
            level=MatchLevel.NORMALIZED_EXACT,
            score=1.0,
            matched_text="测",
        )

    def test_pick_lists_name_the_page_they_will_draw(self) -> None:
        store = CandidateStore()
        choices = (self._board("a", "榜单甲"), self._board("b", "榜单乙"))

        build = store.remember(
            "测", choices, view=CandidateView.BUILD, battle_rank=2
        )
        data = store.remember("测", choices, view=CandidateView.DATA)

        self.assertIn("养成查询匹配到 2 个榜单", format_candidates(build))
        self.assertIn("战报数据查询匹配到 2 个榜单", format_candidates(data))
        entry, choice = store.resolve(build.code, "2")
        self.assertIs(entry.view, CandidateView.BUILD)
        self.assertEqual(entry.battle_rank, 2)
        self.assertEqual(choice.target.key, "b")


if __name__ == "__main__":
    unittest.main()
