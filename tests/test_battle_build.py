"""战报 养成: each fielded character's build, from the battle to the view model."""

import unittest

from core.loadout import weapon_affix_name, weapon_skill_lines
from core.models import parse_battle_detail
from core.presentation import build_battle_build_page
from tests.helpers import battle_detail_payload

WEB = "https://zmdlogs.com"


def weapon_payload(*skills: tuple[str, int], refine: int | None = 1) -> dict:
    """The fixture with 洛茜's weapon carrying ``skills`` at ``refine``."""

    payload = battle_detail_payload()
    weapon = payload["battle"]["roster"][0]["weapon"]
    weapon["weaponRefine"] = refine
    weapon["skills"] = [
        {"skillKey": key, "level": level, "potentialLevel": 0} for key, level in skills
    ]
    return payload


def build(payload: dict | None = None, **kwargs):
    battle = parse_battle_detail(payload or battle_detail_payload())
    return build_battle_build_page(
        battle, query="养成 罗丹", web_base_url=WEB, **kwargs
    )


def pips(line) -> str:
    """The pips as one letter each: on, off, locked past the 精炼 cap."""

    marks = {"on": "o", "off": "-", "locked": "x"}
    return "".join(marks[pip.value] for pip in line.pips)


def blades(star) -> str:
    """The blades in the order they light, A → B → E → D → C."""

    return "".join({"lit": "W", "lead": "Y", "base": "."}[b.value] for b in star.blades)


class WeaponSplitTests(unittest.TestCase):
    def test_the_weapon_line_is_two_affixes_then_the_weapon_skill(self) -> None:
        # 寒夜幽影 as btl_upload_c5cf4810aa09 recorded it: upstream sends the
        # special affix first and the base attribute last.
        page = build(
            weapon_payload(
                ("wpn_sp_attr_atk_high", 9),
                ("sk_wpn_funnel_0019", 4),
                ("wpn_attr_agi_high", 9),
                refine=1,
            )
        )
        weapon = page.characters[0].weapon

        self.assertEqual(
            [(line.name, line.level, line.cap) for line in weapon.lines],
            [("敏捷提升·大", 9, 9), ("攻击提升·大", 9, 9), ("武器技能", 4, 4)],
        )
        self.assertEqual(
            [line.level_label for line in weapon.lines], ["9/9", "9/9", "4/4"]
        )
        self.assertEqual(
            [line.is_weapon_skill for line in weapon.lines], [False, False, True]
        )

    def test_the_weapon_skill_is_capped_by_the_refinement(self) -> None:
        for refine, level, cap, drawn in (
            (1, 4, 4, "oooo" + "x" * 5),
            (3, 5, 6, "ooooo-" + "xxx"),
            (5, 2, 8, "oo------x"),
            (6, 9, 9, "o" * 9),
        ):
            with self.subTest(refine=refine):
                page = build(
                    weapon_payload(("sk_wpn_sword_0021", level), refine=refine)
                )
                (line,) = page.characters[0].weapon.lines

                self.assertEqual(line.cap, cap)
                self.assertEqual(pips(line), drawn)
                # 9 − cap pips are crossed out: past what this 精炼 allows.
                self.assertEqual(pips(line).count("x"), 9 - cap)

    def test_an_affix_is_capped_at_nine_whatever_the_refinement(self) -> None:
        page = build(weapon_payload(("wpn_attr_wisd_mid", 4), refine=1))
        (line,) = page.characters[0].weapon.lines

        self.assertEqual((line.level, line.cap), (4, 9))
        self.assertEqual(pips(line), "oooo-----")
        self.assertEqual(line.level_label, "4/9")

    def test_max_is_shown_at_level_nine_only(self) -> None:
        page = build(
            weapon_payload(
                ("wpn_attr_will_high", 9),
                ("wpn_sp_attr_heal_high", 8),
                ("sk_wpn_lance_0003", 4),
                refine=1,
            )
        )
        lines = page.characters[0].weapon.lines

        # The weapon skill is at its cap (4/4) but not at 9: no MAX.
        self.assertEqual([line.maxed for line in lines], [True, False, False])
        self.assertEqual([line.at_cap for line in lines], [True, False, True])

    def test_a_level_past_the_cap_is_never_crossed_out(self) -> None:
        # Seen on the boards: 精炼 1 with a weapon skill at 5 or 8. The
        # upload contradicts itself; the level it recorded is drawn reached.
        page = build(weapon_payload(("sk_wpn_sword_0021", 8), refine=1))
        (line,) = page.characters[0].weapon.lines

        self.assertEqual(line.cap, 8)
        self.assertEqual(pips(line), "o" * 8 + "x")

    def test_an_unrecorded_refinement_crosses_nothing_out(self) -> None:
        page = build(weapon_payload(("sk_wpn_sword_0021", 3), refine=None))
        weapon = page.characters[0].weapon
        (line,) = weapon.lines

        self.assertEqual((line.cap, pips(line)), (9, "ooo------"))
        self.assertIsNone(weapon.refine)
        self.assertIsNone(weapon.refine_star)


