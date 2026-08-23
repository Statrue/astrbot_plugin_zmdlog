"""Handler-level tests that drive ``main.py`` with fakes.

``main.py`` imports ``astrbot.api``, so these only run when AstrBot is
installed in the test environment (it is in the project venv); elsewhere the
whole module is skipped rather than failing. Everything here targets behaviour
the review found wrong at the seam between main.py and core/ — the places the
pure core tests cannot reach.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import (
    battle_detail_payload,
    hot_bosses_payload,
    public_user_rankings_payload,
    ranking_payload_with_rows,
)

try:
    import astrbot  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    astrbot = None

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT.parent))

if astrbot is not None:
    # main.py imports its own ``.core`` package, so the exception classes the
    # handlers catch are the package's, not the ``core.*`` modules the rest of
    # the suite imports from the repo root. Use the same identities here.
    from astrbot_plugin_zmdlog import main as plugin_main
    from astrbot_plugin_zmdlog.core.client import (
        ZmdLogsAPIError,
        ZmdLogsClientError,
    )
    from astrbot_plugin_zmdlog.core.models import (
        parse_battle_detail,
        parse_boss_ranking,
        parse_hot_bosses,
        parse_public_user_rankings,
    )
    from astrbot_plugin_zmdlog.core.render import RenderError

GROUP = "aiocqhttp:GroupMessage:1"
OTHER_GROUP = "aiocqhttp:GroupMessage:2"


class Reply:
    """Stands in for AstrBot's Reply component, matched by class name."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.message_str = text


class FakeEvent:
    def __init__(self, text: str, *, origin: str = GROUP, quoted: str | None = None):
        self._text = text
        self.unified_msg_origin = origin
        chain = [Reply(quoted)] if quoted is not None else []
        self.message_obj = SimpleNamespace(message_str=text, message=chain)

    def get_message_str(self) -> str:
        return self._text

    def plain_result(self, text: str):
        return ("plain", text)

    def image_result(self, path: str):
        return ("image", path)

    def is_admin(self) -> bool:
        return False

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_sender_id(self) -> str:
        return "111"


class FakeContext:
    def get_config(self, origin):
        return {"wake_prefix": ["/"]}


class FakeRenderer:
    async def render_battle(self, battle, **kwargs):
        return "/tmp/battle.png"

    async def render_account(self, account, **kwargs):
        return "/tmp/account.png"

    async def render_ranking(self, ranking, **kwargs):
        return "/tmp/ranking.png"

    async def render_all_top3(self, cards, **kwargs):
        return "/tmp/top3.png"


def run(coro):
    return asyncio.run(coro)


async def collect(agen):
    return [item async for item in agen]


