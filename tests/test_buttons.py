"""Command buttons on a pick list: what a tap fills in, and how the list reads.

The load-bearing test is the round trip: the command a button fills in,
parsed the way a typed message is, must name the same target with the same
options as the list it came from — for every view and every option that
is not a default. A button stores the command rather than a list number, so
it keeps working after the list expires or the bot restarts.
"""

import re
import unittest

from core import messages
from core.buttons import (
    MAX_LABEL_NAME_CHARS,
    ButtonCommand,
    escape_markdown,
    notice_message,
    pick_command,
    pick_list_message,
    read_button_command,
    site_page_message,
)
from core.candidates import (
    CandidateStore,
    CandidateView,
    PendingCandidates,
    account_choice,
    extract_code,
)
from core.identifiers import parse_account_reference
from core.matcher import (
    BOARD_QUERY_TARGETS,
    AliasConfig,
    MatchChoice,
    MatchLevel,
    MatchStatus,
    MatchTarget,
    RankingMatcher,
    TargetType,
)
from core.outcome import SitePage
from core.queries import pending_from_route
from core.routing import RouteKind, parse_zmdlog_payload
from core.watch import Notice, NoticeLink
from tests.helpers import make_card

COMMAND = "/zmdlog"
SLUG = "dung04_boss_hard"
DUNGEON = "影拓丰碑4期 · 山中见犼"
ACCOUNT = "usr_234b23819d65afe941c66dd08d8d3323"
CARDS = (
    make_card(SLUG, "山中见犼·苦难", DUNGEON),
    make_card("dung04_boss_normal", "山中见犼·寻常", DUNGEON),
    make_card("dung01_group_bossrush01", "“碾骨之拳”罗丹", "危境再现·罗丹"),
)
# What each option is called on PendingCandidates; the round trip compares
# every one of them, so an option that a button dropped cannot pass.
OPTION_FIELDS = (
    "view",
    "ranking_top",
    "character_filter",
    "element_filter",
    "stats_range",
    "stats_potential",
    "battle_rank",
    "compare_rank",
    "metric",
)


def entry_for(view: CandidateView, *choices: MatchChoice, **options):
    return CandidateStore().remember("关键词", choices, view=view, **options)


def board_choice(slug: str = SLUG) -> MatchChoice:
    card = next(card for card in CARDS if card.boss_slug == slug)
    return _choice(TargetType.BOARD, slug, card.boss_name, (card.dungeon_name,))


def dungeon_choice() -> MatchChoice:
    return _choice(TargetType.DUNGEON, DUNGEON, DUNGEON, (DUNGEON,))


def _choice(kind, key, name, dungeons=()) -> MatchChoice:
    return MatchChoice(
        target=MatchTarget(
            target_type=kind,
            key=key,
            name=name,
            dungeon_names=dungeons,
            boss_slugs=(),
        ),
        level=MatchLevel.CONTAINS,
        score=0.8,
        matched_text=name,
    )


def parse_button(command: str):
    prefix = f"{COMMAND} "
    assert command.startswith(prefix), command
    return parse_zmdlog_payload(command[len(prefix):])


