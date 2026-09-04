import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from core.matcher import MatchChoice, MatchLevel, MatchTarget, TargetType
from core.models import (
    BossRanking,
    BossRankingRow,
    ContractTag,
    parse_battle_detail,
    parse_public_user_rankings,
)
from core.presentation import (
    PresentationError,
    build_dungeon_top3_page,
    build_ranking_page,
    format_duration,
    format_number,
)
from core.render import (
    AssetCache,
    LongImageRenderer,
    RenderError,
    TemplateConfigurationError,
    TemplateRenderer,
)
from tests.helpers import (
    battle_detail_payload,
    make_card,
    public_user_rankings_payload,
)


class AssetCacheTests(unittest.TestCase):
    def _put(self, cache, url, size=4, now=0.0, status=200):
        cache.put(
            url,
            status=status,
            content_type="image/png",
            body=b"x" * size,
            now=now,
        )

    def test_entries_expire_by_ttl(self) -> None:
        cache = AssetCache(ttl_seconds=10)
        self._put(cache, "u", now=0.0)

        self.assertIsNotNone(cache.get("u", now=9.9))
        self.assertIsNone(cache.get("u", now=10.0))
        self.assertEqual(len(cache), 0)

    def test_total_byte_cap_evicts_oldest_first(self) -> None:
        cache = AssetCache(max_total_bytes=10, max_item_bytes=10)
        self._put(cache, "a", size=4, now=0.0)
        self._put(cache, "b", size=4, now=1.0)
        self._put(cache, "c", size=4, now=2.0)

        self.assertIsNone(cache.get("a", now=2.0))
        self.assertIsNotNone(cache.get("b", now=2.0))
        self.assertIsNotNone(cache.get("c", now=2.0))
        self.assertEqual(cache.total_bytes, 8)

    def test_an_oversized_item_is_not_stored(self) -> None:
        cache = AssetCache(max_item_bytes=3)
        self._put(cache, "big", size=4)

        self.assertIsNone(cache.get("big", now=0.0))
        self.assertEqual(cache.total_bytes, 0)

    def test_overwriting_a_url_replaces_its_bytes(self) -> None:
        cache = AssetCache()
        self._put(cache, "u", size=4, now=0.0)
        self._put(cache, "u", size=6, now=1.0)

        self.assertEqual(cache.total_bytes, 6)
        self.assertEqual(len(cache), 1)


class TemplateRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parents[1]
        self.renderer = TemplateRenderer.from_plugin_root(self.root)

    def test_help_is_self_contained_and_uses_manifest_version(self) -> None:
        html = self.renderer.render_help(command_prefix="!")

        self.assertIn("!zmdlog 榜单", html)
        self.assertIn("!zmdlog 战报 | 配装 | 技能 | 技能轴", html)
        self.assertIn("ZmdLogBot", html)
        self.assertIn("v0.9.0", html)
        self.assertIn("data:image/svg+xml;base64,", html)
        self.assertIn("@font-face", html)
        self.assertNotIn("astrbot_plugin_zmdlog", html)
        self.assertNotIn("/zmdlog", html)
        self.assertNotIn("固定口径", html)
        self.assertNotIn("影拓4", html)

    def test_metadata_uses_current_plugin_identity(self) -> None:
        metadata = (self.root / "metadata.yaml").read_text(encoding="utf-8")

        self.assertIn("name: astrbot_plugin_zmdlog", metadata)
        self.assertIn("display_name: ZmdLogBot", metadata)
        self.assertIn(
            "repo: https://github.com/Statrue/astrbot_plugin_zmdlog",
            metadata,
        )
        self.assertNotIn("astrbot_plugin_zmdbot", metadata)

    def test_account_page_shows_exact_identity_and_best_records(self) -> None:
        payload = public_user_rankings_payload()
        payload["accountDisplayName"] = "测试<script>账号"
        account = parse_public_user_rankings(payload)

        html = self.renderer.render_account(
            account,
            query="usr_1234567890abcdef",
            web_base_url="https://zmdlogs.com",
        )

        self.assertIn("测试&lt;script&gt;账号", html)
        self.assertIn("usr_1234567890abcdef", html)
        self.assertIn("https://zmdlogs.com/records/usr_1234567890abcdef", html)
        self.assertIn("110,061.2", html)
        self.assertIn("队伍总 DPS", html)
        self.assertIn("95%", html)

    def test_battle_page_uses_compact_detail_fields_and_resolves_avatar(self) -> None:
        battle = parse_battle_detail(battle_detail_payload())

        html = self.renderer.render_battle(
            battle,
            query="https://zmdlogs.com/battle/btl_upload_abcdef123456",
            web_base_url="https://zmdlogs.com",
        )

        self.assertIn("“碾骨之拳”罗丹", html)
        self.assertIn("110,061.2", html)
        self.assertIn("97,325.01", html)
        self.assertIn("26,428.42", html)
        self.assertIn("81.2%", html)
        self.assertIn(
            "https://zmdlogs.com/images/character/luoxi.png",
            html,
        )
        self.assertNotIn("ignored", html)

    def test_specific_ranking_defaults_to_ten_and_supports_top_thirty(self) -> None:
        ranking = BossRanking(
            boss_slug="test-boss",
            boss_name="测试首领",
            dungeon_name="测试副本",
            profession_groups=(),
            rows=tuple(
                BossRankingRow(
                    rank=rank,
                    score_percent=100,
                    battle_id=f"battle-{rank}",
                    battle_end_at="2026-01-01T00:00:00Z",
                    character_name=f"角色{rank}",
                    character_profession="近战",
                    account_id=f"account-{rank}",
                    account_display_name=f"公开账号{rank}",
                    dps=100_000 - rank,
                    duration_ms=60_000,
                    roster_summary=(),
                    roster_entries=(),
                )
                for rank in range(1, 36)
            ),
        )

        page = build_ranking_page(
            ranking,
            query="测试",
        )

        self.assertEqual(page.row_count, 35)
        self.assertEqual(len(page.rows), 10)
        self.assertEqual(page.rows[-1].rank, 10)
        self.assertFalse(page.show_contract_score)
        self.assertEqual(page.header.title, "测试首领")
        self.assertEqual(page.header.subtitle, "测试副本")
        self.assertEqual(page.header.matched_name, "测试副本 · 测试首领")

        expanded_page = build_ranking_page(
            ranking,
            query="测试",
            display_limit=30,
        )
        self.assertEqual(len(expanded_page.rows), 30)
        self.assertEqual(expanded_page.rows[-1].rank, 30)

        expanded_html = self.renderer.render_ranking(
            ranking,
            query="测试",
            ranking_limit=30,
        )
        self.assertIn("公开账号30", expanded_html)
        self.assertNotIn("公开账号31", expanded_html)

    def test_specific_ranking_rejects_out_of_range_display_limit(self) -> None:
        ranking = BossRanking(
            boss_slug="test-boss",
            boss_name="测试首领",
            dungeon_name="测试副本",
            profession_groups=(),
            rows=(),
        )

        for display_limit in (0, 31, True):
            with self.subTest(display_limit=display_limit):
                with self.assertRaises(PresentationError):
                    build_ranking_page(
                        ranking,
                        query="测试",
                        display_limit=display_limit,
                    )

    def test_contract_ranking_only_shows_score(self) -> None:
        ranking = BossRanking(
            boss_slug="indie_group_ccdg",
            boss_name="破潮之像",
            dungeon_name="危机合约",
            profession_groups=(),
            rows=(
                BossRankingRow(
                    rank=1,
                    score_percent=100,
                    battle_id="secret-battle-id",
                    battle_end_at="2026-01-01T00:00:00Z",
                    character_name="狼卫",
                    character_profession="术士",
                    account_id="account-1",
                    account_display_name="公开账号",
                    dps=17_184.36,
                    duration_ms=385_648,
                    roster_summary=(),
                    roster_entries=(),
                    contract_tag_score=52,
                    contract_tags=(
                        ContractTag(
                            tag_id=1,
                            score=2,
                            name="队列：折刃",
                        ),
                    ),
                ),
            ),
        )

        page = build_ranking_page(ranking, query="危机合约")
        html = self.renderer.render_ranking(ranking, query="危机合约")

        self.assertEqual(page.header.title, "危机合约")
        self.assertEqual(page.header.subtitle, "活动竞速")
        self.assertEqual(page.header.matched_name, "危机合约")
        self.assertNotIn("破潮之像", html)
        self.assertIn("合约分数", html)
        self.assertIn("<b>52</b>", html)
        self.assertIn('<span class="head-right">DPS</span>', html)
        self.assertIn('<span class="head-right">用时</span>', html)
        self.assertNotIn("DPS / 用时", html)
        self.assertIn("<strong>公开账号", html)
        self.assertNotIn("队列：折刃", html)
        self.assertNotIn("secret-battle-id", html)
        self.assertNotIn("战斗详情", html)

    def test_public_account_column_uses_bold_style(self) -> None:
        css = (self.root / "resources" / "ranking" / "ranking.css").read_text(
            encoding="utf-8",
        )

        self.assertRegex(
            css,
            r"\.cell-id strong\s*\{[^}]*font-weight:\s*800;",
        )

    def test_top_three_template_does_not_leak_ranking_fields(self) -> None:
        card = make_card(
            "secret-slug",
            "首领<script>",
            "完整副本名",
            with_run=True,
        )
        html = self.renderer.render_all_top3((card,), query="<全部>")

        self.assertIn("首领&lt;script&gt;", html)
        self.assertIn("&lt;全部&gt;", html)
        self.assertNotIn("secret-slug", html)
        self.assertNotIn("battle-secret-slug", html)
        self.assertIn("1:01.234", html)

    def test_dungeon_scope_groups_cards_by_dungeon(self) -> None:
        cards = (
            make_card("a-1", "榜单甲", "影拓丰碑1期", with_run=True),
            make_card("b-1", "榜单乙", "影拓丰碑2期", with_run=True),
            make_card("a-2", "榜单丙", "影拓丰碑1期", with_run=True),
        )
        choice = MatchChoice(
            target=MatchTarget(
                target_type=TargetType.DUNGEON_SCOPE,
                key="scope:影拓丰碑",
                name="影拓丰碑1—2期",
                dungeon_names=("影拓丰碑1期", "影拓丰碑2期"),
                boss_slugs=tuple(card.boss_slug for card in cards),
                query_text="丰碑",
            ),
            level=MatchLevel.NORMALIZED_EXACT,
            score=1.0,
            matched_text="丰碑",
        )

        page = build_dungeon_top3_page(choice, cards, query="丰碑")
        html = self.renderer.render_dungeon_top3(choice, cards, query="丰碑")

        self.assertTrue(page.group_by_dungeon)
        self.assertEqual(
            tuple(group.dungeon_name for group in page.card_groups),
            ("影拓丰碑1期", "影拓丰碑2期"),
        )
        self.assertEqual(
            tuple(len(group.cards) for group in page.card_groups),
            (2, 1),
        )
        self.assertEqual(html.count('class="dungeon-group"'), 2)
        self.assertNotIn('class="top3-card-dungeon"', html)
        self.assertLess(
            html.index("<h3>影拓丰碑1期</h3>"),
            html.index("<h3>影拓丰碑2期</h3>"),
        )
        self.assertIn("2 个榜单", html)
        self.assertIn("1 个榜单", html)

    def test_missing_background_is_a_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resources = root / "resources"
            resources.mkdir()
            metadata = root / "metadata.yaml"
            metadata.write_text('version: "1.0.0"\n', encoding="utf-8")

            with self.assertRaises(TemplateConfigurationError):
                TemplateRenderer(
                    resources,
                    metadata,
                    resources / "missing.jpg",
                )

    def test_relative_avatar_paths_resolve_against_web_base_url(self) -> None:
        ranking = BossRanking(
            boss_slug="test-boss",
            boss_name="测试首领",
            dungeon_name="测试副本",
            profession_groups=(),
            rows=(
                BossRankingRow(
                    rank=1,
                    score_percent=100,
                    battle_id="battle-1",
                    battle_end_at="2026-01-01T00:00:00Z",
                    character_name="洛茜",
                    character_profession="近卫",
                    account_id="account-1",
                    account_display_name="公开账号",
                    dps=1.0,
                    duration_ms=1_000,
                    roster_summary=(),
                    roster_entries=(),
                    character_avatar_url="/images/character/luoxi.png",
                ),
                BossRankingRow(
                    rank=2,
                    score_percent=90,
                    battle_id="battle-2",
                    battle_end_at="2026-01-01T00:00:00Z",
                    character_name="卡缪",
                    character_profession="重装",
                    account_id="account-2",
                    account_display_name="公开账号2",
                    dps=1.0,
                    duration_ms=1_000,
                    roster_summary=(),
                    roster_entries=(),
                    character_avatar_url="javascript:alert(1)",
                ),
            ),
        )

        page = build_ranking_page(
            ranking,
            query="测试",
            web_base_url="https://zmdlogs.com",
        )
        self.assertEqual(
            page.rows[0].character_avatar_url,
            "https://zmdlogs.com/images/character/luoxi.png",
        )
        self.assertIsNone(page.rows[1].character_avatar_url)

        # Without a base URL only absolute HTTP(S) URLs survive.
        page = build_ranking_page(ranking, query="测试")
        self.assertIsNone(page.rows[0].character_avatar_url)

        html = self.renderer.render_all_top3(
            (make_card("a", "首领", "副本", with_run=True),),
            query="榜单",
            web_base_url="https://zmdlogs.com",
        )
        self.assertNotIn("javascript:", html)

    def test_number_and_duration_formats(self) -> None:
        self.assertEqual(format_duration(61_234), "1:01.234")
        self.assertEqual(format_number(123_456.78), "123,456.78")


class LongImageValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parents[1]

    async def test_missing_page_frame_is_a_render_error(self) -> None:
        class MissingPage:
            async def evaluate(self, script):
                return None

        renderer = object.__new__(LongImageRenderer)
        with self.assertRaises(RenderError):
            await renderer._validate_page(MissingPage())

    async def test_capture_failure_keeps_rendered_html_for_fallback(self) -> None:
        renderer = LongImageRenderer(self.root)

        async def failing_capture(html: str, page_kind: str) -> str:
            raise RenderError("browser capture failed")

        renderer._capture_once = failing_capture
        with self.assertRaises(RenderError) as context:
            await renderer.render_help(command_prefix="/")

        self.assertIn("/zmdlog", context.exception.html or "")
        self.assertIsNone(RenderError("renderer is unavailable").html)
        await renderer.close()

    async def test_default_output_directory_uses_current_plugin_name(self) -> None:
        renderer = LongImageRenderer(self.root)

        self.assertEqual(renderer.output_dir.name, "astrbot_plugin_zmdlog")
        await renderer.close()

    async def test_render_concurrency_is_bounded(self) -> None:
        renderer = LongImageRenderer(
            self.root,
            max_concurrent_renders=2,
        )
        release = asyncio.Event()
        two_started = asyncio.Event()
        active = 0
        peak = 0

        async def capture_once(html: str, page_kind: str) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
            try:
                await release.wait()
                return page_kind
            finally:
                active -= 1

        renderer._capture_once = capture_once
        tasks = tuple(
            asyncio.create_task(renderer._capture("", f"page-{index}"))
            for index in range(6)
        )
        await asyncio.wait_for(two_started.wait(), timeout=1)
        await asyncio.sleep(0)

        self.assertEqual(peak, 2)
        self.assertEqual(active, 2)

        release.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(len(results), 6)
        self.assertEqual(peak, 2)
        await renderer.close()

    async def test_output_pruning_removes_expired_and_excess_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            renderer = LongImageRenderer(
                self.root,
                output_dir=output_dir,
                output_ttl_seconds=60,
                max_output_files=2,
            )
            now = time.time()
            stale = output_dir / "zmd-help-stale.png"
            recent_paths = tuple(
                output_dir / f"zmd-help-recent-{index}.png"
                for index in range(3)
            )
            unrelated = output_dir / "keep.txt"
            for path in (stale, *recent_paths, unrelated):
                path.write_bytes(b"test")
            os.utime(stale, (now - 120, now - 120))
            for index, path in enumerate(recent_paths):
                modified_at = now - (3 - index)
                os.utime(path, (modified_at, modified_at))

            await renderer._prune_output_files()

            self.assertFalse(stale.exists())
            self.assertFalse(recent_paths[0].exists())
            self.assertTrue(recent_paths[1].exists())
            self.assertTrue(recent_paths[2].exists())
            self.assertTrue(unrelated.exists())
            await renderer.close()

    async def test_completed_output_expires_after_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            renderer = LongImageRenderer(
                self.root,
                output_dir=output_dir,
                output_ttl_seconds=0.01,
            )
            output_path = output_dir / "zmd-help-expiring.png"
            output_path.write_bytes(b"test")

            await renderer._complete_output(output_path)
            await asyncio.sleep(0.05)

            self.assertFalse(output_path.exists())
            self.assertNotIn(output_path, renderer._created_files)
            await renderer.close()


if __name__ == "__main__":
    unittest.main()
