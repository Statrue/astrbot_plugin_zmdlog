"""Result images on the QQ official bot: a markdown picture and a jump button.

A picture there goes out as a markdown image so a keyboard can hang under it
(an image message drops its buttons without a word). Two things must hold.
The declared size has the picture's true proportions — a declared shape
shorter than the real one is cropped around its middle, losing the page
header and the first rows — and the renderer, not an assumption, says how
many device pixels one CSS pixel took. And the button opens the ZMDLogs page
of the one thing the picture is about.
"""

import asyncio
import logging
import unittest

from core.buttons import result_image_message, result_keyboard
from core.candidates import CandidateStore, CandidateView
from core.matcher import AliasConfig, MatcherCache
from core.models import (
    parse_battle_detail,
    parse_battle_export,
    parse_boss_ranking,
    parse_hot_bosses,
    parse_public_user_rankings,
)
from core.outcome import PageSubject, PageTarget
from core.queries import QueryService
from core.routing import RouteKind, RouteRequest
from core.settings import PluginSettings
from tests.helpers import (
    battle_detail_payload,
    battle_export_payload,
    hot_bosses_payload,
    public_user_rankings_payload,
    ranking_payload_with_rows,
)
from tests.test_compare import second_battle_payload
from tests.test_metrics import rdps_payload
from tests.test_recipes import OfflineClient
from tests.test_tools import FakeData, FakeRenderer

WEB = "https://zmdlogs.com"
ACCOUNT = "usr_234b23819d65afe941c66dd08d8d3323"
BATTLE = "btl_upload_abcdef123456"
SLUG = "dung01_group_bossrush01"
# The board the ranking fixture is, and a keyword that finds it.
BOARD_SLUG = "dung01_group_bossrush02"
BOARD_KEYWORD = "三位一体"
RAW_URL = (
    "https://qqbot-file-upload-1251316161.cos.accelerate.myqcloud.com"
    "/f0/part_1?q-sign-algorithm=sha1&q-ak=AKID&q-signature=0a1b"
)
KEYBOARD = {"content": {"rows": []}}


class ResultImageMessageTests(unittest.TestCase):
    def test_the_declared_size_is_the_capture_in_css_pixels(self) -> None:
        # Pixels over the scale the renderer used. A long page captured at
        # 1x and a short one at 2x both come out 1280 CSS pixels wide.
        for scale, size, declared in (
            (1, (1280, 7588), "#1280px #7588px"),
            (2, (2560, 3000), "#1280px #1500px"),
        ):
            with self.subTest(scale=scale):
                message = result_image_message(
                    RAW_URL, size=size, scale=scale, keyboard=KEYBOARD
                )

                self.assertEqual(
                    message.markdown,
                    f"![img {declared}]"
                    f"({RAW_URL}&response-content-type=image%2Fpng)",
                )
                self.assertIs(message.keyboard, KEYBOARD)

    def test_a_half_pixel_is_declared_taller_never_shorter(self) -> None:
        # 5001 device pixels at 2x is 2500.5 CSS pixels. 2500 would declare a
        # shape a hair shorter than the picture, the one error that crops.
        message = result_image_message(
            RAW_URL, size=(2560, 5001), scale=2, keyboard=KEYBOARD
        )

        self.assertTrue(message.markdown.startswith("![img #1280px #2501px]("))

    def test_a_link_that_would_break_out_of_the_image_is_refused(self) -> None:
        # The link comes from the platform, and it is written into markdown
        # unescaped: anything that could end the image, or is no web link at
        # all, sends the native picture instead.
        for raw_url in (
            f"{RAW_URL})[forged](https://example.com",
            f"{RAW_URL} tail",
            f"{RAW_URL}\n# heading",
            "javascript:alert(1)?x=1",
            "//cos.example.com/part_1?x=1",
            "",
        ):
            with self.subTest(raw_url=raw_url):
                self.assertIsNone(
                    result_image_message(
                        raw_url, size=(2560, 3000), scale=2, keyboard=KEYBOARD
                    )
                )

    def test_a_link_without_a_query_gets_one(self) -> None:
        message = result_image_message(
            "https://cos.example.com/part_1",
            size=(1280, 900),
            scale=1,
            keyboard=KEYBOARD,
        )

        self.assertEqual(
            message.markdown,
            "![img #1280px #900px]"
            "(https://cos.example.com/part_1?response-content-type=image%2Fpng)",
        )

    def test_a_size_or_scale_that_is_no_picture_is_refused(self) -> None:
        for size, scale in (((2560, 3000), 0), ((0, 3000), 2), ((2560, 0), 1)):
            with self.subTest(size=size, scale=scale):
                self.assertIsNone(
                    result_image_message(
                        RAW_URL, size=size, scale=scale, keyboard=KEYBOARD
                    )
                )