@unittest.skipIf(astrbot is None, "AstrBot is not installed; main.py is unimportable")
class HandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep the plugin away from the real data dir and from Chromium.
        self._star_tools = plugin_main.StarTools
        plugin_main.StarTools = None
        self.plugin = plugin_main.ZmdLogBotPlugin(
            FakeContext(),
            {"auto_expand_battle_links": True, "rank_watch_enabled": False},
        )
        self.plugin.renderer = FakeRenderer()
        self.cards = parse_hot_bosses(hot_bosses_payload())

        async def list_hot_bosses():
            return self.cards

        self.plugin._list_hot_bosses = list_hot_bosses

    def tearDown(self) -> None:
        plugin_main.StarTools = self._star_tools

    # --- auto-expand cooldown -------------------------------------------------

    def _expand(self, text: str):
        return run(collect(self.plugin.expand_battle_link(FakeEvent(text))))

    def test_a_dead_link_is_answered_once_per_cooldown(self) -> None:
        async def missing(battle_id):
            raise ZmdLogsAPIError(404, "battle_not_found", "gone")

        self.plugin._get_battle_detail = missing
        link = "看 https://zmdlogs.com/battle/btl_upload_abcdef123456"

        first = self._expand(link)
        second = self._expand(link)

        self.assertEqual(
            first, [("plain", "链接对应的公开战报不存在、未公开或已删除。")]
        )
        self.assertEqual(second, [])

    def test_a_transient_failure_releases_the_cooldown(self) -> None:
        async def down(battle_id):
            raise ZmdLogsClientError("down")

        self.plugin._get_battle_detail = down
        link = "看 https://zmdlogs.com/battle/btl_upload_abcdef123456"

        first = self._expand(link)
        second = self._expand(link)

        self.assertEqual(first[0][0], "plain")
        self.assertEqual(second[0][0], "plain")

    def test_an_image_from_the_fallback_renderer_keeps_the_cooldown(self) -> None:
        async def detail(battle_id):
            return parse_battle_detail(battle_detail_payload())

        async def capture_fails(battle, **kwargs):
            raise RenderError("no chromium", html="<html></html>")

        async def fallback(error):
            return "/tmp/fallback.png"

        self.plugin._get_battle_detail = detail
        self.plugin.renderer.render_battle = capture_fails
        self.plugin._render_with_astrbot = fallback
        link = "看 https://zmdlogs.com/battle/btl_upload_abcdef123456"

        first = self._expand(link)
        second = self._expand(link)

        self.assertEqual(first, [("image", "/tmp/fallback.png")])
        self.assertEqual(second, [])

    # --- account wording and slug fallback ------------------------------------

    def _zmdlog(self, text: str, **kwargs):
        return run(collect(self.plugin.zmdlog(FakeEvent(text, **kwargs))))

    def test_a_vanished_account_is_reported_as_an_account(self) -> None:
        async def search(query, *, limit):
            return SimpleNamespace(
                query=query,
                has_more=False,
                accounts=(
                    SimpleNamespace(account_id="usr_a", account_display_name="CPU 0"),
                    SimpleNamespace(account_id="usr_b", account_display_name="cpu0"),
                ),
            )

        async def gone(account_id):
            raise ZmdLogsAPIError(404, "account_not_found", "gone")

        self.plugin.client.search_public_accounts = search
        self.plugin._get_public_user_rankings = gone

        (kind, listing), = self._zmdlog("zmdlog 账号 CPU")
        self.assertEqual(kind, "plain")
        self.assertIn("候选编号", listing)

        (kind, reply), = self._zmdlog("zmdlog 2", quoted=listing)
        self.assertEqual(kind, "plain")
        self.assertEqual(reply, "没有找到这个公开账号，或该账号暂无公开榜单记录。")

    def test_a_candidate_code_does_not_work_from_another_chat(self) -> None:
        async def search(query, *, limit):
            return SimpleNamespace(
                query=query,
                has_more=False,
                accounts=(
                    SimpleNamespace(account_id="usr_a", account_display_name="CPU 0"),
                    SimpleNamespace(account_id="usr_b", account_display_name="cpu0"),
                ),
            )

        self.plugin.client.search_public_accounts = search
        (_, listing), = self._zmdlog("zmdlog 账号 CPU")

        (kind, reply), = self._zmdlog("zmdlog 2", quoted=listing, origin=OTHER_GROUP)

        self.assertEqual(kind, "plain")
        self.assertIn("已过期或序号无效", reply)

    def test_a_slug_shaped_nickname_still_reaches_the_account_search(self) -> None:
        searched: list[str] = []

        async def board_missing(boss_slug):
            raise ZmdLogsAPIError(404, "boss_not_found", "missing")

        async def search(query, *, limit):
            searched.append(query)
            return SimpleNamespace(
                query=query,
                has_more=False,
                accounts=(
                    SimpleNamespace(account_id="usr_a", account_display_name="Re-Zero"),
                ),
            )

        async def account(account_id):
            return parse_public_user_rankings(public_user_rankings_payload())

        self.plugin._get_boss_ranking = board_missing
        self.plugin.client.search_public_accounts = search
        self.plugin._get_public_user_rankings = account

        (kind, result), = self._zmdlog("zmdlog Re-Zero")

        self.assertEqual(searched, ["Re-Zero"])
        self.assertEqual((kind, result), ("image", "/tmp/account.png"))

    # --- parse-layer robustness -----------------------------------------------

    def test_an_absurd_top_value_gets_a_short_reply_not_a_traceback(self) -> None:
        (kind, reply), = self._zmdlog("zmdlog 罗丹 --top " + "9" * 4301)

        self.assertEqual(kind, "plain")
        self.assertIn("--top", reply)

    def test_an_over_long_keyword_is_not_echoed_back(self) -> None:
        async def no_accounts(query, *, limit):
            return SimpleNamespace(query=query, has_more=False, accounts=())

        self.plugin.client.search_public_accounts = no_accounts

        (kind, reply), = self._zmdlog("zmdlog " + "首" * 3000)

        self.assertEqual(kind, "plain")
        self.assertLess(len(reply), 120)

    def test_a_ratio_config_typo_is_clamped_at_load(self) -> None:
        plugin = plugin_main.ZmdLogBotPlugin(
            FakeContext(),
            {"fuzzy_match_threshold": 65, "ambiguity_score_gap": "x"},
        )

        self.assertEqual(plugin.fuzzy_match_threshold, 0.65)
        self.assertEqual(plugin.ambiguity_score_gap, 0.08)

    def test_ranking_fixture_still_renders_through_the_guarded_ladder(self) -> None:
        async def ranking(boss_slug):
            return parse_boss_ranking(ranking_payload_with_rows())

        self.plugin._get_boss_ranking = ranking

        (kind, result), = self._zmdlog("zmdlog 三位一体")

        self.assertEqual((kind, result), ("image", "/tmp/ranking.png"))


if __name__ == "__main__":
    unittest.main()
