import asyncio
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

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
    FONT_ORIGIN,
    AssetCache,
    LongImageRenderer,
    RenderError,
    TemplateConfigurationError,
    TemplateRenderer,
    read_plugin_version,
)
from tests.helpers import (
    battle_detail_payload,
    crisis_contract_tags,
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
        self.assertIn("终末地·藕粉铺子", html)
        # Read from the manifest rather than repeating it: the version is
        # bumped every release, and the point of the test is that the page
        # shows whatever metadata.yaml says.
        version = read_plugin_version(self.root / "metadata.yaml")
        self.assertIn(f"v{version}", html)
        self.assertIn("data:image/svg+xml;base64,", html)
        self.assertIn("@font-face", html)
        self.assertNotIn("astrbot_plugin_zmdlog", html)
        self.assertNotIn("/zmdlog", html)
        self.assertNotIn("固定口径", html)
        self.assertNotIn("影拓4", html)

    def test_metadata_uses_current_plugin_identity(self) -> None:
        metadata = (self.root / "metadata.yaml").read_text(encoding="utf-8")

        self.assertIn("name: astrbot_plugin_zmdlog", metadata)
        self.assertIn("display_name: 终末地·藕粉铺子", metadata)
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
        self.assertIn("DPS 为整队合计", html)
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
        # Not a contract record: no 合约分数, and no contract section at all
        # (the stylesheet's comments are inlined, so test the heading).
        self.assertNotIn("合约分数", html)
        self.assertNotIn("<h2>危机合约</h2>", html)

    def test_battle_page_lists_contract_tags_by_family_without_descriptions(
        self,
    ) -> None:
        payload = battle_detail_payload()
        tags = crisis_contract_tags()
        # Upstream has no sprite for some tags (101603 改写：热量汲取 is null).
        tags[-1]["iconUrl"] = None
        payload["battle"]["contractTags"] = tags
        payload["battle"]["contractTagScore"] = sum(tag["score"] for tag in tags)
        battle = parse_battle_detail(payload)

        html = self.renderer.render_battle(
            battle,
            query="btl_upload_526563531445",
            web_base_url="https://zmdlogs.com",
        )

        self.assertIn("<h2>危机合约</h2>", html)
        # A sprite covers the initial; without one the initial is the tile.
        self.assertIn(
            '<span class="contract-icon">折<img src="https://zmdlogs.com/images/'
            'contract-tag/icon_activity_contract_tag_208.png"',
            html,
        )
        self.assertIn('<span class="contract-icon">禁</span>', html)
        self.assertNotIn("icon_activity_contract_tag_104", html)
        self.assertIn("合约分数</span><strong>14 分</strong>", html)
        self.assertIn("每条 1–3 分，合计即合约分数", html)
        # The section is the record's preconditions, so it precedes the data.
        self.assertLess(
            html.index("<h2>危机合约</h2>"), html.index("<h2>战斗贡献</h2>")
        )
        # One block per family in canonical order, each with count and score.
        self.assertLess(
            html.index("<strong>队列</strong>"), html.index("<strong>改写</strong>")
        )
        self.assertLess(
            html.index("<strong>改写</strong>"), html.index("<strong>环境</strong>")
        )
        self.assertIn("<strong>队列</strong><span>2 条 · 5 分</span>", html)
        self.assertIn("<strong>改写</strong><span>2 条 · 3 分</span>", html)
        self.assertIn("<strong>环境</strong><span>2 条 · 6 分</span>", html)
        self.assertIn(
            '<span class="contract-name">环境：禁锢</span>'
            '<b class="contract-points">3</b>',
            html,
        )
        self.assertIn(
            "https://zmdlogs.com/images/contract-tag/icon_activity_contract_tag_208.png",
            html,
        )
        # The description is a raw game template and never reaches the page:
        # not its text, not a placeholder, not the colour markup.
        self.assertNotIn("禁止闪避", html)
        self.assertNotIn("dmg_scale", html)
        self.assertNotIn("color=#cc9900", html)

    def test_the_note_claims_the_total_only_when_the_total_is_drawn(self) -> None:
        # 合计即合约分数 points at the 合约分数 row, which is drawn on its own
        # condition. Tags without a score would leave the claim pointing at
        # nothing, so the clause goes when the row does.
        payload = battle_detail_payload()
        payload["battle"]["contractTags"] = crisis_contract_tags()
        payload["battle"]["contractTagScore"] = None

        html = self.renderer.render_battle(
            parse_battle_detail(payload),
            query="btl_upload_526563531445",
            web_base_url="https://zmdlogs.com",
        )

        self.assertIn("<h2>危机合约</h2>", html)
        self.assertIn("每条 1–3 分", html)
        self.assertNotIn("合计即合约分数", html)
        self.assertNotIn("合约分数</span>", html)

    def test_every_font_size_is_a_scale_token(self) -> None:
        # Colours were tokens from the first commit; sizes drifted into
        # nineteen values with half-pixels between them. The --fs-* scale in
        # base.css is now the only place a size may come from, and every
        # token a page uses must exist there.
        base_css = (self.root / "resources" / "common" / "base.css").read_text(
            encoding="utf-8"
        )
        root_start = base_css.index(":root {")
        root_block = base_css[root_start : base_css.index("}", root_start)]
        defined = set(re.findall(r"--fs-[a-z0-9-]+(?=:)", root_block))
        self.assertTrue(defined)

        raw_size = re.compile(r"font-size:\s*[0-9.]+(?:px|em|rem|%)")
        used: set[str] = set()
        for path in sorted((self.root / "resources").rglob("*.css")):
            css = path.read_text(encoding="utf-8")
            self.assertEqual(
                raw_size.findall(css), [], f"{path.name} sets a raw font-size"
            )
            used.update(re.findall(r"font-size:\s*var\((--fs-[a-z0-9-]+)\)", css))

        # The rule is about sizes, not about stylesheets. An inline
        # style="font-size: 13px" or an SVG font-size="13" would put an
        # untokenised size on the page and never be seen by a .css scan.
        # A token spelled inline is still a token, so only digits are refused.
        raw_inline = re.compile(r"font-size\s*[:=]\s*[\"']?\s*[0-9.]")
        for path in sorted((self.root / "resources").rglob("*.html")):
            markup = path.read_text(encoding="utf-8")
            self.assertEqual(
                raw_inline.findall(markup), [], f"{path.name} sets a raw font-size"
            )
            used.update(
                re.findall(r"font-size\s*[:=]\s*[\"']?\s*var\((--fs-[a-z0-9-]+)\)",
                           markup)
            )

        self.assertTrue(used)
        self.assertEqual(used - defined, set())

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


class AssetRoutingTests(unittest.TestCase):
    """The capture-time image gate: what it fetches, caches and refuses."""

    class FakeRequest:
        def __init__(self, url, resource_type="image"):
            self.url = url
            self.resource_type = resource_type

    class FakeRoute:
        def __init__(self, request, responses):
            self.request = request
            self._responses = responses
            self.fetched: list[str] = []
            self.fulfilled = None
            self.aborted = False

        async def fetch(self, url=None, max_redirects=None):
            target = url or self.request.url
            self.fetched.append(target)
            reply = self._responses.get(target)
            if reply is None:
                raise RuntimeError("no such asset")
            return reply

        async def fulfill(self, *, status, content_type, body):
            self.fulfilled = (status, content_type, body)

        async def abort(self):
            self.aborted = True

        async def continue_(self):
            self.aborted = False

    class FakeResponse:
        def __init__(self, status, body, content_type="image/png"):
            self.status = status
            self._body = body
            self.headers = {"content-type": content_type}

        async def body(self):
            return self._body

    ORIGIN = "https://zmdlogs.com"
    PATH = "/images/character/charremoteicon/icon_chr_0032.png"

    def _renderer(self):
        return LongImageRenderer(
            Path(__file__).parents[1],
            allowed_image_origins=(self.ORIGIN,),
        )

    def _thumb(self):
        from urllib.parse import quote

        return (
            f"{self.ORIGIN}/_next/image?url={quote(self.PATH, safe='')}&w=128&q=75"
        )

    def test_a_pages_images_are_fetched_before_the_capture(self) -> None:
        # One image per route-handler fetch, and those do not overlap: on a
        # link where an image takes a second, a dozen avatars cannot finish
        # inside the settle budget and come out as initials. Prefetching
        # them together is what keeps that off a slow host.
        renderer = self._renderer()
        other = "/images/character/charremoteicon/icon_chr_0028.png"
        renderer._asset_cache.put(
            self.ORIGIN + other,
            status=200,
            content_type="image/png",
            body=b"already here",
        )
        asked: list[str] = []

        async def fetch(url):
            asked.append(url)
            return "image/png", b"prefetched"

        renderer._fetch_image = fetch
        html = (
            f'<img src="{self.ORIGIN}{self.PATH}" alt="">'
            f'<img src="{self.ORIGIN}{self.PATH}" alt="">'
            f'<img src="{self.ORIGIN}{other}" alt="">'
            '<img src="https://evil.example/x.png" alt="">'
        )

        asyncio.run(renderer._prefetch_images(html))

        # The resized copy, once for the repeated image; the cached one and
        # the foreign origin are never asked for.
        self.assertEqual(asked, [self._thumb()])
        cached = renderer._asset_cache.get(self.ORIGIN + self.PATH)
        self.assertEqual(cached.body, b"prefetched")
        self.assertEqual(
            renderer._asset_cache.get(self.ORIGIN + other).body, b"already here"
        )

    def test_a_prefetched_image_costs_the_capture_no_request(self) -> None:
        renderer = self._renderer()

        async def fetch(url):
            return "image/png", b"prefetched"

        renderer._fetch_image = fetch
        asyncio.run(
            renderer._prefetch_images(f'<img src="{self.ORIGIN}{self.PATH}">')
        )
        route = self.FakeRoute(self.FakeRequest(self.ORIGIN + self.PATH), {})

        asyncio.run(renderer._route_asset_request(route))

        self.assertEqual(route.fetched, [])
        self.assertEqual(route.fulfilled, (200, "image/png", b"prefetched"))

    def test_prefetch_keeps_the_original_when_there_is_no_resized_copy(self) -> None:
        renderer = self._renderer()
        asked: list[str] = []

        async def fetch(url):
            asked.append(url)
            if url == self._thumb():
                return None
            return "image/png", b"full size"

        renderer._fetch_image = fetch

        asyncio.run(
            renderer._prefetch_images(f'<img src="{self.ORIGIN}{self.PATH}">')
        )

        self.assertEqual(asked, [self._thumb(), self.ORIGIN + self.PATH])
        self.assertEqual(
            renderer._asset_cache.get(self.ORIGIN + self.PATH).body, b"full size"
        )

    def test_an_unreachable_image_is_left_to_the_route_handler(self) -> None:
        renderer = self._renderer()

        async def fetch(url):
            return None

        renderer._fetch_image = fetch

        asyncio.run(
            renderer._prefetch_images(f'<img src="{self.ORIGIN}{self.PATH}">')
        )

        self.assertIsNone(renderer._asset_cache.get(self.ORIGIN + self.PATH))
        route = self.FakeRoute(
            self.FakeRequest(self.ORIGIN + self.PATH),
            {self._thumb(): self.FakeResponse(200, b"late")},
        )
        asyncio.run(renderer._route_asset_request(route))
        self.assertEqual(route.fulfilled, (200, "image/png", b"late"))

    def test_a_site_image_is_fetched_resized(self) -> None:
        # Upstream portraits are about 1 MB each; a page of them cannot load
        # inside the capture's image budget.
        renderer = self._renderer()
        route = self.FakeRoute(
            self.FakeRequest(self.ORIGIN + self.PATH),
            {self._thumb(): self.FakeResponse(200, b"small")},
        )

        asyncio.run(renderer._route_asset_request(route))

        self.assertEqual(route.fetched, [self._thumb()])
        self.assertEqual(route.fulfilled, (200, "image/png", b"small"))
        # Cached under the URL the page asked for, not the resized one.
        self.assertIsNotNone(renderer._asset_cache.get(self.ORIGIN + self.PATH))

    def test_the_original_is_used_when_there_is_no_resized_copy(self) -> None:
        renderer = self._renderer()
        route = self.FakeRoute(
            self.FakeRequest(self.ORIGIN + self.PATH),
            {
                self._thumb(): self.FakeResponse(404, b"gone"),
                self.ORIGIN + self.PATH: self.FakeResponse(200, b"whole"),
            },
        )

        asyncio.run(renderer._route_asset_request(route))

        self.assertEqual(route.fulfilled, (200, "image/png", b"whole"))

    def test_a_foreign_origin_is_refused(self) -> None:
        renderer = self._renderer()
        route = self.FakeRoute(self.FakeRequest("https://evil.example/a.png"), {})

        asyncio.run(renderer._route_asset_request(route))

        self.assertTrue(route.aborted)
        self.assertEqual(route.fetched, [])

    def test_a_redirect_is_refused(self) -> None:
        renderer = self._renderer()
        route = self.FakeRoute(
            self.FakeRequest(self.ORIGIN + self.PATH),
            {
                self._thumb(): self.FakeResponse(302, b""),
                self.ORIGIN + self.PATH: self.FakeResponse(302, b""),
            },
        )

        asyncio.run(renderer._route_asset_request(route))

        self.assertTrue(route.aborted)
        self.assertIsNone(route.fulfilled)


class RenderQueueTests(unittest.IsolatedAsyncioTestCase):
    """The semaphore bounds concurrency; the queue behind it needs bounds too."""

    def setUp(self) -> None:
        self.root = Path(__file__).parents[1]

    async def test_a_full_queue_is_refused_instead_of_joined(self) -> None:
        # The cap counts callers waiting for a slot, not the one rendering.
        renderer = LongImageRenderer(
            self.root,
            render_timeout_ms=1_000,
            max_concurrent_renders=1,
            max_queued_renders=2,
        )
        release = asyncio.Event()

        async def capture_once(html: str, page_kind: str) -> str:
            await release.wait()
            return page_kind

        renderer._capture_once = capture_once
        rendering = asyncio.create_task(renderer._capture("", "rendering"))
        waiting = [
            asyncio.create_task(renderer._capture("", f"waiting-{index}"))
            for index in range(2)
        ]
        for _ in range(4):
            await asyncio.sleep(0)
        self.assertEqual(renderer._queued_renders, 2)

        with self.assertRaises(RenderError) as refused:
            await renderer._capture("", "refused")
        self.assertIn("queued", str(refused.exception))

        release.set()
        self.assertEqual(await rendering, "rendering")
        self.assertEqual(await asyncio.gather(*waiting), ["waiting-0", "waiting-1"])
        # The slots are given back, so the next caller is served normally.
        self.assertEqual(await renderer._capture("", "later"), "later")
        await renderer.close()

    async def test_waiting_for_a_slot_is_under_a_timeout(self) -> None:
        renderer = LongImageRenderer(
            self.root, render_timeout_ms=1_000, max_concurrent_renders=1
        )
        # The wait is what is under test, not a real second of it.
        renderer.render_timeout_ms = 50
        release = asyncio.Event()

        async def capture_once(html: str, page_kind: str) -> str:
            await release.wait()
            return page_kind

        renderer._capture_once = capture_once
        held = asyncio.create_task(renderer._capture("", "held"))
        await asyncio.sleep(0)

        with self.assertRaises(RenderError) as timed_out:
            await renderer._capture("", "waiting")
        self.assertIn("render slot", str(timed_out.exception))

        release.set()
        await held
        await renderer.close()


class FakeRoute:
    def __init__(self, url: str, resource_type: str = "font") -> None:
        self.request = SimpleNamespace(url=url, resource_type=resource_type)
        self.fulfilled: dict | None = None
        self.aborted = False

    async def fulfill(self, **kwargs) -> None:
        self.fulfilled = kwargs

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        raise AssertionError("must not continue")

    async def fetch(self, **kwargs):
        raise AssertionError("must not fetch")


class FontDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """Chromium gets linked fonts served from memory; fallbacks get them embedded."""

    def setUp(self) -> None:
        self.root = Path(__file__).parents[1]
        self.templates = TemplateRenderer.from_plugin_root(self.root)
        if not self.templates.font_files:
            self.skipTest("bundled fonts are not present")

    def test_linked_fonts_keep_the_document_small(self) -> None:
        linked = self.templates.render_help(command_prefix="/", embed_fonts=False)
        embedded = self.templates.render_help(command_prefix="/")

        self.assertIn(f"{FONT_ORIGIN}/NotoSansSC-Regular.woff2", linked)
        self.assertNotIn("data:font", linked)
        self.assertIn("data:font/woff2;base64,", embedded)
        self.assertNotIn(FONT_ORIGIN, embedded)
        self.assertLess(len(linked), len(embedded) // 10)

    async def test_capture_error_carries_the_self_contained_copy(self) -> None:
        renderer = LongImageRenderer(self.root)
        captured: list[str] = []

        async def failing_capture(html: str, page_kind: str) -> str:
            captured.append(html)
            raise RenderError("browser capture failed")

        renderer._capture_once = failing_capture
        with self.assertRaises(RenderError) as context:
            await renderer.render_help(command_prefix="/")

        # Chromium was handed the small document; the AstrBot fallback cannot
        # reach the route handler, so its copy embeds the fonts.
        self.assertIn(FONT_ORIGIN, captured[0])
        self.assertIn("data:font/woff2", context.exception.html or "")
        await renderer.close()

    async def test_font_requests_are_served_from_memory(self) -> None:
        renderer = LongImageRenderer(
            self.root, allowed_image_origins=("https://zmdlogs.com",)
        )

        known = FakeRoute(f"{FONT_ORIGIN}/NotoSansSC-Regular.woff2")
        await renderer._route_asset_request(known)
        self.assertIsNotNone(known.fulfilled)
        self.assertEqual(
            known.fulfilled["body"],
            renderer.templates.font_files["NotoSansSC-Regular.woff2"],
        )

        unknown = FakeRoute(f"{FONT_ORIGIN}/other.woff2")
        await renderer._route_asset_request(unknown)
        self.assertTrue(unknown.aborted)

        # Only the reserved origin serves fonts; the image origins stay images.
        elsewhere = FakeRoute("https://zmdlogs.com/NotoSansSC-Regular.woff2")
        await renderer._route_asset_request(elsewhere)
        self.assertTrue(elsewhere.aborted)
        await renderer.close()
