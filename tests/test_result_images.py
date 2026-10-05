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
from types import SimpleNamespace

from core.battle_views import BATTLE_VIEWS
from core.buttons import (
    MAX_BUTTON_DATA_CHARS,
    read_button_command,
    result_image_message,
    result_keyboard,
)
from core.candidates import CandidateStore, CandidateView
from core.client import ZmdLogsAPIError
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
from core.routing import ALL_PAGES, RouteKind, RouteRequest, parse_zmdlog_payload
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
COMMAND = "/zmdlog"


class ResultImageMessageTests(unittest.TestCase):
    def test_the_declared_size_is_the_capture_in_css_pixels(self) -> None:
        # Pixels over the scale the renderer used: the same 960 page
        # captured at 1x or at 2x is declared 960 CSS pixels wide.
        for scale, size, declared in (
            (1, (960, 7588), "#960px #7588px"),
            (2, (1920, 3000), "#960px #1500px"),
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

    def test_a_notice_picture_has_nothing_under_it(self) -> None:
        message = result_image_message(
            RAW_URL, size=(1920, 3000), scale=2, keyboard=None
        )

        self.assertTrue(message.markdown.startswith("![img #960px #1500px]("))
        self.assertIsNone(message.keyboard)

    def test_a_half_pixel_is_declared_taller_never_shorter(self) -> None:
        # 5001 device pixels at 2x is 2500.5 CSS pixels. 2500 would declare a
        # shape a hair shorter than the picture, the one error that crops.
        message = result_image_message(
            RAW_URL, size=(1920, 5001), scale=2, keyboard=KEYBOARD
        )

        self.assertTrue(message.markdown.startswith("![img #960px #2501px]("))

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
                        raw_url, size=(1920, 3000), scale=2, keyboard=KEYBOARD
                    )
                )

    def test_a_link_without_a_query_gets_one(self) -> None:
        message = result_image_message(
            "https://cos.example.com/part_1",
            size=(1920, 1800),
            scale=2,
            keyboard=KEYBOARD,
        )

        self.assertEqual(
            message.markdown,
            "![img #960px #900px]"
            "(https://cos.example.com/part_1?response-content-type=image%2Fpng)",
        )

    def test_a_size_or_scale_that_is_no_picture_is_refused(self) -> None:
        for size, scale in (((1920, 3000), 0), ((0, 3000), 2), ((1920, 0), 1)):
            with self.subTest(size=size, scale=scale):
                self.assertIsNone(
                    result_image_message(
                        RAW_URL, size=size, scale=scale, keyboard=KEYBOARD
                    )
                )


def buttons_of(keyboard) -> list[dict]:
    return [button for row in keyboard["content"]["rows"] for button in row["buttons"]]


def jump_links(target: PageTarget, web_base_url: str = WEB) -> list[str] | None:
    keyboard = result_keyboard(target, web_base_url=web_base_url, command=COMMAND)
    if keyboard is None:
        return None
    return [
        button["action"]["data"]
        for button in buttons_of(keyboard)
        if button["action"]["type"] == 0
    ]


