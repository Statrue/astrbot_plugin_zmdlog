import re
import unittest

from core.help import build_help_page
from core.routing import (
    ALL_PAGES,
    RouteKind,
    RouteParseError,
    parse_zmdlog_payload,
)


class RoutingTests(unittest.TestCase):
    def test_root_and_help_share_the_help_route(self) -> None:
        self.assertEqual(parse_zmdlog_payload("").kind, RouteKind.HELP)
        self.assertEqual(parse_zmdlog_payload("help").kind, RouteKind.HELP)
        self.assertEqual(parse_zmdlog_payload(" HELP ").kind, RouteKind.HELP)

    def test_board_routes_keep_their_query(self) -> None:
        dungeons = parse_zmdlog_payload("榜单")
        ranking = parse_zmdlog_payload(" 榜单   罗丹 ")
        shortcut = parse_zmdlog_payload(" 影拓 ")

        self.assertEqual(dungeons.kind, RouteKind.DUNGEON_LIST)
        self.assertEqual(ranking.kind, RouteKind.RANKING_QUERY)
        self.assertEqual(ranking.query, "罗丹")
        self.assertEqual(shortcut.kind, RouteKind.SMART_QUERY)
        self.assertEqual(shortcut.query, "影拓")

    def test_account_and_battle_routes_keep_exact_references(self) -> None:
        account = parse_zmdlog_payload("账号 usr_1234567890abcdef")
        battle = parse_zmdlog_payload(
            "战报 https://zmdlogs.com/battle/btl_upload_abcdef123456?metric=dps"
        )

        self.assertEqual(account.kind, RouteKind.ACCOUNT_QUERY)
        self.assertEqual(account.query, "usr_1234567890abcdef")
        self.assertEqual(battle.kind, RouteKind.BATTLE_QUERY)
        self.assertIn("btl_upload_abcdef123456", battle.query)

    def test_page_option_is_removed_before_matching(self) -> None:
        for payload, page in (
            ("罗丹 --页 2", 2),
            ("罗丹 --page 2", 2),
            ("罗丹 -p 2", 2),
            ("罗丹 -P 3", 3),
            ("罗丹 --页 全部", ALL_PAGES),
            ("罗丹 --页 all", ALL_PAGES),
            ("罗丹 --page ALL", ALL_PAGES),
        ):
            with self.subTest(payload=payload):
                route = parse_zmdlog_payload(payload)
                self.assertEqual(route.kind, RouteKind.SMART_QUERY)
                self.assertEqual(route.query, "罗丹")
                self.assertEqual(route.ranking_page, page)

        ranking = parse_zmdlog_payload("榜单 罗丹 --页 2 --角色 黎风 --口径 rdps")
        self.assertEqual(ranking.kind, RouteKind.RANKING_QUERY)
        self.assertEqual(ranking.query, "罗丹")
        self.assertEqual(ranking.ranking_page, 2)
        self.assertEqual(ranking.character_filter, "黎风")
        self.assertEqual(ranking.metric, "rdps")

    def test_concrete_ranking_defaults_to_the_first_page(self) -> None:
        self.assertIsNone(parse_zmdlog_payload("罗丹").ranking_page)

    def test_invalid_page_options_have_clear_errors(self) -> None:
        invalid_payloads = (
            "罗丹 --页",
            "罗丹 --页 abc",
            "罗丹 --页 0",
            "罗丹 --页 -1",
            "罗丹 --页 1 --page 2",
            "罗丹 -p 1 -p 2",
            "罗丹 --页 2 额外内容",
            "榜单 --页 2",
            "阵容 罗丹 --页 2",
            "账号 usr_1234567890abcdef --页 2",
            "战报 btl_upload_abcdef123456 --页 2",
            "角色统计 罗丹 --页 2",
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RouteParseError, "--页"):
                    parse_zmdlog_payload(payload)

    def test_top_is_gone_and_says_where_paging_went(self) -> None:
        # Every command that took --top refuses it the same way, whatever
        # the value: the user is told the option became --页.
        for payload in (
            "罗丹 --top 30",
            "榜单 罗丹 --top 12",
            "阵容 罗丹 --top 5",
            "罗丹 --TOP 5",
            "罗丹 --top",
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RouteParseError, "--页"):
                    parse_zmdlog_payload(payload)

    def test_account_and_battle_require_a_reference(self) -> None:
        for payload in ("账号", "账户", "战报"):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)


