import unittest

from core.matcher import (
    AliasConfig,
    MatchStatus,
    RankingMatcher,
    TargetType,
    normalize_search_text,
)

from tests.helpers import make_card


def _cards():
    return (
        make_card(
            "dung01_group_bossrush02",
            "危境再现·三位一体",
            "危境再现 · 测试区",
        ),
        make_card("phase1", "首领一", "影拓丰碑1期 · 区域一"),
        make_card("phase2", "首领二", "影拓丰碑2期 · 区域二"),
        make_card("phase3", "首领三", "影拓丰碑3期 · 区域三"),
        make_card("phase4", "首领四", "影拓丰碑4期 · 山中见犼"),
    )


def _aliases():
    return AliasConfig(
        boards={"dung01_group_bossrush02": ("三位", "31")},
        dungeons={
            "影拓丰碑4期 · 山中见犼": (
                "影拓丰碑4期",
                "丰碑4",
                "影拓4",
                "山中见犼",
            )
        },
    )


class MatcherTests(unittest.TestCase):
    def test_normalization_handles_chinese_phase_and_punctuation(self) -> None:
        self.assertEqual(
            normalize_search_text(" 影拓丰碑四期 · 榜单 "),
            "影拓丰碑4期",
        )

    def test_slug_and_board_aliases_match_one_board(self) -> None:
        matcher = RankingMatcher(_cards(), _aliases())
        for query in ("三位", "31", "dung01_group_bossrush02"):
            with self.subTest(query=query):
                result = matcher.match(query)
                self.assertEqual(result.status, MatchStatus.MATCHED)
                self.assertIsNotNone(result.selected)
                self.assertEqual(
                    result.selected.target.target_type,
                    TargetType.BOARD,
                )
                self.assertEqual(
                    result.selected.target.key,
                    "dung01_group_bossrush02",
                )

    def test_phase_alias_only_matches_phase_four_dungeon(self) -> None:
        matcher = RankingMatcher(_cards(), _aliases())
        for query in ("影拓4", "丰碑4", "山中见犼"):
            with self.subTest(query=query):
                result = matcher.match(query)
                self.assertEqual(result.status, MatchStatus.MATCHED)
                self.assertEqual(
                    result.selected.target.target_type,
                    TargetType.DUNGEON,
                )
                self.assertEqual(result.selected.target.boss_slugs, ("phase4",))

    def test_family_query_builds_a_scope_in_source_order(self) -> None:
        matcher = RankingMatcher(_cards(), _aliases())
        result = matcher.match("影拓")

        self.assertEqual(result.status, MatchStatus.MATCHED)
        self.assertEqual(
            result.selected.target.target_type,
            TargetType.DUNGEON_SCOPE,
        )
        self.assertEqual(
            result.selected.target.boss_slugs,
            ("phase1", "phase2", "phase3", "phase4"),
        )
        self.assertEqual(len(result.selected.target.dungeon_names), 4)

    def test_single_board_dungeon_query_opens_the_concrete_ranking(self) -> None:
        for boss_name in ("危机合约", "破潮之像"):
            with self.subTest(boss_name=boss_name):
                cards = (
                    make_card("crisis-contract", boss_name, "危机合约"),
                )
                matcher = RankingMatcher(cards, AliasConfig.empty())

                result = matcher.match("危机合约")

                self.assertEqual(result.status, MatchStatus.MATCHED)
                self.assertEqual(
                    result.selected.target.target_type,
                    TargetType.BOARD,
                )
                self.assertEqual(result.selected.target.key, "crisis-contract")

    def test_multi_board_dungeon_query_keeps_the_top_three_page(self) -> None:
        cards = (
            make_card("crisis-one", "首领一", "危机合约"),
            make_card("crisis-two", "首领二", "危机合约"),
        )
        matcher = RankingMatcher(cards, AliasConfig.empty())

        result = matcher.match("危机合约")

        self.assertEqual(result.status, MatchStatus.MATCHED)
        self.assertEqual(result.selected.target.target_type, TargetType.DUNGEON)
        self.assertEqual(
            result.selected.target.boss_slugs,
            ("crisis-one", "crisis-two"),
        )

    def test_short_non_unique_query_is_ambiguous(self) -> None:
        cards = (
            make_card("one", "巨像一", "测试副本一"),
            make_card("two", "巨像二", "测试副本二"),
        )
        matcher = RankingMatcher(cards, AliasConfig.empty())
        result = matcher.match(
            "巨",
            allowed_types=frozenset({TargetType.BOARD}),
        )

        self.assertEqual(result.status, MatchStatus.AMBIGUOUS)
        self.assertEqual(len(result.candidates), 2)

    def test_invalid_alias_targets_and_collisions_are_reported(self) -> None:
        aliases = AliasConfig(
            boards={
                "missing": ("重复",),
                "one": ("重复",),
                "two": ("重复",),
            },
            dungeons={},
        )
        cards = (
            make_card("one", "首领一", "测试副本"),
            make_card("two", "首领二", "测试副本"),
        )
        matcher = RankingMatcher(cards, aliases)

        self.assertTrue(
            any("target does not exist" in issue for issue in matcher.issues)
        )
        self.assertTrue(
            any("multiple targets" in issue for issue in matcher.issues)
        )


if __name__ == "__main__":
    unittest.main()