class AffixNamingTests(unittest.TestCase):
    def test_every_key_upstream_knows_is_named_as_the_game_names_it(self) -> None:
        # The game data catalog's own names (/api/game-data/weapon/{id},
        # skilllist), matched to the keys on 120 board battles (UPSTREAM.md).
        for key, name in (
            ("wpn_attr_str_low", "力量提升·小"),
            ("wpn_attr_agi_mid", "敏捷提升·中"),
            ("wpn_attr_wisd_high", "智识提升·大"),
            ("wpn_attr_will_high", "意志提升·大"),
            ("wpn_attr_main_high", "主能力提升·大"),
            ("wpn_sp_attr_atk_mid", "攻击提升·中"),
            ("wpn_sp_attr_hp_low", "生命提升·小"),
            ("wpn_sp_attr_heal_high", "治疗效率提升·大"),
            ("wpn_sp_attr_crirate_high", "暴击率提升·大"),
            ("wpn_sp_attr_cridmg_high", "暴击伤害提升·大"),
            ("wpn_sp_attr_usgs_high", "终结技充能效率提升·大"),
            ("wpn_sp_attr_phy_spell_high", "源石技艺强度提升·大"),
            ("wpn_sp_attr_phydam_low", "物理伤害提升·小"),
            ("wpn_sp_attr_firedam_mid", "灼热伤害提升·中"),
            ("wpn_sp_attr_crystdam_high", "寒冷伤害提升·大"),
            ("wpn_sp_attr_electrondam_mid", "电磁伤害提升·中"),
            ("wpn_sp_attr_naturaldam_high", "自然伤害提升·大"),
            ("wpn_sp_attr_magicdam_high", "法术提升·大"),
            ("WPN_ATTR_STR_HIGH", "力量提升·大"),
        ):
            with self.subTest(key=key):
                self.assertEqual(weapon_affix_name(key), name)

    def test_a_key_that_cannot_be_named_is_never_printed(self) -> None:
        for key in (
            "wpn_sp_attr_mystery_high",
            "wpn_attr_str_huge",
            "wpn_attr_str",
            "wpn_sk_atk",
            "wpn_sp_normalattack_high",
            "",
        ):
            with self.subTest(key=key):
                self.assertEqual(weapon_affix_name(key), "名称未收录")

        page = build(
            weapon_payload(("wpn_sp_attr_mystery_high", 3), ("wpn_sk_cscd", 2))
        )

        self.assertEqual(
            [line.name for line in page.characters[0].weapon.lines],
            ["名称未收录", "名称未收录"],
        )
        self.assertNotIn("mystery", repr(page))
        self.assertNotIn("wpn_sk_cscd", repr(page))

    def test_lines_keep_the_base_attribute_first_and_drop_unlevelled_skills(
        self,
    ) -> None:
        battle = parse_battle_detail(
            weapon_payload(
                ("sk_wpn_sword_0021", 3),
                ("wpn_sp_attr_atk_high", 5),
                ("wpn_attr_str_high", 6),
            )
        )
        lines = weapon_skill_lines(battle.roster[0].weapon)

        self.assertEqual(
            [(line.name, line.level, line.is_weapon_skill) for line in lines],
            [
                ("力量提升·大", 6, False),
                ("攻击提升·大", 5, False),
                ("武器技能", 3, True),
            ],
        )
        # An older upload recorded no weapon skills at all.
        self.assertEqual(weapon_skill_lines(battle.roster[1].weapon), ())


class StarTests(unittest.TestCase):
    def test_potential_k_lights_k_blades_and_marks_the_next(self) -> None:
        for potential, drawn, full in (
            (0, "Y....", False),
            (1, "WY...", False),
            (2, "WWY..", False),
            (4, "WWWWY", False),
            (5, "WWWWW", True),
        ):
            with self.subTest(potential=potential):
                payload = battle_detail_payload()
                payload["battle"]["roster"][0]["characterPotential"] = potential
                character = build(payload).characters[0]

                self.assertEqual(character.potential, potential)
                self.assertEqual(blades(character.potential_star), drawn)
                self.assertEqual(character.potential_star.full, full)

    def test_refinement_lights_the_same_star_from_one(self) -> None:
        # 精炼 1 is the weapon as it comes: nothing gained, the first blade next.
        for refine, drawn, label, full in (
            (1, "Y....", "1", False),
            (3, "WWY..", "3", False),
            (5, "WWWWY", "5", False),
            (6, "WWWWW", "MAX", True),
        ):
            with self.subTest(refine=refine):
                weapon = build(weapon_payload(refine=refine)).characters[0].weapon

                self.assertEqual(blades(weapon.refine_star), drawn)
                self.assertEqual(weapon.refine_star.full, full)
                self.assertEqual(weapon.refine_label, label)

    def test_an_unrecorded_potential_lights_nothing(self) -> None:
        payload = battle_detail_payload()
        payload["battle"]["roster"][0]["characterPotential"] = None
        character = build(payload).characters[0]

        self.assertIsNone(character.potential)
        self.assertEqual(blades(character.potential_star), ".....")
        self.assertFalse(character.potential_star.full)