class PageOptionBoundsTests(unittest.TestCase):
    def test_absurd_page_values_are_rejected_as_route_errors(self) -> None:
        # int() raises a plain ValueError past 4300 digits, which no handler
        # guard would catch; the bound must trip first.
        for raw in ("9" * 4301, "9" * 5000, "1" * 10, "１"):
            with self.subTest(raw=raw):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(f"罗丹 --页 {raw}")
        self.assertEqual(parse_zmdlog_payload("罗丹 --页 003").ranking_page, 3)


class HelpTests(unittest.TestCase):
    def test_help_uses_prefix_and_keeps_one_row_per_question(self) -> None:
        page = build_help_page("!")
        commands = tuple(
            command.command
            for section in page.sections
            for command in section.commands
        )

        # Commands that share an argument share a row; the page is the answer
        # to "which commands exist", so there is no row for help itself.
        self.assertEqual(
            commands,
            (
                (
                    "!zmdlog <榜单关键词> [--页 N|全部] [--角色 角色名…] "
                    "[--属性 属性] [--口径 rdps]"
                ),
                "!zmdlog 榜单",
                "!zmdlog 阵容 <榜单关键词> [--口径 rdps]",
                "!zmdlog 新纪录 [--范围 7d|14d|30d] [--口径 rdps]",
                "!zmdlog 玩家排名 [--范围 7d|14d|30d] [--口径 rdps]",
                (
                    "!zmdlog 角色统计 [榜单关键词或角色名] "
                    "[--范围 7d|14d|30d|all] [--潜能 0|1-5|all] [--口径 rdps]"
                ),
                "!zmdlog 角色排名 [角色名… | --属性 属性 | --职业 职业] [--口径 rdps]",
                (
                    "!zmdlog 角色档案 <角色名> [--榜单 榜单关键词] "
                    "[--范围 7d|14d|30d|all]"
                ),
                "!zmdlog 账号 <昵称、accountId或主页链接>",
                (
                    "!zmdlog 战报 | 数据 | 配装 | 技能轴 "
                    "<battleId、链接或榜单关键词 [名次]> [--口径 rdps]"
                ),
                "!zmdlog 对比 <榜单关键词 [名次 名次] 或 两个battleId> [--口径 rdps]",
                "!zmdlog 对比 <榜单关键词> 我 [名次] [--口径 rdps]",
                "!zmdlog 关注 [<榜单关键词> | 全部]",
                "!zmdlog 趋势 <账号> [--范围 7d|14d|30d|all]",
                "!zmdlog 绑定 <绑定码>",
                "!zmdlog 我的 [序号或昵称]",
                "!zmdlog 别名 [添加 <榜单或副本> <别名…> | 删除 <别名>]",
            ),
        )
        self.assertEqual(
            [section.title for section in page.sections],
            ["榜单", "角色", "战报", "关注", "绑定", "管理"],
        )
        visible_text = repr(page)
        self.assertNotIn("固定口径", visible_text)
        self.assertNotIn("智能匹配", visible_text)
        self.assertNotIn("副本范围", visible_text)
        self.assertNotIn("影拓", visible_text)

    def test_every_row_states_what_it_does_in_one_line(self) -> None:
        # The page groups look-alike commands (阵容 / 角色统计 / --角色), so
        # the statement beside each is what tells them apart: one line, a
        # statement rather than a question, with no closing punctuation.
        page = build_help_page("/")
        for section in page.sections:
            with self.subTest(section=section.title):
                self.assertTrue(section.summary)
            for command in section.commands:
                with self.subTest(command=command.command):
                    self.assertTrue(command.answers.strip())
                    self.assertNotIn("\n", command.answers)
                    self.assertNotIn(command.answers[-1], "。？！?!.，,；;：:")
                    self.assertNotIn("？", command.answers)

    def test_the_bare_board_command_lists_the_dungeons(self) -> None:
        # 榜单 lists the dungeons to pick from; it no longer draws every
        # board's top three.
        page = build_help_page("/")
        (row,) = (
            command
            for section in page.sections
            for command in section.commands
            if command.command == "/zmdlog 榜单"
        )

        self.assertEqual(row.answers, "列出全部副本和榜单")
        self.assertNotIn("前三", row.answers)

    def test_each_row_shows_its_bare_form_without_options(self) -> None:
        # The options are explained once, in their own card; a row shows the
        # shortest way to write its command.
        page = build_help_page("/")
        short = {
            command.command.split(" <")[0].split(" [")[0]: command.short
            for section in page.sections
            for command in section.commands
        }

        self.assertEqual(short["/zmdlog"], "/zmdlog <榜单关键词>")
        self.assertEqual(short["/zmdlog 角色排名"], "/zmdlog 角色排名")
        self.assertEqual(
            short["/zmdlog 角色统计"], "/zmdlog 角色统计 [榜单关键词或角色名]"
        )
        for section in page.sections:
            for command in section.commands:
                with self.subTest(command=command.command):
                    self.assertNotIn("--", command.short)
                    self.assertTrue(command.command.startswith(command.short))

    def test_every_option_a_command_takes_is_explained_once(self) -> None:
        # Stripped from the rows, the options would otherwise vanish from
        # the page: each one a command spells gets exactly one line in the
        # options card, and the card explains nothing no command takes.
        page = build_help_page("/")
        spelled = {
            option
            for section in page.sections
            for command in section.commands
            for option in re.findall(r"--[^\s|\]]+", command.command)
        }
        explained = [
            option
            for entry in page.options
            for option in re.findall(r"--[^\s|/]+", entry.option)
        ]

        self.assertEqual(sorted(explained), sorted(spelled))
        for entry in page.options:
            with self.subTest(option=entry.option):
                self.assertTrue(entry.does.strip())
        options = [entry.option for entry in page.options]
        # Paging is never offered on the picture, and 我 is a word, not an
        # --option: the card is where a reader learns either exists.
        self.assertIn("--页 N|全部", options)
        self.assertIn("对比 … 我", options)

    def test_the_three_character_verbs_share_a_section(self) -> None:
        # 角色统计, 角色排名 and 角色档案 all take a character name and
        # answer different questions about it; side by side, the answers
        # are what tells them apart.
        page = build_help_page("/")
        (section,) = (entry for entry in page.sections if entry.title == "角色")

        self.assertEqual(
            [command.command.split()[1] for command in section.commands],
            ["角色统计", "角色排名", "角色档案"],
        )
        self.assertEqual(
            len({command.answers for command in section.commands}), 3
        )

    def test_only_the_official_bot_page_states_its_two_preconditions(self) -> None:
        # The official bot hears a group only when @-ed unless the group lets
        # it read every message, and pushes a rank notice only when the group
        # lets it speak unasked. Nowhere else does either apply.
        plain = build_help_page("/")
        official = build_help_page("/", official=True)

        self.assertEqual(plain.notes, ())
        mention, notice = official.notes
        self.assertIn("获取群内全部消息", mention)
        self.assertIn("@机器人", mention)
        self.assertIn("群主或管理员", notice)
        self.assertIn("机器人主动在群聊内发言", notice)
        self.assertEqual(official.sections, plain.sections)

    def test_watch_help_never_claims_who_overtook_the_account(self) -> None:
        # One board read cannot prove who caused a drop, so the notice only
        # reports the new records above. The help page must promise no more.
        page = build_help_page("/")
        section = next(entry for entry in page.sections if entry.title == "关注")

        self.assertNotIn("超", repr(section))

    def test_help_names_the_rdps_option_where_it_works(self) -> None:
        # rDPS is a request, never a default: the option shows on the rows
        # that take it and nowhere is rDPS presented as the reading.
        page = build_help_page("/")
        commands = [
            command.command
            for section in page.sections
            for command in section.commands
        ]
        with_option = [c for c in commands if "--口径 rdps" in c]
        self.assertEqual(len(with_option), 9)
        self.assertTrue(all("--口径 rdps" not in c for c in commands if "账号" in c))


