"""Tests for the 0.4.0 features: character statistics, roster page, --角色."""

import unittest
from pathlib import Path

import httpx

from core.candidates import CandidateStore, CandidateView, format_candidates
from core.characters import (
    CharacterResolutionStatus,
    ranking_character_names,
    resolve_character_name,
)
from core.client import ZmdLogsClient, ZmdLogsProtocolError
from core.matcher import AliasConfig, MatchStatus, RankingMatcher, TargetType
from core.models import (
    ModelValidationError,
    parse_boss_ranking,
    parse_character_statistics,
)
from core.presentation import (
    build_character_stats_page,
    build_ranking_page,
    build_roster_page,
)
from core.render import TemplateRenderer
from core.routing import RouteKind, RouteParseError, parse_zmdlog_payload
from tests.helpers import (
    character_statistics_payload,
    make_card,
    ranking_payload_with_rows,
)


class OptionRoutingTests(unittest.TestCase):
    def test_character_stats_routes(self) -> None:
        route = parse_zmdlog_payload("角色")
        self.assertEqual(route.kind, RouteKind.CHARACTER_STATS)
        self.assertEqual(route.query, "")
        self.assertEqual(route.stats_range, "all")
        self.assertEqual(route.stats_potential, "all")

        route = parse_zmdlog_payload("角色 罗丹 --潜能 0 --范围 7天")
        self.assertEqual(route.kind, RouteKind.CHARACTER_STATS)
        self.assertEqual(route.query, "罗丹")
        self.assertEqual(route.stats_range, "7d")
        self.assertEqual(route.stats_potential, "0")

        route = parse_zmdlog_payload("角色 --range 30D --potential 1-5")
        self.assertEqual(route.stats_range, "30d")
        self.assertEqual(route.stats_potential, "1-5")

    def test_roster_route(self) -> None:
        route = parse_zmdlog_payload("阵容 罗丹 --top 30")
        self.assertEqual(route.kind, RouteKind.ROSTER_QUERY)
        self.assertEqual(route.query, "罗丹")
        self.assertEqual(route.ranking_top, 30)
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("阵容")

    def test_character_filter_option(self) -> None:
        route = parse_zmdlog_payload("罗丹 --角色 黎风 --top 5")
        self.assertEqual(route.kind, RouteKind.SMART_QUERY)
        self.assertEqual(route.query, "罗丹")
        self.assertEqual(route.character_filter, "黎风")
        self.assertEqual(route.ranking_top, 5)
        route = parse_zmdlog_payload("榜单 罗丹 --char lf")
        self.assertEqual(route.kind, RouteKind.RANKING_QUERY)
        self.assertEqual(route.character_filter, "lf")

    def test_options_rejected_where_they_do_not_apply(self) -> None:
        cases = {
            "角色 罗丹 --top 5": "--top",
            "角色 罗丹 --角色 黎风": "--角色",
            "阵容 罗丹 --范围 7d": "--范围",
            "罗丹 --潜能 0": "--潜能",
            "榜单 --角色 黎风": "--角色",
            "账号 usr_x --角色 黎风": "--角色",
            "角色 --范围 3d": "--范围",
            "角色 --潜能 6": "--潜能",
            "罗丹 --范围": "--范围",
            "罗丹 --角色 黎风 --角色 洛茜": "--角色",
            "罗丹 --角色 黎风 多余": "--角色",
            "罗丹 --unknown 1": "--unknown",
        }
        for payload, fragment in cases.items():
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RouteParseError, fragment):
                    parse_zmdlog_payload(payload)