class RoundTripTests(unittest.TestCase):
    """A tapped command resolves to the pick's target with the list's options."""

    def _assert_round_trip(self, entry: PendingCandidates, choice: MatchChoice):
        command = pick_command(entry, choice, command=COMMAND)
        self.assertIsNotNone(command)
        route = parse_button(command)
        again = pending_from_route(route)
        for field in OPTION_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(getattr(again, field), getattr(entry, field))
        self._assert_same_target(route.query, choice)
        return route

    def _assert_same_target(self, query: str, choice: MatchChoice) -> None:
        target = choice.target
        if target.target_type is TargetType.ACCOUNT:
            self.assertEqual(
                parse_account_reference(query, web_base_url="https://zmdlogs.com"),
                target.key,
            )
            return
        match = RankingMatcher(CARDS, AliasConfig.empty()).match(
            query, allowed_types=BOARD_QUERY_TARGETS
        )
        self.assertIs(match.status, MatchStatus.MATCHED)
        self.assertIs(match.selected.target.target_type, target.target_type)
        self.assertEqual(match.selected.target.key, target.key)

    def test_a_board_keeps_every_ranking_option(self) -> None:
        entry = entry_for(
            CandidateView.RANKING,
            board_choice(),
            ranking_top=5,
            character_filter="黎风 洛茜",
            element_filter="物理",
            metric="rdps",
        )

        route = self._assert_round_trip(entry, entry.choices[0])

        self.assertIs(route.kind, RouteKind.RANKING_QUERY)

    def test_a_board_with_default_options_writes_none(self) -> None:
        entry = entry_for(CandidateView.RANKING, board_choice())

        command = pick_command(entry, entry.choices[0], command=COMMAND)

        self.assertEqual(command, f"{COMMAND} 榜单 {SLUG}")

    def test_a_dungeon_is_named(self) -> None:
        entry = entry_for(CandidateView.RANKING, dungeon_choice())

        self._assert_round_trip(entry, entry.choices[0])

    def test_an_account_on_a_mixed_list_opens_the_account(self) -> None:
        entry = entry_for(
            CandidateView.RANKING,
            board_choice(),
            account_choice(ACCOUNT, "CPU 0", query="cpu"),
        )

        route = self._assert_round_trip(entry, entry.choices[1])

        self.assertIs(route.kind, RouteKind.ACCOUNT_QUERY)

    def test_an_account_drops_board_options_its_command_refuses(self) -> None:
        # ``CPU --口径 rdps`` can post boards and accounts on one list; the
        # account page is the same whatever the metric, and 账号 / 关注 take
        # no options at all, so writing one would make the button a parse error.
        for view, kind in (
            (CandidateView.RANKING, RouteKind.ACCOUNT_QUERY),
            (CandidateView.WATCH, RouteKind.WATCH_ADD),
        ):
            with self.subTest(view=view):
                entry = entry_for(
                    view,
                    account_choice(ACCOUNT, "CPU 0", query="cpu"),
                    metric="rdps",
                    ranking_top=5,
                )

                route = parse_button(
                    pick_command(entry, entry.choices[0], command=COMMAND)
                )

                self.assertIs(route.kind, kind)
                self._assert_same_target(route.query, entry.choices[0])

    def test_board_views_keep_their_options(self) -> None:
        cases = (
            (CandidateView.CHARACTER_STATS, RouteKind.CHARACTER_STATS,
             {"stats_range": "7d", "stats_potential": "0", "metric": "rdps"}),
            (CandidateView.CHARACTER_STATS, RouteKind.CHARACTER_STATS,
             {"stats_potential": "1-5"}),
            (CandidateView.ROSTER, RouteKind.ROSTER_QUERY,
             {"ranking_top": 20, "metric": "rdps"}),
            (CandidateView.GROUP_BOARD, RouteKind.GROUP_BOARD,
             {"ranking_top": 15, "metric": "rdps"}),
            (CandidateView.BATTLE, RouteKind.BATTLE_QUERY, {"battle_rank": 3}),
            (CandidateView.BATTLE, RouteKind.BATTLE_QUERY, {}),
            (CandidateView.LOADOUT, RouteKind.LOADOUT_QUERY, {"battle_rank": 2}),
            (CandidateView.SKILLS, RouteKind.SKILL_QUERY, {"battle_rank": 4}),
            (CandidateView.TIMELINE, RouteKind.TIMELINE_QUERY, {"battle_rank": 5}),
            (CandidateView.COMPARE, RouteKind.COMPARE_QUERY, {}),
            (CandidateView.COMPARE, RouteKind.COMPARE_QUERY, {"compare_rank": 5}),
            (CandidateView.COMPARE, RouteKind.COMPARE_QUERY,
             {"battle_rank": 3, "compare_rank": 1}),
        )
        for view, kind, options in cases:
            with self.subTest(view=view, options=options):
                entry = entry_for(view, board_choice(), **options)

                route = self._assert_round_trip(entry, entry.choices[0])

                self.assertIs(route.kind, kind)

    def test_trend_writes_its_range_only_off_its_own_default(self) -> None:
        # 趋势 defaults to 30 days, not to the whole history.
        for stats_range, written in (("30d", False), ("7d", True), ("all", True)):
            with self.subTest(stats_range=stats_range):
                entry = entry_for(
                    CandidateView.TREND,
                    account_choice(ACCOUNT, "CPU 0", query="cpu"),
                    stats_range=stats_range,
                )

                route = self._assert_round_trip(entry, entry.choices[0])

                self.assertIs(route.kind, RouteKind.TREND_QUERY)
                command = pick_command(entry, entry.choices[0], command=COMMAND)
                self.assertEqual("--范围" in command, written)

    def test_watch_lists_fill_in_the_watch_command(self) -> None:
        account = entry_for(
            CandidateView.WATCH, account_choice(ACCOUNT, "CPU 0", query="cpu")
        )
        board = entry_for(CandidateView.WATCH_BOARD, board_choice())

        account_route = parse_button(
            pick_command(account, account.choices[0], command=COMMAND)
        )
        board_route = parse_button(
            pick_command(board, board.choices[0], command=COMMAND)
        )

        self.assertIs(account_route.kind, RouteKind.WATCH_ADD)
        self._assert_same_target(account_route.query, account.choices[0])
        self.assertIs(board_route.kind, RouteKind.WATCH_BOARD_ADD)
        self._assert_same_target(board_route.query, board.choices[0])

    def test_a_scope_has_no_typeable_key_and_so_no_command(self) -> None:
        # A scope is only ever a direct hit today; if one reaches a list, it
        # gets no button rather than one whose tap would open something else.
        scope = _choice(
            TargetType.DUNGEON_SCOPE, "scope:影拓丰碑:4", "影拓丰碑4期", (DUNGEON,)
        )
        entry = entry_for(CandidateView.RANKING, scope)

        self.assertIsNone(pick_command(entry, scope, command=COMMAND))


