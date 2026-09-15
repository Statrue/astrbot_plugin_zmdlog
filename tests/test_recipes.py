"""One recipe per page: the command and the tool draw the same picture.

``core/recipes`` exists so that a filter, a window or a section cannot live
on one path only. These tests drive :class:`QueryService` and
:class:`ToolService` with the same fakes and compare the call the renderer
received. The page's label (``query``) is the one field allowed to differ:
it echoes what was typed, and the two paths are typed at differently.
"""

import asyncio
import logging
import unittest

from core import messages
from core.candidates import CandidateStore
from core.client import ZmdLogsAPIError, ZmdLogsClientError
from core.matcher import AliasConfig, MatcherCache
from core.models import (
    parse_battle_detail,
    parse_boss_ranking,
    parse_hot_bosses,
    parse_public_user_rankings,
)
from core.queries import QueryService
from core.routing import RouteKind, RouteRequest
from core.settings import PluginSettings
from core.toolbox import ToolService
from tests.helpers import (
    battle_detail_payload,
    hot_bosses_payload,
    public_user_rankings_payload,
    ranking_payload_with_rows,
)
from tests.test_compare import second_battle_payload
from tests.test_tools import WEB, FakeData, FakeRenderer


def run(coro):
    return asyncio.run(coro)


class OfflineClient:
    """The smart route also searches nicknames; here that read is an outage."""

    async def search_public_accounts(self, query, *, limit):
        raise ZmdLogsClientError("offline")


class SamePictureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ranking = parse_boss_ranking(ranking_payload_with_rows())
        self.data = FakeData(
            ranking=self.ranking,
            battles={
                "btl_upload_abcdef123456": parse_battle_detail(
                    battle_detail_payload()
                ),
                "btl_upload_bbbbbbbbbbbb": parse_battle_detail(
                    second_battle_payload()
                ),
            },
            account=parse_public_user_rankings(public_user_rankings_payload()),
        )
        matchers = MatcherCache()

        def board_matcher(cards):
            return matchers.matcher_for(cards, AliasConfig.empty())

        settings = PluginSettings(web_base_url=WEB)
        logger = logging.getLogger("test")
        self.command_renderer = FakeRenderer()
        self.tool_renderer = FakeRenderer()
        self.queries = QueryService(
            client=OfflineClient(),
            data=self.data,
            renderer=lambda: self.command_renderer,
            candidates=CandidateStore(),
            board_matcher=board_matcher,
            watcher=None,
            settings=settings,
            logger=logger,
        )
        self.tools = ToolService(
            client=None,
            data=self.data,
            renderer=lambda: self.tool_renderer,
            board_matcher=board_matcher,
            settings=settings,
            logger=logger,
        )

    def _command(self, kind: RouteKind, query: str = "", **options):
        route = RouteRequest(kind, query=query, **options)
        return run(self.queries.dispatch(route, command_prefix="/"))

    def _assert_same_call(self, page: str) -> None:
        self.assertEqual(self.command_renderer.calls, [page])
        self.assertEqual(self.tool_renderer.calls, [page])
        self.assertEqual(
            self.command_renderer.args[page], self.tool_renderer.args[page]
        )
        by_command = dict(self.command_renderer.kwargs[page])
        by_tool = dict(self.tool_renderer.kwargs[page])
        by_command.pop("query")
        by_tool.pop("query")
        self.assertEqual(by_command, by_tool)

    def test_the_champions_board(self) -> None:
        outcome = self._command(RouteKind.CHARACTER_STANDINGS)
        answer = run(self.tools.character(""))

        self.assertEqual(outcome.image_path, "/tmp/character_champions.png")
        self.assertEqual(answer.image_path, "/tmp/character_champions.png")
        self._assert_same_call("character_champions")
        # Until 2026-09-15 the tool drew the 从未上榜 section and the command
        # did not; both now name the catalog's characters no record fields.
        kwargs = self.command_renderer.kwargs["character_champions"]
        self.assertEqual(kwargs["unseen"], ("未上榜者",))
        self.assertIn("从未出现在公开记录里的角色：未上榜者", answer.text)

    def test_the_champions_board_cut_to_a_class_over_a_window(self) -> None:
        self._command(
            RouteKind.CHARACTER_STANDINGS, profession_filter="近卫", stats_range="7d"
        )
        run(self.tools.character("", profession="近卫", time_range="7d"))

        self._assert_same_call("character_champions")
        kwargs = self.tool_renderer.kwargs["character_champions"]
        self.assertEqual(kwargs["profession"], "近卫")
        self.assertEqual(kwargs["window_label"], "近 7 天")

    def test_the_champions_board_cut_to_an_element(self) -> None:
        self._command(RouteKind.CHARACTER_STANDINGS, element_filter="自然")
        run(self.tools.character("", element="自然"))

        self._assert_same_call("character_champions")
        # After an element cut nobody is 从未上榜: the other elements were
        # cut, not absent.
        kwargs = self.tool_renderer.kwargs["character_champions"]
        self.assertEqual(kwargs["unseen"], ())

    def test_one_character(self) -> None:
        name = self.ranking.rows[0].roster_entries[0].character_name

        self._command(RouteKind.CHARACTER_STANDINGS, query=name)
        run(self.tools.character(name))

        self._assert_same_call("character_standings")

    def test_a_team(self) -> None:
        self._command(RouteKind.CHARACTER_STANDINGS, query="黎风 卡缪")
        run(self.tools.character("黎风 卡缪"))

        self._assert_same_call("character_standings")
        (standings,) = self.command_renderer.args["character_standings"]
        self.assertEqual(standings.characters, ("黎风", "卡缪"))

    def test_the_player_board(self) -> None:
        self._command(RouteKind.PLAYER_CHAMPIONS, stats_range="30d")
        run(self.tools.account("", time_range="30d"))

        self._assert_same_call("player_champions")
        kwargs = self.tool_renderer.kwargs["player_champions"]
        self.assertEqual(kwargs["window_label"], "近 30 天")

    def test_the_records_page(self) -> None:
        self._command(RouteKind.RECORDS_QUERY, stats_range="14d")
        run(self.tools.board("", time_range="14d"))

        self._assert_same_call("records")
        kwargs = self.tool_renderer.kwargs["records"]
        self.assertEqual(kwargs["window_label"], "近 14 天")

    # --- boards -----------------------------------------------------------------

    def test_a_board(self) -> None:
        self._command(RouteKind.RANKING_QUERY, query="三位一体")
        run(self.tools.board("三位一体"))

        self._assert_same_call("ranking")

    def test_a_board_cut_to_a_character(self) -> None:
        self._command(
            RouteKind.RANKING_QUERY, query="三位一体", character_filter="黎风"
        )
        run(self.tools.board("三位一体", character="黎风"))

        self._assert_same_call("ranking")
        kwargs = self.tool_renderer.kwargs["ranking"]
        self.assertEqual(kwargs["character_filter"], ("黎风",))

    def test_a_board_cut_to_a_team(self) -> None:
        # The tool used to resolve one name only; "黎风 洁尔佩塔" now means
        # the teams fielding both, as --角色 黎风 洁尔佩塔 does.
        self._command(
            RouteKind.RANKING_QUERY, query="三位一体", character_filter="黎风 洁尔佩塔"
        )
        answer = run(self.tools.board("三位一体", character="黎风 洁尔佩塔"))

        self._assert_same_call("ranking")
        kwargs = self.tool_renderer.kwargs["ranking"]
        self.assertEqual(kwargs["character_filter"], ("黎风", "洁尔佩塔"))
        self.assertIn("阵容包含「黎风、洁尔佩塔」", answer.text)

    def test_a_board_cut_to_an_element(self) -> None:
        self._command(RouteKind.RANKING_QUERY, query="三位一体", element_filter="自然")
        run(self.tools.board("三位一体", element="自然"))

        self._assert_same_call("ranking")

    def test_a_board_refuses_a_filter_the_same_way(self) -> None:
        outcome = self._command(
            RouteKind.RANKING_QUERY, query="三位一体", element_filter="电磁"
        )
        answer = run(self.tools.board("三位一体", element="雷"))

        self.assertIsNone(outcome.image_path)
        self.assertIsNone(answer.image_path)
        self.assertEqual(outcome.message, answer.text)
        self.assertIn("没有主 C 为电磁属性的记录", answer.text)
        self.assertEqual(self.command_renderer.calls, [])
        self.assertEqual(self.tool_renderer.calls, [])

    def test_filters_that_leave_nothing_together_are_refused_on_both(self) -> None:
        # 洛茜 leads records and 自然 keeps rows, but no 自然 record has 洛茜
        # as main C. The command refused this; the tool drew an empty page.
        outcome = self._command(
            RouteKind.RANKING_QUERY,
            query="三位一体",
            character_filter="洛茜",
            element_filter="自然",
        )
        answer = run(self.tools.board("三位一体", character="洛茜", element="自然"))

        self.assertIsNone(outcome.image_path)
        self.assertIsNone(answer.image_path)
        self.assertEqual(outcome.message, answer.text)
        self.assertIn("主 C 为自然属性且带「洛茜」", answer.text)

    def test_every_board(self) -> None:
        self._command(RouteKind.ALL_RANKINGS)
        run(self.tools.board("全部"))

        self._assert_same_call("all_top3")

    def test_a_dungeon(self) -> None:
        first = hot_bosses_payload()[0]
        second = dict(
            first, bossSlug="dung01_group_bossrush03", bossName="危境再现·白垩界卫"
        )
        self.data.cards = parse_hot_bosses([first, second])

        self._command(RouteKind.RANKING_QUERY, query="测试区")
        run(self.tools.board("测试区"))

        self._assert_same_call("dungeon_top3")
        _choice, cards = self.tool_renderer.args["dungeon_top3"]
        self.assertEqual(len(cards), 2)

    # --- statistics -------------------------------------------------------------

    def test_the_distribution_on_a_board(self) -> None:
        self._command(RouteKind.CHARACTER_STATS, query="三位一体")
        run(self.tools.character("", board="三位一体"))

        self._assert_same_call("character_stats")
        self.assertEqual(self.data.stats_calls[0], self.data.stats_calls[1])

    def test_the_distribution_over_every_board(self) -> None:
        self._command(RouteKind.CHARACTER_STATS)
        run(self.tools.character("", board="全部"))

        self._assert_same_call("character_stats")
        self.assertEqual(self.data.stats_calls[0][0], None)
        self.assertEqual(self.data.stats_calls[0], self.data.stats_calls[1])

    def test_a_six_star_with_no_record(self) -> None:
        # 提弗洛斯 is in the catalog and in no roster: the distribution alone.
        self._command(RouteKind.CHARACTER_STATS, query="提弗洛斯")
        run(self.tools.character("提弗洛斯"))

        self._assert_same_call("character_boss")

    # --- battles and accounts ---------------------------------------------------

    def test_a_battle(self) -> None:
        self._command(RouteKind.BATTLE_QUERY, query="btl_upload_abcdef123456")
        run(self.tools.battle("btl_upload_abcdef123456"))

        self._assert_same_call("battle")

    def test_a_battle_whose_export_the_endpoint_refused(self) -> None:
        # The command's card said why its 施法节奏 section was missing; the
        # tool's picture said nothing. Both now carry the note.
        async def old_upload(battle_id):
            raise ZmdLogsAPIError(422, "battle_export_unsupported", "old")

        self.data.get_battle_export = old_upload

        self._command(RouteKind.BATTLE_QUERY, query="btl_upload_abcdef123456")
        run(self.tools.battle("btl_upload_abcdef123456"))

        self._assert_same_call("battle")
        kwargs = self.tool_renderer.kwargs["battle"]
        self.assertIsNone(kwargs["export"])
        self.assertEqual(kwargs["export_note"], messages.NO_TIMELINE)

    def test_a_comparison(self) -> None:
        first, second = "btl_upload_abcdef123456", "btl_upload_bbbbbbbbbbbb"
        self._command(RouteKind.COMPARE_QUERY, query=first, compare_target=second)
        run(self.tools.battle(first, compare_with=second))

        self._assert_same_call("compare")

    def test_an_account(self) -> None:
        self._command(RouteKind.ACCOUNT_QUERY, query="usr_1234567890abcdef")
        run(self.tools.account("usr_1234567890abcdef"))

        self._assert_same_call("account")
