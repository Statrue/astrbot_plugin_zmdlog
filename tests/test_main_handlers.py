"""Handler-level tests that drive ``main.py`` with fakes.

``main.py`` imports ``astrbot.api``, so these only run when AstrBot is
installed in the test environment (it is in the project venv); elsewhere the
whole module is skipped rather than failing. Everything here targets behaviour
the review found wrong at the seam between main.py and core/ — the places the
pure core tests cannot reach.
"""

import asyncio
import dataclasses
import re
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from tests.helpers import (
    battle_detail_payload,
    battle_export_payload,
    hot_bosses_payload,
    public_user_rankings_payload,
    ranking_payload_with_rows,
    standing,
)

try:
    import astrbot  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    astrbot = None

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT.parent))

if astrbot is not None:
    from astrbot.api.event import ResultContentType

    # main.py imports its own ``.core`` package, so the exception classes the
    # handlers catch are the package's, not the ``core.*`` modules the rest of
    # the suite imports from the repo root. Use the same identities here.
    from astrbot_plugin_zmdlog import main as plugin_main
    from astrbot_plugin_zmdlog.core.board_changes import NoticeEntry
    from astrbot_plugin_zmdlog.core.characters import CharacterFilterScope
    from astrbot_plugin_zmdlog.core.client import (
        ZmdLogsAPIError,
        ZmdLogsClientError,
    )
    from astrbot_plugin_zmdlog.core.cooldown import Cooldown
    from astrbot_plugin_zmdlog.core.models import (
        parse_battle_detail,
        parse_battle_export,
        parse_boss_ranking,
        parse_hot_bosses,
        parse_public_user_rankings,
    )
    from astrbot_plugin_zmdlog.core.render import RenderedImage, RenderError
    from astrbot_plugin_zmdlog.core.toolbox import ToolAnswer
    from astrbot_plugin_zmdlog.core.watch import NoticeBatch

GROUP = "aiocqhttp:GroupMessage:1"
OTHER_GROUP = "aiocqhttp:GroupMessage:2"


class Reply:
    """Stands in for AstrBot's Reply component, matched by class name."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.message_str = text


class FakeResult(tuple):
    """``(kind, payload)``, compared as a plain tuple, that also records
    ``use_markdown`` the way AstrBot's MessageEventResult does."""

    use_markdown_ = None

    def use_markdown(self, use: bool | None = True):
        self.use_markdown_ = use
        return self


class FakeEvent:
    def __init__(
        self,
        text: str,
        *,
        origin: str = GROUP,
        quoted: str | None = None,
        sender: str = "111",
        isolated_from: str | None = None,
        platform: str = "aiocqhttp",
        group_openid: str | None = None,
        user_openid: str | None = None,
        api: "FakeBotApi | None" = None,
    ):
        """``isolated_from`` is the adapter's own session id when 隔离对话
        rewrote ``origin`` per member, the way AstrBot's waking stage does.

        ``group_openid`` / ``user_openid`` shape the raw botpy message of a
        QQ official group or private chat, and ``api`` stands in for the
        adapter's ``bot.api``; a wild-bot event has no ``bot`` at all, so a
        handler that reached for one there would fail loudly."""

        self._text = text
        self._sender = sender
        self._platform = platform
        self.unified_msg_origin = origin
        chain = [Reply(quoted)] if quoted is not None else []
        self.message_obj = SimpleNamespace(
            message_str=text,
            message=chain,
            session_id=isolated_from or "",
            message_id="msg-1",
            raw_message=SimpleNamespace(
                group_openid=group_openid,
                author=SimpleNamespace(user_openid=user_openid),
            ),
        )
        if api is not None:
            self.bot = SimpleNamespace(api=api)
        self._extras = {"_session_isolated": isolated_from is not None}
        self.sent: list = []
        # What the pipeline is delivering; a streamed reply sets it.
        self.result = None
        self.stopped = False

    def get_message_str(self) -> str:
        return self._text

    def get_result(self):
        return self.result

    async def send(self, chain) -> None:
        self.sent.append(chain)

    def plain_result(self, text: str):
        return FakeResult(("plain", text))

    def image_result(self, path: str):
        return FakeResult(("image", path))

    def is_admin(self) -> bool:
        return False

    def get_platform_name(self) -> str:
        return self._platform

    def stop_event(self) -> None:
        self.stopped = True

    def get_sender_id(self) -> str:
        return self._sender

    def get_extra(self, key: str, default=None):
        return self._extras.get(key, default)


class FakeHttpResponse:
    """One aiohttp response, as AstrBot's uploader reads it."""

    status = 200

    def __init__(self, body: dict) -> None:
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def json(self, content_type=None):
        return self._body

    async def text(self, errors=None):
        return ""


class FakeBotHttp:
    """botpy's ``BotHttp`` as AstrBot's chunked uploader drives it.

    Every request is answered from memory: the prepare hands out one part,
    its PUT and acknowledgement succeed, and the merge answers with
    ``raw_url`` (left out when None). ``error`` fails the first request.
    ``paths`` records the path of every request, in order.
    """

    is_sandbox = False
    _headers: dict = {}

    def __init__(
        self, *, raw_url: str | None = None, error: Exception | None = None
    ) -> None:
        self.raw_url = raw_url
        self.error = error
        self.paths: list[str] = []
        self._session = self

    async def check_session(self) -> None:
        return None

    def request(self, method, url, **kwargs):
        if self.error is not None:
            raise self.error
        path = urlsplit(url).path
        self.paths.append(path)
        body: dict = {}
        if path.endswith("/upload_prepare"):
            body = {
                "upload_id": "upload-1",
                "block_size": 1 << 20,
                "parts": [{"index": 1, "presigned_url": "https://cos.invalid/part"}],
            }
        elif path.endswith("/files"):
            body = {"file_uuid": "uuid-1", "file_info": "info-1", "ttl": 0}
            if self.raw_url is not None:
                body["raw_url"] = self.raw_url
        return FakeHttpResponse(body)