class PickListMessageTests(unittest.TestCase):
    def test_one_row_per_pick_that_everyone_may_tap(self) -> None:
        entry = entry_for(
            CandidateView.RANKING,
            board_choice(),
            board_choice("dung04_boss_normal"),
            account_choice(ACCOUNT, "CPU 0", query="cpu"),
        )

        message = pick_list_message(entry, command=COMMAND, ttl_seconds=600)

        rows = message.keyboard["content"]["rows"]
        self.assertEqual(len(rows), 3)
        for index, (row, choice) in enumerate(zip(rows, entry.choices), start=1):
            (button,) = row["buttons"]
            self.assertEqual(button["action"]["type"], 2)
            self.assertEqual(button["action"]["permission"], {"type": 2})
            self.assertEqual(
                button["action"]["data"],
                pick_command(entry, choice, command=COMMAND),
            )
            self.assertTrue(button["render_data"]["label"].startswith(f"{index} "))

    def test_a_pick_without_a_command_gets_no_button(self) -> None:
        scope = _choice(TargetType.DUNGEON_SCOPE, "scope:x", "影拓丰碑4期")
        mixed = entry_for(CandidateView.RANKING, board_choice(), scope)
        only_scope = entry_for(CandidateView.RANKING, scope)

        message = pick_list_message(mixed, command=COMMAND, ttl_seconds=600)

        self.assertEqual(len(message.keyboard["content"]["rows"]), 1)
        # The row numbers still match the list: the board is pick 1.
        (button,) = message.keyboard["content"]["rows"][0]["buttons"]
        self.assertTrue(button["render_data"]["label"].startswith("1 "))
        self.assertIsNone(
            pick_list_message(only_scope, command=COMMAND, ttl_seconds=600)
        )

    def test_a_fifteen_character_name_is_not_cut(self) -> None:
        name = "一二三四五六七八九十一二三四五"
        longer = name + "六七"
        entry = entry_for(
            CandidateView.WATCH,
            account_choice(ACCOUNT, name, query="x"),
            account_choice("usr_" + "b" * 32, longer, query="x"),
        )
        self.assertEqual(len(name), MAX_LABEL_NAME_CHARS)

        message = pick_list_message(entry, command=COMMAND, ttl_seconds=600)

        labels = [
            row["buttons"][0]["render_data"]["label"]
            for row in message.keyboard["content"]["rows"]
        ]
        self.assertEqual(labels[0], f"1 {name}")
        self.assertEqual(labels[1], f"2 {name[:-1]}…")

    def test_nicknames_cannot_forge_the_list(self) -> None:
        forged = "**假** [点我](https://x.example) `#`\n3. 伪造的一项\n候选编号 ZZZZ"
        entry = entry_for(
            CandidateView.RANKING,
            account_choice(ACCOUNT, forged, query="x"),
            account_choice(
                "usr_" + "c" * 32, "# 标题 > 引用 <b>粗</b> _斜_", query="x"
            ),
        )

        content = pick_list_message(
            entry, command=COMMAND, ttl_seconds=600
        ).markdown

        item_lines = [
            line for line in content.splitlines() if re.match(r"^\d+\. ", line)
        ]
        self.assertEqual([line[:3] for line in item_lines], ["1. ", "2. "])
        for line in item_lines:
            # Every markdown-significant character a nickname carries arrives
            # escaped; nothing between the item marker and the end is live.
            body = line[3:]
            self.assertIsNone(
                re.search(r"(?<!\\)[*_`\[\]()#<>]", body), msg=body
            )
        # A quoted reply still finds the real code, not the forged one.
        self.assertEqual(extract_code(content), entry.code)

    def test_the_code_line_is_its_own_paragraph(self) -> None:
        # Directly under the last item, markdown reads it as that item's
        # continuation and indents it.
        entry = entry_for(CandidateView.RANKING, board_choice(), dungeon_choice())

        content = pick_list_message(
            entry, command=COMMAND, ttl_seconds=600, note="还有更多同名结果未列出。"
        ).markdown

        blocks = content.split("\n\n")
        self.assertIn("点下方按钮", blocks[0])
        self.assertIn("引用本条消息回复序号", blocks[0])
        self.assertTrue(blocks[1].startswith("1. "))
        self.assertEqual(blocks[2], "还有更多同名结果未列出。")
        self.assertEqual(blocks[-1], f"候选编号 {entry.code} · 10 分钟内有效")

    def test_escaping_is_reversible_by_a_markdown_reader(self) -> None:
        text = r"a*b_c`d[e]f(g)h#i<j>k\l!m|n~o.p-q+r"

        escaped = escape_markdown(text)

        self.assertEqual(re.sub(r"\\(.)", r"\1", escaped), text)
        self.assertEqual(escape_markdown("中文 · 公开账号"), "中文 · 公开账号")


