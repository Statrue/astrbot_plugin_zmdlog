"""角色冠军榜: every character's first places counted over all boards."""

import unittest
from pathlib import Path

from core import facts
from core.models import parse_boss_ranking
from core.presentation import build_character_champions_page
from core.render import TemplateRenderer
from core.standings import character_tallies
from tests.helpers import ranking_payload_with_rows

WEB = "https://zmdlogs.com"


def ranking(slug: str, boss_name: str, *, rows: int | None = None):
    payload = ranking_payload_with_rows()
    payload["bossSlug"] = slug
    payload["bossName"] = boss_name
    if rows is not None:
        payload["rows"] = payload["rows"][:rows]
    return parse_boss_ranking(payload)


class TalliesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rankings = (
            ranking("dung01_group_bossrush02", "三位一体"),
            ranking("dung01_group_bossrush03", "罗丹", rows=3),
        )
        self.leader = self.rankings[0].rows[0]

    def test_the_first_record_members_each_get_a_first_place(self) -> None:
        tallies = character_tallies(self.rankings)
        by_name = {tally.name: tally for tally in tallies}

        for entry in self.leader.roster_entries:
            with self.subTest(character=entry.character_name):
                # Both fixture boards share the same #1 record.
                self.assertEqual(by_name[entry.character_name].first_places, 2)
        self.assertEqual(by_name[self.leader.character_name].first_places_as_main, 2)
        # Only the main C of the #1 record counts as its champion.
        others = [
            e.character_name
            for e in self.leader.roster_entries
            if e.character_name != self.leader.character_name
        ]
        self.assertTrue(all(by_name[name].first_places_as_main == 0 for name in others))

    def test_tallies_sort_by_champions_then_first_place_teams(self) -> None:
        tallies = character_tallies(self.rankings)

        # The #1 record's main C is the champion and leads; its team mates
        # follow on first-place teams; everyone else after.
        self.assertEqual(tallies[0].name, self.leader.character_name)
        mains = [tally.first_places_as_main for tally in tallies]
        self.assertEqual(mains, sorted(mains, reverse=True))

    def test_podiums_and_top_tens_count_boards_not_records(self) -> None:
        tallies = character_tallies(self.rankings)
        by_name = {tally.name: tally for tally in tallies}

        for tally in by_name.values():
            with self.subTest(character=tally.name):
                self.assertLessEqual(tally.first_places, tally.podiums)
                self.assertLessEqual(tally.podiums, tally.top_tens)
                self.assertLessEqual(tally.top_tens, tally.boards)
                self.assertLessEqual(tally.boards, len(self.rankings))

    def test_the_text_states_the_counting_rule_and_the_leader(self) -> None:
        tallies = character_tallies(self.rankings)

        text = facts.format_character_tallies(tallies, board_count=2, age_seconds=30)

        self.assertIn("全部 2 个榜", text)
        self.assertIn("四名角色各算一个", text)
        self.assertIn(f"冠军最多：{self.leader.character_name} 2 个榜", text)
        self.assertIn(f"{self.leader.character_name} · 冠军 2 · 第一名队伍 2", text)
        self.assertIn("不代表哪个角色更强", text)


class ChampionsPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rankings = (
            ranking("dung01_group_bossrush02", "三位一体"),
            ranking("dung01_group_bossrush03", "罗丹", rows=3),
        )
        self.tallies = character_tallies(self.rankings)

    def test_the_page_ranks_characters_and_folds_the_rest(self) -> None:
        page = build_character_champions_page(
            self.tallies,
            board_count=2,
            query="角色排名",
            web_base_url=WEB,
            age_seconds=90,
        )

        self.assertEqual(page.header.target_type, "角色排名")
        self.assertEqual(page.board_count, 2)
        self.assertEqual(page.rows[0].position, 1)
        self.assertEqual(page.rows[0].bar_width, 100.0)
        self.assertEqual(page.top_main.name, self.rankings[0].rows[0].character_name)
        self.assertTrue(all(row.podiums >= 1 for row in page.rows))
        self.assertEqual(
            len(page.rows) + len(page.others), len(self.tallies)
        )
        self.assertEqual(page.as_of_label, "数据截至 1 分钟前")

    def test_the_template_renders(self) -> None:
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])

        html = renderer.render_character_champions(
            self.tallies, board_count=2, query="角色排名", web_base_url=WEB
        )

        self.assertIn("角色冠军榜", html)
        self.assertIn("当主 C", html)
        self.assertIn(self.rankings[0].rows[0].character_name, html)


if __name__ == "__main__":
    unittest.main()
