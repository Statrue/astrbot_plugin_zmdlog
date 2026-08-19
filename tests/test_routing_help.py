import unittest

from core.help import build_help_page
from core.routing import (
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
        all_boards = parse_zmdlog_payload("榜单")
        ranking = parse_zmdlog_payload(" 榜单   罗丹 ")
        shortcut = parse_zmdlog_payload(" 影拓 ")

        self.assertEqual(all_boards.kind, RouteKind.ALL_RANKINGS)
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

    def test_top_option_is_removed_before_matching(self) -> None:
        shortcut = parse_zmdlog_payload("罗丹 --top 30")
        ranking = parse_zmdlog_payload("榜单 罗丹 --top 12")

        self.assertEqual(shortcut.kind, RouteKind.SMART_QUERY)
        self.assertEqual(shortcut.query, "罗丹")
        self.assertEqual(shortcut.ranking_top, 30)
        self.assertEqual(shortcut.ranking_limit, 30)
        self.assertEqual(ranking.kind, RouteKind.RANKING_QUERY)
        self.assertEqual(ranking.query, "罗丹")
        self.assertEqual(ranking.ranking_top, 12)

    def test_concrete_ranking_defaults_to_top_ten(self) -> None:
        route = parse_zmdlog_payload("罗丹")

        self.assertIsNone(route.ranking_top)
        self.assertEqual(route.ranking_limit, 10)

    def test_invalid_top_options_have_clear_errors(self) -> None:
        invalid_payloads = (
            "罗丹 --top",
            "罗丹 --top abc",
            "罗丹 --top 0",
            "罗丹 --top 31",
            "罗丹 --top 10 --top 20",
            "罗丹 --top 10 额外内容",
            "榜单 --top 10",
            "账号 usr_1234567890abcdef --top 10",
            "战报 btl_upload_abcdef123456 --top 10",
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RouteParseError, "--top"):
                    parse_zmdlog_payload(payload)

    def test_account_and_battle_require_a_reference(self) -> None:
        for payload in ("账号", "账户", "战报"):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)


class HelpTests(unittest.TestCase):
    def test_help_uses_prefix_and_lists_help_before_query_forms(self) -> None:
        page = build_help_page("!")
        commands = tuple(
            command.command
            for section in page.sections
            for command in section.commands
        )

        self.assertEqual(
            commands,
            (
                "!zmdlog",
                "!zmdlog help",
                "!zmdlog 榜单",
                "!zmdlog 榜单 <关键词>",
                "!zmdlog 关键词 [--top 数量]",
                "!zmdlog 账号 <accountId或账号主页链接>",
                "!zmdlog 战报 <battleId或战报链接>",
                "!zmdlog 别名",
                "!zmdlog 别名 添加 <榜单或副本> <别名...>",
                "!zmdlog 别名 删除 <别名>",
            ),
        )
        visible_text = repr(page)
        self.assertNotIn("固定口径", visible_text)
        self.assertNotIn("智能匹配", visible_text)
        self.assertNotIn("副本范围", visible_text)
        self.assertNotIn("影拓", visible_text)

    def test_help_has_no_alternate_metric_option(self) -> None:
        page = build_help_page("/")
        visible_text = repr(page)
        self.assertNotIn("RDPS", visible_text)


if __name__ == "__main__":
    unittest.main()


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
        for payload in ("别名 添加 罗丹", "别名 删除", "别名 看看", "别名 --top 3"):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)