def jump_links(target: PageTarget, web_base_url: str = WEB) -> list[str] | None:
    keyboard = result_keyboard(target, web_base_url=web_base_url)
    if keyboard is None:
        return None
    return [
        button["action"]["data"]
        for row in keyboard["content"]["rows"]
        for button in row["buttons"]
    ]


class JumpButtonTests(unittest.TestCase):
    def test_one_jump_button_that_everyone_may_tap(self) -> None:
        keyboard = result_keyboard(
            PageTarget(PageSubject.ACCOUNT, ACCOUNT), web_base_url=WEB
        )

        self.assertEqual(
            keyboard,
            {
                "content": {
                    "rows": [
                        {
                            "buttons": [
                                {
                                    "id": "open",
                                    "render_data": {
                                        "label": "在 ZMDLogs 打开",
                                        "visited_label": "在 ZMDLogs 打开",
                                        "style": 1,
                                    },
                                    "action": {
                                        "type": 0,
                                        "permission": {"type": 2},
                                        "data": f"{WEB}/records/{ACCOUNT}",
                                        "unsupport_tips": "请升级 QQ 后使用按钮",
                                    },
                                }
                            ]
                        }
                    ]
                }
            },
        )

    def test_each_page_opens_its_counterpart_on_zmdlogs(self) -> None:
        # The site's own routes: /records, /battle, /axis (its 排轴 view),
        # /boss and /boss/<slug>/statistics.
        view = CandidateView
        for target, link in (
            (PageTarget(PageSubject.ACCOUNT, ACCOUNT), f"/records/{ACCOUNT}"),
            (
                PageTarget(PageSubject.ACCOUNT, ACCOUNT, view.TREND, stats_range="7d"),
                f"/records/{ACCOUNT}",
            ),
            (PageTarget(PageSubject.BATTLE, BATTLE, view.BATTLE), f"/battle/{BATTLE}"),
            (PageTarget(PageSubject.BATTLE, BATTLE, view.LOADOUT), f"/battle/{BATTLE}"),
            (PageTarget(PageSubject.BATTLE, BATTLE, view.SKILLS), f"/battle/{BATTLE}"),
            (PageTarget(PageSubject.BATTLE, BATTLE, view.TIMELINE), f"/axis/{BATTLE}"),
            (PageTarget(PageSubject.BOARD, SLUG), f"/boss/{SLUG}"),
            (PageTarget(PageSubject.BOARD, SLUG, view.ROSTER), f"/boss/{SLUG}"),
            (PageTarget(PageSubject.BOARD, SLUG, view.GROUP_BOARD), f"/boss/{SLUG}"),
            (
                PageTarget(PageSubject.BOARD, SLUG, metric="rdps"),
                f"/boss/{SLUG}?metric=rdps",
            ),
            (
                PageTarget(PageSubject.BOARD, SLUG, view.CHARACTER_STATS),
                f"/boss/{SLUG}/statistics",
            ),
            (
                PageTarget(
                    PageSubject.BOARD,
                    SLUG,
                    view.CHARACTER_STATS,
                    metric="rdps",
                    stats_range="7d",
                    stats_potential="1-5",
                ),
                f"/boss/{SLUG}/statistics?metric=rdps&range=7d&potential=1-5",
            ),
        ):
            with self.subTest(target=target):
                self.assertEqual(jump_links(target), [WEB + link])

    def test_a_key_cannot_leave_its_path(self) -> None:
        self.assertEqual(
            jump_links(PageTarget(PageSubject.BOARD, "../admin?x=1#y")),
            [f"{WEB}/boss/..%2Fadmin%3Fx%3D1%23y"],
        )

    def test_a_site_address_that_is_no_web_link_gets_no_button(self) -> None:
        for web in ("", "zmdlogs.com", "javascript:alert(1)"):
            with self.subTest(web=web):
                self.assertIsNone(
                    jump_links(PageTarget(PageSubject.ACCOUNT, ACCOUNT), web)
                )


