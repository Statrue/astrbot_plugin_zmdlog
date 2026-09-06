"""Professions: the label, the aliases people type, the whole-class board."""

import unittest
from pathlib import Path

from core import facts
from core.models import parse_boss_ranking, parse_character_types
from core.presentation import build_character_champions_page
from core.professions import PROFESSIONS, normalize_profession
from core.render import TemplateRenderer
from core.routing import RouteKind, RouteParseError, parse_zmdlog_payload
from core.standings import by_profession, character_tallies, unseen_characters
from tests.helpers import ranking_payload_with_rows

WEB = "https://zmdlogs.com"


def ranking(slug: str, boss_name: str, *, rows: int | None = None):
    payload = ranking_payload_with_rows()
    payload["bossSlug"] = slug
    payload["bossName"] = boss_name
    if rows is not None:
        payload["rows"] = payload["rows"][:rows]
    return parse_boss_ranking(payload)


class NormalizeTests(unittest.TestCase):
    def test_labels_aliases_and_suffixes(self) -> None:
        self.assertEqual(len(PROFESSIONS), 6)
        for typed, label in (
            ("突击", "突击"),
            ("术师", "术士"),
            ("术士", "术士"),
            ("突击位", "突击"),
            ("突击干员", "突击"),
            ("辅助职业", "辅助"),
            ("奶", "辅助"),
            ("Caster", "术士"),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(normalize_profession(typed), label)
        for junk in ("刺客", "", "突击手手"):
            with self.subTest(typed=junk):
                self.assertIsNone(normalize_profession(junk))

    def test_the_catalog_carries_the_profession_as_the_game_spells_it(self) -> None:
        types = parse_character_types(
            {
                "entries": [
                    {
                        "name": "大潘",
                        "charTypeName": "物理",
                        "weaponTypeName": "斧",
                        "professionName": "突击",
                    },
                    {"name": "旧条目", "charTypeName": "物理"},
                ]
            }
        )

        self.assertEqual([t.profession for t in types], ["突击", ""])


class ClassBoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rankings = (
            ranking("dung01_group_bossrush02", "三位一体"),
            ranking("dung01_group_bossrush03", "罗丹", rows=3),
        )
        self.tallies = character_tallies(self.rankings)
        self.professions = {t.name: t.profession for t in self.tallies}
        # The catalog spells the caster 术师; rosters say 术士.
        self.catalog = {
            **{
                name: label.replace("术士", "术师")
                for name, label in self.professions.items()
            },
            "未上榜者": "近卫",
            "别人": "术师",
        }

    def test_by_profession_keeps_the_class_in_order(self) -> None:
        guards = by_profession(self.tallies, "近卫")
        casters = by_profession(self.tallies, "术士")

        self.assertEqual(
            [t.name for t in guards],
            [t.name for t in self.tallies if t.profession == "近卫"],
        )
        self.assertTrue(guards)
        self.assertEqual({t.profession for t in casters}, {"术士"})

    def test_unseen_characters_are_the_catalog_members_no_record_fields(self) -> None:
        self.assertEqual(
            unseen_characters(self.tallies, self.catalog, profession="近卫"),
            ("未上榜者",),
        )
        # 术师 in the catalog is the same class as 术士 in the rosters.
        self.assertEqual(
            unseen_characters(self.tallies, self.catalog, profession="术士"),
            ("别人",),
        )
        self.assertEqual(
            unseen_characters(self.tallies, self.catalog), ("未上榜者", "别人")
        )

    def test_the_text_lists_the_class_whole_and_names_the_fewest(self) -> None:
        guards = by_profession(self.tallies, "近卫")

        text = facts.format_character_tallies(
            guards, board_count=2, limit=1, profession="近卫", unseen=("未上榜者",)
        )

        self.assertIn("近卫角色各占几个", text)
        for tally in guards:
            with self.subTest(character=tally.name):
                self.assertIn(f"{tally.name} · 冠军 {tally.first_places}", text)
        # An absentee has zero of everything, so it joins the fewest.
        zeros = [t.name for t in guards if t.first_places == 0]
        self.assertIn(f"冠军最少：{'、'.join([*zeros, '未上榜者'])}（0 个）", text)
        self.assertIn("从未出现在公开记录里的近卫角色：未上榜者", text)

    def test_the_unfiltered_text_names_the_zero_champion_characters(self) -> None:
        zeros = [t.name for t in self.tallies if t.first_places == 0]

        text = facts.format_character_tallies(self.tallies, board_count=2, limit=1)

        self.assertTrue(zeros)
        self.assertIn(f"冠军 0 个：{'、'.join(zeros)}）", text)

    def test_the_page_makes_every_member_a_row_and_lists_the_absentees(self) -> None:
        guards = by_profession(self.tallies, "近卫")

        page = build_character_champions_page(
            guards,
            board_count=2,
            query="角色排名",
            web_base_url=WEB,
            profession="近卫",
            unseen=("未上榜者",),
        )
        html = TemplateRenderer.from_plugin_root(
            Path(__file__).parents[1]
        ).render_character_champions(
            guards,
            board_count=2,
            query="角色排名",
            web_base_url=WEB,
            profession="近卫",
            unseen=("未上榜者",),
        )

        self.assertEqual(page.profession, "近卫")
        self.assertEqual(len(page.rows), len(guards))
        self.assertEqual(page.others, ())
        self.assertEqual(page.unseen, ("未上榜者",))
        self.assertIn("近卫", page.header.title)
        self.assertIn("从未上榜", html)
        self.assertIn("未上榜者", html)


class RoutingTests(unittest.TestCase):
    def test_the_bare_command_takes_a_profession(self) -> None:
        route = parse_zmdlog_payload("角色排名 --职业 突击")
        self.assertEqual(route.kind, RouteKind.CHARACTER_STANDINGS)
        self.assertEqual(route.profession_filter, "突击")
        self.assertEqual(
            parse_zmdlog_payload("角色排名 --职业 术师").profession_filter, "术士"
        )
        self.assertIsNone(parse_zmdlog_payload("角色排名").profession_filter)

    def test_a_name_a_board_or_junk_rejects_the_option(self) -> None:
        for payload in (
            "角色排名 诀 --职业 突击",
            "罗丹 --职业 突击",
            "角色排名 --职业 刺客",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)


if __name__ == "__main__":
    unittest.main()
