import random
import unittest
from difflib import SequenceMatcher

from core.matcher import (
    AliasConfig,
    MatcherCache,
    MatchLevel,
    MatchStatus,
    RankingMatcher,
    TargetType,
    _partial_similarity,
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
        make_card("phase4b", "首领五", "影拓丰碑4期 · 山中见犼"),
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
    def test_over_long_queries_skip_the_fuzzy_scan(self) -> None:
        # The sliding-window similarity is linear in the query and runs on the
        # event loop; a multi-kilobyte message must not turn into seconds of
        # matching (or be echoed back). Nothing real is longer than the cap.
        import time

        from core.matcher import MAX_QUERY_LENGTH

        matcher = RankingMatcher(_cards(), _aliases())
        started = time.perf_counter()
        result = matcher.match("首" * (MAX_QUERY_LENGTH + 1))
        elapsed = time.perf_counter() - started

        self.assertIs(result.status, MatchStatus.NOT_FOUND)
        self.assertLess(elapsed, 0.05)
        # Garbage exactly at the cap still gets the full (bounded) treatment.
        started = time.perf_counter()
        matcher.match("首" * MAX_QUERY_LENGTH)
        self.assertLess(time.perf_counter() - started, 2.0)

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
                self.assertEqual(
                    result.selected.target.boss_slugs,
                    ("phase4", "phase4b"),
                )

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
            ("phase1", "phase2", "phase3", "phase4", "phase4b"),
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


class DerivedAliasTests(unittest.TestCase):
    def _live_like_cards(self):
        return (
            make_card("dung01_group_bossrush01", "危境再现·罗丹", "危境再现"),
            make_card("dung01_group_bossrush03", "危境再现·白垩界卫", "危境再现"),
            make_card("indie_hard022_s", "撼山雾火·苦难", "影拓丰碑4期 · 山中见犼"),
            make_card("indie_hard024_s", "山犼争王·苦难", "影拓丰碑4期 · 山中见犼"),
            make_card("indie_hard008_s", "怨憎雾海·苦难", "影拓丰碑1期 · 灼痛疤痕"),
            make_card("indie_hard002_s", "矢影环伺·苦难", "影拓丰碑1期 · 无机造物"),
            make_card("indie_battletower004_ex", "斧柄纪年·残酷", "战争回响"),
        )

    def test_derived_aliases_strip_prefix_suffix_and_split_dungeons(self) -> None:
        from core.matcher import derived_board_aliases, derived_dungeon_aliases

        self.assertEqual(
            derived_board_aliases("危境再现·罗丹", "危境再现"),
            ("罗丹", "危境再现·罗丹"),
        )
        self.assertEqual(
            derived_board_aliases("山犼争王·苦难", "影拓丰碑4期 · 山中见犼"),
            ("山犼争王",),
        )
        self.assertEqual(
            derived_dungeon_aliases("影拓丰碑4期 · 山中见犼"),
            ("影拓丰碑4期", "山中见犼", "影拓丰碑4", "影拓4", "丰碑4"),
        )
        self.assertEqual(derived_dungeon_aliases("战争回响"), ())

    def test_stripped_names_hit_boards_without_configured_aliases(self) -> None:
        matcher = RankingMatcher(self._live_like_cards(), AliasConfig.empty())
        for query, slug in (
            ("罗丹", "dung01_group_bossrush01"),
            ("山犼争王", "indie_hard024_s"),
            ("斧柄纪年", "indie_battletower004_ex"),
            ("罗丹榜单", "dung01_group_bossrush01"),
        ):
            with self.subTest(query=query):
                result = matcher.match(query)
                self.assertEqual(result.status, MatchStatus.MATCHED)
                self.assertEqual(result.selected.target.key, slug)

    def test_phase_alias_spanning_dungeons_becomes_a_scope(self) -> None:
        matcher = RankingMatcher(self._live_like_cards(), AliasConfig.empty())
        result = matcher.match("丰碑1")

        self.assertEqual(result.status, MatchStatus.MATCHED)
        self.assertEqual(
            result.selected.target.target_type,
            TargetType.DUNGEON_SCOPE,
        )
        self.assertEqual(
            result.selected.target.dungeon_names,
            ("影拓丰碑1期 · 灼痛疤痕", "影拓丰碑1期 · 无机造物"),
        )
        single = matcher.match("丰碑4")
        self.assertEqual(single.selected.target.target_type, TargetType.DUNGEON)

    def test_pinyin_initials_and_full_pinyin_match(self) -> None:
        try:
            import pypinyin  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("pypinyin not installed")
        matcher = RankingMatcher(self._live_like_cards(), AliasConfig.empty())
        for query in ("ld", "luodan", "LD", "fb4"):
            with self.subTest(query=query):
                result = matcher.match(query)
                self.assertEqual(result.status, MatchStatus.MATCHED)
        self.assertEqual(
            matcher.match("ld").selected.target.key,
            "dung01_group_bossrush01",
        )
        self.assertEqual(
            matcher.match("fb4").selected.target.target_type,
            TargetType.DUNGEON,
        )


class HomophoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = RankingMatcher(
            (
                make_card("thunder", "蚀影噪雷", "危境碎片"),
                make_card("rodin", "危境再现·罗丹", "危境再现"),
                make_card("other", "其他首领", "别的副本"),
            ),
            AliasConfig.empty(),
        )

    def test_wrong_character_with_same_reading_still_matches(self) -> None:
        whole = self.matcher.match("罗单")
        self.assertEqual(whole.status, MatchStatus.MATCHED)
        self.assertEqual(whole.selected.target.key, "rodin")
        self.assertEqual(whole.selected.level, MatchLevel.PINYIN_EXACT)

        tail = self.matcher.match("躁雷")
        self.assertEqual(tail.status, MatchStatus.MATCHED)
        self.assertEqual(tail.selected.target.key, "thunder")
        # Correct spelling must still rank above the homophone.
        self.assertLess(
            tail.selected.score,
            self.matcher.match("噪雷").selected.score,
        )

    def test_single_weak_hit_is_shown_instead_of_a_one_item_pick_list(self) -> None:
        matcher = RankingMatcher(
            (make_card("only", "完全不同的名字", "某副本"),),
            AliasConfig.empty(),
            fuzzy_threshold=0.99,
        )
        result = matcher.match("完全不")
        self.assertEqual(result.status, MatchStatus.MATCHED)
        self.assertEqual(result.selected.target.key, "only")


def _naive_partial_similarity(left: str, right: str) -> float:
    """The original one-matcher-per-window scan, kept as the oracle."""

    if not left or not right:
        return 0.0
    shorter, longer = sorted((left, right), key=len)
    return max(
        SequenceMatcher(
            None,
            shorter,
            longer[index : index + len(shorter)],
            autojunk=False,
        ).ratio()
        for index in range(len(longer) - len(shorter) + 1)
    )


class SimilarityTests(unittest.TestCase):
    def test_bounded_window_scan_returns_the_naive_maximum(self) -> None:
        # quick_ratio only ever skips windows that cannot beat the best so
        # far, so the fast scan must agree with the exhaustive one bit for bit.
        rng = random.Random(20260905)
        alphabet = "abcdefg山中见犼丰碑影拓罗丹噪雷1234"
        pairs = [
            ("", "abc"),
            ("abc", "abc"),
            ("罗丹", "罗丹苦难"),
            ("luodan", "abcdefghijklmnopqrstuvwxyz0123456789" * 2),
        ]
        for _ in range(400):
            pairs.append(
                (
                    "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12))),
                    "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40))),
                )
            )
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertEqual(
                    _partial_similarity(left, right),
                    _naive_partial_similarity(left, right),
                )


class MatcherCacheTests(unittest.TestCase):
    def test_matcher_is_reused_until_cards_or_aliases_change(self) -> None:
        issues: list[str] = []
        cache = MatcherCache(warn=issues.append)
        cards = _cards()
        aliases = AliasConfig(boards={"missing": ("x",)}, dungeons={})

        first = cache.matcher_for(cards, aliases)
        self.assertIs(cache.matcher_for(cards, aliases), first)
        # Index problems are reported when the index is built, not per query.
        self.assertEqual(issues, ["board alias target does not exist: missing"])

        rebuilt = cache.matcher_for(_cards(), aliases)
        self.assertIsNot(rebuilt, first)
        self.assertEqual(len(issues), 2)

        # Identity, not equality: an alias edit installs a new AliasConfig.
        empty = AliasConfig.empty()
        swapped = cache.matcher_for(rebuilt.cards, empty)
        self.assertIsNot(swapped, rebuilt)
        self.assertEqual(len(issues), 2)
        self.assertIs(cache.matcher_for(rebuilt.cards, empty), swapped)
        self.assertIsNot(cache.matcher_for(rebuilt.cards, AliasConfig.empty()), swapped)

    def test_thresholds_are_validated_once_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            MatcherCache(fuzzy_threshold=65)


class AliasPrecedenceTests(unittest.TestCase):
    """An admin alias must be able to pin a name both difficulties derive."""

    @staticmethod
    def _cards():
        dungeon = "影拓丰碑4期 · 山中见犼"
        return (
            make_card("kunan", "清波访客·苦难", dungeon),
            make_card("cankuk", "清波访客·残酷", dungeon),
        )

    def test_two_difficulties_derive_the_same_alias_and_tie(self) -> None:
        result = RankingMatcher(self._cards(), AliasConfig.empty()).match("清波访客")

        self.assertEqual(result.status, MatchStatus.AMBIGUOUS)

    def test_a_configured_alias_outranks_the_derived_one(self) -> None:
        aliases = AliasConfig(boards={"kunan": ("清波访客",)}, dungeons={})

        result = RankingMatcher(self._cards(), aliases).match("清波访客")

        self.assertEqual(result.status, MatchStatus.MATCHED)
        self.assertEqual(result.selected.target.key, "kunan")
        self.assertEqual(result.selected.level, MatchLevel.ALIAS_EXACT)

    def test_a_fuzzy_hit_below_the_floor_is_a_miss(self) -> None:
        # 一二三四 shares one character with 首领一; that used to be a 0.33
        # "closest board" instead of a miss.
        result = RankingMatcher(_cards(), AliasConfig.empty()).match("一二三四")

        self.assertEqual(result.status, MatchStatus.NOT_FOUND)
