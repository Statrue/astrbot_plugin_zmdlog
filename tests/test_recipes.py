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

from core.candidates import CandidateStore
from core.matcher import AliasConfig, MatcherCache
from core.models import parse_boss_ranking, parse_public_user_rankings
from core.queries import QueryService
from core.routing import RouteKind, RouteRequest
from core.settings import PluginSettings
from core.toolbox import ToolService
from tests.helpers import public_user_rankings_payload, ranking_payload_with_rows
from tests.test_tools import WEB, FakeData, FakeRenderer


def run(coro):
    return asyncio.run(coro)


class SamePictureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ranking = parse_boss_ranking(ranking_payload_with_rows())
        self.data = FakeData(
            ranking=self.ranking,
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
            client=None,
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
