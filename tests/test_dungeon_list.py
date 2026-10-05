"""榜单 is a pick list of dungeons; a pick draws what typing its name draws.

The bare command used to draw every board's top three on one page, forty-nine
cards long. It now lists the dungeons, in the board list's order, through the
same candidate store every other pick list uses; a dungeon picked or typed
draws its top-three page, and a dungeon of one board draws that board.
"""

import asyncio
import logging
import unittest

from core import messages
from core.buttons import result_keyboard
from core.candidates import CandidateStore, CandidateView, format_candidates
from core.matcher import AliasConfig, MatcherCache, TargetType
from core.models import parse_boss_ranking
from core.outcome import PageSubject, PageTarget
from core.queries import QueryService
from core.routing import RouteKind, RouteRequest, parse_zmdlog_payload
from core.settings import PluginSettings
from tests.helpers import make_card, ranking_payload_with_rows
from tests.test_recipes import OfflineClient
from tests.test_result_images import buttons_of
from tests.test_tools import WEB, FakeData, FakeRenderer

GROUP = "aiocqhttp:GroupMessage:100"
CONTRACT = "indie_group_ccdg"
# Seven dungeons, more than a match list holds. The three 1期 dungeons make
# a phase a keyword can name as one scope; 危境再现 comes back after other
# dungeons, as the board list may interleave them.
CARDS = (
    make_card("dung01_group_bossrush01", "危境再现·罗丹", "危境再现", with_run=True),
    make_card("dung01_group_bossrush02", "危境再现·三位一体", "危境再现"),
    make_card(CONTRACT, "破潮之像", "危机合约", with_run=True),
    make_card("tower01_scar", "怨憎雾海·苦难", "影拓丰碑1期 · 灼痛疤痕"),
    make_card("tower01_inorganic", "矢影环伺·苦难", "影拓丰碑1期 · 无机造物"),
    make_card("tower01_outcast", "毒雾求生·苦难", "影拓丰碑1期 · 大地的弃子"),
    make_card("dung01_group_bossrush03", "危境再现·白垩界卫", "危境再现"),
    make_card("tower04_fire", "撼山雾火·苦难", "影拓丰碑4期 · 山中见犼"),
    make_card("echo_blade", "白刃穿水·残酷", "战争回响"),
    make_card("echo_beast", "野性旧事·残酷", "战争回响"),
)
DUNGEONS = [
    "危境再现",
    "危机合约",
    "影拓丰碑1期 · 灼痛疤痕",
    "影拓丰碑1期 · 无机造物",
    "影拓丰碑1期 · 大地的弃子",
    "影拓丰碑4期 · 山中见犼",
    "战争回响",
]


def run(coro):
    return asyncio.run(coro)


class DungeonListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = FakeData(ranking=parse_boss_ranking(ranking_payload_with_rows()))
        self.data.cards = CARDS
        self.renderer = FakeRenderer()
        self.store = CandidateStore()
        matchers = MatcherCache()
        self.service = QueryService(
            client=OfflineClient(),
            data=self.data,
            renderer=lambda: self.renderer,
            candidates=self.store,
            board_matcher=lambda cards: matchers.matcher_for(
                cards, AliasConfig.empty()
            ),
            trend=None,
            settings=PluginSettings(web_base_url=WEB),
            logger=logging.getLogger("test"),
        )

    def _dispatch(self, kind: RouteKind, query: str = ""):
        return run(
            self.service.dispatch(
                RouteRequest(kind, query=query), command_prefix="/", origin=GROUP
            )
        )

    def _pick(self, number: str):
        outcome = self._dispatch(RouteKind.DUNGEON_LIST)
        entry, choice = self.store.resolve(
            outcome.candidates.code, number, origin=GROUP
        )
        return run(self.service.render_pick(entry, choice))

    def test_the_bare_command_lists_every_dungeon_in_board_list_order(self) -> None:
        outcome = self._dispatch(RouteKind.DUNGEON_LIST)

        entry = outcome.candidates
        self.assertIsNone(outcome.image_path)
        self.assertEqual(self.renderer.calls, [])
        self.assertIs(entry.view, CandidateView.DUNGEONS)
        self.assertEqual(entry.origin, GROUP)
        self.assertEqual([choice.target.name for choice in entry.choices], DUNGEONS)
        self.assertEqual(
            outcome.message,
            format_candidates(entry, ttl_seconds=self.store.ttl_seconds),
        )

    def test_a_phase_is_not_listed_but_each_of_its_dungeons_is(self) -> None:
        outcome = self._dispatch(RouteKind.DUNGEON_LIST)

        targets = [choice.target for choice in outcome.candidates.choices]
        self.assertTrue(all(t.target_type is TargetType.DUNGEON for t in targets))
        self.assertNotIn("影拓丰碑1期", [target.name for target in targets])
        self.assertEqual(
            [target.boss_slugs for target in targets[2:5]],
            [("tower01_scar",), ("tower01_inorganic",), ("tower01_outcast",)],
        )

    def test_a_number_past_five_picks_from_the_list(self) -> None:
        outcome = self._dispatch(RouteKind.DUNGEON_LIST)

        _entry, choice = self.store.resolve(
            outcome.candidates.code, "7", origin=GROUP
        )

        self.assertEqual(choice.target.name, "战争回响")

    def test_picking_a_dungeon_draws_its_top_three(self) -> None:
        outcome = self._pick("1")

        self.assertEqual(self.renderer.calls, ["dungeon_top3"])
        choice, cards = self.renderer.args["dungeon_top3"]
        self.assertEqual(choice.target.name, "危境再现")
        self.assertEqual(
            [card.boss_slug for card in cards],
            [
                "dung01_group_bossrush01",
                "dung01_group_bossrush02",
                "dung01_group_bossrush03",
            ],
        )
        self.assertIsNotNone(outcome.image_path)
        # Its boards go under the picture, in the board list's order too.
        self.assertEqual(
            outcome.target,
            PageTarget(
                PageSubject.DUNGEON,
                "危境再现",
                boards=(
                    ("dung01_group_bossrush01", "危境再现·罗丹"),
                    ("dung01_group_bossrush02", "危境再现·三位一体"),
                    ("dung01_group_bossrush03", "危境再现·白垩界卫"),
                ),
            ),
        )

    def test_a_board_button_opens_the_ranking_page_and_its_keyboard_as_ever(
        self,
    ) -> None:
        podiums = self._dispatch(RouteKind.SMART_QUERY, "战争回响")
        buttons = buttons_of(
            result_keyboard(podiums.target, web_base_url=WEB, command="/zmdlog")
        )
        self.assertEqual(
            [button["action"]["data"] for button in buttons],
            ["/zmdlog echo_blade", "/zmdlog echo_beast"],
        )

        route = parse_zmdlog_payload(
            buttons[0]["action"]["data"].removeprefix("/zmdlog ")
        )
        board = run(self.service.dispatch(route, command_prefix="/", origin=GROUP))

        self.assertEqual(self.renderer.calls[-1], "ranking")
        self.assertEqual(
            board.target,
            PageTarget(PageSubject.BOARD, "echo_blade", record_count=5),
        )
        # The ranking page offers its own views and pages and no other board
        # of the dungeon: those would push its views off.
        keyboard = result_keyboard(board.target, web_base_url=WEB, command="/zmdlog")
        self.assertEqual(
            [
                [
                    (
                        button["render_data"]["label"],
                        button["action"]["type"],
                        button["action"]["data"],
                    )
                    for button in row["buttons"]
                ]
                for row in keyboard["content"]["rows"]
            ],
            [
                [
                    ("阵容", 2, "/zmdlog 阵容 echo_blade"),
                    ("角色统计", 2, "/zmdlog 角色统计 echo_blade"),
                ],
                [
                    ("第一名战报", 2, "/zmdlog 战报 echo_blade"),
                    ("对比第一名", 2, "/zmdlog 对比 echo_blade 我"),
                ],
                [("在 ZMDLogs 打开", 0, f"{WEB}/boss/echo_blade")],
            ],
        )

    def test_picking_a_one_board_dungeon_draws_that_board(self) -> None:
        outcome = self._pick("2")

        self.assertEqual(self.renderer.calls, ["ranking"])
        # The ranking page as typing its name draws it, buttons and all.
        self.assertEqual(
            outcome.target,
            PageTarget(
                PageSubject.BOARD, CONTRACT, CandidateView.RANKING, record_count=5
            ),
        )

    def test_typing_a_dungeon_draws_what_picking_it_draws(self) -> None:
        for query, page in (
            ("战争回响", "dungeon_top3"),
            ("危机合约", "ranking"),
            # A partial name misses the matcher's exact one-board rule; the
            # page is still the board, not a top three of one card.
            ("危机合", "ranking"),
        ):
            with self.subTest(query=query):
                self.renderer.calls.clear()

                self._dispatch(RouteKind.SMART_QUERY, query)

                self.assertEqual(self.renderer.calls, [page])

    def test_no_boards_at_all_is_said_rather_than_an_empty_list(self) -> None:
        self.data.cards = ()

        outcome = self._dispatch(RouteKind.DUNGEON_LIST)

        self.assertIsNone(outcome.candidates)
        self.assertEqual(outcome.message, messages.NO_PUBLIC_BOARDS)


if __name__ == "__main__":
    unittest.main()
