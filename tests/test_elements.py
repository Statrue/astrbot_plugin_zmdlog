"""Elements: the catalog label, the aliases people type, the page rings."""

import asyncio
import logging
import unittest

from core.client import ZmdLogsClientError
from core.datasource import ZmdLogsDataSource
from core.elements import ELEMENTS, element_key, normalize_element
from core.models import parse_boss_ranking, parse_character_types
from core.presentation import build_character_champions_page, build_ranking_page
from core.routing import RouteKind, RouteParseError, parse_zmdlog_payload
from core.settings import PluginSettings
from core.standings import character_tallies
from tests.helpers import ranking_payload_with_rows

CATALOG = {
    "kind": "character",
    "count": 4,
    "entries": [
        {
            "id": "chr_0001",
            "name": "管理员",
            "charTypeName": "物理",
            "weaponTypeName": "单手剑",
        },
        {
            "id": "chr_0001b",
            "name": "管理员",
            "charTypeName": "物理",
            "weaponTypeName": "单手剑",
        },
        {
            "id": "chr_0002",
            "name": "提弗洛斯",
            "charTypeName": "自然",
            "weaponTypeName": "手铳",
        },
        {"id": "chr_0003", "name": "无类型", "charTypeName": None},
        "not a dict",
    ],
}


def run(coro):
    return asyncio.run(coro)


class NormalizeTests(unittest.TestCase):
    def test_labels_aliases_and_suffixes_resolve(self) -> None:
        for typed, label in (
            ("物理", "物理"),
            ("火", "灼热"),
            ("火属性", "灼热"),
            ("雷队", "电磁"),
            ("ICE", "寒冷"),
            (" 自然系 ", "自然"),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(normalize_element(typed), label)

    def test_anything_else_is_not_an_element(self) -> None:
        for typed in ("", "光", "提弗洛斯", "属性"):
            with self.subTest(typed=typed):
                self.assertIsNone(normalize_element(typed))

    def test_every_label_has_a_css_key(self) -> None:
        self.assertEqual(len({element_key(label) for label in ELEMENTS}), 5)
        self.assertIsNone(element_key(None))
        self.assertIsNone(element_key("光"))


class CatalogTests(unittest.TestCase):
    def test_the_catalog_is_read_leniently(self) -> None:
        types = parse_character_types(CATALOG)

        by_name = {entry.name: entry for entry in types}
        self.assertEqual(by_name["提弗洛斯"].element, "自然")
        self.assertEqual(by_name["提弗洛斯"].weapon_type, "手铳")
        self.assertEqual(by_name["管理员"].element, "物理")
        self.assertNotIn("无类型", by_name)


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def get_character_types(self):
        self.calls += 1
        if self.fail:
            raise ZmdLogsClientError("offline")
        return parse_character_types(CATALOG)


class DataSourceTests(unittest.TestCase):
    def test_the_catalog_is_read_once_and_kept_on_failure(self) -> None:
        client = FakeClient()
        source = ZmdLogsDataSource(
            client,
            settings=PluginSettings(),
            data_dir=None,
            logger=logging.getLogger("t"),
        )

        first = run(source.get_character_types())
        client.fail = True
        second = run(source.get_character_types())

        self.assertEqual(first["提弗洛斯"].element, "自然")
        self.assertEqual(second, first)
        self.assertEqual(client.calls, 1)


class RoutingTests(unittest.TestCase):
    def test_the_option_is_normalised_and_scoped(self) -> None:
        route = parse_zmdlog_payload("罗丹 --属性 火")
        self.assertEqual(route.kind, RouteKind.SMART_QUERY)
        self.assertEqual(route.element_filter, "灼热")
        self.assertEqual(
            parse_zmdlog_payload("榜单 罗丹 --属性 物理").element_filter, "物理"
        )
        bare = parse_zmdlog_payload("角色排名 --属性 电磁")
        self.assertEqual(bare.kind, RouteKind.CHARACTER_STANDINGS)
        self.assertEqual(bare.element_filter, "电磁")

    def test_bad_values_and_bad_routes_are_refused(self) -> None:
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("罗丹 --属性 光")
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("角色排名 诀 --属性 物理")
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("账号 someone --属性 物理")


class PageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ranking = parse_boss_ranking(ranking_payload_with_rows())
        lead = self.ranking.rows[0].character_name
        member = next(
            entry.character_name
            for entry in self.ranking.rows[0].roster_entries
            if entry.character_name != lead
        )
        self.elements = {lead: "自然", member: "物理"}
        self.lead = lead

    def test_ranking_page_filters_on_main_c_element_and_rings_avatars(self) -> None:
        page = build_ranking_page(
            self.ranking,
            query="罗丹",
            element_filter="自然",
            elements=self.elements,
        )

        self.assertEqual(page.element_filter, "自然")
        self.assertTrue(all(row.character_name == self.lead for row in page.rows))
        self.assertEqual(page.filtered_count, len(page.rows))
        keys = {m.element_key for row in page.rows for m in row.roster if m.element_key}
        self.assertTrue(keys <= {"natural", "physical"})
        self.assertTrue(keys)

    def test_an_element_nobody_leads_with_leaves_the_page_empty(self) -> None:
        page = build_ranking_page(
            self.ranking, query="罗丹", element_filter="电磁", elements=self.elements
        )

        self.assertEqual(page.rows, ())
        self.assertEqual(page.filtered_count, 0)

    def test_the_champions_page_names_the_element_it_was_filtered_to(self) -> None:
        tallies = character_tallies((self.ranking,))
        kept = tuple(t for t in tallies if self.elements.get(t.name) == "自然")

        page = build_character_champions_page(
            kept,
            board_count=1,
            query="角色排名",
            element="自然",
            elements=self.elements,
        )

        self.assertIn("自然", page.header.title)
        self.assertTrue(all(row.element_key == "natural" for row in page.rows))


if __name__ == "__main__":
    unittest.main()