class CallbackButtonTests(unittest.TestCase):
    """With callbacks on, a pick that draws a page answers the tap itself.

    The button carries the very command the fill-in button would, so the
    page is the one the typed command draws. A command that changes what
    the plugin keeps is never a callback: the tap handler refuses those.
    """

    def test_a_query_pick_becomes_a_callback_with_the_same_command(self) -> None:
        entry = entry_for(
            CandidateView.RANKING,
            board_choice(),
            account_choice(ACCOUNT, "CPU 0", query="cpu"),
        )

        filled = pick_list_message(entry, command=COMMAND, ttl_seconds=600)
        tapped = pick_list_message(
            entry, command=COMMAND, ttl_seconds=600, callback=True
        )

        self.assertEqual(tapped.markdown, filled.markdown)
        for filled_row, tapped_row in zip(
            filled.keyboard["content"]["rows"],
            tapped.keyboard["content"]["rows"],
            strict=True,
        ):
            (fill,) = filled_row["buttons"]
            (tap,) = tapped_row["buttons"]
            self.assertEqual(tap["action"]["type"], 1)
            self.assertEqual(tap["action"]["data"], fill["action"]["data"])
            self.assertEqual(tap["action"]["permission"], {"type": 2})

    def test_a_watch_pick_still_only_fills_the_command_in(self) -> None:
        for view, choice in (
            (CandidateView.WATCH, account_choice(ACCOUNT, "CPU 0", query="cpu")),
            (CandidateView.WATCH_BOARD, board_choice()),
        ):
            with self.subTest(view=view):
                message = pick_list_message(
                    entry_for(view, choice),
                    command=COMMAND,
                    ttl_seconds=600,
                    callback=True,
                )

                (row,) = message.keyboard["content"]["rows"]
                self.assertEqual(row["buttons"][0]["action"]["type"], 2)

    def test_button_data_reads_as_the_typed_command_would(self) -> None:
        entry = entry_for(
            CandidateView.COMPARE,
            board_choice(),
            battle_rank=2,
            compare_rank=5,
        )
        data = pick_command(entry, entry.choices[0], command=COMMAND)

        request = read_button_command(data)

        self.assertEqual(request.prefix, "/")
        self.assertEqual(
            parse_zmdlog_payload(request.payload), parse_button(data)
        )
        for data, prefix, payload in (
            ("zmdlog 账号 usr_a", "", "账号 usr_a"),
            ("#zmdlog   榜单\n罗丹 ", "#", "榜单 罗丹"),
            ("/zmdlog", "/", ""),
        ):
            with self.subTest(data=data):
                self.assertEqual(
                    read_button_command(data), ButtonCommand(prefix, payload)
                )

    def test_data_that_is_no_zmdlog_command_is_not_ours(self) -> None:
        for data in (
            None,
            42,
            "",
            "   ",
            "账号 usr_a",
            "/zmdlogs 账号 usr_a",
            "/help zmdlog",
            "a-very-long-prefix/zmdlog 账号 usr_a",
            "/zmdlog " + "罗" * 600,
        ):
            with self.subTest(data=data):
                self.assertIsNone(read_button_command(data))


