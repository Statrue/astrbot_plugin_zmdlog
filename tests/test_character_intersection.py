"""Tests for ``--角色 A B``: rankings of teams fielding every named character."""

import unittest
from pathlib import Path

from core.characters import CharacterFilterScope
from core.models import parse_boss_ranking
from core.presentation import build_ranking_page
from core.render import TemplateRenderer
from core.routing import RouteParseError, parse_zmdlog_payload
from tests.helpers import ranking_payload_with_rows


class IntersectionRoutingTests(unittest.TestCase):
    def test_role_option_takes_every_name_up_to_the_next_option(self) -> None:
        route = parse_zmdlog_payload("罗丹 --角色 黎风 洛茜 --页 5")
        self.assertEqual(route.character_filter, "黎风 洛茜")
        self.assertEqual(route.ranking_page, 5)
        self.assertEqual(route.query, "罗丹")

        route = parse_zmdlog_payload("榜单 罗丹 -p 5 --角色 黎风 洛茜")
        self.assertEqual(route.character_filter, "黎风 洛茜")
        # A repeated name is one name.
        route = parse_zmdlog_payload("罗丹 --角色 黎风 黎风")
        self.assertEqual(route.character_filter, "黎风")
        # A single name works exactly as before.
        self.assertEqual(parse_zmdlog_payload("罗丹 --角色 lf").character_filter, "lf")

    def test_limits_and_errors(self) -> None:
        with self.assertRaisesRegex(RouteParseError, "最多"):
            parse_zmdlog_payload("罗丹 --角色 a b c d e")
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("罗丹 --角色")
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("罗丹 --角色 黎风 --角色 洛茜")
        # Other options still take exactly one value.
        with self.assertRaisesRegex(RouteParseError, "查询末尾"):
            parse_zmdlog_payload("罗丹 --页 2 额外内容")


class IntersectionPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ranking = parse_boss_ranking(ranking_payload_with_rows())

    def test_every_named_character_must_be_in_the_team(self) -> None:
        page = build_ranking_page(
            self.ranking,
            query="q",
            character_filter=("黎风", "洁尔佩塔"),
            character_filter_scope=CharacterFilterScope.ROSTER,
        )
        self.assertEqual([row.rank for row in page.rows], [1, 2, 4])
        self.assertEqual(page.record_count, 3)
        self.assertEqual(page.character_filters, ("黎风", "洁尔佩塔"))

        page = build_ranking_page(
            self.ranking,
            query="q",
            character_filter=("黎风", "洛茜"),
            character_filter_scope=CharacterFilterScope.ROSTER,
        )
        self.assertEqual(page.rows, ())
        self.assertEqual(page.record_count, 0)

    def test_a_single_name_still_behaves_as_before(self) -> None:
        page = build_ranking_page(self.ranking, query="q", character_filter="黎风")

        self.assertEqual([row.rank for row in page.rows], [1, 2, 4])
        self.assertEqual(page.character_filters, ("黎风",))

    def test_the_page_counts_the_teams_fielding_every_name(self) -> None:
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])

        html = renderer.render_ranking(
            self.ranking,
            query="q",
            character_filter=("黎风", "洁尔佩塔"),
            character_filter_scope=CharacterFilterScope.ROSTER,
        )

        self.assertIn("<b>3</b> 条公开记录", html)
        self.assertNotIn("公开账号3<", html)
        # Each row still marks only its own main C.
        self.assertEqual(html.count("i-face is-lead"), 3)


if __name__ == "__main__":
    unittest.main()
