import unittest

from core.help import build_help_page
from core.routing import RouteKind, parse_zmdlog_payload


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


class HelpTests(unittest.TestCase):
    def test_help_uses_the_supplied_prefix_and_covers_commands(self) -> None:
        page = build_help_page("!")
        command_text = "\n".join(
            command.command
            for section in page.sections
            for command in section.commands
        )

        self.assertIn("!zmdlog", command_text)
        self.assertIn("!zmdlog help", command_text)
        self.assertIn("!zmdlog 榜单", command_text)
        self.assertIn("!zmdlog 榜单 <关键词>", command_text)
        self.assertIn("!zmdlog <关键词>", command_text)
        self.assertIn("!zmdlog 影拓", command_text)
        self.assertIn("!zmdlog 影拓4", command_text)
        self.assertNotIn("/zmdlog", command_text)

    def test_help_has_no_alternate_metric_option(self) -> None:
        page = build_help_page("/")
        visible_text = repr(page)
        self.assertNotIn("RDPS", visible_text)


if __name__ == "__main__":
    unittest.main()