class MetricOptionTests(unittest.TestCase):
    def test_the_option_reads_every_spelling_and_defaults_to_dps(self) -> None:
        self.assertEqual(parse_zmdlog_payload("罗丹").metric, "dps")
        for text in ("rdps", "RDPS", "rDPS", "团队贡献"):
            with self.subTest(text=text):
                route = parse_zmdlog_payload(f"罗丹 --口径 {text}")
                self.assertEqual(route.kind, RouteKind.SMART_QUERY)
                self.assertEqual((route.query, route.metric), ("罗丹", "rdps"))
        self.assertEqual(parse_zmdlog_payload("罗丹 --metric dps").metric, "dps")
        self.assertEqual(parse_zmdlog_payload("罗丹 --口径 直伤").metric, "dps")
        with self.assertRaisesRegex(RouteParseError, "--口径"):
            parse_zmdlog_payload("罗丹 --口径 xdps")

    def test_every_index_page_takes_the_option(self) -> None:
        for payload, kind in (
            ("榜单 罗丹 --口径 rdps", RouteKind.RANKING_QUERY),
            ("阵容 罗丹 --口径 rdps", RouteKind.ROSTER_QUERY),
            ("角色统计 --口径 rdps", RouteKind.CHARACTER_STATS),
            ("角色统计 罗丹 --口径 rdps", RouteKind.CHARACTER_STATS),
            ("角色排名 --口径 rdps", RouteKind.CHARACTER_STANDINGS),
            ("角色排名 诀 --口径 rdps", RouteKind.CHARACTER_STANDINGS),
            ("玩家排名 --口径 rdps", RouteKind.PLAYER_CHAMPIONS),
            ("新纪录 --口径 rdps", RouteKind.RECORDS_QUERY),
        ):
            with self.subTest(payload=payload):
                route = parse_zmdlog_payload(payload)
                self.assertEqual((route.kind, route.metric), (kind, "rdps"))

    def test_pages_without_a_second_ranking_refuse_the_option(self) -> None:
        for payload in (
            "榜单 --口径 rdps",
            "账号 usr_1234567890abcdef --口径 rdps",
            "战报 btl_upload_abcdef123456 --口径 rdps",
            "趋势 CPU --口径 rdps",
            "玩家排名 CPU --口径 rdps",
            "关注 CPU --口径 rdps",
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RouteParseError, "--口径"):
                    parse_zmdlog_payload(payload)