class SitePageMessageTests(unittest.TestCase):
    """A text that sends its reader to the site, with the page one tap away."""

    def test_the_how_to_bind_text_carries_the_binding_page(self) -> None:
        text = messages.NOT_BOUND.format(command=COMMAND)

        message = site_page_message(
            text, SitePage.BINDING, web_base_url="https://zmdlogs.com"
        )

        # The text is the plain reply, escaped: its dashes and slashes stay.
        self.assertEqual(re.sub(r"\\(.)", r"\1", message.markdown), text)
        (row,) = message.keyboard["content"]["rows"]
        (button,) = row["buttons"]
        self.assertEqual(button["render_data"]["label"], "去 ZMDLogs 生成绑定码")
        self.assertEqual(button["action"]["type"], 0)
        self.assertEqual(
            button["action"]["data"], "https://zmdlogs.com/account/binding"
        )

    def test_the_page_is_on_the_configured_site(self) -> None:
        message = site_page_message(
            "x", SitePage.BINDING, web_base_url="https://mirror.example/logs/"
        )

        (row,) = message.keyboard["content"]["rows"]
        self.assertEqual(
            row["buttons"][0]["action"]["data"],
            "https://mirror.example/logs/account/binding",
        )

    def test_a_site_address_that_is_no_web_link_gets_no_message(self) -> None:
        self.assertIsNone(
            site_page_message("x", SitePage.BINDING, web_base_url="zmdlogs.com")
        )


