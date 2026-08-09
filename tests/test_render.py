import tempfile
import unittest
from pathlib import Path

from core.presentation import format_duration, format_number
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