class JumpButtonTests(unittest.TestCase):
    def test_one_jump_button_that_everyone_may_tap(self) -> None:
        keyboard = result_keyboard(
            PageTarget(PageSubject.ACCOUNT, ACCOUNT), web_base_url=WEB, command=COMMAND
        )

        # The first row, full width; the page's other views go under it.
        self.assertEqual(
            keyboard["content"]["rows"][0],
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
            },
        )

    def test_each_page_opens_its_counterpart_on_zmdlogs(self) -> None:
        # The site's own routes: /records, /battle, /axis (its 排轴 view),
        # /boss and /boss/<slug>/statistics.
        view = CandidateView
        for target, link in (
            (PageTarget(PageSubject.ACCOUNT, ACCOUNT), f"/records/{ACCOUNT}"),
            (
                PageTarget(PageSubject.ACCOUNT, ACCOUNT, view.TREND),
                f"/records/{ACCOUNT}",
            ),
            (PageTarget(PageSubject.BATTLE, BATTLE, view.BATTLE), f"/battle/{BATTLE}"),
            (PageTarget(PageSubject.BATTLE, BATTLE, view.BUILD), f"/battle/{BATTLE}"),
            (PageTarget(PageSubject.BATTLE, BATTLE, view.DATA), f"/battle/{BATTLE}"),
            (PageTarget(PageSubject.BATTLE, BATTLE, view.CAST), f"/axis/{BATTLE}"),
            (PageTarget(PageSubject.BOARD, SLUG), f"/boss/{SLUG}"),
            (PageTarget(PageSubject.BOARD, SLUG, view.ROSTER), f"/boss/{SLUG}"),
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


def sibling_buttons(target: PageTarget) -> list[tuple[str, str]]:
    """``(label, command)`` of every command button under ``target``'s picture."""

    keyboard = result_keyboard(target, web_base_url=WEB, command=COMMAND)
    return [
        (button["render_data"]["label"], button["action"]["data"])
        for button in buttons_of(keyboard)
        if button["action"]["type"] == 2
    ]


V = CandidateView
# Every page with a target, as (subject, view): the sibling table must name
# each one, and nothing else.
EVERY_PAGE = (
    (PageSubject.ACCOUNT, V.RANKING),
    (PageSubject.ACCOUNT, V.TREND),
    (PageSubject.BATTLE, V.BATTLE),
    (PageSubject.BATTLE, V.DATA),
    (PageSubject.BATTLE, V.CAST),
    (PageSubject.BATTLE, V.BUILD),
    (PageSubject.BOARD, V.RANKING),
    (PageSubject.BOARD, V.ROSTER),
    (PageSubject.BOARD, V.CHARACTER_STATS),
    (PageSubject.CHARACTER, V.CHARACTER_PROFILE),
)
_KEYS = {
    PageSubject.ACCOUNT: ACCOUNT,
    PageSubject.BATTLE: BATTLE,
    PageSubject.CHARACTER: "chr_0016_laevat",
}


def page(subject: PageSubject, view: CandidateView, **options) -> PageTarget:
    # A command names a character by its name; the others by their key.
    if subject is PageSubject.CHARACTER:
        options.setdefault("name", "莱万汀")
    return PageTarget(subject, _KEYS.get(subject, SLUG), view, **options)


# What a label promises, and the route that draws it for each subject.
LABELS = {
    "摘要": V.BATTLE,
    "数据": V.DATA,
    "排轴": V.CAST,
    "养成": V.BUILD,
    "榜单": V.RANKING,
    "阵容": V.ROSTER,
    "角色统计": V.CHARACTER_STATS,
    "账号": V.RANKING,
    "名次趋势": V.TREND,
    "角色排名": V.CHARACTER_STANDINGS,
}
ROUTE_OF = {
    (PageSubject.ACCOUNT, V.RANKING): RouteKind.ACCOUNT_QUERY,
    (PageSubject.ACCOUNT, V.TREND): RouteKind.TREND_QUERY,
    (PageSubject.BATTLE, V.BATTLE): RouteKind.BATTLE_QUERY,
    (PageSubject.BATTLE, V.DATA): RouteKind.DATA_QUERY,
    (PageSubject.BATTLE, V.CAST): RouteKind.CAST_QUERY,
    (PageSubject.BATTLE, V.BUILD): RouteKind.BUILD_QUERY,
    (PageSubject.BOARD, V.RANKING): RouteKind.RANKING_QUERY,
    (PageSubject.BOARD, V.ROSTER): RouteKind.ROSTER_QUERY,
    (PageSubject.BOARD, V.CHARACTER_STATS): RouteKind.CHARACTER_STATS,
    (PageSubject.BOARD, V.BATTLE): RouteKind.BATTLE_QUERY,
    (PageSubject.CHARACTER, V.CHARACTER_STATS): RouteKind.CHARACTER_STATS,
    (PageSubject.CHARACTER, V.CHARACTER_STANDINGS): RouteKind.CHARACTER_STANDINGS,
}


class SiblingButtonTests(unittest.TestCase):
    """The page's other views, one command button each, under its jump button."""

    def test_each_page_offers_its_other_views(self) -> None:
        c = COMMAND
        for target, expected in (
            (
                page(PageSubject.BOARD, V.CHARACTER_STATS),
                [("榜单", f"{c} 榜单 {SLUG}"), ("阵容", f"{c} 阵容 {SLUG}")],
            ),
            (
                page(PageSubject.ACCOUNT, V.RANKING),
                [("名次趋势", f"{c} 趋势 {ACCOUNT}")],
            ),
            (
                page(PageSubject.ACCOUNT, V.TREND),
                [("账号", f"{c} 账号 {ACCOUNT}")],
            ),
        ):
            with self.subTest(target=target):
                self.assertEqual(sibling_buttons(target), expected)

    def test_a_board_page_keeps_its_metric_in_every_command(self) -> None:
        # The ranking's own buttons are RankingKeyboardTests'.
        commands = [
            command
            for _, command in sibling_buttons(
                page(PageSubject.BOARD, V.ROSTER, metric="rdps")
            )
        ]

        self.assertEqual(
            commands,
            [
                f"{COMMAND} 榜单 {SLUG} --口径 rdps",
                f"{COMMAND} 角色统计 {SLUG} --口径 rdps",
            ],
        )

    def test_a_board_page_keeps_its_page_to_itself(self) -> None:
        # Only the ranking is paged, and its other views open on their own
        # first page (RankingKeyboardTests); they open the ranking on its
        # first page in turn.
        for view, expected in (
            (
                V.ROSTER,
                [f"{COMMAND} 榜单 {SLUG}", f"{COMMAND} 角色统计 {SLUG}"],
            ),
        ):
            with self.subTest(view=view):
                target = page(PageSubject.BOARD, view, ranking_page=3)

                self.assertEqual(
                    [command for _, command in sibling_buttons(target)], expected
                )

    def test_every_command_parses_back_to_the_page_its_label_names(self) -> None:
        # The load-bearing check: typed back in, each command must draw the
        # page its label promises, of this target, under the same metric.
        for subject, view in EVERY_PAGE:
            if (subject, view) == (PageSubject.BOARD, V.RANKING):
                # Its keyboard pages as well; RankingKeyboardTests checks it.
                continue
            board = subject is PageSubject.BOARD
            for metric in ("dps", "rdps") if board else ("dps",):
                for top in (None, 3) if board else (None,):
                    target = page(subject, view, metric=metric, ranking_page=top)
                    for label, command in sibling_buttons(target):
                        with self.subTest(command=command):
                            payload = command.removeprefix(f"{COMMAND} ")
                            route = parse_zmdlog_payload(payload)
                            drawn = LABELS[label]

                            self.assertIs(route.kind, ROUTE_OF[subject, drawn])
                            self.assertEqual(route.query, target.name or target.key)
                            self.assertEqual(route.metric, metric)
                            self.assertEqual(route.battle_rank, 1)
                            self.assertIsNone(route.ranking_page)

    def test_every_page_with_a_target_has_a_list(self) -> None:
        for subject, view in EVERY_PAGE:
            if (subject, view) == (PageSubject.BATTLE, V.BATTLE) and len(
                BATTLE_VIEWS
            ) == 1:
                # The 摘要 alone on the strip: nothing to offer yet.
                continue
            with self.subTest(subject=subject, view=view):
                self.assertTrue(sibling_buttons(page(subject, view)))

    def test_a_battles_pages_offer_the_others_on_its_strip(self) -> None:
        # The buttons under a battle's picture are the pages its foot lists,
        # by the same names, never a pre-1.3.0 view the strip does not name.
        for entry in BATTLE_VIEWS:
            with self.subTest(view=entry.view):
                self.assertEqual(
                    sibling_buttons(page(PageSubject.BATTLE, entry.view)),
                    [
                        (other.label, f"{COMMAND} {other.word} {BATTLE}")
                        for other in BATTLE_VIEWS
                        if other is not entry
                    ],
                )
        summary = page(PageSubject.BATTLE, V.BATTLE)
        for word in ("配装", "技能"):
            self.assertNotIn(
                word, [label for label, _ in sibling_buttons(summary)]
            )
        # The jump button stays: the battle is still on ZMDLogs.
        self.assertEqual(jump_links(summary), [f"{WEB}/battle/{BATTLE}"])

    def test_a_view_the_target_lacks_gets_no_button(self) -> None:
        # An older upload has no roster, skill statistics or casts; an
        # account the rank watch never polled has no trend.
        battle = page(
            PageSubject.BATTLE,
            V.BUILD,
            unavailable=frozenset({V.CAST}),
        )
        account = page(
            PageSubject.ACCOUNT, V.RANKING, unavailable=frozenset({V.TREND})
        )

        self.assertEqual(
            sibling_buttons(battle),
            [
                ("摘要", f"{COMMAND} 战报 {BATTLE}"),
                ("数据", f"{COMMAND} 数据 {BATTLE}"),
            ],
        )
        self.assertEqual(sibling_buttons(account), [])
        # The jump button stays: the page itself is still on ZMDLogs.
        self.assertEqual(jump_links(account), [f"{WEB}/records/{ACCOUNT}"])

    def test_with_callbacks_every_other_view_answers_the_tap(self) -> None:
        for subject, view in EVERY_PAGE:
            target = page(subject, view)
            with self.subTest(subject=subject, view=view):
                filled = buttons_of(
                    result_keyboard(target, web_base_url=WEB, command=COMMAND)
                )
                tapped = buttons_of(
                    result_keyboard(
                        target, web_base_url=WEB, command=COMMAND, callback=True
                    )
                )

                # The same buttons with the same commands; only a command
                # button turns into a callback, the jump stays a link.
                self.assertEqual(
                    [button["action"]["data"] for button in tapped],
                    [button["action"]["data"] for button in filled],
                )
                self.assertEqual(
                    [button["action"]["type"] for button in tapped],
                    [
                        1 if button["action"]["type"] == 2 else 0
                        for button in filled
                    ],
                )

    def test_no_keyboard_breaks_the_five_by_five_limit(self) -> None:
        for subject, view in EVERY_PAGE:
            with self.subTest(subject=subject, view=view):
                keyboard = result_keyboard(
                    page(subject, view), web_base_url=WEB, command=COMMAND
                )
                rows = keyboard["content"]["rows"]

                self.assertLessEqual(len(rows), 5)
                for row in rows:
                    self.assertLessEqual(len(row["buttons"]), 5)
                # A button's id is unique within its keyboard.
                ids = [button["id"] for button in buttons_of(keyboard)]
                self.assertEqual(len(ids), len(set(ids)))


def keyboard_rows(target: PageTarget, **options) -> list[list[tuple[str, int, str]]]:
    """``(label, action type, data)`` of each button, row by row."""

    keyboard = result_keyboard(target, web_base_url=WEB, command=COMMAND, **options)
    return [
        [
            (
                button["render_data"]["label"],
                button["action"]["type"],
                button["action"]["data"],
            )
            for button in row["buttons"]
        ]
        for row in keyboard["content"]["rows"]
    ]


def ranking(**options) -> PageTarget:
    """A ranking page of the board ``SLUG``; 25 rows, three pages, by default."""

    options.setdefault("record_count", 25)
    return PageTarget(PageSubject.BOARD, SLUG, **options)


def views_row(*options: str) -> list[tuple[str, int, str]]:
    """The ranking's first row, ``options`` after each command."""

    tail = "".join(f" {option}" for option in options)
    return [
        ("阵容", 2, f"{COMMAND} 阵容 {SLUG}{tail}"),
        ("角色统计", 2, f"{COMMAND} 角色统计 {SLUG}{tail}"),
        ("第一名战报", 2, f"{COMMAND} 战报 {SLUG}{tail}"),
        ("对比第一名", 2, f"{COMMAND} 对比 {SLUG} 我{tail}"),
    ]


def page_button(label: str, page_word: str, *options: str) -> tuple[str, int, str]:
    tail = "".join(f" {option}" for option in options)
    return (label, 2, f"{COMMAND} 榜单 {SLUG} --页 {page_word}{tail}")


OPEN_ROW = [("在 ZMDLogs 打开", 0, f"{WEB}/boss/{SLUG}")]


class RankingKeyboardTests(unittest.TestCase):
    """A board's ranking: its views, its pages, and the site, in three rows."""

    def test_the_first_page_offers_every_view_all_rows_and_the_next(self) -> None:
        self.assertEqual(
            keyboard_rows(ranking()),
            [
                views_row(),
                [page_button("全部", "全部"), page_button("下一页", "2")],
                OPEN_ROW,
            ],
        )

    def test_a_middle_page_turns_to_the_one_after_it(self) -> None:
        self.assertEqual(
            keyboard_rows(ranking(ranking_page=2))[1],
            [page_button("全部", "全部"), page_button("下一页", "3")],
        )

    def test_the_last_page_has_no_next(self) -> None:
        for page, count in ((3, 25), (2, 20), (2, 11)):
            with self.subTest(page=page, count=count):
                self.assertEqual(
                    keyboard_rows(ranking(ranking_page=page, record_count=count)),
                    [views_row(), [page_button("全部", "全部")], OPEN_ROW],
                )

    def test_a_board_of_one_page_offers_no_page(self) -> None:
        # 全部 would draw the very rows the page shows.
        for page, count in ((None, 7), (1, 10), (None, 0)):
            with self.subTest(page=page, count=count):
                self.assertEqual(
                    keyboard_rows(ranking(ranking_page=page, record_count=count)),
                    [views_row(), OPEN_ROW],
                )

    def test_the_all_picture_offers_no_page(self) -> None:
        for count in (5, 30):
            with self.subTest(count=count):
                self.assertEqual(
                    keyboard_rows(ranking(ranking_page=ALL_PAGES, record_count=count)),
                    [views_row(), OPEN_ROW],
                )

    def test_past_thirty_the_all_picture_sends_the_rest_to_the_site(self) -> None:
        # Its second row is the board on ZMDLogs, named for the rows the
        # picture leaves there; the same link a row below would repeat it.
        self.assertEqual(
            keyboard_rows(ranking(ranking_page=ALL_PAGES, record_count=42)),
            [views_row(), [("官网查看其余 12 条", 0, f"{WEB}/boss/{SLUG}")]],
        )

    def test_paging_keeps_the_filters_but_first_place_is_the_whole_boards(
        self,
    ) -> None:
        target = ranking(
            ranking_page=2, character_filter="黎风 洛茜", element_filter="物理"
        )
        filters = ("--角色 黎风 洛茜", "--属性 物理")

        self.assertEqual(
            keyboard_rows(target),
            [
                views_row(),
                [
                    page_button("全部", "全部", *filters),
                    page_button("下一页", "3", *filters),
                ],
                OPEN_ROW,
            ],
        )

    def test_an_rdps_page_keeps_rdps_on_every_button(self) -> None:
        target = ranking(metric="rdps", character_filter="洛茜")

        self.assertEqual(
            keyboard_rows(target),
            [
                views_row("--口径 rdps"),
                [
                    page_button("全部", "全部", "--角色 洛茜", "--口径 rdps"),
                    page_button("下一页", "2", "--角色 洛茜", "--口径 rdps"),
                ],
                [("在 ZMDLogs 打开", 0, f"{WEB}/boss/{SLUG}?metric=rdps")],
            ],
        )
        rest = keyboard_rows(
            ranking(metric="rdps", ranking_page=ALL_PAGES, record_count=31)
        )
        self.assertEqual(
            rest[1], [("官网查看其余 1 条", 0, f"{WEB}/boss/{SLUG}?metric=rdps")]
        )

    def test_every_command_parses_back_to_the_page_it_names(self) -> None:
        # Typed back in — or tapped, which reads it the same way — each
        # command draws what its label says, of this board.
        for metric in ("dps", "rdps"):
            for page in (None, 2):
                for character, element in ((None, None), ("黎风 洛茜", "物理")):
                    target = ranking(
                        metric=metric,
                        ranking_page=page,
                        character_filter=character,
                        element_filter=element,
                    )
                    rows = keyboard_rows(target)
                    commands = {
                        label: data
                        for row in rows
                        for label, kind, data in row
                        if kind == 2
                    }
                    for label, data in commands.items():
                        with self.subTest(command=data):
                            request = read_button_command(data)
                            route = parse_zmdlog_payload(request.payload)

                            self.assertEqual(request.prefix, "/")
                            self.assertEqual(route.query, SLUG)
                            self.assertEqual(route.metric, metric)
                            expected = RANKING_BUTTON_ROUTES[label]
                            self.assertIs(route.kind, expected["kind"])
                            for field, value in expected.items():
                                if field == "kind":
                                    continue
                                if value is FROM_PAGE:
                                    value = (page or 1) + 1
                                self.assertEqual(getattr(route, field), value)
                            paged = label in ("全部", "下一页")
                            self.assertEqual(
                                route.character_filter, character if paged else None
                            )
                            self.assertEqual(
                                route.element_filter, element if paged else None
                            )

    def test_with_callbacks_every_command_answers_the_tap(self) -> None:
        for target in (
            ranking(),
            ranking(metric="rdps", ranking_page=ALL_PAGES, record_count=42),
        ):
            with self.subTest(target=target):
                filled = keyboard_rows(target)
                tapped = keyboard_rows(target, callback=True)

                self.assertEqual(
                    tapped,
                    [
                        [
                            (label, 1 if kind == 2 else kind, data)
                            for label, kind, data in row
                        ]
                        for row in filled
                    ],
                )

    def test_no_ranking_keyboard_breaks_the_platform_limits(self) -> None:
        for target in (
            ranking(),
            ranking(ranking_page=ALL_PAGES, record_count=42),
            ranking(metric="rdps", character_filter="黎风 洛茜 卡缪 佩丽卡"),
        ):
            with self.subTest(target=target):
                keyboard = result_keyboard(target, web_base_url=WEB, command=COMMAND)
                rows = keyboard["content"]["rows"]
                self.assertLessEqual(len(rows), 5)
                for row in rows:
                    self.assertLessEqual(len(row["buttons"]), 5)
                ids = [button["id"] for button in buttons_of(keyboard)]
                self.assertEqual(len(ids), len(set(ids)))
                for button in buttons_of(keyboard):
                    self.assertLessEqual(
                        len(button["action"]["data"]), MAX_BUTTON_DATA_CHARS
                    )


# The page the next one is: the drawn page's number plus one.
FROM_PAGE = object()
RANKING_BUTTON_ROUTES = {
    "阵容": {"kind": RouteKind.ROSTER_QUERY},
    "角色统计": {"kind": RouteKind.CHARACTER_STATS},
    "第一名战报": {"kind": RouteKind.BATTLE_QUERY, "battle_rank": 1},
    "对比第一名": {
        "kind": RouteKind.COMPARE_QUERY,
        "compare_self": True,
        "compare_rank": 1,
    },
    "全部": {"kind": RouteKind.RANKING_QUERY, "ranking_page": ALL_PAGES},
    "下一页": {"kind": RouteKind.RANKING_QUERY, "ranking_page": FROM_PAGE},
}


# 战争回响's boards as the live board list orders them (2026-10-01).
ECHO = (
    ("indie_battletower001_ex", "白刃穿水·残酷"),
    ("indie_battletower002_ex", "野性旧事·残酷"),
    ("indie_battletower003_ex", "弓弩表象·残酷"),
    ("indie_battletower004_ex", "斧柄纪年·残酷"),
    ("indie_battletower005_ex", "铳弹砺石·残酷"),
    ("indie_battletower006_ex", "裂地旧创·残酷"),
    ("indie_battletower007_ex", "死兽鸣吼·残酷"),
    ("indie_battletower008_ex", "战争简史·残酷"),
    ("indie_battletower009_ex", "掩埋阵线·残酷"),
    ("indie_battletower010_ex", "方阵庇护·残酷"),
    ("indie_battletower011_ex", "重伤之围·残酷"),
    ("indie_battletower012_ex", "无机狂热·残酷"),
    ("indie_battletower013_ex", "斩首蓄势·残酷"),
    ("indie_battletower014_ex", "野兽诡计·残酷"),
)


def dungeon_page(boards, **options) -> PageTarget:
    return PageTarget(PageSubject.DUNGEON, "战争回响", boards=boards, **options)


def board_buttons(target: PageTarget, **options) -> list[dict]:
    return buttons_of(
        result_keyboard(target, web_base_url=WEB, command=COMMAND, **options)
    )


class DungeonBoardButtonTests(unittest.TestCase):
    """A dungeon's podiums: a command button per board, and nothing else."""

    def test_each_board_is_one_command_in_board_list_order(self) -> None:
        buttons = board_buttons(dungeon_page(ECHO))

        self.assertEqual(
            [button["action"]["data"] for button in buttons],
            [f"{COMMAND} {slug}" for slug, _ in ECHO],
        )
        # Fill-in commands only: the site has no page of a dungeon to open.
        self.assertEqual({button["action"]["type"] for button in buttons}, {2})

    def test_past_twenty_five_boards_the_rest_have_no_button(self) -> None:
        # A phase spans several dungeons; 影拓丰碑 alone is 25 boards today.
        boards = tuple((f"indie_hard{n:03d}_s", f"榜{n}·苦难") for n in range(1, 28))

        keyboard = result_keyboard(
            dungeon_page(boards), web_base_url=WEB, command=COMMAND
        )

        rows = keyboard["content"]["rows"]
        self.assertEqual([len(row["buttons"]) for row in rows], [5] * 5)
        self.assertEqual(
            [button["action"]["data"] for button in buttons_of(keyboard)],
            [f"{COMMAND} indie_hard{n:03d}_s" for n in range(1, 26)],
        )
        ids = [button["id"] for button in buttons_of(keyboard)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_boards_sharing_a_row_drop_what_every_name_shares(self) -> None:
        # Six boards or more share their rows, and a shared row shows only
        # the start of a label: 危境再现's boards differ after the dungeon's
        # name, 战争回响's before the difficulty they all share.
        rescue = tuple(
            (f"rescue_{n}", f"危境再现·{name}")
            for n, name in enumerate(
                ("罗丹", "三位一体", "白垩界卫", "阮一", "聂菲斯", "阿莱克琉斯")
            )
        )
        scar = (
            ("indie_hard008_s", "怨憎雾海·苦难"),
            ("indie_hard009_s", "血肉熔点·苦难"),
            ("indie_hard007_s", "呼吼炽焰·苦难"),
        )
        shards = (
            ("dung02_group_minibossrush01", "巨山犼兽"),
            ("dung02_group_minibossrush02", "蚀影噪雷"),
            ("dung02_group_minibossrush03", "幽林之怒"),
        )
        for boards, labels in (
            (ECHO[:7], ["白刃穿水", "野性旧事", "弓弩表象", "斧柄纪年",
                        "铳弹砺石", "裂地旧创", "死兽鸣吼"]),
            (rescue, ["罗丹", "三位一体", "白垩界卫", "阮一", "聂菲斯", "阿莱克琉斯"]),
            # One to a row, a label is the board's name as its card prints it.
            (scar, ["怨憎雾海·苦难", "血肉熔点·苦难", "呼吼炽焰·苦难"]),
            (shards, ["巨山犼兽", "蚀影噪雷", "幽林之怒"]),
        ):
            with self.subTest(board=boards[0][1]):
                buttons = board_buttons(dungeon_page(boards))

                self.assertEqual(
                    [button["render_data"]["label"] for button in buttons], labels
                )

    def test_every_command_parses_back_to_its_board_under_the_metric_asked(
        self,
    ) -> None:
        # The podiums show no metric, but `战争回响 --口径 rdps` asked for
        # one: the board it leads to is the rDPS board.
        for metric in ("dps", "rdps"):
            for (slug, _), button in zip(
                ECHO, board_buttons(dungeon_page(ECHO, metric=metric))
            ):
                with self.subTest(metric=metric, slug=slug):
                    command = button["action"]["data"]
                    route = parse_zmdlog_payload(command.removeprefix(f"{COMMAND} "))

                    self.assertEqual(route.query, slug)
                    self.assertEqual(route.metric, metric)
                    if metric == "dps":
                        self.assertEqual(command, f"{COMMAND} {slug}")

    def test_with_callbacks_every_board_answers_the_tap(self) -> None:
        filled = board_buttons(dungeon_page(ECHO))
        tapped = board_buttons(dungeon_page(ECHO), callback=True)

        self.assertEqual(
            [button["action"]["data"] for button in tapped],
            [button["action"]["data"] for button in filled],
        )
        self.assertEqual({button["action"]["type"] for button in tapped}, {1})

    def test_a_dungeon_without_boards_has_no_keyboard(self) -> None:
        self.assertIsNone(
            result_keyboard(dungeon_page(()), web_base_url=WEB, command=COMMAND)
        )


OTHER = "btl_upload_bbbbbbbbbbbb"


def comparison(*battles) -> PageTarget:
    return PageTarget(
        PageSubject.COMPARISON,
        BATTLE,
        V.COMPARE,
        battles=battles or ((BATTLE, "shiki"), (OTHER, "KevNe")),
    )


class ComparisonButtonTests(unittest.TestCase):
    """对比: a button a side to that battle's 摘要, and nothing else."""

    def test_each_side_opens_its_battle_by_the_uploaders_name(self) -> None:
        keyboard = result_keyboard(comparison(), web_base_url=WEB, command=COMMAND)

        (row,) = keyboard["content"]["rows"]
        self.assertEqual(
            [
                (button["render_data"]["label"], button["action"]["data"])
                for button in row["buttons"]
            ],
            [
                ("shiki 的战报", f"{COMMAND} 战报 {BATTLE}"),
                ("KevNe 的战报", f"{COMMAND} 战报 {OTHER}"),
            ],
        )
        # No 在 ZMDLogs 打开: the comparison is neither battle's site page.
        self.assertEqual({button["action"]["type"] for button in row["buttons"]}, {2})
        self.assertEqual(jump_links(comparison()), [])
        self.assertNotIn(WEB, repr(keyboard))

    def test_every_command_draws_that_battles_summary(self) -> None:
        for button in buttons_of(
            result_keyboard(comparison(), web_base_url=WEB, command=COMMAND)
        ):
            with self.subTest(label=button["render_data"]["label"]):
                route = parse_zmdlog_payload(
                    button["action"]["data"].removeprefix(f"{COMMAND} ")
                )

                self.assertEqual(route.kind, RouteKind.BATTLE_QUERY)
                self.assertIn(route.query, (BATTLE, OTHER))

    def test_one_uploader_twice_keeps_the_pages_names(self) -> None:
        target = comparison(
            (BATTLE, "测试账号（第 1 名）"), (OTHER, "测试账号（第 2 名）")
        )

        self.assertEqual(
            [button["render_data"]["label"] for button in board_buttons(target)],
            ["测试账号（第 1 名） 的战报", "测试账号（第 2 名） 的战报"],
        )

    def test_a_long_name_is_cut_before_its_suffix(self) -> None:
        long_name = "一个非常非常非常非常长的上传者名字"
        (left, _) = board_buttons(comparison((BATTLE, long_name), (OTHER, "KevNe")))

        self.assertEqual(
            left["render_data"]["label"], "一个非常非常非常非常长的上传… 的战报"
        )

    def test_with_callbacks_each_side_answers_the_tap(self) -> None:
        filled = board_buttons(comparison())
        tapped = board_buttons(comparison(), callback=True)

        self.assertEqual(
            [button["action"]["data"] for button in tapped],
            [button["action"]["data"] for button in filled],
        )
        self.assertEqual({button["action"]["type"] for button in tapped}, {1})

    def test_a_comparison_without_battles_has_no_keyboard(self) -> None:
        self.assertIsNone(
            result_keyboard(
                PageTarget(PageSubject.COMPARISON, BATTLE, V.COMPARE),
                web_base_url=WEB,
                command=COMMAND,
            )
        )


class FakeTrend:
    """The rank trend as the account page asks it: whose trace is on record."""

    def __init__(self, *traced: str) -> None:
        self.traced = set(traced)

    def history_for(self, account_id):
        if account_id not in self.traced:
            return None
        return SimpleNamespace(account_id=account_id, boards=("one board",))


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
            trend=FakeTrend(ACCOUNT),
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

    def test_an_account_without_a_trace_has_no_trend_to_offer(self) -> None:
        self.queries._trend = FakeTrend()

        outcome = self._command(RouteKind.ACCOUNT_QUERY, query=ACCOUNT)

        self.assertEqual(outcome.target.unavailable, frozenset({V.TREND}))

    def test_a_battle_names_the_pages_its_upload_cannot_draw(self) -> None:
        async def old_upload(battle_id):
            raise ZmdLogsAPIError(422, "battle_export_unsupported", "old")

        payload = battle_detail_payload()
        payload["roleSkillStats"] = []
        self.data.battles[BATTLE] = parse_battle_detail(payload)
        self.data.get_battle_export = old_upload
        for kind, unavailable in (
            # The card read the detail and heard the export refused.
            (RouteKind.BATTLE_QUERY, {V.DATA, V.CAST}),
            # 养成 read the detail only; casts are unknown.
            (RouteKind.BUILD_QUERY, {V.DATA}),
        ):
            with self.subTest(kind=kind):
                outcome = self._command(kind, query=BATTLE)

                self.assertEqual(outcome.target.unavailable, frozenset(unavailable))

    def test_a_battle_without_a_roster_has_no_build(self) -> None:
        payload = battle_detail_payload()
        payload["battle"]["roster"] = []
        self.data.battles[BATTLE] = parse_battle_detail(payload)

        outcome = self._command(RouteKind.BATTLE_QUERY, query=BATTLE)

        self.assertIn(V.BUILD, outcome.target.unavailable)
        # The 摘要 then has no 养成 button, the strip no 养成 entry.
        self.assertNotIn(
            "养成", [label for label, _ in sibling_buttons(outcome.target)]
        )
        self.assertEqual(
            self.renderer.kwargs["battle"]["views"],
            (("摘要", True), ("数据", False), ("排轴", False)),
        )

    def test_a_battle_with_every_page_hides_nothing(self) -> None:
        outcome = self._command(RouteKind.BATTLE_QUERY, query=BATTLE)

        self.assertEqual(outcome.target.unavailable, frozenset())

    def test_battle_pages_carry_their_battle(self) -> None:
        async def export(battle_id):
            return parse_battle_export(battle_export_payload())

        self.data.get_battle_export = export
        for kind, view in (
            (RouteKind.BATTLE_QUERY, CandidateView.BATTLE),
            (RouteKind.BUILD_QUERY, CandidateView.BUILD),
            (RouteKind.DATA_QUERY, CandidateView.DATA),
            (RouteKind.CAST_QUERY, CandidateView.CAST),
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
            RouteKind.BUILD_QUERY, query=BOARD_KEYWORD, battle_rank=2
        )

        self.assertEqual(
            outcome.target,
            PageTarget(PageSubject.BATTLE, second, CandidateView.BUILD),
        )

    def test_board_pages_carry_their_board_and_the_options_drawn_with(self) -> None:
        for kind, options, target in (
            (
                RouteKind.RANKING_QUERY,
                {},
                PageTarget(PageSubject.BOARD, BOARD_SLUG, record_count=5),
            ),
            (
                RouteKind.RANKING_QUERY,
                {"metric": "rdps"},
                PageTarget(
                    PageSubject.BOARD, BOARD_SLUG, metric="rdps", record_count=2
                ),
            ),
            (
                # What the keyboard pages on: the filters as typed, and how
                # many rows they keep.
                RouteKind.RANKING_QUERY,
                {
                    "ranking_page": ALL_PAGES,
                    "character_filter": "黎风",
                    "element_filter": "自然",
                },
                PageTarget(
                    PageSubject.BOARD,
                    BOARD_SLUG,
                    ranking_page=ALL_PAGES,
                    character_filter="黎风",
                    element_filter="自然",
                    record_count=3,
                ),
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
        # ZMDLogs counterpart.
        for kind, query, options in (
            (RouteKind.HELP, "", {}),
            (RouteKind.CHARACTER_STATS, "", {}),
            (RouteKind.CHARACTER_STATS, "提弗洛斯", {}),
            (RouteKind.CHARACTER_STANDINGS, "", {}),
            (RouteKind.PLAYER_CHAMPIONS, "", {}),
            (RouteKind.RECORDS_QUERY, "", {}),
        ):
            with self.subTest(kind=kind, query=query):
                outcome = self._command(kind, query=query, **options)

                self.assertIsNotNone(outcome.image_path)
                self.assertEqual(outcome.image_scale, 2)
                self.assertIsNone(outcome.target)

    def test_a_comparison_carries_both_battles_by_the_pages_names(self) -> None:
        # Two uploads by one person, named outright and fought the same
        # minute: told apart by their places, as on the page.
        outcome = self._command(
            RouteKind.COMPARE_QUERY, query=BATTLE, compare_target=OTHER
        )

        self.assertEqual(outcome.image_scale, 2)
        self.assertEqual(
            outcome.target,
            PageTarget(
                PageSubject.COMPARISON,
                BATTLE,
                V.COMPARE,
                battles=((BATTLE, "测试账号（左）"), (OTHER, "测试账号（右）")),
            ),
        )
        # Picked by rank, each side is its uploader.
        for rank, uploader in ((1, "shiki"), (2, "KevNe")):
            payload = battle_detail_payload()
            payload["battle"]["id"] = f"btl_upload_00000000000{rank}"
            payload["battle"]["uploaderNickname"] = uploader
            self.data.battles[payload["battle"]["id"]] = parse_battle_detail(payload)

        outcome = self._command(
            RouteKind.COMPARE_QUERY, query=BOARD_KEYWORD, battle_rank=1, compare_rank=2
        )

        self.assertEqual(
            outcome.target.battles,
            (
                ("btl_upload_000000000001", "shiki"),
                ("btl_upload_000000000002", "KevNe"),
            ),
        )

    def test_a_dungeon_carries_its_boards_and_the_metric_asked(self) -> None:
        # No page of its own on the site; what it offers is its boards.
        first = hot_bosses_payload()[0]
        second = dict(
            first, bossSlug="dung01_group_bossrush03", bossName="危境再现·白垩界卫"
        )
        self.data.cards = parse_hot_bosses([first, second])
        boards = (
            ("dung01_group_bossrush02", "危境再现·三位一体"),
            ("dung01_group_bossrush03", "危境再现·白垩界卫"),
        )
        for options, metric in (({}, "dps"), ({"metric": "rdps"}, "rdps")):
            with self.subTest(metric=metric):
                self.renderer.calls.clear()

                outcome = self._command(
                    RouteKind.RANKING_QUERY, query="测试区", **options
                )

                self.assertEqual(self.renderer.calls, ["dungeon_top3"])
                self.assertEqual(outcome.image_scale, 2)
                self.assertEqual(
                    outcome.target,
                    PageTarget(
                        PageSubject.DUNGEON,
                        "危境再现 · 测试区",
                        metric=metric,
                        boards=boards,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