if __name__ == "__main__":
    unittest.main()


class WatchRouteTests(unittest.TestCase):
    def test_watch_subcommands(self) -> None:
        from core.routing import RouteParseError

        self.assertEqual(parse_zmdlog_payload("关注").kind, RouteKind.WATCH_LIST)
        add = parse_zmdlog_payload("关注 罗丹")
        self.assertEqual((add.kind, add.query), (RouteKind.WATCH_ADD, "罗丹"))
        self.assertEqual(parse_zmdlog_payload("盯 罗丹").kind, RouteKind.WATCH_ADD)
        remove = parse_zmdlog_payload("取关 2")
        self.assertEqual((remove.kind, remove.query), (RouteKind.WATCH_REMOVE, "2"))
        self.assertEqual(
            parse_zmdlog_payload("取消关注 罗丹").kind, RouteKind.WATCH_REMOVE
        )
        for payload in ("取关", "关注 罗丹 --页 3", "取关 1 --角色 黎风"):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)

    def test_every_board_has_its_own_two_routes(self) -> None:
        self.assertEqual(parse_zmdlog_payload("关注 全部").kind, RouteKind.WATCH_ALL)
        self.assertEqual(
            parse_zmdlog_payload("取关 全部").kind, RouteKind.UNWATCH_ALL
        )

    def test_every_board_is_spelled_one_way(self) -> None:
        # 全部 alone names every board; 全部榜单 is an ordinary keyword.
        add = parse_zmdlog_payload("关注 全部榜单")
        self.assertEqual((add.kind, add.query), (RouteKind.WATCH_ADD, "全部榜单"))
        remove = parse_zmdlog_payload("取关 全部榜单")
        self.assertEqual(
            (remove.kind, remove.query), (RouteKind.WATCH_REMOVE, "全部榜单")
        )

    def test_the_old_board_marker_is_part_of_the_keyword(self) -> None:
        # 关注 榜单 X was the board form while accounts could be watched; it
        # is now an ordinary keyword, matched like any other.
        add = parse_zmdlog_payload("关注 榜单 罗丹")
        self.assertEqual((add.kind, add.query), (RouteKind.WATCH_ADD, "榜单 罗丹"))
        remove = parse_zmdlog_payload("取关 榜单 1")
        self.assertEqual(
            (remove.kind, remove.query), (RouteKind.WATCH_REMOVE, "榜单 1")
        )


