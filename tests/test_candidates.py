import unittest

from core.candidates import (
    CandidateStore,
    describe_choice,
    extract_code,
    format_candidates,
    parse_selection,
)
from core.matcher import (
    AliasConfig,
    MatchChoice,
    MatchLevel,
    MatchStatus,
    MatchTarget,
    RankingMatcher,
    TargetType,
)
from core.persistence import load_json, save_json
from tests.helpers import make_card


def _ambiguous_choices():
    cards = (
        make_card("one", "巨像一", "测试副本一"),
        make_card("two", "巨像二", "测试副本二"),
    )
    result = RankingMatcher(cards, AliasConfig.empty()).match("巨")
    assert result.status is MatchStatus.AMBIGUOUS
    return result.candidates


class CandidateAbuseTests(unittest.TestCase):
    def _account_choice(self, name: str, key: str = "usr_x") -> MatchChoice:
        return MatchChoice(
            target=MatchTarget(
                target_type=TargetType.ACCOUNT,
                key=key,
                name=name,
                dungeon_names=(),
                boss_slugs=(),
                query_text=name,
            ),
            level=MatchLevel.STANDARD_EXACT,
            score=1.0,
            matched_text=name,
        )

    def test_a_nickname_spelling_a_marker_cannot_hijack_the_code(self) -> None:
        # The choice lines print upstream nicknames before the real marker
        # line, so the code must be read from the last marker, not the first.
        store = CandidateStore()
        entry = store.remember(
            "cpu",
            (
                self._account_choice("候选编号 ABCD"),
                self._account_choice("候选编号AAAA x"),
            ),
        )

        self.assertEqual(extract_code(format_candidates(entry)), entry.code)

    def test_a_code_only_works_in_the_chat_that_received_it(self) -> None:
        store = CandidateStore()
        entry = store.remember(
            "cpu", (self._account_choice("CPU 0"),), origin="aiocqhttp:GroupMessage:1"
        )

        self.assertIsNotNone(
            store.resolve(entry.code, "1", origin="aiocqhttp:GroupMessage:1")
        )
        self.assertIsNone(
            store.resolve(entry.code, "1", origin="aiocqhttp:GroupMessage:2")
        )
        self.assertIsNone(store.resolve(entry.code, "1", origin=""))
        # Callers that do not track origins keep the old behaviour.
        self.assertIsNotNone(store.resolve(entry.code, "1"))

    def test_nicknames_stay_on_one_line(self) -> None:
        text = describe_choice(self._account_choice("a\nb\r\n  c"))

        self.assertEqual(text, "a b c · 公开账号")


class CandidateStoreTests(unittest.TestCase):
    def test_remember_format_and_resolve_by_quote(self) -> None:
        store = CandidateStore(ttl_seconds=60)
        entry = store.remember("巨", _ambiguous_choices(), ranking_top=5, now=0.0)
        text = format_candidates(entry, ttl_seconds=60)

        self.assertIn("「巨」匹配到", text)
        self.assertIn("1. 巨像一 · 榜单 · 测试副本一", text)
        self.assertTrue(text.rstrip().endswith(f"候选编号 {entry.code} · 1 分钟内有效"))
        self.assertEqual(extract_code(text), entry.code)
        self.assertIsNone(extract_code("随便什么 候选编号 12"))

        resolved = store.resolve(entry.code, "2", now=1.0)
        self.assertIsNotNone(resolved)
        pending, choice = resolved
        self.assertEqual(pending.ranking_top, 5)
        self.assertEqual(choice.target.key, "two")
        self.assertIsNone(store.resolve(entry.code, "3", now=1.0))
        self.assertIsNone(store.resolve("ZZZZ", "1", now=1.0))
        self.assertIsNone(store.resolve(entry.code, "1", now=61.0))

    def test_selection_parsing(self) -> None:
        for text, expected in (
            ("1", 1),
            (" 选 2 ", 2),
            ("第3个", 3),
            ("②", 2),
            ("２", 2),
            ("2.", 2),
            ("3。", 3),
            ("0", None),
            ("6", None),
            ("罗丹", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_selection(text), expected)


class PersistenceTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "snapshot.json"
            self.assertTrue(save_json(path, {"a": ["甲", 1]}))
            self.assertEqual(load_json(path), {"a": ["甲", 1]})
            self.assertIsNone(load_json(Path(directory) / "missing.json"))
            self.assertEqual(list(Path(directory, "nested").iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