class BuildPageTests(unittest.TestCase):
    def test_each_character_in_roster_order_with_the_main_c_marked(self) -> None:
        page = build()
        luoxi, kamiu = page.characters

        self.assertEqual(page.header.title, "“碾骨之拳”罗丹")
        self.assertEqual(page.uploader_display_name, "测试账号")
        self.assertEqual(page.duration, "0:20.833")
        self.assertEqual(
            (luoxi.slot_label, luoxi.character_name, luoxi.profession, luoxi.element),
            ("01", "洛茜", "近卫", "灼热"),
        )
        self.assertEqual(
            luoxi.avatar_url, "https://zmdlogs.com/images/character/luoxi.png"
        )
        self.assertEqual((luoxi.level, luoxi.damage_share), (90, "88.4%"))
        self.assertTrue(luoxi.lead)
        self.assertFalse(kamiu.lead)
        self.assertEqual((kamiu.slot_label, kamiu.level), ("02", 80))
        self.assertIsNone(kamiu.element)
        self.assertEqual(kamiu.damage_share, "11.6%")

    def test_the_four_skill_slots_are_always_drawn_in_game_order(self) -> None:
        luoxi, kamiu = build().characters

        self.assertEqual(
            [(slot.label, slot.level) for slot in luoxi.skill_levels],
            [("普攻", 12), ("战技", 12), ("连携", 9), ("终结", 12)],
        )
        # Nothing recorded: the slots stay, each without a level.
        self.assertEqual(
            [(slot.label, slot.level) for slot in kamiu.skill_levels],
            [("普攻", None), ("战技", None), ("连携", None), ("终结", None)],
        )

    def test_gear_reads_armour_then_gloves_then_the_kits(self) -> None:
        # The fixture uploads 护手, 护甲, 配件, 配件 in that slot order.
        luoxi, kamiu = build().characters

        self.assertEqual(
            [tile.part_name for tile in luoxi.gear], ["护甲", "护手", "配件", "配件"]
        )
        body, hand, kit, _ = luoxi.gear
        self.assertEqual(hand.label, "点剑护手")
        self.assertEqual(hand.enhance, "+3 / +3 / +2")
        self.assertEqual(
            hand.icon_url,
            "https://zmdlogs.com/images/equip/iconbig/item_equip_t4_suit_phy01_hand_01.png",
        )
        self.assertEqual(
            [(stat.name, stat.value, stat.is_main) for stat in hand.stats],
            [
                ("防御力", "30", True),
                ("力量", "61", False),
                ("物理伤害提升", "14.9%", False),
                ("主能力", "26.9%", False),
            ],
        )
        self.assertIsNone(kit.enhance)
        # Upstream named neither the piece nor its suit: never the raw id.
        self.assertEqual(body.label, "名称未收录")
        # A character with no gear recorded keeps four empty places.
        self.assertEqual(kamiu.gear, (None, None, None, None))

    def test_the_suit_catalog_names_a_piece_upstream_left_raw(self) -> None:
        body = build(suits={"suit_phy01": "点剑"}).characters[0].gear[0]

        self.assertEqual(body.label, "点剑 · 护甲")

    def test_kits_keep_the_upload_order_between_themselves(self) -> None:
        payload = battle_detail_payload()
        equips = payload["battle"]["roster"][0]["equips"]
        equips[2]["pieceName"], equips[3]["pieceName"] = "甲配件", "乙配件"
        equips[2]["slot"], equips[3]["slot"] = 3, 2

        gear = build(payload).characters[0].gear

        self.assertEqual([tile.label for tile in gear[2:]], ["乙配件", "甲配件"])

    def test_the_strip_names_the_battles_pages(self) -> None:
        page = build(views=(("摘要", False), ("养成", True)))

        self.assertEqual(
            [(view.label, view.current) for view in page.views],
            [("摘要", False), ("养成", True)],
        )


if __name__ == "__main__":
    unittest.main()