class FakeBotApi:
    """botpy's ``bot.api``: records what was sent, or fails every send.

    ``http`` is its authenticated client, which uploads go through."""

    def __init__(
        self, *, error: Exception | None = None, http: FakeBotHttp | None = None
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.acks: list[tuple[str, int, int]] = []
        self._error = error
        self._http = http

    async def on_interaction_result(self, interaction_id: str, code: int):
        # With how many messages had gone out: the acknowledgement comes first.
        self.acks.append((interaction_id, code, len(self.calls)))

    async def post_group_message(self, **payload):
        return self._record("group", payload)

    async def post_c2c_message(self, **payload):
        return self._record("c2c", payload)

    def _record(self, scene: str, payload: dict):
        self.calls.append((scene, payload))
        if self._error is not None:
            raise self._error
        return {"id": "sent"}


class FakeContext:
    """AstrBot's plugin context. ``platforms`` are the adapter instances by
    id; ``pushed`` records every ``send_message``, which answers the way
    AstrBot's does: whether an instance with the origin's id exists."""

    def __init__(self, stars=()) -> None:
        self.stars = list(stars)
        self.platforms: dict = {}
        self.pushed: list = []

    def get_config(self, origin):
        return {"wake_prefix": ["/"]}

    def get_all_stars(self):
        return self.stars

    def get_platform_inst(self, platform_id: str):
        return self.platforms.get(platform_id)

    async def send_message(self, session, message_chain) -> bool:
        self.pushed.append((str(session), message_chain))
        return str(session).split(":", 1)[0] in self.platforms


def platform_instance(platform_id: str, name: str, *, api=None, scenes=None):
    """An adapter instance as AstrBot keeps it: its metadata, and on the
    built-in QQ official one the botpy client and the scene each session was
    last seen in (empty after a restart)."""

    return SimpleNamespace(
        meta=lambda: SimpleNamespace(id=platform_id, name=name),
        client=SimpleNamespace(api=api),
        _session_scene=dict(scenes or {}),
    )


def official_adapter_class(api: "FakeBotApi"):
    """AstrBot's built-in adapter class as the callback patch sees it: an
    adapter whose botpy ``client`` has intents and ``api``."""

    class Adapter:
        def __init__(self, platform_config, platform_settings, event_queue):
            self.config = platform_config
            self.client = SimpleNamespace(intents=1 << 30, api=api)

        def meta(self):
            return SimpleNamespace(id=self.config["id"])

    return Adapter


def tap_of(data: str, *, private: bool = False, member: str = "111"):
    """A button tap in the official group G1, or in U1's private chat."""

    return SimpleNamespace(
        id="itx-1",
        type=11,
        event_id="INTERACTION_CREATE:e-1",
        data=SimpleNamespace(resolved=SimpleNamespace(button_data=data)),
        group_openid=None if private else "G1",
        group_member_openid=None if private else member,
        user_openid="U1" if private else None,
    )


def capture(path: str, scale: int = 2) -> "RenderedImage":
    """What the renderer hands back for one page."""

    return RenderedImage(path, scale)


class FakeRenderer:
    async def render_battle(self, battle, **kwargs):
        return capture("/tmp/battle.png")

    async def render_battle_build(self, battle, **kwargs):
        return capture("/tmp/build.png")

    async def render_battle_data(self, battle, **kwargs):
        return capture("/tmp/data.png")

    async def render_trend(self, history, **kwargs):
        return capture("/tmp/trend.png")

    async def render_battle_cast(self, export, **kwargs):
        return capture("/tmp/cast.png")

    async def render_compare(self, first, second, **kwargs):
        return capture("/tmp/compare.png")

    async def render_account(self, account, **kwargs):
        return capture("/tmp/account.png")

    async def render_ranking(self, ranking, **kwargs):
        return capture("/tmp/ranking.png")


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

        async def unfilled(metric="dps"):
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

        async def board_missing(boss_slug, **kwargs):
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

    # --- option composition -----------------------------------------------------

    def test_element_and_profession_together_never_call_a_fielded_name_unseen(
        self,
    ) -> None:
        from astrbot_plugin_zmdlog.core.models import CharacterType, parse_boss_ranking
        from astrbot_plugin_zmdlog.core.ranking_index import IndexEntry
        from tests.helpers import ranking_payload_with_rows

        ranking = parse_boss_ranking(ranking_payload_with_rows())
        index = self.plugin.data.ranking_index
        index._slugs = (ranking.boss_slug,)
        index._entries[ranking.boss_slug] = IndexEntry(ranking, 0.0)

        async def types(*, names=()):
            return {
                "黎风": CharacterType("黎风", "物理", "长枪", "近卫"),
                "洛茜": CharacterType("洛茜", "自然", "手铳", "近卫"),
            }

        captured: dict = {}

        async def render(tallies, **kwargs):
            captured["tallies"] = tallies
            captured.update(kwargs)
            return capture("/tmp/champions.png")

        self.plugin.data.get_character_types = types
        self.plugin.renderer.render_character_champions = render

        (kind, result), = self._zmdlog("zmdlog 角色排名 --属性 物理 --职业 近卫")

        self.assertEqual((kind, result), ("image", "/tmp/champions.png"))
        self.assertEqual([tally.name for tally in captured["tallies"]], ["黎风"])
        # 洛茜 is fielded; she is merely not 物理, so she is not "unseen".
        self.assertEqual(captured["unseen"], ())

    def test_several_names_draw_the_teams_fielding_all_of_them(self) -> None:
        from astrbot_plugin_zmdlog.core.models import parse_boss_ranking
        from astrbot_plugin_zmdlog.core.ranking_index import IndexEntry
        from tests.helpers import ranking_payload_with_rows

        ranking = parse_boss_ranking(ranking_payload_with_rows())
        index = self.plugin.data.ranking_index
        index._slugs = (ranking.boss_slug,)
        index._entries[ranking.boss_slug] = IndexEntry(ranking, 0.0)
        drawn: list = []

        async def render(standings, **kwargs):
            drawn.append(standings)
            return capture("/tmp/standings.png")

        self.plugin.renderer.render_character_standings = render

        (kind, result), = self._zmdlog("zmdlog 角色排名 黎风 卡缪")
        self.assertEqual((kind, result), ("image", "/tmp/standings.png"))
        self.assertEqual(drawn[0].characters, ("黎风", "卡缪"))
        # 、 separates too, and a pinyin initial resolves like anywhere else.
        (kind, _), = self._zmdlog("zmdlog 角色排名 lf、卡缪")
        self.assertEqual(kind, "image")
        self.assertEqual(drawn[1].characters, ("黎风", "卡缪"))
        # Two main Cs never share a team: a sentence, not an empty page.
        (kind, result), = self._zmdlog("zmdlog 角色排名 黎风 洛茜")
        self.assertEqual(kind, "plain")
        self.assertIn("没有同时带「黎风」「洛茜」的队伍", result)
        # A name in no roster is named, the rest are not blamed.
        (kind, result), = self._zmdlog("zmdlog 角色排名 黎风 没有这个人")
        self.assertEqual(kind, "plain")
        self.assertIn("「没有这个人」", result)
        (kind, result), = self._zmdlog("zmdlog 角色排名 a b c d e")
        self.assertEqual((kind, result), ("plain", "角色排名 最多写 4 个角色。"))

    def test_row_options_are_refused_on_a_dungeon_target(self) -> None:
        first = hot_bosses_payload()[0]
        second = dict(
            first, bossSlug="dung01_group_bossrush03", bossName="危境再现·白垩界卫"
        )
        self.cards = parse_hot_bosses([first, second])

        (kind, result), = self._zmdlog("zmdlog 测试区 --属性 物理")

        self.assertEqual(kind, "plain")
        self.assertIn("--属性 仅适用于", result)

    def test_an_alias_of_only_punctuation_is_refused(self) -> None:
        event = FakeEvent("zmdlog 别名 添加 三位一体 ·")
        event.is_admin = lambda: True

        (kind, result), = run(collect(self.plugin.zmdlog(event)))

        self.assertEqual(kind, "plain")
        self.assertIn("别名不能只有标点或符号", result)

    # --- tool reply shaping -----------------------------------------------------

    def test_a_long_tool_reply_is_cut_at_a_line_and_keeps_its_source(self) -> None:
        note = plugin_main.facts.SOURCE_NOTE
        text = "\n".join(f"#{i} 行 {'x' * 60}" for i in range(120)) + "\n\n" + note

        cut = plugin_main._shorten_tool_reply(text)

        self.assertLess(len(cut), len(text))
        self.assertTrue(cut.endswith(note))
        body, _, _ = cut.partition("（篇幅所限")
        # The cut lands on a line boundary: the last kept row is complete.
        self.assertTrue(body.rstrip().endswith("x" * 60), body[-80:])

    def test_counts_the_model_writes_in_words_are_understood(self) -> None:
        for raw, expected in (
            ("5", 5),
            ("前5", 5),
            ("五名", 5),
            ("二十", 20),
            ("十", 10),
            ("十五", 15),
            ("１０", 10),
            ("abc", 10),
            ("0", 10),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(plugin_main._positive_int(raw, 10), expected)

    # --- parse-layer robustness -----------------------------------------------

    def test_an_absurd_page_value_gets_a_short_reply_not_a_traceback(self) -> None:
        (kind, reply), = self._zmdlog("zmdlog 罗丹 --页 " + "9" * 4301)

        self.assertEqual(kind, "plain")
        self.assertIn("--页", reply)

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
        async def ranking(boss_slug, **kwargs):
            return parse_boss_ranking(ranking_payload_with_rows())

        self.plugin.data.get_boss_ranking = ranking

        (kind, result), = self._zmdlog("zmdlog 三位一体")

        self.assertEqual((kind, result), ("image", "/tmp/ranking.png"))

    # --- 养成 / 技能 share the 战报 lookup ------------------------------------

    def test_build_and_data_commands_pick_the_ranked_battle(self) -> None:
        fetched: list[str] = []

        async def ranking(boss_slug, **kwargs):
            return parse_boss_ranking(ranking_payload_with_rows())

        async def detail(battle_id):
            fetched.append(battle_id)
            return parse_battle_detail(battle_detail_payload())

        self.plugin.data.get_boss_ranking = ranking
        self.plugin.data.get_battle_detail = detail

        (kind, result), = self._zmdlog("zmdlog 养成 三位一体 2")
        self.assertEqual((kind, result), ("image", "/tmp/build.png"))
        self.assertEqual(fetched, ["btl_upload_000000000002"])

        (kind, result), = self._zmdlog("zmdlog 数据 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/data.png"))
        self.assertEqual(fetched[-1], "btl_upload_abcdef123456")

        (kind, result), = self._zmdlog("zmdlog 数据 三位一体 2")
        self.assertEqual((kind, result), ("image", "/tmp/data.png"))
        self.assertEqual(fetched[-1], "btl_upload_000000000002")

        (kind, reply), = self._zmdlog("zmdlog 数据 三位一体 9")
        self.assertEqual(kind, "plain")
        self.assertIn("没有第 9 名", reply)

    def test_a_battle_without_skill_stats_answers_in_text(self) -> None:
        async def detail(battle_id):
            payload = battle_detail_payload()
            payload["roleSkillStats"] = []
            payload["battle"]["roster"] = []
            return parse_battle_detail(payload)

        self.plugin.data.get_battle_detail = detail

        (kind, reply), = self._zmdlog("zmdlog 数据 btl_upload_abcdef123456")
        self.assertEqual((kind, reply), ("plain", "这份战报没有技能统计数据。"))
        (kind, reply), = self._zmdlog("zmdlog 养成 btl_upload_abcdef123456")
        self.assertEqual((kind, reply), ("plain", "这份战报没有记录阵容的养成。"))
        # The summary card itself keeps working without a roster.
        (kind, result), = self._zmdlog("zmdlog 战报 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/battle.png"))

    # --- QQ official pick-list buttons ----------------------------------------

    def _two_accounts_named_cpu(self) -> None:
        async def search(query, *, limit):
            return SimpleNamespace(
                query=query,
                has_more=False,
                accounts=(
                    SimpleNamespace(account_id="usr_a", account_display_name="CPU 0"),
                    SimpleNamespace(account_id="usr_b", account_display_name="cpu*0"),
                ),
            )

        self.plugin.client.search_public_accounts = search

    @staticmethod
    def _official_event(
        text: str, *, api: "FakeBotApi", private: bool = False, **extra
    ) -> FakeEvent:
        return FakeEvent(
            text,
            origin=(
                "default:FriendMessage:U1" if private else "default:GroupMessage:G1"
            ),
            platform="qq_official",
            group_openid=None if private else "G1",
            user_openid="U1" if private else None,
            api=api,
            **extra,
        )

    def _official(self, text: str, *, api: "FakeBotApi", private: bool = False):
        event = self._official_event(text, api=api, private=private)
        return event, run(collect(self.plugin.zmdlog(event)))

    def _assert_sent_with_buttons(self, api, event, *, scene: str, commands):
        (sent_scene, payload), = api.calls
        self.assertEqual(sent_scene, scene)
        if scene == "group":
            self.assertEqual(payload["group_openid"], "G1")
        else:
            self.assertEqual(payload["openid"], "U1")
        # A passive reply to the message asked, numbered clear of the range
        # AstrBot draws its own sequence numbers from.
        self.assertEqual(payload["msg_id"], "msg-1")
        self.assertGreater(payload["msg_seq"], 10_000)
        self.assertEqual(payload["msg_type"], 2)
        self.assertIn("点下方按钮", payload["markdown"]["content"])
        self.assertIn("cpu\\*0", payload["markdown"]["content"])
        self.assertEqual(
            [
                row["buttons"][0]["action"]["data"]
                for row in payload["keyboard"]["content"]["rows"]
            ],
            commands,
        )
        # Sent outside the pipeline, so AstrBot must not answer on its own.
        self.assertTrue(event.stopped)

    def test_an_official_group_gets_the_pick_list_with_buttons(self) -> None:
        self._two_accounts_named_cpu()
        api = FakeBotApi()

        event, results = self._official("/zmdlog 账号 CPU", api=api)

        self.assertEqual(results, [])
        self._assert_sent_with_buttons(
            api,
            event,
            scene="group",
            commands=["/zmdlog 账号 usr_a", "/zmdlog 账号 usr_b"],
        )

    def test_an_official_private_chat_gets_the_same_buttons(self) -> None:
        self._two_accounts_named_cpu()
        api = FakeBotApi()

        event, results = self._official("/zmdlog 账号 CPU", api=api, private=True)

        self.assertEqual(results, [])
        self._assert_sent_with_buttons(
            api,
            event,
            scene="c2c",
            commands=["/zmdlog 账号 usr_a", "/zmdlog 账号 usr_b"],
        )

    def test_a_watch_pick_list_fills_in_the_watch_command(self) -> None:
        self._enable_watch_storage()
        self._two_boards_in_one_dungeon()
        api = FakeBotApi()

        event, results = self._official("/zmdlog 关注 测试区", api=api)

        self.assertEqual(results, [])
        (scene, payload), = api.calls
        self.assertEqual(scene, "group")
        self.assertIn("选一个关注", payload["markdown"]["content"])
        self.assertEqual(
            [
                row["buttons"][0]["action"]["data"]
                for row in payload["keyboard"]["content"]["rows"]
            ],
            [
                "/zmdlog 关注 dung01_group_bossrush02",
                "/zmdlog 关注 dung01_group_bossrush03",
            ],
        )
        self.assertTrue(event.stopped)

    def _two_boards_in_one_dungeon(self) -> None:
        first = hot_bosses_payload()[0]
        second = dict(
            first, bossSlug="dung01_group_bossrush03", bossName="危境再现·白垩界卫"
        )
        self.cards = parse_hot_bosses([first, second])

    def test_the_dungeon_list_buttons_each_dungeon_by_its_name(self) -> None:
        first = hot_bosses_payload()[0]
        contract = dict(
            first,
            bossSlug="indie_group_ccdg",
            bossName="破潮之像",
            dungeonName="危机合约",
        )
        self.cards = parse_hot_bosses([first, contract])
        api = FakeBotApi()

        event, results = self._official("/zmdlog 榜单", api=api)

        self.assertEqual(results, [])
        (_, payload), = api.calls
        self.assertIn("共 2 个副本", payload["markdown"]["content"])
        self.assertEqual(
            [
                row["buttons"][0]["action"]["data"]
                for row in payload["keyboard"]["content"]["rows"]
            ],
            ["/zmdlog 危境再现 · 测试区", "/zmdlog 危机合约"],
        )
        self.assertTrue(event.stopped)

    def test_a_failed_button_send_falls_back_to_the_plain_list(self) -> None:
        self._two_accounts_named_cpu()
        for private in (False, True):
            with self.subTest(private=private):
                api = FakeBotApi(error=RuntimeError("response body with secrets"))

                with mock.patch.object(plugin_main, "logger") as logger:
                    event, results = self._official(
                        "/zmdlog 账号 CPU", api=api, private=private
                    )

                self.assertEqual(len(api.calls), 1)
                (kind, listing), = results
                self.assertEqual(kind, "plain")
                # Its nicknames are unescaped, so markdown would misread them.
                self.assertIs(results[0].use_markdown_, False)
                self.assertTrue(
                    listing.startswith(
                        "「CPU」匹配到 2 个目标，引用本条消息回复序号即可："
                    )
                )
                self.assertFalse(event.stopped)
                logged = repr(logger.warning.call_args_list)
                self.assertIn("RuntimeError", logged)
                self.assertNotIn("secrets", logged)

    def test_other_platforms_and_the_switch_keep_the_plain_list(self) -> None:
        self._two_accounts_named_cpu()
        wild = run(collect(self.plugin.zmdlog(FakeEvent("/zmdlog 账号 CPU"))))
        self.assertEqual(wild[0][0], "plain")

        webhook_api = FakeBotApi()
        webhook = FakeEvent(
            "/zmdlog 账号 CPU",
            platform="qq_official_webhook",
            group_openid="G1",
            api=webhook_api,
        )
        switched_api = FakeBotApi()
        self.plugin.settings = dataclasses.replace(
            self.plugin.settings, disable_qq_official_buttons=True
        )
        (webhook_result,) = run(collect(self.plugin.zmdlog(webhook)))
        webhook_kind, webhook_listing = webhook_result
        switched, results = self._official("/zmdlog 账号 CPU", api=switched_api)

        (switched_kind, switched_listing), = results
        # Byte for byte what the wild bot gets, bar the per-list code.
        code = re.compile(r"候选编号 [A-Z2-9]{4}")
        for kind, listing in (
            (webhook_kind, webhook_listing),
            (switched_kind, switched_listing),
        ):
            self.assertEqual(kind, "plain")
            self.assertEqual(code.sub("", listing), code.sub("", wild[0][1]))
        # Nor is the platform's markdown default overridden there.
        for reply in (wild[0], webhook_result, results[0]):
            self.assertIsNone(reply.use_markdown_)
        self.assertEqual(webhook_api.calls, [])
        self.assertEqual(switched_api.calls, [])

    def test_a_reply_that_is_not_a_pick_list_is_not_sent_by_hand(self) -> None:
        api = FakeBotApi()

        event, results = self._official("/zmdlog 账号 x", api=api)

        (kind, _), = results
        self.assertEqual(kind, "plain")
        self.assertEqual(api.calls, [])
        self.assertFalse(event.stopped)

    def test_the_help_page_carries_the_official_notes_on_the_official_bot(
        self,
    ) -> None:
        asked: list[bool] = []

        async def render_help(*, command_prefix, official=False):
            asked.append(official)
            return capture("/tmp/help.png")

        self.plugin.renderer.render_help = render_help

        def notes_for(platform: str) -> list[bool]:
            asked.clear()
            run(collect(self.plugin.zmdlog(FakeEvent("/zmdlog", platform=platform))))
            return list(asked)

        # Only the built-in WebSocket adapter is the official bot; the webhook
        # and v2 adapters are other platforms, whose page stays as it was.
        for platform, expected in (
            ("qq_official", True),
            ("qq_official_webhook", False),
            ("qq_official_v2", False),
            ("aiocqhttp", False),
        ):
            with self.subTest(platform=platform):
                self.assertEqual(notes_for(platform), [expected])

        # With the buttons switched off the official bot answers as before.
        self.plugin.settings = dataclasses.replace(
            self.plugin.settings, disable_qq_official_buttons=True
        )
        self.assertEqual(notes_for("qq_official"), [False])

    def test_every_other_official_text_reply_goes_out_as_plain_text(self) -> None:
        # A nickname's * or _ would otherwise set the reply in bold or italic.
        self._enable_watch_storage()
        self._enable_binding_storage()
        for private in (False, True):
            api = FakeBotApi()
            for text in (
                "/zmdlog 别名 乱写",  # a parse error
                "/zmdlog 账号 x",  # a query error
                "/zmdlog 别名",
                "/zmdlog 关注",
            ):
                with self.subTest(text=text, private=private):
                    _, results = self._official(text, api=api, private=private)
                    (reply,) = results
                    self.assertEqual(reply[0], "plain")
                    self.assertIs(reply.use_markdown_, False)
            with self.subTest("an expired pick", private=private):
                expired = self._official_event(
                    "2", api=api, private=private, quoted="候选编号 ABCD"
                )
                (reply,) = run(collect(self.plugin.pick_candidate(expired)))
                self.assertEqual(reply[0], "plain")
                self.assertIs(reply.use_markdown_, False)
            self.assertEqual(api.calls, [])

        async def missing(battle_id):
            raise ZmdLogsAPIError(404, "battle_not_found", "gone")

        self.plugin.data.get_battle_detail = missing
        link = self._official_event(
            "看 https://zmdlogs.com/battle/btl_upload_abcdef123456", api=api
        )
        (reply,) = run(collect(self.plugin.expand_battle_link(link)))
        self.assertEqual(reply[0], "plain")
        self.assertIs(reply.use_markdown_, False)

    # --- QQ official: the binding page one tap away ---------------------------

    def test_a_how_to_bind_reply_carries_the_binding_page(self) -> None:
        self._enable_binding_storage()
        for private, text in (
            (False, "/zmdlog 我的"),
            (False, "/zmdlog 绑定"),
            # Asked before any board lookup, in a group or a private chat.
            (False, "/zmdlog 对比 三位一体 我"),
            (True, "/zmdlog 对比 三位一体 我 3"),
            (True, "/zmdlog 我的"),
            (True, "/zmdlog 绑定"),
        ):
            with self.subTest(text=text):
                api = FakeBotApi()

                event, results = self._official(text, api=api, private=private)

                self.assertEqual(results, [])
                self.assertTrue(event.stopped)
                (_, payload), = api.calls
                self.assertEqual(payload["msg_type"], 2)
                self.assertIn("ZMD\\-XXXX\\-XXXX", payload["markdown"]["content"])
                (row,) = payload["keyboard"]["content"]["rows"]
                self.assertEqual(
                    row["buttons"][0]["action"]["data"],
                    "https://zmdlogs.com/account/binding",
                )

    def test_a_how_to_bind_reply_that_cannot_go_with_its_button_goes_plain(
        self,
    ) -> None:
        self._enable_binding_storage()
        api = FakeBotApi(error=RuntimeError("rejected"))

        event, results = self._official("/zmdlog 我的", api=api)

        (reply,) = results
        self.assertEqual(reply[0], "plain")
        self.assertIn("还没有绑定账号", reply[1])
        self.assertIs(reply.use_markdown_, False)
        self.assertFalse(event.stopped)

        wild = run(collect(self.plugin.zmdlog(FakeEvent("/zmdlog 我的"))))
        self.assertEqual(wild[0][0], "plain")
        self.assertIsNone(wild[0].use_markdown_)
        self.plugin.settings = dataclasses.replace(
            self.plugin.settings, disable_qq_official_buttons=True
        )
        switched_api = FakeBotApi()
        _, switched = self._official("/zmdlog 我的", api=switched_api)
        self.assertEqual(switched[0][0], "plain")
        self.assertEqual(switched_api.calls, [])

    # --- QQ official result images ----------------------------------------------

    RAW_URL = (
        "https://qqbot-file-upload-1251316161.cos.accelerate.myqcloud.com"
        "/f0/part_1?q-sign-algorithm=sha1&q-ak=AKID&q-signature=secret0a1b"
    )
    ACCOUNT = "usr_1234567890abcdef"

    def _png(self, width: int, height: int) -> str:
        """A PNG whose header says ``width`` x ``height``, as a capture's does."""

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        chunk = (
            struct.pack(">I", len(header))
            + b"IHDR"
            + header
            + struct.pack(">I", zlib.crc32(b"IHDR" + header))
        )
        path = Path(directory.name) / "zmd-account.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk)
        return str(path)

    def _account_page(self, *, size=(1920, 3000), scale=2) -> str:
        """The account page, drawn as a capture of ``size`` at ``scale``."""

        path = self._png(*size)

        async def account(requested_id):
            return parse_public_user_rankings(public_user_rankings_payload())

        async def render_account(account, **kwargs):
            return capture(path, scale)

        self.plugin.data.get_public_user_rankings = account
        self.plugin.renderer.render_account = render_account
        return path

    def test_an_official_chat_gets_the_picture_as_markdown_with_its_link(
        self,
    ) -> None:
        self._account_page(size=(1920, 3000), scale=2)
        for private, scene, prefix in (
            (False, "group", "/v2/groups/G1/"),
            (True, "c2c", "/v2/users/U1/"),
        ):
            with self.subTest(scene=scene):
                http = FakeBotHttp(raw_url=self.RAW_URL)
                api = FakeBotApi(http=http)

                event, results = self._official(
                    f"/zmdlog 账号 {self.ACCOUNT}", api=api, private=private
                )

                self.assertEqual(results, [])
                self.assertTrue(event.stopped)
                # Uploaded in pieces to where the reply goes: the merge is
                # the one response that carries a link to the stored file.
                self.assertTrue(http.paths[0].startswith(prefix))
                self.assertTrue(http.paths[-1].endswith("/files"))
                (sent_scene, payload), = api.calls
                self.assertEqual(sent_scene, scene)
                self.assertEqual(payload["msg_id"], "msg-1")
                self.assertGreater(payload["msg_seq"], 10_000)
                self.assertEqual(payload["msg_type"], 2)
                # 1920 x 3000 device pixels at 2x: 960 x 1500 CSS pixels.
                self.assertEqual(
                    payload["markdown"]["content"],
                    f"![img #960px #1500px]"
                    f"({self.RAW_URL}&response-content-type=image%2Fpng)",
                )
                (row,) = payload["keyboard"]["content"]["rows"]
                (button,) = row["buttons"]
                self.assertEqual(button["action"]["type"], 0)
                self.assertEqual(
                    button["action"]["data"],
                    f"https://zmdlogs.com/records/{self.ACCOUNT}",
                )

    def test_a_page_is_declared_at_the_scale_it_was_captured_at(self) -> None:
        self._account_page(size=(960, 7588), scale=1)
        api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))

        self._official(f"/zmdlog 账号 {self.ACCOUNT}", api=api)

        (_, payload), = api.calls
        self.assertTrue(
            payload["markdown"]["content"].startswith("![img #960px #7588px](")
        )

    def test_a_picture_that_cannot_go_as_markdown_goes_as_itself(self) -> None:
        path = self._account_page()
        for label, api in (
            (
                "the upload fails",
                FakeBotApi(
                    http=FakeBotHttp(error=RuntimeError("401 with a token in it"))
                ),
            ),
            ("the upload gives no link", FakeBotApi(http=FakeBotHttp())),
            (
                "the send fails",
                FakeBotApi(
                    http=FakeBotHttp(raw_url=self.RAW_URL),
                    error=RuntimeError("rejected with a body"),
                ),
            ),
        ):
            for private in (False, True):
                with self.subTest(label, private=private):
                    with mock.patch.object(plugin_main, "logger") as logger:
                        event, results = self._official(
                            f"/zmdlog 账号 {self.ACCOUNT}", api=api, private=private
                        )

                    self.assertEqual(results, [("image", path)])
                    self.assertFalse(event.stopped)
                    logged = repr(logger.method_calls)
                    self.assertIn("ZmdLogBot", logged)
                    # Only an exception's type: never the signed link, and
                    # never what the platform said back.
                    for secret in ("secret0a1b", "token", "body", "myqcloud"):
                        self.assertNotIn(secret, logged)

    def test_the_quoted_pick_and_the_expanded_link_go_as_markdown_too(
        self,
    ) -> None:
        self._account_page()
        self._two_accounts_named_cpu()
        listing_api = FakeBotApi()
        self._official("/zmdlog 账号 CPU", api=listing_api)
        (_, listing), = listing_api.calls
        api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))
        # A reply quoting the list carries the list's text back.
        pick = self._official_event(
            "1", api=api, quoted=listing["markdown"]["content"]
        )

        self.assertEqual(run(collect(self.plugin.pick_candidate(pick))), [])
        (_, payload), = api.calls
        self.assertIn("/records/usr_a", repr(payload["keyboard"]))

        async def detail(battle_id):
            return parse_battle_detail(battle_detail_payload())

        path = self._png(1920, 2000)

        async def render_battle(battle, **kwargs):
            return capture(path)

        self.plugin.data.get_battle_detail = detail
        self.plugin.renderer.render_battle = render_battle
        api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))
        link = self._official_event(
            "看 https://zmdlogs.com/battle/btl_upload_abcdef123456", api=api
        )

        self.assertEqual(run(collect(self.plugin.expand_battle_link(link))), [])
        (_, payload), = api.calls
        self.assertIn(
            "https://zmdlogs.com/battle/btl_upload_abcdef123456",
            repr(payload["keyboard"]),
        )

    def test_the_trend_links_its_account(self) -> None:
        self._seed_history()
        path = self._png(1920, 2000)

        async def draw(*args, **kwargs):
            return capture(path)

        self.plugin.renderer.render_trend = draw
        api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))

        _, results = self._official("/zmdlog 趋势 usr_1234567890abcdef", api=api)

        self.assertEqual(results, [])
        (_, payload), = api.calls
        jump, views = payload["keyboard"]["content"]["rows"]
        self.assertEqual(
            jump["buttons"][0]["action"]["data"],
            "https://zmdlogs.com/records/usr_1234567890abcdef",
        )
        # The other views fill in a command with the prefix typed.
        self.assertEqual(
            [button["action"]["data"] for button in views["buttons"]],
            ["/zmdlog 账号 usr_1234567890abcdef"],
        )

    def test_a_ranking_carries_its_views_its_pages_and_its_link(self) -> None:
        received = self._long_board()  # 17 rows led by 黎风: two pages
        path = self._png(1080, 3000)

        async def render_ranking(ranking, **kwargs):
            received.append(kwargs)
            return capture(path)

        self.plugin.renderer.render_ranking = render_ranking
        slug = "dung01_group_bossrush02"
        typed = "/zmdlog 三位一体 --角色 黎风 --口径 rdps"
        api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))

        event, results = self._official(typed, api=api)

        self.assertEqual(results, [])
        self.assertTrue(event.stopped)
        (_, payload), = api.calls
        self.assertTrue(
            payload["markdown"]["content"].startswith("![img #540px #1500px](")
        )
        rows = [
            [
                (button["render_data"]["label"], button["action"]["data"])
                for button in row["buttons"]
            ]
            for row in payload["keyboard"]["content"]["rows"]
        ]
        self.assertEqual(
            rows,
            [
                [
                    ("阵容", f"/zmdlog 阵容 {slug} --口径 rdps"),
                    ("角色统计", f"/zmdlog 角色统计 {slug} --口径 rdps"),
                ],
                [
                    ("第一名战报", f"/zmdlog 战报 {slug} --口径 rdps"),
                    ("对比第一名", f"/zmdlog 对比 {slug} 我 --口径 rdps"),
                ],
                [
                    ("全部", f"/zmdlog 榜单 {slug} --页 全部 --角色 黎风 --口径 rdps"),
                    ("下一页", f"/zmdlog 榜单 {slug} --页 2 --角色 黎风 --口径 rdps"),
                ],
                [("在 ZMDLogs 打开", f"https://zmdlogs.com/boss/{slug}?metric=rdps")],
            ],
        )

        # Sent, 下一页 draws the last page, filtered, which turns no further.
        api.calls.clear()
        self._official(rows[2][1][1], api=api)
        self.assertEqual(received[-1]["page"], 2)
        self.assertEqual(received[-1]["character_filter"], ("黎风",))
        (_, payload), = api.calls
        self.assertEqual(
            [
                button["render_data"]["label"]
                for button in payload["keyboard"]["content"]["rows"][2]["buttons"]
            ],
            ["全部"],
        )
        # Every other platform gets the picture, and nothing else.
        self.assertEqual(
            run(collect(self.plugin.zmdlog(FakeEvent(typed)))), [("image", path)]
        )

    def _dungeon_page(self) -> str:
        """A dungeon of two boards, its podiums drawn as a capture."""

        first = hot_bosses_payload()[0]
        second = dict(
            first, bossSlug="dung01_group_bossrush03", bossName="危境再现·白垩界卫"
        )
        self.cards = parse_hot_bosses([first, second])
        path = self._png(1920, 3000)

        async def render_dungeon_top3(choice, cards, **kwargs):
            return capture(path)

        self.plugin.renderer.render_dungeon_top3 = render_dungeon_top3
        return path

    def test_a_dungeons_podiums_carry_a_button_per_board(self) -> None:
        self._dungeon_page()
        api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))

        event, results = self._official("/zmdlog 测试区", api=api)

        self.assertEqual(results, [])
        self.assertTrue(event.stopped)
        (_, payload), = api.calls
        self.assertTrue(
            payload["markdown"]["content"].startswith("![img #960px #1500px](")
        )
        # Each board's ranking, one tap away; no link, the site has no
        # page of a dungeon.
        self.assertEqual(
            [
                [button["action"]["data"] for button in row["buttons"]]
                for row in payload["keyboard"]["content"]["rows"]
            ],
            [["/zmdlog dung01_group_bossrush02"], ["/zmdlog dung01_group_bossrush03"]],
        )

    def test_podiums_that_cannot_go_as_markdown_go_as_themselves(self) -> None:
        path = self._dungeon_page()
        api = FakeBotApi(
            http=FakeBotHttp(raw_url=self.RAW_URL), error=RuntimeError("rejected")
        )

        event, results = self._official("/zmdlog 测试区", api=api)

        self.assertEqual(results, [("image", path)])
        self.assertFalse(event.stopped)
        # And everywhere else as they always went.
        wild = run(collect(self.plugin.zmdlog(FakeEvent("/zmdlog 测试区"))))
        self.assertEqual(wild, [("image", path)])

    def _comparison_page(self) -> str:
        """Two battles by two uploaders, their comparison drawn as a capture."""

        async def detail(battle_id):
            payload = battle_detail_payload()
            payload["battle"]["id"] = battle_id
            payload["battle"]["uploaderNickname"] = (
                "shiki" if battle_id.endswith("a") else "KevNe"
            )
            return parse_battle_detail(payload)

        path = self._png(1920, 4000)

        async def render_compare(first, second, **kwargs):
            return capture(path)

        self.plugin.data.get_battle_detail = detail
        self.plugin.renderer.render_compare = render_compare
        return path

    def test_a_comparison_carries_a_button_to_each_sides_battle(self) -> None:
        path = self._comparison_page()
        typed = "/zmdlog 对比 btl_upload_aaaaaaaaaaaa btl_upload_bbbbbbbbbbbb"
        api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))

        event, results = self._official(typed, api=api)

        self.assertEqual(results, [])
        self.assertTrue(event.stopped)
        (_, payload), = api.calls
        self.assertTrue(
            payload["markdown"]["content"].startswith("![img #960px #2000px](")
        )
        # Each side's 摘要 by its uploader; no link, the comparison is
        # neither battle's page on the site.
        self.assertEqual(
            [
                [
                    (button["render_data"]["label"], button["action"]["data"])
                    for button in row["buttons"]
                ]
                for row in payload["keyboard"]["content"]["rows"]
            ],
            [
                [
                    ("shiki 的战报", "/zmdlog 战报 btl_upload_aaaaaaaaaaaa"),
                    ("KevNe 的战报", "/zmdlog 战报 btl_upload_bbbbbbbbbbbb"),
                ]
            ],
        )
        # Every other platform, and the official bot with the switch on,
        # get the picture and nothing else.
        wild = run(collect(self.plugin.zmdlog(FakeEvent(typed))))
        self.plugin.settings = dataclasses.replace(
            self.plugin.settings, disable_qq_official_buttons=True
        )
        switched_api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))
        _, switched = self._official(typed, api=switched_api)
        for results in (wild, switched):
            self.assertEqual(results, [("image", path)])
        self.assertEqual(switched_api.calls, [])

    def test_a_comparison_whose_buttons_fail_goes_as_its_picture(self) -> None:
        path = self._comparison_page()
        api = FakeBotApi(
            http=FakeBotHttp(raw_url=self.RAW_URL), error=RuntimeError("rejected")
        )

        event, results = self._official(
            "/zmdlog 对比 btl_upload_aaaaaaaaaaaa btl_upload_bbbbbbbbbbbb", api=api
        )

        self.assertEqual(results, [("image", path)])
        self.assertFalse(event.stopped)

    def test_pages_about_no_one_thing_stay_native_pictures(self) -> None:
        async def render_help(*, command_prefix, official=False):
            return capture(self._png(1920, 9000), 2)

        self.plugin.renderer.render_help = render_help
        http = FakeBotHttp(raw_url=self.RAW_URL)
        api = FakeBotApi(http=http)

        _, results = self._official("/zmdlog help", api=api)

        ((kind, _),) = results
        self.assertEqual(kind, "image")
        self.assertEqual(api.calls, [])
        self.assertEqual(http.paths, [])

    def test_other_platforms_and_the_switch_keep_the_native_picture(self) -> None:
        path = self._account_page()
        text = f"/zmdlog 账号 {self.ACCOUNT}"
        wild = run(collect(self.plugin.zmdlog(FakeEvent(text))))
        webhook_api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))
        webhook = FakeEvent(
            text, platform="qq_official_webhook", group_openid="G1", api=webhook_api
        )
        webhook_results = run(collect(self.plugin.zmdlog(webhook)))
        self.plugin.settings = dataclasses.replace(
            self.plugin.settings, disable_qq_official_buttons=True
        )
        switched_api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))
        _, switched_results = self._official(text, api=switched_api)

        for results in (wild, webhook_results, switched_results):
            self.assertEqual(results, [("image", path)])
        self.assertEqual(webhook_api.calls, [])
        self.assertEqual(webhook_api._http.paths, [])
        self.assertEqual(switched_api.calls, [])
        self.assertEqual(switched_api._http.paths, [])

    # --- board notices -----------------------------------------------------------

    def _notice_picture(self, *, fail: bool = False) -> str:
        """The 顶屁股通告 as the renderer draws it: 960 wide at 2x."""

        path = self._png(1920, 2400)
        self.drawn: list[tuple] = []

        async def render_notice(entries, **kwargs):
            if fail:
                raise RenderError("capture failed")
            self.drawn.append((entries, kwargs))
            return capture(path, 2)

        self.plugin.renderer.render_notice = render_notice
        return path

    def _notify(self, origin: str) -> bool:
        """Deliver one chat's batch the way the notice cycle does."""

        record = parse_boss_ranking(ranking_payload_with_rows()).rows[0]
        batch = NoticeBatch(
            entries=(
                NoticeEntry(
                    boss_slug=self.TRIO,
                    boss_name="三位一体",
                    dungeon_name="危境再现",
                    seen_at="2026-10-05T02:38:00+00:00",
                    rank=1,
                    record=record,
                    champion=True,
                ),
            ),
            window_start="2026-10-05T02:30:00+00:00",
            window_end="2026-10-05T02:45:00+00:00",
            top_n=10,
        )
        return run(self.plugin.watcher.notify(origin, batch))

    def _official_platform(self, api, *, scenes=None) -> None:
        self.plugin.context.platforms["default"] = platform_instance(
            "default", "qq_official", api=api, scenes=scenes
        )

    def test_a_notice_is_one_picture_of_the_batch(self) -> None:
        path = self._notice_picture()
        self.plugin.context.platforms["aiocqhttp"] = platform_instance(
            "aiocqhttp", "aiocqhttp"
        )

        self.assertTrue(self._notify(GROUP))

        ((entries, kwargs),) = self.drawn
        self.assertEqual([entry.rank for entry in entries], [1])
        self.assertEqual(
            kwargs,
            {
                "window_start": "2026-10-05T02:30:00+00:00",
                "window_end": "2026-10-05T02:45:00+00:00",
                "top_n": 10,
                "web_base_url": self.plugin.web_base_url,
            },
        )
        ((session, chain),) = self.plugin.context.pushed
        self.assertEqual(session, GROUP)
        (image,) = chain.chain
        self.assertEqual(type(image).__name__, "Image")
        self.assertIn(Path(path).name, str(image.file))

    def test_a_notice_that_cannot_be_drawn_is_not_delivered(self) -> None:
        self._notice_picture(fail=True)
        api = FakeBotApi()
        self._official_platform(api)
        # Nor AstrBot's own renderer.
        self.plugin.settings = dataclasses.replace(
            self.plugin.settings, fallback_to_astrbot_renderer=False
        )

        self.assertFalse(self._notify(GROUP))
        self.assertFalse(self._notify("default:GroupMessage:G1"))
        self.assertEqual(self.plugin.context.pushed, [])
        self.assertEqual(api.calls, [])

    def test_every_other_platform_pushes_through_astrbot(self) -> None:
        self._notice_picture()
        api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))
        context = self.plugin.context
        context.platforms["aiocqhttp"] = platform_instance("aiocqhttp", "aiocqhttp")
        context.platforms["hook"] = platform_instance(
            "hook", "qq_official_webhook", api=api
        )
        context.platforms["v2"] = platform_instance("v2", "qq_official_v2", api=api)
        # A guild channel's origin reads like a group's; the adapter knows it
        # by the scene it last saw the session in.
        self._official_platform(api, scenes={"C1": "channel"})
        for origin in (
            GROUP,
            "hook:GroupMessage:G1",
            "v2:GroupMessage:G1",
            "default:GroupMessage:C1",
        ):
            with self.subTest(origin):
                context.pushed.clear()

                self.assertTrue(self._notify(origin))

                ((session, chain),) = context.pushed
                self.assertEqual(session, origin)
                self.assertEqual(
                    [type(component).__name__ for component in chain.chain],
                    ["Image"],
                )
        self.assertEqual(api.calls, [])
        with self.subTest("no such platform"):
            context.platforms.clear()
            self.assertFalse(self._notify(GROUP))

    def test_an_official_notice_is_a_markdown_picture_without_buttons(self) -> None:
        # Pushed by the plugin itself: right after a restart the adapter has
        # seen no session, and AstrBot would skip the push while reporting
        # it sent.
        self._notice_picture()
        for origin, scene, key, openid in (
            ("default:GroupMessage:G1", "group", "group_openid", "G1"),
            ("default:FriendMessage:U1", "c2c", "openid", "U1"),
            # A member's origin under 隔离对话, kept from before core/origins:
            # AstrBot's own push reached the group, and so does this one.
            ("default:GroupMessage:M1_G1", "group", "group_openid", "G1"),
        ):
            with self.subTest(origin):
                api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))
                self._official_platform(api)

                self.assertTrue(self._notify(origin))

                self.assertEqual(self.plugin.context.pushed, [])
                ((sent_scene, payload),) = api.calls
                self.assertEqual(sent_scene, scene)
                self.assertEqual(payload[key], openid)
                # Pushed, not a reply: nothing it answers.
                self.assertNotIn("msg_id", payload)
                self.assertNotIn("event_id", payload)
                self.assertEqual(payload["msg_type"], 2)
                self.assertTrue(
                    payload["markdown"]["content"].startswith(
                        "![img #960px #1200px]("
                    )
                )
                self.assertNotIn("keyboard", payload)

    def test_an_official_notice_falls_back_to_the_native_picture(self) -> None:
        self._notice_picture()

        class NoMarkdownApi(FakeBotApi):
            async def post_group_message(self, **payload):
                self.calls.append(("group", payload))
                if payload["msg_type"] == 2:
                    raise RuntimeError("markdown refused")
                return {"id": "sent"}

        api = NoMarkdownApi(http=FakeBotHttp(raw_url=self.RAW_URL))
        self._official_platform(api)

        self.assertTrue(self._notify("default:GroupMessage:G1"))

        self.assertEqual([payload["msg_type"] for _, payload in api.calls], [2, 7])
        self.assertEqual(self.plugin.context.pushed, [])
        with self.subTest("the native picture fails too"):
            failing = FakeBotApi(
                error=RuntimeError("40034105"),
                http=FakeBotHttp(raw_url=self.RAW_URL),
            )
            self._official_platform(failing)

            self.assertFalse(self._notify("default:GroupMessage:G1"))
            self.assertEqual(
                [payload["msg_type"] for _, payload in failing.calls], [2, 7]
            )
            self.assertEqual(self.plugin.context.pushed, [])
        with self.subTest("the send stalls"):

            class StalledApi(FakeBotApi):
                async def post_group_message(self, **payload):
                    await asyncio.Event().wait()

            self._official_platform(
                StalledApi(http=FakeBotHttp(raw_url=self.RAW_URL))
            )
            with mock.patch.object(plugin_main, "_NOTICE_SEND_TIMEOUT_SECONDS", 0.05):
                self.assertFalse(self._notify("default:GroupMessage:G1"))
            self.assertEqual(self.plugin.context.pushed, [])

    def test_with_the_switch_on_an_official_notice_is_the_native_picture(
        self,
    ) -> None:
        self._notice_picture()
        self.plugin.settings = dataclasses.replace(
            self.plugin.settings, disable_qq_official_buttons=True
        )
        api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))
        self._official_platform(api)

        self.assertTrue(self._notify("default:GroupMessage:G1"))

        # Pushed by the plugin still, as a plain picture.
        ((_, payload),) = api.calls
        self.assertEqual(payload["msg_type"], 7)
        self.assertEqual(self.plugin.context.pushed, [])

    # --- QQ official: button callbacks -------------------------------------------

    def _enable_callbacks(self, api: "FakeBotApi"):
        """Switch callbacks on and load; the adapter AstrBot builds after."""

        self.plugin.settings = dataclasses.replace(
            self.plugin.settings, qq_official_callbacks=True
        )
        cls = official_adapter_class(api)
        with mock.patch.object(
            plugin_main.qq_official, "_builtin_adapter_class", return_value=cls
        ):
            self.plugin._install_callbacks()
        self.addCleanup(self.plugin._remove_callbacks)
        return cls({"id": "default"}, {}, None)

    def _tap(self, adapter, data: str, *, private: bool = False) -> None:
        run(adapter.client.on_interaction_create(tap_of(data, private=private)))

    def test_callbacks_patch_nothing_unless_switched_on(self) -> None:
        for label, settings in (
            ("off by default", {}),
            (
                "buttons disabled",
                {"qq_official_callbacks": True, "disable_qq_official_buttons": True},
            ),
        ):
            with self.subTest(label):
                self.plugin.settings = dataclasses.replace(
                    plugin_main.load_settings({}, warn=lambda message: None),
                    **settings,
                )
                cls = official_adapter_class(FakeBotApi())
                original = cls.__init__
                with mock.patch.object(
                    plugin_main.qq_official, "_builtin_adapter_class", return_value=cls
                ):
                    self.plugin._install_callbacks()

                self.assertIs(cls.__init__, original)
                client = cls({"id": "default"}, {}, None).client
                self.assertEqual(client.intents, 1 << 30)
                self.assertFalse(hasattr(client, "on_interaction_create"))

    def test_the_patch_follows_the_plugin_in_and_out(self) -> None:
        cls = official_adapter_class(FakeBotApi())
        original = cls.__init__
        self.plugin.settings = dataclasses.replace(
            self.plugin.settings, qq_official_callbacks=True
        )
        with mock.patch.object(
            plugin_main.qq_official, "_builtin_adapter_class", return_value=cls
        ):
            self.plugin._install_callbacks()
            patched = cls.__init__
            self.plugin.renderer = None  # the fake has nothing to close
            run(self.plugin.terminate())

        self.assertIsNot(patched, original)
        self.assertIs(cls.__init__, original)

    def test_with_expand_enabled_taps_are_left_to_it(self) -> None:
        expand = SimpleNamespace(
            name=plugin_main.qq_official.EXPAND_PLUGIN, activated=True
        )
        self.plugin.context = FakeContext([expand])
        api = FakeBotApi()

        with mock.patch.object(plugin_main, "logger") as logger:
            adapter = self._enable_callbacks(api)

        self.assertFalse(hasattr(adapter.client, "on_interaction_create"))
        self.assertEqual(adapter.client.intents, 1 << 30)
        self.assertIn("qqoffice_expand", repr(logger.warning.call_args_list))

    def test_a_tap_draws_the_page_the_typed_command_draws(self) -> None:
        self._account_page()
        self._seed_history()  # so the page offers its trend
        data = f"/zmdlog 账号 {self.ACCOUNT}"
        for private, scene in ((False, "group"), (True, "c2c")):
            with self.subTest(scene=scene):
                api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))
                adapter = self._enable_callbacks(api)
                typed = self._official_event(data, api=api, private=private)
                # The typed command, on the connection that takes taps.
                typed.bot = adapter.client
                run(collect(self.plugin.zmdlog(typed)))

                self._tap(adapter, data, private=private)

                (_, by_hand), (tapped_scene, by_tap) = api.calls
                self.assertEqual(tapped_scene, scene)
                # Acknowledged before the page went out.
                self.assertEqual(api.acks, [("itx-1", 0, 1)])
                self.assertEqual(by_tap["event_id"], "INTERACTION_CREATE:e-1")
                self.assertNotIn("msg_id", by_tap)
                self.assertGreater(by_tap["msg_seq"], 10_000)
                for field in ("msg_type", "markdown", "keyboard"):
                    self.assertEqual(by_tap[field], by_hand[field])
                # Its other views answer a tap too, carrying the same commands.
                (jump,), (trend,) = (
                    row["buttons"] for row in by_tap["keyboard"]["content"]["rows"]
                )
                self.assertEqual(jump["action"]["type"], 0)
                self.assertEqual(trend["action"]["type"], 1)
                self.assertEqual(
                    trend["action"]["data"], f"/zmdlog 趋势 {self.ACCOUNT}"
                )

    def test_a_button_tapped_again_in_the_chat_draws_nothing(self) -> None:
        # Two members tapping one button: the first picture went to both.
        self._account_page()
        data = f"/zmdlog 账号 {self.ACCOUNT}"
        now = [0.0]
        self.plugin._tapped = Cooldown(60, clock=lambda: now[0])
        api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))
        adapter = self._enable_callbacks(api)

        run(adapter.client.on_interaction_create(tap_of(data, member="111")))
        run(adapter.client.on_interaction_create(tap_of(data, member="222")))

        self.assertEqual(len(api.calls), 1)
        # Both taps acknowledged, so neither tapper sees 操作失败.
        self.assertEqual(len(api.acks), 2)
        # Another chat is another audience; the claim running out, the
        # same chat is answered again.
        self._tap(adapter, data, private=True)
        self.assertEqual(len(api.calls), 2)
        now[0] += 60
        run(adapter.client.on_interaction_create(tap_of(data, member="222")))
        self.assertEqual(len(api.calls), 3)

    def test_a_tap_that_failed_for_now_leaves_the_button_free(self) -> None:
        async def down(*args, **kwargs):
            raise ZmdLogsClientError("down")

        self.plugin.data.get_public_user_rankings = down
        data = f"/zmdlog 账号 {self.ACCOUNT}"
        api = FakeBotApi()
        adapter = self._enable_callbacks(api)

        self._tap(adapter, data)
        self._tap(adapter, data)

        # Upstream down is worth a retry: both taps are answered.
        self.assertEqual(len(api.calls), 2)

    def test_a_tapped_pick_list_answers_taps_but_a_watch_list_fills_in(
        self,
    ) -> None:
        self._enable_watch_storage()
        self._two_accounts_named_cpu()
        api = FakeBotApi()
        adapter = self._enable_callbacks(api)

        self._tap(adapter, "/zmdlog 账号 CPU")
        self._two_boards_in_one_dungeon()
        watch = self._official_event("/zmdlog 关注 测试区", api=api)
        watch.bot = adapter.client
        run(collect(self.plugin.zmdlog(watch)))

        (_, listing), (_, watching) = api.calls
        for payload, action in ((listing, 1), (watching, 2)):
            rows = payload["keyboard"]["content"]["rows"]
            self.assertEqual(
                [row["buttons"][0]["action"]["type"] for row in rows],
                [action, action],
            )
        # The quoted-reply code of a tapped list works like a typed one's.
        code = re.search(r"候选编号 ([A-Z2-9]{4})", listing["markdown"]["content"])
        self.assertIsNotNone(
            self.plugin.candidates.resolve(
                code[1], "1", origin="default:GroupMessage:G1"
            )
        )

    def test_a_tap_that_would_change_settings_is_refused(self) -> None:
        self._enable_watch_storage()
        self._enable_binding_storage()
        api = FakeBotApi()
        adapter = self._enable_callbacks(api)
        for data in (
            "/zmdlog 关注 usr_a",
            "/zmdlog 取关 usr_a",
            "/zmdlog 绑定 ZMD-AAAA-BBBB",
            "/zmdlog 别名 添加 罗丹 dung01_group_bossrush01",
        ):
            with self.subTest(data=data):
                with mock.patch.object(plugin_main, "logger") as logger:
                    self._tap(adapter, data)

                self.assertEqual(api.calls, [])
                logged = repr(logger.method_calls)
                self.assertIn("ZmdLogBot", logged)
                self.assertNotIn("usr_a", logged)
                self.assertNotIn("ZMD-AAAA", logged)
        self.assertFalse(self.plugin.watcher.watchlist_store.path.exists())
        self.assertFalse(self.plugin.bindings.store.path.exists())

    def test_a_tapped_query_that_fails_says_what_the_typed_one_says(self) -> None:
        async def down(*args, **kwargs):
            raise ZmdLogsClientError("down")

        self.plugin.data.get_public_user_rankings = down
        for data in (
            "/zmdlog 账号 x",  # refused before any request
            f"/zmdlog 账号 {self.ACCOUNT}",  # upstream down
            "/zmdlog 榜单 罗丹 --页 0",  # a parse error
        ):
            with self.subTest(data=data):
                api = FakeBotApi()
                adapter = self._enable_callbacks(api)
                _, typed = self._official(data, api=FakeBotApi())

                self._tap(adapter, data)

                (_, payload), = api.calls
                (reply,) = typed
                self.assertEqual(payload["msg_type"], 0)
                self.assertEqual(payload["content"], reply[1])
                self.assertEqual(payload["event_id"], "INTERACTION_CREATE:e-1")

    def test_a_tapped_page_about_no_one_thing_goes_as_a_native_picture(
        self,
    ) -> None:
        path = self._png(1920, 2000)

        async def render_help(*, command_prefix, official=False):
            return capture(path)

        self.plugin.renderer.render_help = render_help
        http = FakeBotHttp(raw_url=self.RAW_URL)
        api = FakeBotApi(http=http)
        adapter = self._enable_callbacks(api)

        self._tap(adapter, "/zmdlog help", private=True)

        self.assertTrue(http.paths[0].startswith("/v2/users/U1/"))
        (scene, payload), = api.calls
        self.assertEqual(scene, "c2c")
        self.assertEqual(payload["msg_type"], 7)
        self.assertEqual(payload["media"]["file_info"], "info-1")
        self.assertEqual(payload["event_id"], "INTERACTION_CREATE:e-1")

    def test_a_tapped_compare_with_first_place_compares_the_tapper(self) -> None:
        # 111 asks for the ranking; whoever taps 对比第一名 under it is 我.
        from astrbot_plugin_zmdlog.core.models import AccountSearchHit

        self._enable_binding_storage()
        accounts = {
            "111": ("usr_00000000000000000000000000000003", "公开账号3"),
            "222": ("usr_00000000000000000000000000000005", "公开账号5"),
            "333": ("usr_00000000000000000000000000000001", "公开账号1"),
            "444": ("usr_1234567890abcdef", "测试账号"),
        }
        codes = dict(
            zip(
                ("ZMD-AAAA-BBBB", "ZMD-CCCC-DDDD", "ZMD-EEEE-FFFF", "ZMD-GGGG-HHHH"),
                accounts.items(),
            )
        )

        async def lookup(code):
            return AccountSearchHit(*codes[code][1])

        async def ranking(boss_slug, **kwargs):
            return parse_boss_ranking(ranking_payload_with_rows())

        async def detail(battle_id):
            payload = battle_detail_payload()
            payload["battle"]["id"] = battle_id
            return parse_battle_detail(payload)

        compared: list[tuple[str, str]] = []
        picture = self._png(1920, 2000)

        async def render_compare(first, second, **kwargs):
            compared.append((first.battle_id, second.battle_id))
            return capture(picture)

        async def render_ranking(ranking, **kwargs):
            return capture(picture)

        self.plugin.client.get_binding_code_account = lookup
        self.plugin.data.get_boss_ranking = ranking
        self.plugin.data.get_battle_detail = detail
        self.plugin.renderer.render_compare = render_compare
        self.plugin.renderer.render_ranking = render_ranking
        for code, (member, _) in codes.items():
            (_, reply), = self._zmdlog(
                f"zmdlog 绑定 {code}", platform="qq_official", sender=member
            )
            self.assertIn("已绑定", reply)
        api = FakeBotApi(http=FakeBotHttp(raw_url=self.RAW_URL))
        adapter = self._enable_callbacks(api)
        typed = self._official_event("/zmdlog 三位一体", api=api, sender="111")
        typed.bot = adapter.client
        run(collect(self.plugin.zmdlog(typed)))
        (_, page), = api.calls
        compare = page["keyboard"]["content"]["rows"][1]["buttons"][1]
        self.assertEqual(compare["render_data"]["label"], "对比第一名")
        self.assertEqual(compare["action"]["type"], 1)
        data = compare["action"]["data"]
        self.assertEqual(data, "/zmdlog 对比 dung01_group_bossrush02 我")

        def tap(member: str) -> dict:
            api.calls.clear()
            run(adapter.client.on_interaction_create(tap_of(data, member=member)))
            (_, payload), = api.calls
            return payload

        # 222's own record (5th) against the first, not 111's (3rd), with a
        # button to each side's battle.
        mine = tap("222")
        self.assertEqual(mine["msg_type"], 2)
        self.assertEqual(
            [
                button["render_data"]["label"]
                for row in mine["keyboard"]["content"]["rows"]
                for button in row["buttons"]
            ],
            ["测试账号（第 1 名） 的战报", "测试账号（第 5 名） 的战报"],
        )
        self.assertEqual(
            compared, [("btl_upload_000000000001", "btl_upload_000000000005")]
        )
        # The tapper is first place, has no record here, or is not bound:
        # the typed command's replies, sent to the tapper's tap.
        first = tap("333")
        self.assertIn("公开账号1 就是「危境再现·三位一体」第 1 名", first["content"])
        missing = tap("444")
        self.assertIn("测试账号", missing["content"])
        self.assertIn("没有公开记录", missing["content"])
        unbound = tap("999")
        self.assertIn("还没有绑定账号", unbound["markdown"]["content"])
        (binding,), = (
            row["buttons"] for row in unbound["keyboard"]["content"]["rows"]
        )
        self.assertEqual(binding["action"]["type"], 0)
        self.assertEqual(len(compared), 1)

    # --- board watch ----------------------------------------------------------

    TRIO = "dung01_group_bossrush02"
    RODAN = "dung01_group_bossrush01"
    RODAN_LABEL = "危境再现 · 罗丹 · “碾骨之拳”罗丹"

    def _enable_watch_storage(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        watcher = self.plugin.watcher
        watcher.enabled = True
        watcher.watchlist_store.path = root / "watchlist.json"
        watcher.board_snapshot_store.path = root / "board-snapshot.json"
        # Two boards: the fixture's 三位一体 and 罗丹, in two dungeons.
        payload = hot_bosses_payload()
        payload.append(
            dict(
                payload[0],
                bossSlug=self.RODAN,
                bossKey="bossrush01",
                bossName="“碾骨之拳”罗丹",
                dungeonName="危境再现·罗丹",
            )
        )
        self.cards = parse_hot_bosses(payload)

    def _watch_slugs(self, origin: str = GROUP) -> list[str]:
        catalog = (self.TRIO, self.RODAN)
        return [
            slug
            for slug, origins in self.plugin.watcher.watchlist.origins_by_board(
                catalog
            ).items()
            if origin in origins
        ]

    def test_a_board_is_followed_by_its_keyword_listed_and_unfollowed(self) -> None:
        self._enable_watch_storage()
        # The index has read the board: following it snapshots it at once.
        self.plugin.data._board_read(None, standing("a1", slug=self.RODAN))

        (kind, reply), = self._zmdlog("zmdlog 关注 罗丹")
        self.assertEqual(kind, "plain")
        self.assertIn(f"已关注榜单「{self.RODAN_LABEL}」，序号 1", reply)
        self.assertIn("新纪录进入前 10 名时会在这里通报", reply)
        self.assertIn(self.RODAN, self.plugin.watcher.board_snapshots)
        self.assertTrue(self.plugin.watcher.board_snapshot_store.path.exists())

        (_, again), = self._zmdlog("zmdlog 关注 罗丹")
        self.assertIn("已经在关注列表里", again)
        (_, listing), = self._zmdlog("zmdlog 关注")
        self.assertIn("当前关注的榜单", listing)
        self.assertIn(f"1. {self.RODAN_LABEL}", listing)
        self.assertNotIn("账号", listing)

        (_, removed), = self._zmdlog("zmdlog 取关 罗丹")
        self.assertIn(f"已取消关注榜单「{self.RODAN_LABEL}」", removed)
        self.assertNotIn(self.RODAN, self.plugin.watcher.board_snapshots)
        self.assertEqual(self._watch_slugs(), [])
        (_, empty), = self._zmdlog("zmdlog 关注")
        self.assertIn("还没有关注任何榜单", empty)
        self.assertIn("关注 全部", empty)

    def test_a_board_is_unfollowed_by_its_list_number(self) -> None:
        self._enable_watch_storage()
        self._zmdlog("zmdlog 关注 罗丹")
        self._zmdlog("zmdlog 关注 三位一体")

        (_, removed), = self._zmdlog("zmdlog 取关 2")

        self.assertIn("已取消关注榜单「危境再现 · 测试区 · 三位一体」", removed)
        self.assertEqual(self._watch_slugs(), [self.RODAN])

    def test_an_account_name_is_looked_up_as_a_board_keyword(self) -> None:
        # Account 关注 is gone: a name that matches no board is a board that
        # was not found, and no account search is made.
        self._enable_watch_storage()

        async def unexpected(*args, **kwargs):
            raise AssertionError("no account search expected")

        self.plugin.client.search_public_accounts = unexpected

        (kind, reply), = self._zmdlog("zmdlog 关注 完全无关的昵称")

        self.assertEqual(
            (kind, reply), ("plain", "没有找到与「完全无关的昵称」匹配的榜单。")
        )
        self.assertEqual(self._watch_slugs(), [])

    def test_every_board_is_spelled_as_all_alone(self) -> None:
        # 关注 全部 is the one spelling; 全部榜单 is matched like any keyword.
        self._enable_watch_storage()

        (kind, reply), = self._zmdlog("zmdlog 关注 全部榜单")

        self.assertEqual(
            (kind, reply), ("plain", "没有找到与「全部榜单」匹配的榜单。")
        )
        self.assertEqual(self._watch_slugs(), [])

    def test_every_board_is_followed_and_one_excluded(self) -> None:
        self._enable_watch_storage()
        self._zmdlog("zmdlog 关注 三位一体")

        (kind, reply), = self._zmdlog("zmdlog 关注 全部")
        self.assertEqual(kind, "plain")
        self.assertIn("已关注全部榜单", reply)
        self.assertIn("以后新出的榜单也会自动包含", reply)
        self.assertEqual(self._watch_slugs(), [self.TRIO, self.RODAN])
        # A board nobody has ever seen is watched too.
        self.assertIn(
            GROUP,
            self.plugin.watcher.watchlist.origins_by_board(("brand_new",))[
                "brand_new"
            ],
        )

        (_, excluded), = self._zmdlog("zmdlog 取关 罗丹")
        self.assertIn(f"已在全部榜单中排除「{self.RODAN_LABEL}」", excluded)
        self.assertEqual(self._watch_slugs(), [self.TRIO])

        (_, listing), = self._zmdlog("zmdlog 关注")
        self.assertIn("当前关注：全部榜单", listing)
        self.assertIn(f"已排除：\n- {self.RODAN_LABEL}", listing)

        (_, included), = self._zmdlog("zmdlog 关注 罗丹")
        self.assertIn(f"已恢复关注榜单「{self.RODAN_LABEL}」", included)
        self.assertEqual(self._watch_slugs(), [self.TRIO, self.RODAN])
        (_, covered), = self._zmdlog("zmdlog 关注 罗丹")
        self.assertIn("已经关注了全部榜单", covered)

    def test_unfollowing_every_board_empties_the_chat(self) -> None:
        self._enable_watch_storage()
        for text in ("zmdlog 关注 全部", "zmdlog 取关 罗丹"):
            self._zmdlog(text)

        (kind, reply), = self._zmdlog("zmdlog 取关 全部")

        self.assertEqual((kind, reply), ("plain", "已取消这里的全部榜单关注。"))
        self.assertEqual(self._watch_slugs(), [])
        self.assertEqual(self.plugin.watcher.board_snapshots, {})
        (_, listing), = self._zmdlog("zmdlog 关注")
        self.assertIn("还没有关注任何榜单", listing)
        (_, again), = self._zmdlog("zmdlog 取关 全部")
        self.assertEqual(again, "这里还没有关注任何榜单。")

    def test_only_the_adder_or_an_admin_may_narrow_or_drop_every_board(
        self,
    ) -> None:
        self._enable_watch_storage()
        self._zmdlog("zmdlog 关注 全部", sender="111")

        (_, narrowed), = self._zmdlog("zmdlog 取关 罗丹", sender="222")
        (_, dropped), = self._zmdlog("zmdlog 取关 全部", sender="222")

        self.assertEqual(narrowed, "只有添加这条关注的人或机器人管理员可以取消它。")
        self.assertEqual(dropped, narrowed)
        self.assertEqual(self._watch_slugs(), [self.TRIO, self.RODAN])
        admin = FakeEvent("zmdlog 取关 全部", sender="222")
        admin.is_admin = lambda: True
        (_, done), = run(collect(self.plugin.zmdlog(admin)))
        self.assertEqual(done, "已取消这里的全部榜单关注。")

    def test_a_private_chat_keeps_its_own_watch_list(self) -> None:
        self._enable_watch_storage()
        private = "aiocqhttp:FriendMessage:111"

        (_, reply), = self._zmdlog("zmdlog 关注 罗丹", origin=private)
        self.assertIn("已关注榜单", reply)
        (_, listing), = self._zmdlog("zmdlog 关注", origin=private)

        self.assertIn(f"1. {self.RODAN_LABEL}", listing)
        self.assertEqual(self._watch_slugs(private), [self.RODAN])
        self.assertEqual(self._watch_slugs(GROUP), [])

    def test_isolated_members_of_one_group_share_its_watch_list(self) -> None:
        # 隔离对话 hands every member their own origin; the list is the group's.
        self._enable_watch_storage()

        def member(text: str, sender: str) -> FakeEvent:
            return FakeEvent(
                text,
                origin=f"aiocqhttp:GroupMessage:{sender}_1",
                sender=sender,
                isolated_from="1",
            )

        (_, reply), = run(
            collect(self.plugin.zmdlog(member("zmdlog 关注 三位一体", "111")))
        )
        self.assertIn("已关注榜单", reply)
        (_, listing), = run(collect(self.plugin.zmdlog(member("zmdlog 关注", "222"))))

        self.assertIn("1. 危境再现 · 测试区 · 三位一体", listing)
        self.assertEqual(self._watch_slugs(), [self.TRIO])
        self.assertEqual(
            self.plugin._event_origin(FakeEvent("x", origin=GROUP)), GROUP
        )

    def test_a_new_record_on_a_watched_board_is_pushed_at_the_interval(
        self,
    ) -> None:
        # The whole path: the index's read of a board reaches the watcher
        # through the data source, and the interval's end draws and pushes.
        self._enable_watch_storage()
        for text in ("zmdlog 关注 全部", "zmdlog 取关 罗丹"):
            self._zmdlog(text)
        self._notice_picture()
        self.plugin.context.platforms["aiocqhttp"] = platform_instance(
            "aiocqhttp", "aiocqhttp"
        )
        read = self.plugin.data._board_read
        read(standing("a1", "b1"), standing("n1", "a1", "b1"))
        read(
            standing("a1", slug=self.RODAN), standing("m1", "a1", slug=self.RODAN)
        )
        # An rDPS read is never news.
        read(standing("a1", metric="rdps"), standing("r1", "a1", metric="rdps"))

        run(self.plugin.watcher.run_notice_cycle())

        ((entries, kwargs),) = self.drawn
        self.assertEqual(
            [(entry.boss_slug, entry.record.battle_id) for entry in entries],
            [(self.TRIO, "btl_n1")],
        )
        self.assertTrue(entries[0].champion)
        self.assertEqual(kwargs["top_n"], 10)
        ((origin, _),) = self.plugin.context.pushed
        self.assertEqual(origin, GROUP)
        self.assertEqual(
            self.plugin.watcher.board_snapshots[self.TRIO].battle_ids,
            ("btl_n1", "btl_a1", "btl_b1"),
        )
        run(self.plugin.watcher.run_notice_cycle())
        self.assertEqual(len(self.plugin.context.pushed), 1)

    # --- 趋势 -----------------------------------------------------------------

    def _seed_history(self, name: str = "测试账号") -> None:
        """The index reads a board the account is on; nobody watches it."""

        payload = ranking_payload_with_rows()
        payload["rows"][1]["accountId"] = "usr_1234567890abcdef"
        payload["rows"][1]["accountDisplayName"] = name

        async def get_boss_rankings(slug, *, metric):
            return parse_boss_ranking(payload, metric=metric)

        self.plugin.client.get_boss_rankings = get_boss_rankings
        run(self.plugin.data.ranking_index.get(payload["bossSlug"]))

    def test_trend_answers_for_an_account_nobody_watches(self) -> None:
        (kind, reply), = self._zmdlog("zmdlog 趋势 usr_1234567890abcdef")
        self.assertEqual(kind, "plain")
        self.assertIn("还没有名次记录", reply)
        self.assertNotIn("关注", reply)

        self._seed_history()

        (kind, result), = self._zmdlog("zmdlog 趋势 usr_1234567890abcdef")
        self.assertEqual((kind, result), ("image", "/tmp/trend.png"))

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

    def test_trend_shows_a_week_unless_asked_for_more(self) -> None:
        self._seed_history()
        drawn: list[dict] = []

        async def draw(history, **kwargs):
            drawn.append(kwargs)
            return capture("/tmp/trend.png")

        self.plugin.renderer.render_trend = draw

        (kind, _), = self._zmdlog("zmdlog 趋势 usr_1234567890abcdef")
        self.assertEqual(kind, "image")
        (kind, _), = self._zmdlog("zmdlog 趋势 usr_1234567890abcdef --范围 30d")
        self.assertEqual(kind, "image")
        self.assertEqual([call["time_range"] for call in drawn], ["7d", "30d"])
        # One picture holds every board: the trend takes no page.
        (kind, reply), = self._zmdlog("zmdlog 趋势 usr_1234567890abcdef --页 2")
        self.assertEqual(kind, "plain")
        self.assertIn("--页", reply)

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

    # --- 绑定 / 我的 -------------------------------------------------------------

    def _enable_binding_storage(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.plugin.bindings.store.path = Path(directory.name) / "bindings.json"

    def test_a_bound_user_sees_their_own_account(self) -> None:
        from astrbot_plugin_zmdlog.core.models import AccountSearchHit

        self._enable_binding_storage()
        codes = {
            "ZMD-7K4M-QX2E": AccountSearchHit("usr_1234567890abcdef", "测试账号"),
            "ZMD-AAAA-BBBB": AccountSearchHit("usr_abcdef1234567890", "公开账号3"),
        }

        async def lookup(code):
            hit = codes.get(code)
            if hit is None:
                raise ZmdLogsAPIError(404, "binding_code_invalid", "无效")
            return hit

        async def account(requested_id):
            return parse_public_user_rankings(public_user_rankings_payload())

        self.plugin.client.get_binding_code_account = lookup
        self.plugin.data.get_public_user_rankings = account

        (kind, reply), = self._zmdlog("zmdlog 我的")
        self.assertEqual(kind, "plain")
        self.assertIn("还没有绑定账号", reply)
        # The hint carries the resolved prefix (none in these fake events).
        self.assertIn("zmdlog 绑定 ZMD-XXXX-XXXX", reply)
        (kind, reply), = self._zmdlog("zmdlog 绑定 zmd-7k4m-qx2e")
        self.assertEqual(kind, "plain")
        self.assertIn("已绑定 测试账号（usr_1234567890abcdef）", reply)
        (kind, reply), = self._zmdlog("zmdlog 绑定 ZMD-7K4M-QX2E")
        self.assertIn("已经用过了", reply)
        (kind, result), = self._zmdlog("zmdlog 我的")
        self.assertEqual((kind, result), ("image", "/tmp/account.png"))
        (kind, reply), = self._zmdlog("zmdlog 我的 2")
        self.assertEqual(kind, "plain")
        self.assertIn("绑定列表里没有「2」", reply)

        (_, reply), = self._zmdlog("zmdlog 绑定 ZMD-AAAA-BBBB")
        self.assertIn("2. 公开账号3", reply)
        # A binding is the person's, not the chat's: another group knows it.
        (kind, result), = self._zmdlog("zmdlog 我的 2", origin=OTHER_GROUP)
        self.assertEqual((kind, result), ("image", "/tmp/account.png"))
        (kind, reply), = self._zmdlog("zmdlog 解绑 全部")
        self.assertIn("已解除全部 2 个绑定", reply)

    def test_the_dropped_group_board_is_answered_like_any_unknown_word(
        self,
    ) -> None:
        # 群榜 was removed in 1.3.0: its words go to the smart query, which
        # tries the boards and then the public nicknames, as any text does.
        searched: list[str] = []

        async def search(query, *, limit):
            searched.append(query)
            return SimpleNamespace(
                query=query,
                has_more=False,
                accounts=(
                    SimpleNamespace(account_id="usr_a", account_display_name=query),
                ),
            )

        async def account(account_id):
            return parse_public_user_rankings(public_user_rankings_payload())

        self.plugin.client.search_public_accounts = search
        self.plugin.data.get_public_user_rankings = account
        for text in ("群榜", "群排名"):
            with self.subTest(text=text):
                (kind, result), = self._zmdlog(f"zmdlog {text}")

                self.assertEqual(searched[-1:], [text])
                self.assertEqual((kind, result), ("image", "/tmp/account.png"))

    def test_every_binding_command_works_in_a_private_chat(self) -> None:
        # The same person in a private chat is the same key as in a group
        # (aiocqhttp's sender is the QQ number either way), so a binding
        # made in one answers in the other.
        from astrbot_plugin_zmdlog.core.models import AccountSearchHit

        self._enable_binding_storage()
        ranking = parse_boss_ranking(ranking_payload_with_rows())
        uploader = ranking.rows[2]
        codes = {
            "ZMD-7K4M-QX2E": AccountSearchHit("usr_1234567890abcdef", "测试账号"),
            "ZMD-AAAA-BBBB": AccountSearchHit(
                uploader.account_id, uploader.account_display_name
            ),
        }

        async def lookup(code):
            return codes[code]

        async def account(requested_id):
            return parse_public_user_rankings(public_user_rankings_payload())

        async def ranking_read(boss_slug, **kwargs):
            return ranking

        async def detail(battle_id):
            payload = battle_detail_payload()
            payload["battle"]["id"] = battle_id
            return parse_battle_detail(payload)

        self.plugin.client.get_binding_code_account = lookup
        self.plugin.data.get_public_user_rankings = account
        self.plugin.data.get_boss_ranking = ranking_read
        self.plugin.data.get_battle_detail = detail
        private = "aiocqhttp:FriendMessage:111"

        (kind, reply), = self._zmdlog("zmdlog 绑定 ZMD-7K4M-QX2E", origin=private)
        self.assertEqual(kind, "plain")
        self.assertIn("已绑定 测试账号", reply)
        (_, reply), = self._zmdlog("zmdlog 绑定 ZMD-AAAA-BBBB", origin=private)
        self.assertIn("2. 公开账号3", reply)
        (kind, reply), = self._zmdlog("zmdlog 主账号 2", origin=private)
        self.assertIn("主账号已改为 公开账号3", reply)
        (kind, result), = self._zmdlog("zmdlog 我的", origin=private)
        self.assertEqual((kind, result), ("image", "/tmp/account.png"))
        (kind, result), = self._zmdlog("zmdlog 对比 三位一体 我", origin=private)
        self.assertEqual((kind, result), ("image", "/tmp/compare.png"))
        # Made in private, known in the group.
        (kind, result), = self._zmdlog("zmdlog 我的")
        self.assertEqual((kind, result), ("image", "/tmp/account.png"))
        (kind, reply), = self._zmdlog("zmdlog 解绑 全部", origin=private)
        self.assertIn("已解除全部 2 个绑定", reply)
        self.assertIsNone(self.plugin.bindings.bindings_for("aiocqhttp:111"))

    # --- 对比 <榜单> 我 [名次] ------------------------------------------------------

    def _bind_for_compare(self, *hits) -> list[dict]:
        """Bind ``hits`` (account id, name) in order to the default sender,
        with the board and the details stubbed; what the compare page got."""

        from astrbot_plugin_zmdlog.core.models import AccountSearchHit

        self._enable_binding_storage()
        codes = iter(hits)

        async def lookup(code):
            account_id, name = next(codes)
            return AccountSearchHit(account_id, name)

        async def detail(battle_id):
            payload = battle_detail_payload()
            payload["battle"]["id"] = battle_id
            return parse_battle_detail(payload)

        received: list[dict] = []

        async def render_compare(first, second, **kwargs):
            received.append({"ids": (first.battle_id, second.battle_id), **kwargs})
            return capture("/tmp/compare.png")

        self.plugin.client.get_binding_code_account = lookup
        self.plugin.data.get_battle_detail = detail
        self.plugin.renderer.render_compare = render_compare
        for code in ("ZMD-AAAA-BBBB", "ZMD-CCCC-DDDD")[: len(hits)]:
            (_, reply), = self._zmdlog(f"zmdlog 绑定 {code}")
            self.assertIn("已绑定", reply)
        return received

    def test_compare_me_puts_the_askers_best_record_against_a_rank(self) -> None:
        reads: list[dict] = []

        async def ranking(boss_slug, **kwargs):
            reads.append(kwargs)
            return parse_boss_ranking(ranking_payload_with_rows())

        self.plugin.data.get_boss_ranking = ranking
        received = self._bind_for_compare(
            ("usr_00000000000000000000000000000003", "公开账号3")
        )

        # Against the first place by default; the better rank is drawn first.
        (kind, result), = self._zmdlog("zmdlog 对比 三位一体 我")
        self.assertEqual((kind, result), ("image", "/tmp/compare.png"))
        self.assertEqual(
            received[-1]["ids"], ("btl_upload_000000000001", "btl_upload_000000000003")
        )
        self.assertEqual((received[-1]["rank_a"], received[-1]["rank_b"]), (1, 3))
        self.assertEqual(reads[-1].get("metric", "dps"), "dps")

        # Against any rank, behind the asker's as well as ahead of it.
        (kind, result), = self._zmdlog("zmdlog 对比 三位一体 我 5")
        self.assertEqual((kind, result), ("image", "/tmp/compare.png"))
        self.assertEqual(
            received[-1]["ids"], ("btl_upload_000000000003", "btl_upload_000000000005")
        )
        self.assertEqual((received[-1]["rank_a"], received[-1]["rank_b"]), (3, 5))

        (kind, reply), = self._zmdlog("zmdlog 对比 三位一体 我 3")
        self.assertEqual(kind, "plain")
        self.assertIn("公开账号3 就是「危境再现·三位一体」第 3 名", reply)
        (kind, reply), = self._zmdlog("zmdlog 对比 三位一体 我 9")
        self.assertEqual(kind, "plain")
        self.assertIn("没有第 9 名", reply)

    def test_compare_me_reads_the_primary_account_only(self) -> None:
        async def ranking(boss_slug, **kwargs):
            return parse_boss_ranking(ranking_payload_with_rows())

        self.plugin.data.get_boss_ranking = ranking
        received = self._bind_for_compare(
            ("usr_1234567890abcdef", "测试账号"),
            ("usr_00000000000000000000000000000003", "公开账号3"),
        )

        # The primary has no record here; the second account's is not used.
        (kind, reply), = self._zmdlog("zmdlog 对比 三位一体 我")
        self.assertEqual(kind, "plain")
        self.assertIn("测试账号", reply)
        self.assertIn("没有公开记录", reply)
        self.assertEqual(received, [])

        (_, reply), = self._zmdlog("zmdlog 主账号 2")
        self.assertIn("主账号已改为", reply)
        (kind, result), = self._zmdlog("zmdlog 对比 三位一体 我")
        self.assertEqual((kind, result), ("image", "/tmp/compare.png"))
        self.assertEqual(
            received[-1]["ids"], ("btl_upload_000000000001", "btl_upload_000000000003")
        )

    def test_compare_me_without_a_binding_says_how_to_bind(self) -> None:
        self._enable_binding_storage()
        looked_up: list[str] = []

        async def ranking(boss_slug, **kwargs):
            looked_up.append(boss_slug)
            return parse_boss_ranking(ranking_payload_with_rows())

        self.plugin.data.get_boss_ranking = ranking

        (kind, reply), = self._zmdlog("zmdlog 对比 三位一体 我")

        self.assertEqual(kind, "plain")
        self.assertIn("还没有绑定账号", reply)
        self.assertIn("zmdlog 绑定 ZMD-XXXX-XXXX", reply)
        self.assertEqual(looked_up, [], "refused before any board lookup")

    def test_compare_me_from_a_pick_list_is_whoever_picks(self) -> None:
        # 我 is whoever acts: a list kept only that 我 was asked for, so a
        # quoted pick compares the picker's record, as a tapped button does.
        from astrbot_plugin_zmdlog.core.models import AccountSearchHit

        async def ranking(boss_slug, **kwargs):
            return parse_boss_ranking(ranking_payload_with_rows())

        self.plugin.data.get_boss_ranking = ranking
        board = hot_bosses_payload()[0]
        self.cards = parse_hot_bosses(
            [board, {**board, "bossSlug": "dung01_group_bossrush03",
                     "bossKey": "bossrush03", "bossName": "危境再现·双子"}]
        )
        received = self._bind_for_compare(
            ("usr_00000000000000000000000000000003", "公开账号3")
        )

        (kind, listing), = self._zmdlog("zmdlog 对比 测试区 我 2")
        self.assertEqual(kind, "plain")
        self.assertIn("战报对比匹配到 2 个榜单", listing)
        (kind, result), = self._zmdlog("zmdlog 1", quoted=listing)
        self.assertEqual((kind, result), ("image", "/tmp/compare.png"))
        self.assertEqual((received[-1]["rank_a"], received[-1]["rank_b"]), (2, 3))

        # Someone unbound picking is told how to bind.
        (kind, reply), = self._zmdlog("zmdlog 1", quoted=listing, sender="999")
        self.assertEqual(kind, "plain")
        self.assertIn("还没有绑定账号", reply)

        async def lookup(code):
            return AccountSearchHit("usr_00000000000000000000000000000005", "公开账号5")

        self.plugin.client.get_binding_code_account = lookup
        self._zmdlog("zmdlog 绑定 ZMD-EEEE-FFFF", sender="222")
        (kind, result), = self._zmdlog("zmdlog 1", quoted=listing, sender="222")
        self.assertEqual((kind, result), ("image", "/tmp/compare.png"))
        self.assertEqual((received[-1]["rank_a"], received[-1]["rank_b"]), (2, 5))

    def test_the_rdps_option_picks_ranks_off_the_rdps_board(self) -> None:
        reads: list[dict] = []
        rdps_payload = ranking_payload_with_rows()
        rdps_payload["metric"] = "rdps"
        # Only two records could compute rDPS; on that board they are 1 and 2.
        rdps_payload["rows"] = [
            {**rdps_payload["rows"][4], "rank": 1},
            {**rdps_payload["rows"][2], "rank": 2},
        ]

        async def ranking(boss_slug, **kwargs):
            reads.append(kwargs)
            if kwargs.get("metric") == "rdps":
                return parse_boss_ranking(rdps_payload, metric="rdps")
            return parse_boss_ranking(ranking_payload_with_rows())

        self.plugin.data.get_boss_ranking = ranking
        received = self._bind_for_compare(
            ("usr_00000000000000000000000000000003", "公开账号3")
        )

        (kind, result), = self._zmdlog("zmdlog 对比 三位一体 我 --口径 rdps")
        self.assertEqual((kind, result), ("image", "/tmp/compare.png"))
        self.assertEqual(reads[-1]["metric"], "rdps")
        self.assertEqual(
            received[-1]["ids"], ("btl_upload_000000000005", "btl_upload_000000000003")
        )
        self.assertEqual((received[-1]["rank_a"], received[-1]["rank_b"]), (1, 2))
        self.assertEqual(received[-1]["metric"], "rdps")

        (kind, result), = self._zmdlog("zmdlog 对比 三位一体 1 2 --口径 rdps")
        self.assertEqual((kind, result), ("image", "/tmp/compare.png"))
        self.assertEqual(
            received[-1]["ids"], ("btl_upload_000000000005", "btl_upload_000000000003")
        )

        drawn: list[str] = []

        async def render_battle(battle, **kwargs):
            drawn.append(battle.battle_id)
            return capture("/tmp/battle.png")

        self.plugin.renderer.render_battle = render_battle
        (kind, result), = self._zmdlog("zmdlog 战报 三位一体 1 --口径 rdps")
        self.assertEqual((kind, result), ("image", "/tmp/battle.png"))
        self.assertEqual(reads[-1]["metric"], "rdps")
        self.assertEqual(drawn[-1], "btl_upload_000000000005")
        (kind, result), = self._zmdlog("zmdlog 战报 三位一体 1")
        self.assertEqual(reads[-1].get("metric", "dps"), "dps")
        self.assertEqual(drawn[-1], "btl_upload_000000000001")

    def test_the_v2_adapter_knows_a_person_by_the_official_key(self) -> None:
        # Its sender id is the same member_openid, so a binding made on one
        # adapter holds on the other; the webhook and the wild bot keep theirs.
        for platform, expected in (
            ("qq_official_v2", "qq_official:111"),
            ("qq_official", "qq_official:111"),
            ("qq_official_webhook", "qq_official_webhook:111"),
            ("aiocqhttp", "aiocqhttp:111"),
        ):
            with self.subTest(platform=platform):
                event = FakeEvent("zmdlog 我的", platform=platform)
                self.assertEqual(self.plugin._event_user_key(event), expected)

        from astrbot_plugin_zmdlog.core.models import AccountSearchHit

        self._enable_binding_storage()

        async def lookup(code):
            return AccountSearchHit("usr_1234567890abcdef", "测试账号")

        self.plugin.client.get_binding_code_account = lookup
        (kind, reply), = self._zmdlog(
            "zmdlog 绑定 ZMD-7K4M-QX2E",
            platform="qq_official_v2",
            origin="qq_official_v2:GroupMessage:G1",
        )
        self.assertIn("已绑定 测试账号", reply)
        self.assertEqual(
            [b.account_id for b in self.plugin.bindings.bindings_for(
                "qq_official:111"
            ).accounts],
            ["usr_1234567890abcdef"],
        )

    def test_binding_needs_a_sender_and_survives_an_outage(self) -> None:
        self._enable_binding_storage()

        async def down(code):
            raise ZmdLogsClientError("offline")

        self.plugin.client.get_binding_code_account = down

        (kind, reply), = self._zmdlog("zmdlog 绑定 ZMD-7K4M-QX2E")
        self.assertEqual((kind, reply), ("plain", "ZMDLogs 暂时不可用，请稍后重试。"))
        (kind, reply), = self._zmdlog("zmdlog 绑定 测试账号")
        self.assertIn("绑定只认绑定码", reply)

    # --- 排轴 reads the export, and the detail beside it ------------------------

    def test_cast_uses_the_export_of_the_ranked_battle(self) -> None:
        exported: list[str] = []

        async def ranking(boss_slug, **kwargs):
            return parse_boss_ranking(ranking_payload_with_rows())

        async def export(battle_id):
            exported.append(battle_id)
            return parse_battle_export(battle_export_payload())

        received: list[dict] = []

        async def detail(battle_id):
            return parse_battle_detail(battle_detail_payload())

        async def render_battle_cast(export, **kwargs):
            received.append(kwargs)
            return capture("/tmp/cast.png")

        self.plugin.data.get_boss_ranking = ranking
        self.plugin.data.get_battle_export = export
        self.plugin.data.get_battle_detail = detail
        self.plugin.renderer.render_battle_cast = render_battle_cast

        (kind, result), = self._zmdlog("zmdlog 排轴 三位一体 2")
        self.assertEqual((kind, result), ("image", "/tmp/cast.png"))
        self.assertEqual(exported, ["btl_upload_000000000002"])
        # The detail rides along for the BUFF band when it can be fetched...
        self.assertEqual(received[-1]["battle"].battle_id, "btl_upload_abcdef123456")
        # ...and the foot names the battle's pages, this one lit.
        self.assertEqual(
            received[-1]["views"],
            (("摘要", False), ("数据", False), ("排轴", True), ("养成", False)),
        )

        async def offline(battle_id):
            raise ZmdLogsClientError("offline")

        self.plugin.data.get_battle_detail = offline
        (kind, result), = self._zmdlog("zmdlog 排轴 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/cast.png"))
        # ...and the page still renders without it.
        self.assertIsNone(received[-1]["battle"])

    def test_the_old_timeline_words_are_answered_like_any_unknown_word(
        self,
    ) -> None:
        # 技能轴 and 时间轴 went with the 1.3.0 views, without an alias or a
        # hint: the smart query tries the boards, then the public nicknames.
        searched: list[str] = []

        async def search(query, *, limit):
            searched.append(query)
            return SimpleNamespace(
                query=query,
                has_more=False,
                accounts=(
                    SimpleNamespace(account_id="usr_a", account_display_name=query),
                ),
            )

        async def account(account_id):
            return parse_public_user_rankings(public_user_rankings_payload())

        async def export(battle_id):
            raise AssertionError("no cast export is read")

        self.plugin.client.search_public_accounts = search
        self.plugin.data.get_public_user_rankings = account
        self.plugin.data.get_battle_export = export
        for text in ("技能轴", "时间轴"):
            with self.subTest(text=text):
                (kind, result), = self._zmdlog(f"zmdlog {text}")

                self.assertEqual(searched[-1:], [text])
                self.assertEqual((kind, result), ("image", "/tmp/account.png"))

    def test_a_battle_draws_its_summary_whatever_the_export_answers(self) -> None:
        received: list[dict] = []

        async def detail(battle_id):
            return parse_battle_detail(battle_detail_payload())

        async def export(battle_id):
            return parse_battle_export(battle_export_payload())

        async def old_upload(battle_id):
            raise ZmdLogsAPIError(422, "battle_export_unsupported", "old")

        async def offline(battle_id):
            raise ZmdLogsClientError("offline")

        async def render_battle(battle, **kwargs):
            received.append(kwargs)
            return capture("/tmp/battle.png")

        self.plugin.data.get_battle_detail = detail
        self.plugin.renderer.render_battle = render_battle
        for answer in (export, old_upload, offline):
            with self.subTest(export=answer.__name__):
                self.plugin.data.get_battle_export = answer

                (kind, result), = self._zmdlog("zmdlog 战报 btl_upload_abcdef123456")

                self.assertEqual((kind, result), ("image", "/tmp/battle.png"))
                # The 摘要 draws no casts; its foot names the battle's pages,
                # itself lit, and 排轴 unless the export refused the upload
                # as one with no casts.
                self.assertNotIn("export", received[-1])
                self.assertEqual(
                    received[-1]["views"],
                    (("摘要", True), ("数据", False), ("养成", False))
                    if answer is old_upload
                    else (
                        ("摘要", True),
                        ("数据", False),
                        ("排轴", False),
                        ("养成", False),
                    ),
                )

    def test_data_draws_the_numbers_with_its_own_view_lit(self) -> None:
        received: list[dict] = []
        exported: list[str] = []

        async def detail(battle_id):
            return parse_battle_detail(battle_detail_payload())

        async def export(battle_id):
            exported.append(battle_id)
            return parse_battle_export(battle_export_payload())

        async def render_battle_data(battle, **kwargs):
            received.append(kwargs)
            return capture("/tmp/data.png")

        self.plugin.data.get_battle_detail = detail
        self.plugin.data.get_battle_export = export
        self.plugin.renderer.render_battle_data = render_battle_data

        (kind, result), = self._zmdlog("zmdlog 数据 btl_upload_abcdef123456")

        self.assertEqual((kind, result), ("image", "/tmp/data.png"))
        self.assertEqual(
            received[-1]["views"],
            (("摘要", False), ("数据", True), ("排轴", False), ("养成", False)),
        )
        # An upload without crit rolls has no 暴击期望 to draw.
        self.assertIsNone(received[-1]["crit"])
        # 数据 draws no casts and does not ask for them.
        self.assertEqual(exported, [])

    def test_the_old_skill_words_draw_no_page(self) -> None:
        # 技能 / 技能统计 are no commands any more: the words are a keyword
        # like any other, and no board is called that.
        async def detail(battle_id):
            raise AssertionError("no battle should be read")

        self.plugin.data.get_battle_detail = detail
        for command in ("技能", "技能统计"):
            with self.subTest(command=command):
                (kind, _), = self._zmdlog(
                    f"zmdlog {command} btl_upload_abcdef123456"
                )

                self.assertEqual(kind, "plain")

    # --- ranking pages -----------------------------------------------------------

    def _long_board(self, count: int = 25) -> list[dict]:
        """A board of ``count`` rows, every third one led by 洛茜."""

        received: list[dict] = []
        payload = ranking_payload_with_rows()
        template_a, template_b = payload["rows"][0], payload["rows"][2]
        payload["rows"] = [
            {
                **(template_b if rank % 3 == 0 else template_a),
                "rank": rank,
                "battleId": f"btl_upload_{rank:012d}",
                "accountId": f"usr_{rank:032d}",
                "accountDisplayName": f"公开账号{rank}",
                "durationMs": 60_000 + rank * 1000,
            }
            for rank in range(1, count + 1)
        ]

        async def ranking(boss_slug, **kwargs):
            metric = kwargs.get("metric", "dps")
            return parse_boss_ranking({**payload, "metric": metric}, metric=metric)

        async def render_ranking(ranking, **kwargs):
            received.append({**kwargs, "metric": ranking.metric})
            return capture("/tmp/ranking.png")

        self.plugin.data.get_boss_ranking = ranking
        self.plugin.renderer.render_ranking = render_ranking
        return received

    def test_a_ranking_is_drawn_a_page_at_a_time(self) -> None:
        received = self._long_board()

        for text, page in (
            ("zmdlog 三位一体", 1),
            ("zmdlog 榜单 三位一体", 1),
            ("zmdlog 榜单 三位一体 --页 2", 2),
            ("zmdlog 三位一体 --page 3", 3),
            ("zmdlog 三位一体 -p 2", 2),
            ("zmdlog 榜单 三位一体 --页 全部", "all"),
            ("zmdlog 三位一体 --页 all", "all"),
        ):
            with self.subTest(text=text):
                (kind, result), = self._zmdlog(text)
                self.assertEqual((kind, result), ("image", "/tmp/ranking.png"))
                self.assertEqual(received[-1]["page"], page)

    def test_a_page_past_the_last_says_how_many_there_are(self) -> None:
        received = self._long_board()

        (kind, reply), = self._zmdlog("zmdlog 榜单 三位一体 --页 4")

        self.assertEqual(kind, "plain")
        self.assertIn("共 25 条", reply)
        self.assertIn("共 3 页", reply)
        self.assertEqual(received, [])
        # Filtered, the pages are counted over the rows the filter keeps.
        (kind, reply), = self._zmdlog("zmdlog 三位一体 --角色 洛茜 --页 2")
        self.assertEqual(kind, "plain")
        self.assertIn("筛选后共 8 条", reply)
        self.assertIn("共 1 页", reply)

    def test_paging_keeps_the_filters_and_the_metric(self) -> None:
        received = self._long_board()

        (kind, result), = self._zmdlog(
            "zmdlog 榜单 三位一体 --页 2 --角色 黎风 --口径 rdps"
        )

        self.assertEqual((kind, result), ("image", "/tmp/ranking.png"))
        self.assertEqual(received[-1]["page"], 2)
        self.assertEqual(received[-1]["metric"], "rdps")
        self.assertEqual(received[-1]["character_filter"], ("黎风",))
        self.assertIs(
            received[-1]["character_filter_scope"], CharacterFilterScope.MAIN
        )

    def test_top_is_refused_and_points_at_the_page_option(self) -> None:
        received = self._long_board()

        for text in (
            "zmdlog 榜单 三位一体 --top 5",
            "zmdlog 三位一体 --top 30",
            "zmdlog 阵容 三位一体 --top 5",
        ):
            with self.subTest(text=text):
                (kind, reply), = self._zmdlog(text)
                self.assertEqual(kind, "plain")
                self.assertIn("--top", reply)
                self.assertIn("--页", reply)
        self.assertEqual(received, [])

    def test_a_number_after_the_roster_keyword_is_no_count(self) -> None:
        # 阵容 counts a fixed first ten: a trailing number is read as part
        # of the keyword like any other word, and the page takes no count.
        boards: list[str] = []
        drawn: list[dict] = []

        async def ranking(boss_slug, **kwargs):
            boards.append(boss_slug)
            return parse_boss_ranking(ranking_payload_with_rows())

        async def render_roster(ranking, **kwargs):
            drawn.append(kwargs)
            return capture("/tmp/roster.png")

        self.plugin.data.get_boss_ranking = ranking
        self.plugin.renderer.render_roster = render_roster

        replies = [
            self._zmdlog(text)
            for text in ("zmdlog 阵容 三位一体", "zmdlog 阵容 三位一体 5")
        ]

        self.assertEqual(replies, [[("image", "/tmp/roster.png")]] * 2)
        self.assertEqual(boards, ["dung01_group_bossrush02"] * 2)
        self.assertEqual(
            [sorted(kwargs) for kwargs in drawn], [["query", "web_base_url"]] * 2
        )
        self.assertEqual(drawn[1]["query"], "三位一体 5")

    def test_several_character_names_filter_the_whole_team(self) -> None:
        received: list[dict] = []

        async def ranking(boss_slug, **kwargs):
            return parse_boss_ranking(ranking_payload_with_rows())

        async def render_ranking(ranking, **kwargs):
            received.append(kwargs)
            return capture("/tmp/ranking.png")

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

        async def ranking(boss_slug, **kwargs):
            return parse_boss_ranking(ranking_payload_with_rows())

        async def detail(battle_id):
            fetched.append(battle_id)
            payload = battle_detail_payload()
            payload["battle"]["id"] = battle_id
            return parse_battle_detail(payload)

        async def render_compare(first, second, **kwargs):
            received.append({"ids": (first.battle_id, second.battle_id), **kwargs})
            return capture("/tmp/compare.png")

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

    def test_build_draws_the_battles_strip_with_itself_lit(self) -> None:
        seen: list[dict] = []

        async def detail(battle_id):
            return parse_battle_detail(battle_detail_payload())

        async def render_battle_build(battle, **kwargs):
            seen.append(kwargs)
            return capture("/tmp/build.png")

        self.plugin.data.get_battle_detail = detail
        self.plugin.renderer.render_battle_build = render_battle_build

        (kind, result), = self._zmdlog("zmdlog 养成 btl_upload_abcdef123456")

        self.assertEqual((kind, result), ("image", "/tmp/build.png"))
        self.assertEqual(
            seen[0]["views"],
            (("摘要", False), ("数据", False), ("排轴", False), ("养成", True)),
        )

    def test_the_old_loadout_words_are_no_commands(self) -> None:
        drawn: list[str] = []

        async def render_battle_build(battle, **kwargs):
            drawn.append(battle.battle_id)
            return capture("/tmp/build.png")

        self.plugin.renderer.render_battle_build = render_battle_build
        for word in ("配装", "装备"):
            with self.subTest(word=word):
                replies = self._zmdlog(f"zmdlog {word} btl_upload_abcdef123456")

                self.assertNotIn(("image", "/tmp/build.png"), replies)
        self.assertEqual(drawn, [])

    def test_the_gear_pages_are_handed_the_suit_catalog(self) -> None:
        # The catalog is what names a piece upstream left blank, so it has to
        # reach the page; an outage must still draw the page without it.
        seen: list[dict] = []

        async def detail(battle_id):
            return parse_battle_detail(battle_detail_payload())

        async def render_battle_build(battle, **kwargs):
            seen.append(kwargs.get("suits"))
            return capture("/tmp/build.png")

        wanted_suits: list[tuple] = []

        async def suits(*, wanted=()):
            wanted_suits.append(tuple(wanted))
            return {"suit_phy01": "点剑"}

        self.plugin.data.get_battle_detail = detail
        self.plugin.data.get_equip_suits = suits
        self.plugin.renderer.render_battle_build = render_battle_build

        (kind, result), = self._zmdlog("zmdlog 养成 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/build.png"))
        self.assertEqual(seen, [{"suit_phy01": "点剑"}])
        # The page names the suits it is about to print, so a suit the
        # catalog has never heard of can ask for a re-read.
        self.assertIn("suit_phy01", wanted_suits[0])

        async def broken(*, wanted=()):
            raise ZmdLogsClientError("offline")

        self.plugin.data.get_equip_suits = broken
        (kind, result), = self._zmdlog("zmdlog 养成 btl_upload_abcdef123456")
        self.assertEqual((kind, result), ("image", "/tmp/build.png"))
        self.assertEqual(seen[-1], {})

    def test_cast_explains_old_uploads_and_rate_limits(self) -> None:
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

        (kind, reply), = self._zmdlog("zmdlog 排轴 btl_upload_old000000001")
        self.assertEqual(kind, "plain")
        self.assertIn("旧版客户端", reply)
        self.assertIn("画不了排轴", reply)
        (kind, reply), = self._zmdlog("zmdlog 排轴 btl_upload_busy00000001")
        self.assertEqual(kind, "plain")
        self.assertIn("过于频繁", reply)
        (kind, reply), = self._zmdlog("zmdlog 排轴 btl_upload_gone00000001")
        self.assertEqual((kind, reply), ("plain", "战报不存在、未公开或已删除。"))

    def test_a_dead_battle_is_reported_the_same_way_for_every_page(self) -> None:
        async def missing(battle_id):
            raise ZmdLogsAPIError(404, "battle_not_found", "gone")

        self.plugin.data.get_battle_detail = missing

        for command in ("战报", "数据", "养成"):
            with self.subTest(command=command):
                (kind, reply), = self._zmdlog(
                    f"zmdlog {command} btl_upload_abcdef123456"
                )
                self.assertEqual(kind, "plain")
                self.assertEqual(reply, "战报不存在、未公开或已删除。")

    # --- LLM tool pictures ------------------------------------------------------

    def _tool_turn(self, *answers: ToolAnswer, event: FakeEvent | None = None):
        """One turn's tool calls, then the agent-done hook.

        Returns the replies the model got and the chains the chat received.
        """

        event = event or FakeEvent("突击里谁最菜")
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

    def test_a_tool_picture_on_the_official_bot_stays_a_native_picture(
        self,
    ) -> None:
        http = FakeBotHttp(raw_url=self.RAW_URL)
        api = FakeBotApi(http=http)
        event = self._official_event("突击里谁最菜", api=api)

        _, sent = self._tool_turn(ToolAnswer("洛茜的事实", "/tmp/a.png"), event=event)

        self.assertEqual(type(sent[0].chain[0]).__name__, "Image")
        self.assertEqual(api.calls, [])
        self.assertEqual(http.paths, [])

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

    def _streaming_turn(self, event, *, finish: bool) -> None:
        """One picture drawn while the reply streams; ``finish`` ends the stream."""

        async def character(*args, **kwargs):
            return ToolAnswer("大地的弃子", "/tmp/a.png")

        self.plugin.tools.character = character
        event.result = SimpleNamespace(
            result_content_type=ResultContentType.STREAMING_RESULT
        )

        async def scenario():
            await self.plugin.zmdlogs_character_standings(event, character="x")
            await self.plugin.send_tool_picture(event, None, None)
            await asyncio.sleep(0.05)
            # Still streaming: the picture waits for the reply to go out.
            self.assertEqual(event.sent, [])
            if finish:
                event.result = SimpleNamespace(
                    result_content_type=ResultContentType.STREAMING_FINISH
                )
            await asyncio.gather(*self.plugin._background_tasks)

        with (
            mock.patch.object(plugin_main, "_STREAMED_REPLY_POLL_SECONDS", 0.01),
            mock.patch.object(plugin_main, "_STREAMED_REPLY_WAIT_SECONDS", 0.2),
        ):
            run(scenario())

    def test_a_streamed_reply_goes_out_before_the_picture(self) -> None:
        # QQ official keeps one send buffer per event. A picture sent while
        # the reply streamed replaced the model's text in it, and the stream's
        # closing flush sent the picture a second time.
        event = FakeEvent("输入你的，输出这个是为什么")

        self._streaming_turn(event, finish=True)

        self.assertEqual(len(event.sent), 1)
        self.assertEqual(type(event.sent[0].chain[0]).__name__, "Image")

    def test_a_reply_that_never_finishes_streaming_gets_no_picture(self) -> None:
        # Sending anyway would be the duplicate again; the text is the answer.
        event = FakeEvent("输入你的，输出这个是为什么")

        self._streaming_turn(event, finish=False)

        self.assertEqual(event.sent, [])


if __name__ == "__main__":
    unittest.main()
