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

from core.buttons import result_image_message, result_keyboard
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
from core.routing import RouteKind, RouteRequest, parse_zmdlog_payload
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
    (PageSubject.BATTLE, V.LOADOUT),
    (PageSubject.BATTLE, V.SKILLS),
    (PageSubject.BATTLE, V.TIMELINE),
    (PageSubject.BOARD, V.RANKING),
    (PageSubject.BOARD, V.ROSTER),
    (PageSubject.BOARD, V.CHARACTER_STATS),
    (PageSubject.BOARD, V.GROUP_BOARD),
)
_KEYS = {PageSubject.ACCOUNT: ACCOUNT, PageSubject.BATTLE: BATTLE}


def page(subject: PageSubject, view: CandidateView, **options) -> PageTarget:
    return PageTarget(subject, _KEYS.get(subject, SLUG), view, **options)


# What a label promises, and the route that draws it for each subject.
LABELS = {
    "战报": V.BATTLE,
    "配装": V.LOADOUT,
    "技能": V.SKILLS,
    "技能轴": V.TIMELINE,
    "榜单": V.RANKING,
    "阵容": V.ROSTER,
    "角色统计": V.CHARACTER_STATS,
    "第 1 名战报": V.BATTLE,
    "账号": V.RANKING,
    "名次趋势": V.TREND,
}
ROUTE_OF = {
    (PageSubject.ACCOUNT, V.RANKING): RouteKind.ACCOUNT_QUERY,
    (PageSubject.ACCOUNT, V.TREND): RouteKind.TREND_QUERY,
    (PageSubject.BATTLE, V.BATTLE): RouteKind.BATTLE_QUERY,
    (PageSubject.BATTLE, V.LOADOUT): RouteKind.LOADOUT_QUERY,
    (PageSubject.BATTLE, V.SKILLS): RouteKind.SKILL_QUERY,
    (PageSubject.BATTLE, V.TIMELINE): RouteKind.TIMELINE_QUERY,
    (PageSubject.BOARD, V.RANKING): RouteKind.RANKING_QUERY,
    (PageSubject.BOARD, V.ROSTER): RouteKind.ROSTER_QUERY,
    (PageSubject.BOARD, V.CHARACTER_STATS): RouteKind.CHARACTER_STATS,
    (PageSubject.BOARD, V.BATTLE): RouteKind.BATTLE_QUERY,
}


