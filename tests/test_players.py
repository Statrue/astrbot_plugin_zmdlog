"""玩家排名: every uploading account counted over all boards."""

import unittest
from pathlib import Path

from core import facts
from core.presentation import build_player_champions_page
from core.render import TemplateRenderer
from core.standings import account_tallies, account_tally
from tests.helpers import named_ranking

WEB = "https://zmdlogs.com"


ranking = named_ranking


class AccountTalliesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rankings = (
            ranking("dung01_group_bossrush02", "三位一体"),
            ranking("dung01_group_bossrush03", "罗丹", rows=3),
        )
        self.leader = self.rankings[0].rows[0]

    def test_the_uploader_of_a_first_record_is_that_boards_champion(self) -> None:
        tallies = account_tallies(self.rankings)

        # Both fixture boards share the same #1 record and uploader.
        self.assertEqual(tallies[0].account_id, self.leader.account_id)
        self.assertEqual(tallies[0].first_places, 2)
        self.assertEqual(tallies[0].display_name, self.leader.account_display_name)
        firsts = [tally.first_places for tally in tallies]
        self.assertEqual(firsts, sorted(firsts, reverse=True))

    def test_counts_never_exceed_the_boards_and_records_seen(self) -> None:
        for tally in account_tallies(self.rankings):
            with self.subTest(account=tally.display_name):
                self.assertLessEqual(tally.first_places, tally.podiums)
                self.assertLessEqual(tally.podiums, tally.top_tens)
                self.assertLessEqual(tally.top_tens, tally.boards)
                self.assertLessEqual(tally.boards, len(self.rankings))
                self.assertGreaterEqual(tally.records, tally.boards)

    def test_habits_name_the_most_used_main_c_and_team(self) -> None:
        tally = account_tally(self.rankings, self.leader.account_id)

        self.assertIsNotNone(tally)
        self.assertEqual(tally.main_c, self.leader.character_name)
        self.assertGreaterEqual(tally.main_c_count, 1)
        self.assertEqual(
            tally.team,
            tuple(sorted(e.character_name for e in self.leader.roster_entries)),
        )
        self.assertIsNone(account_tally(self.rankings, "usr_nobody"))

    def test_every_champion_account_is_listed_whatever_the_limit(self) -> None:
        tallies = account_tallies(self.rankings)
        champions = [t.display_name for t in tallies if t.first_places > 0]

        text = facts.format_account_tallies(tallies, board_count=2, limit=1)

        for name in champions:
            with self.subTest(account=name):
                self.assertIn(f"{name} · 冠军", text)
        self.assertIn("冠军 0 个，未列出", text)

    def test_the_text_leads_with_the_champion_and_states_the_rule(self) -> None:
        tallies = account_tallies(self.rankings)

        text = facts.format_account_tallies(tallies, board_count=2, age_seconds=30)

        self.assertIn("全部 2 个榜", text)
        self.assertIn("第一名记录的上传者", text)
        self.assertIn(f"冠军最多：{self.leader.account_display_name} 2 个榜", text)
        self.assertIn("常用主C", text)
        self.assertIn("ZMDLogs", text)


class PlayerPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rankings = (
            ranking("dung01_group_bossrush02", "三位一体"),
            ranking("dung01_group_bossrush03", "罗丹", rows=3),
        )
        self.tallies = account_tallies(self.rankings)

    def test_the_page_ranks_accounts_and_folds_the_rest(self) -> None:
        page = build_player_champions_page(
            self.tallies, board_count=2, query="玩家排名", window_label="近 7 天"
        )

        self.assertEqual(page.header.target_type, "玩家排名")
        self.assertIn("近 7 天", page.header.title)
        self.assertEqual(page.rows[0].position, 1)
        self.assertEqual(page.rows[0].bar_width, 100.0)
        self.assertEqual(page.top.account_id, self.rankings[0].rows[0].account_id)
        self.assertTrue(all(row.podiums >= 1 for row in page.rows))
        self.assertIn("常用主 C", page.rows[0].main_label)
        self.assertIn("常用阵容", page.rows[0].team_label)

    def test_the_template_renders(self) -> None:
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])

        html = renderer.render_player_champions(
            self.tallies, board_count=2, query="玩家排名"
        )

        self.assertIn("玩家冠军榜", html)
        self.assertIn(self.rankings[0].rows[0].account_display_name, html)
        self.assertIn("记录", html)


if __name__ == "__main__":
    unittest.main()