class AliasRouteTests(unittest.TestCase):
    def test_alias_subcommands(self) -> None:
        from core.routing import RouteParseError

        self.assertEqual(parse_zmdlog_payload("别名").kind, RouteKind.ALIAS_LIST)
        add = parse_zmdlog_payload("别名 添加 罗丹 ld 小罗")
        self.assertEqual(add.kind, RouteKind.ALIAS_ADD)
        self.assertEqual(add.query, "罗丹 ld 小罗")
        remove = parse_zmdlog_payload("别名 删除 小罗")
        self.assertEqual(remove.kind, RouteKind.ALIAS_REMOVE)
        self.assertEqual(remove.query, "小罗")
        for payload in ("别名 添加 罗丹", "别名 删除", "别名 看看", "别名 --页 3"):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)


class BindingRouteTests(unittest.TestCase):
    def test_binding_subcommands(self) -> None:
        bind = parse_zmdlog_payload("绑定 ZMD-7K4M-QX2E")
        self.assertEqual(bind.kind, RouteKind.BIND)
        self.assertEqual(bind.query, "ZMD-7K4M-QX2E")
        # Bare 绑定 is a route too: the service answers with how to get a code.
        self.assertEqual(parse_zmdlog_payload("绑定").kind, RouteKind.BIND)
        self.assertEqual(parse_zmdlog_payload("绑定账号 x").kind, RouteKind.BIND)
        unbind = parse_zmdlog_payload("解绑 2")
        self.assertEqual(unbind.kind, RouteKind.UNBIND)
        self.assertEqual(unbind.query, "2")
        self.assertEqual(parse_zmdlog_payload("解绑").kind, RouteKind.UNBIND)
        self.assertEqual(parse_zmdlog_payload("取消绑定 全部").kind, RouteKind.UNBIND)
        self.assertEqual(parse_zmdlog_payload("主账号").kind, RouteKind.PRIMARY_ACCOUNT)
        self.assertEqual(
            parse_zmdlog_payload("主账号 2").kind, RouteKind.PRIMARY_ACCOUNT
        )
        mine = parse_zmdlog_payload("我的")
        self.assertEqual(mine.kind, RouteKind.MY_ACCOUNT)
        self.assertEqual(mine.query, "")
        self.assertEqual(parse_zmdlog_payload("我的 2").query, "2")
        refused = ("我的 --页 3", "绑定 x --页 3")
        for payload in refused:
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)

    def test_the_dropped_group_board_words_are_plain_keywords(self) -> None:
        # 群榜 was removed in 1.3.0: its words are now whatever any unknown
        # text is, a keyword for the smart query.
        for payload in ("群榜", "群榜 罗丹", "群排名 罗丹"):
            with self.subTest(payload=payload):
                route = parse_zmdlog_payload(payload)
                self.assertEqual(route.kind, RouteKind.SMART_QUERY)
                self.assertEqual(route.query, payload)