class SiblingButtonTests(unittest.TestCase):
    """The page's other views, one command button each, under its jump button."""

    def test_each_page_offers_its_other_views(self) -> None:
        c = COMMAND
        for target, expected in (
            (
                page(PageSubject.BATTLE, V.BATTLE),
                [
                    ("配装", f"{c} 配装 {BATTLE}"),
                    ("技能", f"{c} 技能 {BATTLE}"),
                    ("技能轴", f"{c} 技能轴 {BATTLE}"),
                ],
            ),
            (
                page(PageSubject.BATTLE, V.TIMELINE),
                [
                    ("战报", f"{c} 战报 {BATTLE}"),
                    ("配装", f"{c} 配装 {BATTLE}"),
                    ("技能", f"{c} 技能 {BATTLE}"),
                ],
            ),
            (
                page(PageSubject.BOARD, V.RANKING),
                [
                    ("阵容", f"{c} 阵容 {SLUG}"),
                    ("角色统计", f"{c} 角色统计 {SLUG}"),
                    ("第 1 名战报", f"{c} 战报 {SLUG}"),
                ],
            ),
            (
                page(PageSubject.BOARD, V.CHARACTER_STATS),
                [("榜单", f"{c} 榜单 {SLUG}"), ("阵容", f"{c} 阵容 {SLUG}")],
            ),
            (
                page(PageSubject.BOARD, V.GROUP_BOARD),
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
        commands = [
            command
            for _, command in sibling_buttons(
                page(PageSubject.BOARD, V.RANKING, metric="rdps")
            )
        ]

        # No 第 1 名战报: 战报 takes no --口径, so it would open the DPS
        # board's first place under an rDPS page — the two never mix.
        self.assertEqual(
            commands,
            [
                f"{COMMAND} 阵容 {SLUG} --口径 rdps",
                f"{COMMAND} 角色统计 {SLUG} --口径 rdps",
            ],
        )

    def test_a_board_page_keeps_its_length_where_the_view_takes_one(self) -> None:
        # --top means the same on 榜单, 阵容 and 群榜; 角色统计 and 战报
        # refuse it.
        for view, expected in (
            (
                V.RANKING,
                [
                    f"{COMMAND} 阵容 {SLUG} --top 30",
                    f"{COMMAND} 角色统计 {SLUG}",
                    f"{COMMAND} 战报 {SLUG}",
                ],
            ),
            (
                V.GROUP_BOARD,
                [f"{COMMAND} 榜单 {SLUG} --top 30", f"{COMMAND} 阵容 {SLUG} --top 30"],
            ),
        ):
            with self.subTest(view=view):
                target = page(PageSubject.BOARD, view, ranking_top=30)

                self.assertEqual(
                    [command for _, command in sibling_buttons(target)], expected
                )

    def test_every_command_parses_back_to_the_page_its_label_names(self) -> None:
        # The load-bearing check: typed back in, each command must draw the
        # page its label promises, of this target, under the same metric.
        for subject, view in EVERY_PAGE:
            board = subject is PageSubject.BOARD
            for metric in ("dps", "rdps") if board else ("dps",):
                for top in (None, 30) if board else (None,):
                    target = page(subject, view, metric=metric, ranking_top=top)
                    for label, command in sibling_buttons(target):
                        with self.subTest(command=command):
                            payload = command.removeprefix(f"{COMMAND} ")
                            route = parse_zmdlog_payload(payload)
                            drawn = LABELS[label]

                            self.assertIs(route.kind, ROUTE_OF[subject, drawn])
                            self.assertEqual(route.query, target.key)
                            self.assertEqual(route.metric, metric)
                            self.assertEqual(route.battle_rank, 1)
                            if drawn in (V.RANKING, V.ROSTER) and board:
                                self.assertEqual(route.ranking_top, top)

    def test_every_page_with_a_target_has_a_list(self) -> None:
        for subject, view in EVERY_PAGE:
            with self.subTest(subject=subject, view=view):
                self.assertTrue(sibling_buttons(page(subject, view)))

    def test_a_view_the_target_lacks_gets_no_button(self) -> None:
        # An older upload has no loadout, skill statistics or casts; an
        # account the rank watch never polled has no trend.
        battle = page(
            PageSubject.BATTLE,
            V.BATTLE,
            unavailable=frozenset({V.LOADOUT, V.TIMELINE}),
        )
        account = page(
            PageSubject.ACCOUNT, V.RANKING, unavailable=frozenset({V.TREND})
        )

        self.assertEqual(
            sibling_buttons(battle), [("技能", f"{COMMAND} 技能 {BATTLE}")]
        )
        self.assertEqual(sibling_buttons(account), [])
        # The jump button stays: the page itself is still on ZMDLogs.
        self.assertEqual(jump_links(account), [f"{WEB}/records/{ACCOUNT}"])

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


class FakeWatcher:
    """The rank watch as the account page asks it: whose trend is on record."""

    def __init__(self, *watched: str) -> None:
        self.watched = set(watched)

    def history_for(self, account_id):
        if account_id not in self.watched:
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
            watcher=FakeWatcher(ACCOUNT),
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

    def test_an_account_nobody_watched_has_no_trend_to_offer(self) -> None:
        self.queries._watcher = FakeWatcher()

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
            (RouteKind.BATTLE_QUERY, {V.SKILLS, V.TIMELINE}),
            # The loadout page read the detail only; casts are unknown.
            (RouteKind.LOADOUT_QUERY, {V.SKILLS}),
        ):
            with self.subTest(kind=kind):
                outcome = self._command(kind, query=BATTLE)

                self.assertEqual(outcome.target.unavailable, frozenset(unavailable))

    def test_a_battle_with_every_page_hides_nothing(self) -> None:
        outcome = self._command(RouteKind.BATTLE_QUERY, query=BATTLE)

        self.assertEqual(outcome.target.unavailable, frozenset())

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
