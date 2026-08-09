import tempfile
import unittest
from pathlib import Path

from core.models import BossRanking, BossRankingRow, ContractTag
from core.presentation import (
    build_ranking_page,
    format_duration,
    format_number,
)
from core.render import (
    LongImageRenderer,
    RenderError,
    TemplateConfigurationError,
    TemplateRenderer,
)

from tests.helpers import make_card


class TemplateRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parents[1]
        self.renderer = TemplateRenderer.from_plugin_root(self.root)

    def test_help_is_self_contained_and_uses_manifest_version(self) -> None:
        html = self.renderer.render_help(command_prefix="!")

        self.assertIn("!zmdlog 榜单", html)
        self.assertIn("v0.1.0", html)
        self.assertIn("data:image/jpeg;base64,", html)
        self.assertNotIn("/zmdlog", html)
        self.assertNotIn("固定口径", html)
        self.assertNotIn("影拓4", html)

    def test_specific_ranking_is_limited_to_first_fifteen_rows(self) -> None:
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
        self.assertEqual(len(page.rows), 15)
        self.assertEqual(page.rows[-1].rank, 15)
        self.assertFalse(page.show_contract_score)

    def test_contract_ranking_only_shows_score(self) -> None:
        ranking = BossRanking(
            boss_slug="indie_group_ccdg",
            boss_name="危机合约",
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

        html = self.renderer.render_ranking(ranking, query="危机合约")

        self.assertIn("合约分数", html)
        self.assertIn("52 分", html)
        self.assertIn("<th>DPS</th>", html)
        self.assertIn("<th>用时</th>", html)
        self.assertNotIn("DPS / 用时", html)
        self.assertIn(
            'class="ranking-account">公开账号',
            html,
        )
        self.assertNotIn("队列：折刃", html)
        self.assertNotIn("secret-battle-id", html)
        self.assertNotIn("战斗详情", html)

    def test_public_account_column_uses_bold_style(self) -> None:
        css = (self.root / "resources" / "common" / "base.css").read_text(
            encoding="utf-8",
        )

        self.assertRegex(
            css,
            r"\.ranking-account\s*\{[^}]*font-weight:\s*800;",
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

    def test_number_and_duration_formats(self) -> None:
        self.assertEqual(format_duration(61_234), "1:01.234")
        self.assertEqual(format_number(123_456.78), "123,456.78")


class LongImageValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_page_frame_is_a_render_error(self) -> None:
        class MissingPage:
            async def evaluate(self, script):
                return None

        renderer = object.__new__(LongImageRenderer)
        with self.assertRaises(RenderError):
            await renderer._validate_page(MissingPage())


if __name__ == "__main__":
    unittest.main()