class CharacterStandingsRouteTests(unittest.TestCase):
    def test_the_command_names_a_character_and_takes_no_options(self) -> None:
        route = parse_zmdlog_payload("角色排名 提弗洛斯")
        self.assertEqual(route.kind, RouteKind.CHARACTER_STANDINGS)
        self.assertEqual(route.query, "提弗洛斯")
        self.assertEqual(
            parse_zmdlog_payload("角色榜 诀").kind, RouteKind.CHARACTER_STANDINGS
        )
        # No name: every character's first places over all boards.
        bare = parse_zmdlog_payload("角色排名")
        self.assertEqual(bare.kind, RouteKind.CHARACTER_STANDINGS)
        self.assertEqual(bare.query, "")
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("角色排名 诀 --页 5")
        # A window applies to the bare champions board only.
        self.assertEqual(parse_zmdlog_payload("角色排名 --范围 7d").stats_range, "7d")
        players = parse_zmdlog_payload("玩家排名 --范围 30d")
        self.assertEqual(players.kind, RouteKind.PLAYER_CHAMPIONS)
        self.assertEqual(players.stats_range, "30d")
        self.assertEqual(
            parse_zmdlog_payload("玩家榜").kind, RouteKind.PLAYER_CHAMPIONS
        )
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("玩家排名 CPU --范围 7d")
        # Same shape as 角色排名: a name is that one player, which is 账号.
        named = parse_zmdlog_payload("玩家排名 CPU 0")
        self.assertEqual(named.kind, RouteKind.ACCOUNT_QUERY)
        self.assertEqual(named.query, "CPU 0")
        self.assertEqual(
            parse_zmdlog_payload("玩家冠军榜").kind, RouteKind.PLAYER_CHAMPIONS
        )
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("角色排名 诀 --范围 7d")


class RangeAndCompareRoutingTests(unittest.TestCase):
    def test_range_spellings_people_type_are_accepted(self) -> None:
        for text, expected in (
            ("一周", "7d"),
            ("最近7天", "7d"),
            ("两周", "14d"),
            ("14日", "14d"),
            ("近30天", "30d"),
            ("一个月", "30d"),
            ("ALL", "all"),
        ):
            with self.subTest(text=text):
                route = parse_zmdlog_payload(f"角色统计 --范围 {text}")
                self.assertEqual(route.stats_range, expected)
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("角色统计 --范围 上周")

    def test_records_only_take_a_bounded_window(self) -> None:
        self.assertEqual(parse_zmdlog_payload("新纪录").stats_range, "7d")
        self.assertEqual(
            parse_zmdlog_payload("新纪录 --范围 一个月").stats_range, "30d"
        )
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("新纪录 --范围 all")

    def test_compare_with_one_reference_is_a_usage_error(self) -> None:
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("对比 btl_upload_aaaaaaaaaaaa")
        route = parse_zmdlog_payload(
            "对比 btl_upload_aaaaaaaaaaaa btl_upload_bbbbbbbbbbbb"
        )
        self.assertEqual(route.query, "btl_upload_aaaaaaaaaaaa")
        self.assertEqual(route.compare_target, "btl_upload_bbbbbbbbbbbb")
