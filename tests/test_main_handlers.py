"""Handler-level tests that drive ``main.py`` with fakes.

``main.py`` imports ``astrbot.api``, so these only run when AstrBot is
installed in the test environment (it is in the project venv); elsewhere the
whole module is skipped rather than failing. Everything here targets behaviour
the review found wrong at the seam between main.py and core/ — the places the
pure core tests cannot reach.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import (
    battle_detail_payload,
    battle_export_payload,
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
    from astrbot_plugin_zmdlog.core.characters import CharacterFilterScope
    from astrbot_plugin_zmdlog.core.client import (
        ZmdLogsAPIError,
        ZmdLogsClientError,
    )
    from astrbot_plugin_zmdlog.core.history import record_rankings
    from astrbot_plugin_zmdlog.core.models import (
        parse_battle_detail,
        parse_battle_export,
        parse_boss_ranking,
        parse_hot_bosses,
        parse_public_user_rankings,
    )
    from astrbot_plugin_zmdlog.core.render import RenderError
    from astrbot_plugin_zmdlog.core.timestamps import utc_now_text
    from astrbot_plugin_zmdlog.core.toolbox import ToolAnswer
    from astrbot_plugin_zmdlog.core.watch import (
        BoardSnapshot,
        build_board_snapshot,
    )
    from astrbot_plugin_zmdlog.core.watchlist import WatchedAccount, WatchedBoard

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
        self.sent: list = []

    def get_message_str(self) -> str:
        return self._text

    async def send(self, chain) -> None:
        self.sent.append(chain)

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

    async def render_loadout(self, battle, **kwargs):
        return "/tmp/loadout.png"

    async def render_skills(self, battle, **kwargs):
        return "/tmp/skills.png"

    async def render_trend(self, history, **kwargs):
        return "/tmp/trend.png"

    async def render_timeline(self, export, **kwargs):
        return "/tmp/timeline.png"

    async def render_compare(self, first, second, **kwargs):
        return "/tmp/compare.png"

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

        async def offline(*args, **kwargs):
            # Everything the smart route reaches for on the side — the cast
            # export, the nickname search, the six-star catalog — answers as
            # an outage unless a test stubs it. tests/__init__ cuts the real
            # network, so without these the suite would measure upstream.
            raise ZmdLogsClientError("offline")

        async def unfilled():
            # The ranking index stays empty in handler tests: the pages that
            # read it fall back to names and initials, and nothing reaches
            # the network through its fill.
            return None

        self.plugin.data.list_hot_bosses = list_hot_bosses
        self.plugin.data.ranking_index.ensure_filled = unfilled
        self.plugin.data.get_battle_export = offline
        self.plugin.data.get_character_statistics = offline
        self.plugin.data.get_equip_suits = offline
        self.plugin.data.get_character_types = offline
        self.plugin.client.search_public_accounts = offline

    def tearDown(self) -> None:
        plugin_main.StarTools = self._star_tools

    # --- auto-expand cooldown -------------------------------------------------

    def _expand(self, text: str):
        return run(collect(self.plugin.expand_battle_link(FakeEvent(text))))

    def test_a_dead_link_is_answered_once_per_cooldown(self) -> None:
        async def missing(battle_id):
            raise ZmdLogsAPIError(404, "battle_not_found", "gone")

        self.plugin.data.get_battle_detail = missing
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

        self.plugin.data.get_battle_detail = down
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

        self.plugin.data.get_battle_detail = detail
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
        self.plugin.data.get_public_user_rankings = gone

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

        self.plugin.data.get_boss_ranking = board_missing
        self.plugin.client.search_public_accounts = search
        self.plugin.data.get_public_user_rankings = account

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

        self.assertEqual(plugin.settings.fuzzy_match_threshold, 0.65)
        self.assertEqual(plugin.settings.ambiguity_score_gap, 0.08)

    def test_ranking_fixture_still_renders_through_the_guarded_ladder(self) -> None:
        async def ranking(boss_slug):
            return parse_boss_ranking(ranking_payload_with_rows())

        self.plugin.data.get_boss_ranking = ranking

        (kind, result), = self._zmdlog("zmdlog 三位一体")

        self.assertEqual((kind, result), ("image", "/tmp/ranking.png"))

    # --- 配装 / 技能 share the 战报 lookup ------------------------------------

    def test_loadout_and_skill_commands_pick_the_ranked_battle(self) -> None:
        fetched: list[str] = []

        async def ranking(boss_slug):
            return parse_boss_ranking(ranking_payload_with_rows())

        async def detail(battle_id):
            fetched.append(battle_id)
            return parse_battle_detail(battle_detail_payload())

        self.plugin.data.get_boss_ranking = ranking
        self.plugin.data.get_battle_detail = detail

        (kind, result), = self._zmdlog("zmdlog 配装 三位一体 2")
        self.assertEqual((kind, result), ("image", "/tmp/loadout.png"))
        self.assertEqual(fetched, ["btl_upload_000000000002"])

        (kind, result), = self._zmdlog("zmdlog 技能 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/skills.png"))
        self.assertEqual(fetched[-1], "btl_upload_abcdef123456")

        (kind, reply), = self._zmdlog("zmdlog 技能 三位一体 9")
        self.assertEqual(kind, "plain")
        self.assertIn("没有第 9 名", reply)

    def test_a_battle_without_skill_stats_answers_in_text(self) -> None:
        async def detail(battle_id):
            payload = battle_detail_payload()
            payload["roleSkillStats"] = []
            payload["battle"]["roster"] = []
            return parse_battle_detail(payload)

        self.plugin.data.get_battle_detail = detail

        (kind, reply), = self._zmdlog("zmdlog 技能 btl_upload_abcdef123456")
        self.assertEqual((kind, reply), ("plain", "这份战报没有技能统计数据。"))
        (kind, reply), = self._zmdlog("zmdlog 配装 btl_upload_abcdef123456")
        self.assertEqual((kind, reply), ("plain", "这份战报没有记录阵容配装。"))
        # The summary card itself keeps working without loadout data.
        (kind, result), = self._zmdlog("zmdlog 战报 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/battle.png"))

    # --- board watch ----------------------------------------------------------

    def _enable_watch_storage(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        watcher = self.plugin.watcher
        watcher.enabled = True
        watcher.watchlist_store.path = root / "watchlist.json"
        watcher.rank_snapshot_store.path = root / "rank-snapshot.json"
        watcher.board_snapshot_store.path = root / "board-snapshot.json"
        watcher.history_store.path = root / "rank-history.json"

    def _hot_bosses_with_run(self, battle_id: str, nickname: str):
        payload = hot_bosses_payload()
        payload[0]["topSpeedRuns"] = [
            {
                "battleId": battle_id,
                "durationMs": 9_771,
                "uploaderNickname": nickname,
                "characterName": "诀",
            }
        ]
        return parse_hot_bosses(payload), payload

    def test_boards_can_be_followed_listed_and_unfollowed(self) -> None:
        self._enable_watch_storage()
        slug = "dung01_group_bossrush02"

        (kind, reply), = self._zmdlog("zmdlog 关注 榜单 三位一体")
        self.assertEqual(kind, "plain")
        self.assertIn("已关注榜单「危境再现 · 测试区 · 三位一体」，序号 1", reply)
        self.assertIn(slug, self.plugin.watcher.board_snapshots)
        self.assertTrue(self.plugin.watcher.board_snapshot_store.path.exists())

        (_, again), = self._zmdlog("zmdlog 关注 榜单 三位一体")
        self.assertIn("已经在关注列表里", again)
        (_, listing), = self._zmdlog("zmdlog 关注")
        self.assertIn("当前关注的榜单", listing)
        self.assertIn("1. 危境再现 · 测试区 · 三位一体", listing)
        (_, missing), = self._zmdlog("zmdlog 关注 榜单 完全无关的关键词")
        self.assertIn("没有找到", missing)

        (_, removed), = self._zmdlog("zmdlog 取关 榜单 1")
        self.assertIn("已取消关注榜单", removed)
        self.assertNotIn(slug, self.plugin.watcher.board_snapshots)
        self.assertEqual(self.plugin.watcher.watchlist.boards_for(GROUP), ())

    def test_board_watch_cycle_reports_a_new_top_run_once(self) -> None:
        self._enable_watch_storage()
        slug = "dung01_group_bossrush02"
        (_, reply), = self._zmdlog("zmdlog 关注 榜单 三位一体")
        self.assertIn("已关注榜单", reply)
        fresh = self._hot_bosses_with_run("btl_upload_new000000001", "shiki")
        sent: list[tuple[str, str]] = []

        async def fetch():
            return fresh

        async def send(origin, text):
            sent.append((origin, text))
            return True

        self.plugin.client.list_hot_bosses_with_payload = fetch
        self.plugin.watcher.notify = send

        run(self.plugin.watcher.run_board_cycle())

        self.assertEqual(len(sent), 1)
        origin, text = sent[0]
        self.assertEqual(origin, GROUP)
        self.assertIn("前三名有新纪录", text)
        self.assertIn("第 1 名 · shiki · 主C 诀 · 用时 0:09.771", text)
        self.assertIn("https://zmdlogs.com/battle/btl_upload_new000000001", text)
        self.assertEqual(
            self.plugin.watcher.board_snapshots[slug].runs[0].battle_id,
            "btl_upload_new000000001",
        )

        run(self.plugin.watcher.run_board_cycle())
        self.assertEqual(len(sent), 1)

    def test_a_failed_send_keeps_the_baseline_so_the_next_cycle_retries(self) -> None:
        # Saving the snapshot first and sending second lost the event for
        # good: the next cycle compared against the ranks already stored and
        # saw nothing to report.
        self._enable_watch_storage()
        slug = "dung01_group_bossrush02"
        self.plugin.watcher.watchlist, _ = self.plugin.watcher.watchlist.with_board(
            GROUP,
            WatchedBoard(
                boss_slug=slug,
                boss_name="危境再现·三位一体",
                dungeon_name="危境再现 · 测试区",
                added_by="aiocqhttp:111",
                added_at="2026-08-22T10:00:00+00:00",
            ),
        )
        seed, _ = self._hot_bosses_with_run("btl_upload_old000000001", "老王")
        self.plugin.watcher.board_snapshots = {
            slug: build_board_snapshot(seed[0], checked_at=utc_now_text())
        }
        fresh = self._hot_bosses_with_run("btl_upload_new000000009", "新人")
        sent: list[tuple[str, str]] = []
        delivered = [False]

        async def fetch():
            return fresh

        async def send(origin, text):
            sent.append((origin, text))
            return delivered[0]

        self.plugin.client.list_hot_bosses_with_payload = fetch
        self.plugin.watcher.notify = send

        run(self.plugin.watcher.run_board_cycle())
        self.assertEqual(len(sent), 1)
        # The send failed, so the baseline still holds the old run.
        self.assertEqual(
            self.plugin.watcher.board_snapshots[slug].runs[0].battle_id,
            "btl_upload_old000000001",
        )

        delivered[0] = True
        run(self.plugin.watcher.run_board_cycle())
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][1], sent[1][1])
        # Delivered this time, so the baseline finally moves on.
        self.assertEqual(
            self.plugin.watcher.board_snapshots[slug].runs[0].battle_id,
            "btl_upload_new000000009",
        )
        run(self.plugin.watcher.run_board_cycle())
        self.assertEqual(len(sent), 2)

    def test_a_stale_board_baseline_is_reseeded_silently(self) -> None:
        self._enable_watch_storage()
        slug = "dung01_group_bossrush02"
        self.plugin.watcher.watchlist, _ = self.plugin.watcher.watchlist.with_board(
            GROUP,
            WatchedBoard(
                boss_slug=slug,
                boss_name="危境再现·三位一体",
                dungeon_name="危境再现 · 测试区",
                added_by="aiocqhttp:111",
                added_at="2026-08-22T10:00:00+00:00",
            ),
        )
        self.plugin.watcher.board_snapshots = {
            slug: BoardSnapshot(runs=(), checked_at="2020-01-01T00:00:00+00:00")
        }
        fresh = self._hot_bosses_with_run("btl_upload_new000000002", "shiki")
        sent: list[tuple[str, str]] = []

        async def fetch():
            return fresh

        async def send(origin, text):
            sent.append((origin, text))
            return True

        self.plugin.client.list_hot_bosses_with_payload = fetch
        self.plugin.watcher.notify = send

        run(self.plugin.watcher.run_board_cycle())

        self.assertEqual(sent, [])
        self.assertEqual(
            self.plugin.watcher.board_snapshots[slug].runs[0].battle_id,
            "btl_upload_new000000002",
        )

    # --- 趋势 -----------------------------------------------------------------

    def _seed_history(self, name: str = "测试账号") -> None:
        payload = public_user_rankings_payload()
        payload["accountDisplayName"] = name
        account = parse_public_user_rankings(payload)
        self.plugin.watcher.rank_history, _ = record_rankings(
            {}, account, checked_at="2026-09-01T00:00:00+00:00"
        )

    def test_trend_renders_from_local_history_without_a_request(self) -> None:
        self._seed_history()

        async def unexpected(*args, **kwargs):
            raise AssertionError("no upstream request expected")

        self.plugin.client.search_public_accounts = unexpected
        self.plugin.data.get_public_user_rankings = unexpected

        (kind, result), = self._zmdlog("zmdlog 趋势 usr_1234567890abcdef")
        self.assertEqual((kind, result), ("image", "/tmp/trend.png"))
        (kind, result), = self._zmdlog("zmdlog 趋势 测试账号 --范围 7d")
        self.assertEqual((kind, result), ("image", "/tmp/trend.png"))
        (kind, result), = self._zmdlog(
            "zmdlog 趋势 https://zmdlogs.com/records/usr_1234567890abcdef"
        )
        self.assertEqual((kind, result), ("image", "/tmp/trend.png"))

    def test_trend_searches_upstream_for_unknown_nicknames(self) -> None:
        self._seed_history()
        hits = {"CPU 0": "usr_1234567890abcdef", "路人甲": "usr_a"}

        async def search(query, *, limit):
            return SimpleNamespace(
                query=query,
                has_more=False,
                accounts=(
                    SimpleNamespace(account_id=hits[query], account_display_name=query),
                ),
            )

        self.plugin.client.search_public_accounts = search

        (kind, result), = self._zmdlog("zmdlog 趋势 CPU 0")
        self.assertEqual((kind, result), ("image", "/tmp/trend.png"))
        (kind, reply), = self._zmdlog("zmdlog 趋势 路人甲")
        self.assertEqual(kind, "plain")
        self.assertIn("还没有名次记录", reply)

    def test_rank_watch_cycle_records_history_and_unfollowing_drops_it(self) -> None:
        self._enable_watch_storage()
        account_id = "usr_1234567890abcdef"
        self.plugin.watcher.watchlist, _ = self.plugin.watcher.watchlist.with_account(
            GROUP,
            WatchedAccount(
                account_id=account_id,
                display_name="测试账号",
                added_by="aiocqhttp:111",
                added_at="2026-08-22T10:00:00+00:00",
            ),
        )

        async def account(requested_id):
            return parse_public_user_rankings(public_user_rankings_payload())

        self.plugin.data.get_public_user_rankings = account

        run(self.plugin.watcher.run_account_cycle())

        trace = self.plugin.watcher.rank_history[account_id]
        self.assertEqual(trace.board("dung01_group_bossrush01").points[0].rank, 2)
        self.assertTrue(self.plugin.watcher.history_store.path.exists())
        run(self.plugin.watcher.run_account_cycle())
        self.assertEqual(len(trace.board("dung01_group_bossrush01").points), 1)

        (_, removed), = self._zmdlog("zmdlog 取关 1")
        self.assertIn("已取消关注", removed)
        self.assertNotIn(account_id, self.plugin.watcher.rank_history)

    # --- 技能轴 reads the export, not the detail --------------------------------

    def test_timeline_uses_the_export_of_the_ranked_battle(self) -> None:
        exported: list[str] = []

        async def ranking(boss_slug):
            return parse_boss_ranking(ranking_payload_with_rows())

        async def export(battle_id):
            exported.append(battle_id)
            return parse_battle_export(battle_export_payload())

        received: list[dict] = []

        async def detail(battle_id):
            return parse_battle_detail(battle_detail_payload())

        async def render_timeline(export, **kwargs):
            received.append(kwargs)
            return "/tmp/timeline.png"

        self.plugin.data.get_boss_ranking = ranking
        self.plugin.data.get_battle_export = export
        self.plugin.data.get_battle_detail = detail
        self.plugin.renderer.render_timeline = render_timeline

        (kind, result), = self._zmdlog("zmdlog 技能轴 三位一体 2")
        self.assertEqual((kind, result), ("image", "/tmp/timeline.png"))
        self.assertEqual(exported, ["btl_upload_000000000002"])
        # The detail rides along for the BUFF band when it can be fetched...
        self.assertEqual(received[-1]["battle"].battle_id, "btl_upload_abcdef123456")

        async def offline(battle_id):
            raise ZmdLogsClientError("offline")

        self.plugin.data.get_battle_detail = offline
        (kind, result), = self._zmdlog("zmdlog 排轴 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/timeline.png"))
        # ...and the page still renders without it.
        self.assertIsNone(received[-1]["battle"])

    def test_the_battle_card_takes_the_export_when_it_can_get_one(self) -> None:
        received: list[dict] = []

        async def detail(battle_id):
            return parse_battle_detail(battle_detail_payload())

        async def export(battle_id):
            return parse_battle_export(battle_export_payload())

        async def render_battle(battle, **kwargs):
            received.append(kwargs)
            return "/tmp/battle.png"

        self.plugin.data.get_battle_detail = detail
        self.plugin.data.get_battle_export = export
        self.plugin.renderer.render_battle = render_battle

        (kind, result), = self._zmdlog("zmdlog 战报 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/battle.png"))
        self.assertEqual(received[-1]["export"].battle_id, "btl_upload_abcdef123456")
        self.assertIsNone(received[-1]["export_note"])

        async def old_upload(battle_id):
            raise ZmdLogsAPIError(422, "battle_export_unsupported", "old")

        self.plugin.data.get_battle_export = old_upload
        (kind, result), = self._zmdlog("zmdlog 战报 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/battle.png"))
        self.assertIsNone(received[-1]["export"])
        self.assertIn("旧版客户端", received[-1]["export_note"])

        async def offline(battle_id):
            raise ZmdLogsClientError("offline")

        self.plugin.data.get_battle_export = offline
        (kind, result), = self._zmdlog("zmdlog 战报 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/battle.png"))
        self.assertEqual(
            (received[-1]["export"], received[-1]["export_note"]), (None, None)
        )

    def test_several_character_names_filter_the_whole_team(self) -> None:
        received: list[dict] = []

        async def ranking(boss_slug):
            return parse_boss_ranking(ranking_payload_with_rows())

        async def render_ranking(ranking, **kwargs):
            received.append(kwargs)
            return "/tmp/ranking.png"

        self.plugin.data.get_boss_ranking = ranking
        self.plugin.renderer.render_ranking = render_ranking

        (kind, result), = self._zmdlog("zmdlog 三位一体 --角色 黎风 洁尔佩塔")
        self.assertEqual((kind, result), ("image", "/tmp/ranking.png"))
        self.assertEqual(received[-1]["character_filter"], ("黎风", "洁尔佩塔"))
        self.assertIs(
            received[-1]["character_filter_scope"], CharacterFilterScope.ROSTER
        )
        # Pinyin initials resolve per name, and a lone name keeps the main-C rule.
        (kind, result), = self._zmdlog("zmdlog 三位一体 --角色 lf")
        self.assertEqual(received[-1]["character_filter"], ("黎风",))
        self.assertIs(
            received[-1]["character_filter_scope"], CharacterFilterScope.MAIN
        )

        (kind, reply), = self._zmdlog("zmdlog 三位一体 --角色 黎风 洛茜")
        self.assertEqual(kind, "plain")
        self.assertIn("没有同时带上", reply)

    def test_compare_fetches_both_ranked_battles(self) -> None:
        fetched: list[str] = []
        received: list[dict] = []

        async def ranking(boss_slug):
            return parse_boss_ranking(ranking_payload_with_rows())

        async def detail(battle_id):
            fetched.append(battle_id)
            payload = battle_detail_payload()
            payload["battle"]["id"] = battle_id
            return parse_battle_detail(payload)

        async def render_compare(first, second, **kwargs):
            received.append({"ids": (first.battle_id, second.battle_id), **kwargs})
            return "/tmp/compare.png"

        self.plugin.data.get_boss_ranking = ranking
        self.plugin.data.get_battle_detail = detail
        self.plugin.renderer.render_compare = render_compare

        (kind, result), = self._zmdlog("zmdlog 对比 三位一体 1 3")
        self.assertEqual((kind, result), ("image", "/tmp/compare.png"))
        self.assertEqual(
            sorted(fetched), ["btl_upload_000000000001", "btl_upload_000000000003"]
        )
        self.assertEqual(
            received[-1]["ids"], ("btl_upload_000000000001", "btl_upload_000000000003")
        )
        self.assertEqual((received[-1]["rank_a"], received[-1]["rank_b"]), (1, 3))

        # Two explicit references skip the board entirely.
        (kind, result), = self._zmdlog(
            "zmdlog 对比 btl_upload_aaaaaaaaaaaa btl_upload_bbbbbbbbbbbb"
        )
        self.assertEqual((kind, result), ("image", "/tmp/compare.png"))
        self.assertIsNone(received[-1]["rank_a"])

        (kind, reply), = self._zmdlog("zmdlog 对比 三位一体 1 9")
        self.assertEqual(kind, "plain")
        self.assertIn("没有第 9 名", reply)
        (kind, reply), = self._zmdlog(
            "zmdlog 对比 btl_upload_aaaaaaaaaaaa btl_upload_aaaaaaaaaaaa"
        )
        self.assertEqual((kind, reply), ("plain", "两边是同一场战报，没有可比的。"))

        # Two bosses have two rotations; the comparison is refused in text.
        async def other_boss(battle_id):
            payload = battle_detail_payload()
            payload["battle"]["id"] = battle_id
            if battle_id.endswith("b"):
                payload["battle"]["bossName"] = "三位一体"
            return parse_battle_detail(payload)

        self.plugin.data.get_battle_detail = other_boss
        (kind, reply), = self._zmdlog(
            "zmdlog 对比 btl_upload_aaaaaaaaaaaa btl_upload_bbbbbbbbbbbb"
        )
        self.assertEqual(kind, "plain")
        self.assertIn("不做跨榜单对比", reply)
        self.assertIn("三位一体", reply)

    def test_the_card_fetches_the_detail_and_the_export_together(self) -> None:
        # Serially, a slow export spent its whole 15-second client budget in
        # front of the detail. Each stub waits for the other to start, so a
        # serial implementation deadlocks and the wait_for below trips.
        detail_started = asyncio.Event()
        export_started = asyncio.Event()

        async def detail(battle_id):
            detail_started.set()
            await asyncio.wait_for(export_started.wait(), timeout=2)
            return parse_battle_detail(battle_detail_payload())

        async def export(battle_id):
            export_started.set()
            await asyncio.wait_for(detail_started.wait(), timeout=2)
            return parse_battle_export(battle_export_payload())

        self.plugin.data.get_battle_detail = detail
        self.plugin.data.get_battle_export = export

        (kind, result), = self._zmdlog("zmdlog 战报 btl_upload_abcdef123456")

        self.assertEqual((kind, result), ("image", "/tmp/battle.png"))

    def test_the_gear_pages_are_handed_the_suit_catalog(self) -> None:
        # The catalog is what names a piece upstream left blank, so it has to
        # reach the page; an outage must still draw the page without it.
        seen: list[dict] = []

        async def detail(battle_id):
            return parse_battle_detail(battle_detail_payload())

        async def render_loadout(battle, **kwargs):
            seen.append(kwargs.get("suits"))
            return "/tmp/loadout.png"

        async def suits():
            return {"suit_phy01": "点剑"}

        self.plugin.data.get_battle_detail = detail
        self.plugin.data.get_equip_suits = suits
        self.plugin.renderer.render_loadout = render_loadout

        (kind, result), = self._zmdlog("zmdlog 配装 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/loadout.png"))
        self.assertEqual(seen, [{"suit_phy01": "点剑"}])

        async def broken():
            raise ZmdLogsClientError("offline")

        self.plugin.data.get_equip_suits = broken
        (kind, result), = self._zmdlog("zmdlog 配装 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/loadout.png"))
        self.assertEqual(seen[-1], {})

    def test_timeline_explains_old_uploads_and_rate_limits(self) -> None:
        answers = {
            "btl_upload_old000000001": ZmdLogsAPIError(
                422, "battle_export_unsupported", "old"
            ),
            "btl_upload_busy00000001": ZmdLogsAPIError(429, "rate_limited", "busy"),
            "btl_upload_gone00000001": ZmdLogsAPIError(404, "battle_not_found", "gone"),
        }

        async def export(battle_id):
            raise answers[battle_id]

        self.plugin.data.get_battle_export = export

        (kind, reply), = self._zmdlog("zmdlog 技能轴 btl_upload_old000000001")
        self.assertEqual(kind, "plain")
        self.assertIn("旧版客户端", reply)
        (kind, reply), = self._zmdlog("zmdlog 技能轴 btl_upload_busy00000001")
        self.assertEqual(kind, "plain")
        self.assertIn("过于频繁", reply)
        (kind, reply), = self._zmdlog("zmdlog 技能轴 btl_upload_gone00000001")
        self.assertEqual((kind, reply), ("plain", "战报不存在、未公开或已删除。"))

    def test_a_dead_battle_is_reported_the_same_way_for_every_page(self) -> None:
        async def missing(battle_id):
            raise ZmdLogsAPIError(404, "battle_not_found", "gone")

        self.plugin.data.get_battle_detail = missing

        for command in ("战报", "配装", "技能"):
            with self.subTest(command=command):
                (kind, reply), = self._zmdlog(
                    f"zmdlog {command} btl_upload_abcdef123456"
                )
                self.assertEqual(kind, "plain")
                self.assertEqual(reply, "战报不存在、未公开或已删除。")

    # --- LLM tool pictures ------------------------------------------------------

    def _tool_turn(self, *answers: ToolAnswer):
        """One turn's tool calls, then the agent-done hook.

        Returns the replies the model got and the chains the chat received.
        """

        event = FakeEvent("突击里谁最菜")
        replies: list[str] = []

        async def scenario():
            for answer in answers:

                async def character(*args, _answer=answer, **kwargs):
                    return _answer

                self.plugin.tools.character = character
                replies.append(
                    await self.plugin.zmdlogs_character_standings(event, character="x")
                )
            # Nothing goes out while the model is still calling tools.
            self.assertEqual(event.sent, [])
            await self.plugin.send_tool_picture(event, None, None)
            await asyncio.gather(*self.plugin._background_tasks)
            # The hook fires once per turn; a second firing has nothing left.
            await self.plugin.send_tool_picture(event, None, None)
            await asyncio.gather(*self.plugin._background_tasks)

        run(scenario())
        return replies, event.sent

    def test_the_only_picture_of_a_turn_goes_out_when_the_model_is_done(
        self,
    ) -> None:
        replies, sent = self._tool_turn(ToolAnswer("洛茜的事实", "/tmp/a.png"))

        self.assertEqual(replies, ["洛茜的事实"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(type(sent[0].chain[0]).__name__, "Image")

    def test_several_subjects_in_one_turn_send_no_picture(self) -> None:
        # Four characters compared: the first page would be an arbitrary pick.
        replies, sent = self._tool_turn(
            ToolAnswer("A", "/tmp/a.png"),
            ToolAnswer("B", "/tmp/b.png"),
            ToolAnswer("C", "/tmp/c.png"),
        )

        self.assertEqual(sent, [])
        note = plugin_main.messages.TOOL_PICTURE_WITHHELD
        # The first result cannot know yet; every later one says so.
        self.assertEqual(replies[0], "A")
        self.assertEqual(replies[1], "B\n" + note)
        self.assertEqual(replies[2], "C\n" + note)

    def test_a_text_only_answer_is_not_a_second_subject(self) -> None:
        replies, sent = self._tool_turn(
            ToolAnswer("找不到这个名字"),
            ToolAnswer("洛茜的事实", "/tmp/a.png"),
        )

        self.assertEqual(replies, ["找不到这个名字", "洛茜的事实"])
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