class OutcomeTargetTests(unittest.TestCase):
    """What ``dispatch`` hands the host: the picture, its scale, its subject."""

    def setUp(self) -> None:
        self.data = FakeData(
            ranking=parse_boss_ranking(ranking_payload_with_rows()),
            rdps_ranking=parse_boss_ranking(rdps_payload(), metric="rdps"),
            battles={
                BATTLE: parse_battle_detail(battle_detail_payload()),
                "btl_upload_bbbbbbbbbbbb": parse_battle_detail(
                    second_battle_payload()
                ),
            },
            account=parse_public_user_rankings(public_user_rankings_payload()),
        )
        matchers = MatcherCache()
        self.renderer = FakeRenderer()
        self.queries = QueryService(
            client=OfflineClient(),
            data=self.data,
            renderer=lambda: self.renderer,
            candidates=CandidateStore(),
            board_matcher=lambda cards: matchers.matcher_for(
                cards, AliasConfig.empty()
            ),
            watcher=None,
            settings=PluginSettings(web_base_url=WEB),
            logger=logging.getLogger("test"),
        )

    def _command(self, kind: RouteKind, query: str = "", **options):
        route = RouteRequest(kind, query=query, **options)
        return asyncio.run(self.queries.dispatch(route, command_prefix="/"))

    def test_an_account_page_carries_its_account_and_its_capture_scale(self) -> None:
        # A page long enough falls back to 1x: the scale is the renderer's
        # word, never the page kind's usual one.
        self.renderer.scale = 1

        outcome = self._command(RouteKind.ACCOUNT_QUERY, query=ACCOUNT)

        self.assertEqual(outcome.image_path, "/tmp/account.png")
        self.assertEqual(outcome.image_scale, 1)
        self.assertEqual(outcome.target, PageTarget(PageSubject.ACCOUNT, ACCOUNT))

    def test_battle_pages_carry_their_battle(self) -> None:
        async def export(battle_id):
            return parse_battle_export(battle_export_payload())

        self.data.get_battle_export = export
        for kind, view in (
            (RouteKind.BATTLE_QUERY, CandidateView.BATTLE),
            (RouteKind.LOADOUT_QUERY, CandidateView.LOADOUT),
            (RouteKind.SKILL_QUERY, CandidateView.SKILLS),
            (RouteKind.TIMELINE_QUERY, CandidateView.TIMELINE),
        ):
            with self.subTest(view=view):
                outcome = self._command(kind, query=BATTLE)

                self.assertEqual(outcome.image_scale, 2)
                self.assertEqual(
                    outcome.target, PageTarget(PageSubject.BATTLE, BATTLE, view)
                )

    def test_a_battle_found_by_rank_carries_the_battle_not_the_board(self) -> None:
        second = "btl_upload_000000000002"
        self.data.battles[second] = self.data.battles[BATTLE]

        outcome = self._command(
            RouteKind.LOADOUT_QUERY, query=BOARD_KEYWORD, battle_rank=2
        )

        self.assertEqual(
            outcome.target,
            PageTarget(PageSubject.BATTLE, second, CandidateView.LOADOUT),
        )

    def test_board_pages_carry_their_board_and_the_options_drawn_with(self) -> None:
        for kind, options, target in (
            (
                RouteKind.RANKING_QUERY,
                {},
                PageTarget(PageSubject.BOARD, BOARD_SLUG),
            ),
            (
                RouteKind.RANKING_QUERY,
                {"metric": "rdps"},
                PageTarget(PageSubject.BOARD, BOARD_SLUG, metric="rdps"),
            ),
            (
                RouteKind.ROSTER_QUERY,
                {},
                PageTarget(PageSubject.BOARD, BOARD_SLUG, CandidateView.ROSTER),
            ),
            (
                RouteKind.CHARACTER_STATS,
                {"stats_range": "7d", "stats_potential": "0", "metric": "rdps"},
                PageTarget(
                    PageSubject.BOARD,
                    BOARD_SLUG,
                    CandidateView.CHARACTER_STATS,
                    metric="rdps",
                    stats_range="7d",
                    stats_potential="0",
                ),
            ),
        ):
            with self.subTest(kind=kind):
                outcome = self._command(kind, query=BOARD_KEYWORD, **options)

                self.assertEqual(outcome.image_scale, 2)
                self.assertEqual(outcome.target, target)

    def test_pages_about_no_one_thing_carry_no_target(self) -> None:
        # Help, the statistics of every board and the index pages have no
        # ZMDLogs counterpart; a comparison has two, and neither is it.
        for kind, query, options in (
            (RouteKind.HELP, "", {}),
            (RouteKind.ALL_RANKINGS, "", {}),
            (RouteKind.CHARACTER_STATS, "", {}),
            (RouteKind.CHARACTER_STATS, "提弗洛斯", {}),
            (RouteKind.CHARACTER_STANDINGS, "", {}),
            (RouteKind.PLAYER_CHAMPIONS, "", {}),
            (RouteKind.RECORDS_QUERY, "", {}),
            (
                RouteKind.COMPARE_QUERY,
                BATTLE,
                {"compare_target": "btl_upload_bbbbbbbbbbbb"},
            ),
        ):
            with self.subTest(kind=kind, query=query):
                outcome = self._command(kind, query=query, **options)

                self.assertIsNotNone(outcome.image_path)
                self.assertEqual(outcome.image_scale, 2)
                self.assertIsNone(outcome.target)

    def test_a_dungeon_has_no_page_of_its_own(self) -> None:
        first = hot_bosses_payload()[0]
        second = dict(
            first, bossSlug="dung01_group_bossrush03", bossName="危境再现·白垩界卫"
        )
        self.data.cards = parse_hot_bosses([first, second])

        outcome = self._command(RouteKind.RANKING_QUERY, query="测试区")

        self.assertEqual(self.renderer.calls, ["dungeon_top3"])
        self.assertIsNone(outcome.target)


if __name__ == "__main__":
    unittest.main()