class NoticeMessageTests(unittest.TestCase):
    """A rank notice with a jump button under it for every battle it names."""

    def _notice(self, count: int, *, name: str = "新人") -> Notice:
        blocks = []
        urls = []
        for index in range(count):
            url = f"https://zmdlogs.com/battle/btl_upload_{index}"
            blocks.append(
                f"「榜单 {index}」第 1 → 第 2\n期间上方新增纪录：{name}\n{url}"
            )
            urls.append(url)
        return Notice(
            "\n\n".join(blocks),
            tuple(NoticeLink(url, f"战报 {n}") for n, url in enumerate(urls, 1)),
        )

    def test_each_battle_is_a_jump_button_named_in_the_text(self) -> None:
        notice = self._notice(2)

        message = notice_message(notice)

        buttons = [
            button
            for row in message.keyboard["content"]["rows"]
            for button in row["buttons"]
        ]
        self.assertEqual(
            [
                (button["render_data"]["label"], button["action"]["type"])
                for button in buttons
            ],
            [("战报 1", 0), ("战报 2", 0)],
        )
        self.assertEqual(
            [button["action"]["data"] for button in buttons],
            [link.url for link in notice.links],
        )
        self.assertEqual(
            {button["action"]["permission"]["type"] for button in buttons}, {2}
        )
        # The label takes the link's place, on the line that names the record.
        lines = unescape(message.markdown).split("\n")
        self.assertIn("期间上方新增纪录：新人 · 战报 1", lines)
        self.assertIn("期间上方新增纪录：新人 · 战报 2", lines)
        self.assertNotIn("https://", message.markdown)
        # Everything else reads as the plain notice does.
        plain = [line for line in notice.text.split("\n") if "https://" not in line]
        self.assertEqual(
            [re.sub(r" · 战报 \d$", "", line) for line in lines], plain
        )

    def test_a_battle_printed_twice_is_one_button_named_twice(self) -> None:
        url = "https://zmdlogs.com/battle/btl_upload_0"
        notice = Notice(
            f"📉 甲\n纪录 A\n{url}\n\n📉 乙\n纪录 A\n{url}",
            (NoticeLink(url, "战报 1"),),
        )

        message = notice_message(notice)

        self.assertEqual(len(message.keyboard["content"]["rows"]), 1)
        self.assertEqual(unescape(message.markdown).count("纪录 A · 战报 1"), 2)

    def test_a_nickname_cannot_forge_markdown(self) -> None:
        forged = "**假** [点我](https://x.example) `#`\n# 标题"
        message = notice_message(self._notice(1, name=forged))

        for line in message.markdown.split("\n"):
            self.assertIsNone(
                re.search(r"(?<!\\)[*_`\[\]()#<>]", line), msg=line
            )

    def test_a_full_merge_fits_the_keyboard(self) -> None:
        # 3 accounts × 5 boards, 3 boards × 3 runs, and a full keyboard.
        for count in (15, 9, 5, 25):
            with self.subTest(count=count):
                rows = notice_message(self._notice(count)).keyboard["content"][
                    "rows"
                ]

                self.assertLessEqual(len(rows), 5)
                self.assertTrue(all(len(row["buttons"]) <= 5 for row in rows))
                labels = [
                    button["render_data"]["label"]
                    for row in rows
                    for button in row["buttons"]
                ]
                self.assertEqual(labels, [f"战报 {n}" for n in range(1, count + 1)])
        # Few enough for a row each, each is full width.
        rows = notice_message(self._notice(5)).keyboard["content"]["rows"]
        self.assertEqual([len(row["buttons"]) for row in rows], [1] * 5)

    def test_more_battles_than_a_keyboard_holds_go_as_plain_text(self) -> None:
        # Past what either merge can reach: no battle may lose its link.
        self.assertIsNone(notice_message(self._notice(26)))

    def test_a_notice_without_battles_has_no_button_message(self) -> None:
        bare = Notice("📉 甲 被顶屁股了\n\n「榜」第 1 → 第 2")
        self.assertIsNone(notice_message(bare))
        script = "javascript:alert(1)"
        unsafe = Notice(f"x\n{script}", (NoticeLink(script, "战报 1"),))
        self.assertIsNone(notice_message(unsafe))


def unescape(markdown: str) -> str:
    return re.sub(r"\\(.)", r"\1", markdown)


if __name__ == "__main__":
    unittest.main()
