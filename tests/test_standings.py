"""角色排名: where the teams fielding one character stand on every board."""

import unittest
from pathlib import Path

from core import facts
from core.models import parse_boss_ranking
from core.presentation import build_character_standings_page
from core.render import TemplateRenderer
from core.standings import character_standings, roster_character_names
from tests.helpers import ranking_payload_with_rows

WEB = "https://zmdlogs.com"


def ranking(slug: str, boss_name: str, *, rows: int | None = None):
    payload = ranking_payload_with_rows()
    payload["bossSlug"] = slug
    payload["bossName"] = boss_name
    if rows is not None:
        payload["rows"] = payload["rows"][:rows]
    return parse_boss_ranking(payload)


class StandingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first = ranking("dung01_group_bossrush02", "三位一体")
        self.second = ranking("dung01_group_bossrush03", "罗丹", rows=3)
        self.rankings = (self.first, self.second)
        # A name fielded in every fixture row, and one fielded in none.
        self.name = self.first.rows[0].roster_entries[0].character_name
        self.main = self.first.rows[0].character_name

    def test_the_best_record_per_board_is_found_and_boards_sort_by_rank(self) -> None:
        standings = character_standings(self.rankings, self.name)

        self.assertEqual(standings.character, self.name)
        self.assertEqual(len(standings.boards), 2)
        self.assertEqual([board.best.rank for board in standings.boards], [1, 1])
        self.assertEqual(standings.boards[0].total_rows, len(self.first.rows))
        self.assertEqual(standings.boards[1].total_rows, 3)
        fielding = [
            row
            for ranking in self.rankings
            for row in ranking.rows
            if any(e.character_name == self.name for e in row.roster_entries)
        ]
        self.assertEqual(standings.appearances, len(fielding))
        self.assertEqual(standings.absent, ())

    def test_a_board_without_the_character_is_folded_into_absent(self) -> None:
        standings = character_standings(self.rankings, "没有这个人")

        self.assertEqual(standings.boards, ())
        self.assertEqual(
            [board.boss_name for board in standings.absent], ["三位一体", "罗丹"]
        )

    def test_main_c_is_told_apart_from_being_in_the_team(self) -> None:
        standings = character_standings(self.rankings, self.main)

        board = standings.boards[0]
        self.assertTrue(board.best_as_main)
        self.assertGreaterEqual(board.main_appearances, 1)

    def test_every_roster_name_is_offered_for_resolution(self) -> None:
        names = roster_character_names(self.rankings)

        self.assertIn(self.name, names)
        self.assertEqual(len(names), len(set(names)))

    def test_the_text_states_rank_out_of_total_and_the_disclaimer(self) -> None:
        standings = character_standings(self.rankings, self.name)

        text = facts.format_character_standings(standings, age_seconds=185)

        self.assertIn(f"#1/{len(self.first.rows)} 三位一体", text)
        # The count a model is asked for is stated, not left to be counted.
        self.assertIn("第一名 2 个榜", text)
        self.assertIn("前三 2 个榜", text)
        # Every first-place board is named, whatever the row limit.
        self.assertIn("第一名的榜：三位一体、罗丹", text)
        self.assertIn("数据截至 3 分钟前", text)
        self.assertIn("battleId", text)
        self.assertIn("不是角色本身的强度", text)

    def test_the_text_for_an_absent_character_says_so(self) -> None:
        standings = character_standings(self.rankings, "没有这个人")

        text = facts.format_character_standings(standings)

        self.assertIn("没有「没有这个人」出场", text)


class StandingsPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rankings = (
            ranking("dung01_group_bossrush02", "三位一体"),
            ranking("dung01_group_bossrush03", "罗丹", rows=3),
        )
        self.name = self.rankings[0].rows[0].roster_entries[0].character_name

    def test_the_page_carries_one_row_per_board_and_the_index_age(self) -> None:
        standings = character_standings(self.rankings, self.name)

        page = build_character_standings_page(
            standings, query=self.name, web_base_url=WEB, age_seconds=4_000
        )

        self.assertEqual(page.header.target_type, "角色排名")
        self.assertEqual(page.character_name, self.name)
        self.assertEqual(len(page.rows), 2)
        self.assertEqual(page.rows[0].rank, 1)
        self.assertEqual(page.first_places, 2)
        self.assertEqual(page.rows[0].total_rows, len(self.rankings[0].rows))
        self.assertEqual(len(page.rows[0].roster), 4)
        self.assertEqual(page.as_of_label, "数据截至 1 小时 6 分钟前")
        self.assertEqual(page.absent, ())

    def test_the_template_renders_the_rows_and_the_folded_boards(self) -> None:
        standings = character_standings(self.rankings[:1], self.name)
        standings_absent = character_standings(self.rankings, "没有这个人")
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])

        html = renderer.render_character_standings(
            standings, query=self.name, web_base_url=WEB, age_seconds=30
        )
        empty = renderer.render_character_standings(
            standings_absent, query="没有这个人", web_base_url=WEB
        )

        self.assertIn("各榜单最好名次", html)
        self.assertIn("三位一体", html)
        self.assertIn("数据刚刚更新", html)
        self.assertIn("没有出场", empty)
        self.assertIn("罗丹", empty)


if __name__ == "__main__":
    unittest.main()