class CharacterStatisticsModelTests(unittest.TestCase):
    def test_parse_keeps_ranked_and_insufficient_rows(self) -> None:
        stats = parse_character_statistics(character_statistics_payload())

        self.assertEqual(stats.scope, "boss")
        self.assertEqual(stats.metric, "dps")
        self.assertEqual(len(stats.rows), 5)
        self.assertEqual(stats.rows[0].rank, 1)
        self.assertEqual(stats.rows[0].outliers, ((360_000.0, 2),))
        self.assertIsNone(stats.rows[3].rank)
        self.assertTrue(stats.rows[3].insufficient_samples)
        self.assertIsNone(stats.rows[4].median)

    def test_non_dps_and_inconsistent_rank_are_rejected(self) -> None:
        with self.assertRaises(ModelValidationError):
            parse_character_statistics(character_statistics_payload(metric="rdps"))
        payload = character_statistics_payload()
        payload["rows"][0]["rank"] = None
        with self.assertRaises(ModelValidationError):
            parse_character_statistics(payload)
        payload = character_statistics_payload()
        payload["rows"][3]["rank"] = 9
        with self.assertRaises(ModelValidationError):
            parse_character_statistics(payload)


class CharacterStatisticsClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_carry_range_and_potential_but_no_metric(self) -> None:
        seen: list[httpx.URL] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            is_global = request.url.path.endswith("/bosses/character-statistics")
            scope = "all" if is_global else "boss"
            return httpx.Response(200, json=character_statistics_payload(scope=scope))

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        stats = await client.get_character_statistics(None)
        self.assertEqual(stats.scope, "all")
        await client.get_character_statistics(
            "dung01_group_bossrush02", time_range="7d", potential="0"
        )

        self.assertEqual(seen[0].path, "/api/bosses/character-statistics")
        self.assertEqual(
            seen[1].path, "/api/bosses/dung01_group_bossrush02/character-statistics"
        )
        for url in seen:
            self.assertNotIn("metric", str(url))
        self.assertEqual(dict(seen[0].params), {"range": "all", "potential": "all"})
        self.assertEqual(dict(seen[1].params), {"range": "7d", "potential": "0"})

    async def test_rdps_response_is_protocol_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=character_statistics_payload(metric="rdps"))

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        with self.assertRaises(ZmdLogsProtocolError):
            await client.get_character_statistics(None)


class CharacterNameResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ranking = parse_boss_ranking(ranking_payload_with_rows())
        self.names = ranking_character_names(self.ranking)

    def test_names_come_from_rows_rosters_and_usage(self) -> None:
        self.assertEqual(
            set(self.names), {"黎风", "洛茜", "卡缪", "佩丽卡", "洁尔佩塔"}
        )

    def test_exact_pinyin_and_prefix_levels(self) -> None:
        for query, expected in (
            ("黎风", "黎风"),
            (" 黎风 ", "黎风"),
            ("lifeng", "黎风"),
            ("LF", "黎风"),
            ("洁尔", "洁尔佩塔"),
            ("佩塔", "洁尔佩塔"),
        ):
            with self.subTest(query=query):
                result = resolve_character_name(query, self.names)
                self.assertEqual(result.status, CharacterResolutionStatus.MATCHED)
                self.assertEqual(result.name, expected)

    def test_ambiguous_and_missing(self) -> None:
        result = resolve_character_name("黎", ("黎风", "黎明", "洛茜"))
        self.assertEqual(result.status, CharacterResolutionStatus.AMBIGUOUS)
        self.assertEqual(set(result.candidates), {"黎风", "黎明"})
        # Unique prefix wins over a later substring hit.
        result = resolve_character_name("佩", self.names)
        self.assertEqual(result.status, CharacterResolutionStatus.MATCHED)
        self.assertEqual(result.name, "佩丽卡")
        result = resolve_character_name("不存在", self.names)
        self.assertEqual(result.status, CharacterResolutionStatus.NOT_FOUND)


class CharacterFilterPageTests(unittest.TestCase):
    def test_filter_keeps_global_rank_and_counts(self) -> None:
        ranking = parse_boss_ranking(ranking_payload_with_rows())
        page = build_ranking_page(
            ranking, query="测试 --角色 黎风", display_limit=2, character_filter="黎风"
        )
        self.assertEqual(page.character_filter, "黎风")
        self.assertEqual(page.filtered_count, 3)
        self.assertEqual(page.row_count, 5)
        self.assertEqual([row.rank for row in page.rows], [1, 2])

    def test_template_shows_filter_meta(self) -> None:
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])
        ranking = parse_boss_ranking(ranking_payload_with_rows())
        html = renderer.render_ranking(
            ranking, query="q", ranking_limit=10, character_filter="洛茜"
        )
        self.assertIn("主C：洛茜", html)
        self.assertIn("筛选出 1 条", html)
        self.assertIn("共 5 条公开排名", html)


class CharacterStatsPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])

    def test_box_geometry_and_sections(self) -> None:
        stats = parse_character_statistics(character_statistics_payload())
        page = build_character_stats_page(
            stats, query="角色 测试", web_base_url="https://zmdlogs.com"
        )

        self.assertEqual(page.header.title, "危境再现·三位一体")
        self.assertEqual(page.header.target_type, "角色统计")
        self.assertEqual(len(page.rows), 3)
        self.assertEqual([row.rank for row in page.rows], [1, 2, 3])
        top = page.rows[0]
        self.assertEqual(top.median, "120,000")
        self.assertEqual(top.maximum, "260,000")
        self.assertEqual(top.sample_count, 20)
        self.assertEqual(top.outlier_count, 2)
        self.assertIsNotNone(top.maximum_left)
        self.assertLessEqual(top.whisker_left, top.box_left)
        self.assertLessEqual(top.box_left, top.median_left)
        self.assertLessEqual(top.median_left, top.box_left + top.box_width)
        self.assertLessEqual(
            top.box_left + top.box_width, top.whisker_left + top.whisker_width
        )
        self.assertLessEqual(top.whisker_left + top.whisker_width, 100)
        self.assertIsNone(page.rows[1].maximum_left)
        self.assertEqual(
            top.character_avatar_url,
            "https://zmdlogs.com/images/character/chr_lifeng.png",
        )
        self.assertEqual(len(page.axis_labels), 5)
        # Axis follows the whiskers (168k) rather than the 260k outlier maximum.
        self.assertEqual(page.axis_labels[-1], "20万")
        self.assertEqual(top.maximum_left, 100.0)
        # Insufficient rows with zero samples are dropped from the chip list.
        self.assertEqual(
            [chip.character_name for chip in page.insufficient], ["佩丽卡"]
        )
        self.assertEqual(page.insufficient[0].sample_count, 3)

    def test_global_scope_labels(self) -> None:
        stats = parse_character_statistics(character_statistics_payload(scope="all"))
        page = build_character_stats_page(stats, query="角色")
        self.assertEqual(page.header.title, "全部副本")
        self.assertEqual(page.header.subtitle, "角色统计总榜")
        self.assertEqual(page.included_boss_count, 12)

    def test_template_renders_without_leaking_keys(self) -> None:
        stats = parse_character_statistics(character_statistics_payload())
        html = self.renderer.render_character_stats(
            stats, query="角色 测试", web_base_url="https://zmdlogs.com"
        )
        self.assertIn("六星角色 DPS 分布", html)
        self.assertIn("黎风", html)
        self.assertIn("样本不足", html)
        self.assertIn("佩丽卡", html)
        self.assertFalse(hasattr(page_row := build_character_stats_page(
            stats, query="角色 测试").rows[0], "character_key"), page_row)
        self.assertNotIn("无样本", html)
        body = html.split("<body>", 1)[1]
        self.assertNotIn("rdps", body.lower())

    def test_empty_ranked_rows_render_a_notice(self) -> None:
        payload = character_statistics_payload()
        payload["rows"] = payload["rows"][3:]
        stats = parse_character_statistics(payload)
        page = build_character_stats_page(stats, query="角色")
        self.assertEqual(page.rows, ())
        html = self.renderer.render_character_stats(stats, query="角色")
        self.assertIn("该范围内暂无足够样本", html)


class RosterPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])
        self.ranking = parse_boss_ranking(ranking_payload_with_rows())

    def test_combos_ignore_member_order_and_count_main_characters(self) -> None:
        page = build_roster_page(
            self.ranking, query="阵容 测试", web_base_url="https://zmdlogs.com"
        )

        self.assertEqual(page.header.target_type, "榜单阵容")
        self.assertEqual(page.row_count, 5)
        self.assertEqual(page.sample_size, 5)
        self.assertEqual(len(page.profession_usage), 6)
        self.assertEqual(page.profession_usage[0].profession, "近卫")
        self.assertEqual(page.profession_usage[0].entries[0].percent, "60%")
        self.assertEqual(page.profession_usage[0].entries[0].bar_width, 100.0)
        self.assertEqual(page.profession_usage[3].entries, ())

        self.assertEqual(len(page.combos), 2)
        first = page.combos[0]
        self.assertEqual(first.count, 3)
        self.assertEqual(first.percent, "60%")
        self.assertEqual(first.best_rank, 1)
        self.assertEqual(first.best_dps, "130,000.5")
        self.assertEqual(
            [member.character_name for member in first.members],
            ["黎风", "洁尔佩塔", "佩丽卡", "卡缪"],
        )
        self.assertEqual(
            [(main.character_name, main.count) for main in page.main_characters],
            [("黎风", 3), ("卡缪", 1), ("洛茜", 1)],
        )

    def test_display_limit_scopes_local_aggregation(self) -> None:
        page = build_roster_page(self.ranking, query="q", display_limit=2)
        self.assertEqual(page.sample_size, 2)
        self.assertEqual(page.combos[0].count, 2)
        self.assertEqual(page.row_count, 5)

    def test_template_does_not_leak_account_or_battle_ids(self) -> None:
        html = self.renderer.render_roster(
            self.ranking, query="阵容 测试", web_base_url="https://zmdlogs.com"
        )
        self.assertIn("职业位出场率", html)
        self.assertIn("前 5 名常见阵容", html)
        self.assertNotIn("usr_", html)
        self.assertNotIn("btl_upload", html)
        self.assertNotIn("公开账号1", html)


class BoardOnlyCandidateTests(unittest.TestCase):
    def test_expand_to_boards_flattens_dungeons(self) -> None:
        cards = (
            make_card("a", "影拓丰碑4期·山犼争王·苦难", "影拓丰碑4期 · 山中见犼"),
            make_card("b", "影拓丰碑4期·山犼争王·残酷", "影拓丰碑4期 · 山中见犼"),
            make_card("c", "其他", "其他副本"),
        )
        matcher = RankingMatcher(cards, AliasConfig.empty())
        result = matcher.match("山中见犼")
        self.assertEqual(result.status, MatchStatus.MATCHED)
        assert result.selected is not None
        self.assertEqual(result.selected.target.target_type, TargetType.DUNGEON)

        boards = matcher.expand_to_boards((result.selected,))
        self.assertEqual([choice.target.key for choice in boards], ["a", "b"])
        self.assertTrue(
            all(choice.target.target_type is TargetType.BOARD for choice in boards)
        )

    def test_candidate_store_keeps_view_and_options(self) -> None:
        cards = (
            make_card("a", "巨像一", "副本一"),
            make_card("b", "巨像二", "副本二"),
        )
        matcher = RankingMatcher(cards, AliasConfig.empty())
        result = matcher.match("巨")
        self.assertEqual(result.status, MatchStatus.AMBIGUOUS)
        store = CandidateStore(ttl_seconds=60)
        entry = store.remember(
            "巨",
            result.candidates,
            view=CandidateView.CHARACTER_STATS,
            stats_range="7d",
            stats_potential="0",
            now=0.0,
        )
        text = format_candidates(entry, ttl_seconds=60)
        self.assertIn("角色统计", text)
        self.assertIn(f"候选编号 {entry.code}", text)
        resolved = store.resolve(entry.code, "1", now=1.0)
        assert resolved is not None
        pending, _ = resolved
        self.assertEqual(pending.view, CandidateView.CHARACTER_STATS)
        self.assertEqual(pending.stats_range, "7d")
        self.assertEqual(pending.stats_potential, "0")


if __name__ == "__main__":
    unittest.main()
