"""The LLM tool surface: what the model is handed, and what it is not."""

import asyncio
import logging
import unittest

from core import facts
from core.matcher import AliasConfig, MatcherCache
from core.models import (
    parse_battle_detail,
    parse_boss_ranking,
    parse_hot_bosses,
    parse_public_user_rankings,
)
from core.settings import PluginSettings
from core.toolbox import ToolAnswer, ToolService
from tests.helpers import (
    battle_detail_payload,
    hot_bosses_payload,
    public_user_rankings_payload,
    ranking_payload_with_rows,
)
from tests.test_compare import second_battle_payload

WEB = "https://zmdlogs.com"


def run(coro):
    return asyncio.run(coro)


class FakeRenderer:
    """Records what a tool asked to draw and hands back a path."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def _page(self, kind):
        async def render(*args, **kwargs):
            self.calls.append(kind)
            if self.fail:
                raise RuntimeError("no chromium")
            return f"/tmp/{kind}.png"

        return render

    def __getattr__(self, name):
        if name.startswith("render_"):
            return self._page(name.removeprefix("render_"))
        raise AttributeError(name)


class FakeData:
    def __init__(self, ranking=None, battles=None, account=None) -> None:
        self.cards = parse_hot_bosses(hot_bosses_payload())
        self.ranking = ranking
        self.battles = battles or {}
        self.account = account

    async def list_hot_bosses(self):
        return self.cards

    async def get_boss_ranking(self, slug):
        return self.ranking

    async def get_battle_detail(self, battle_id):
        return self.battles[battle_id]

    async def get_battle_export(self, battle_id):
        from core.client import ZmdLogsClientError

        raise ZmdLogsClientError("offline")

    async def get_equip_suits(self):
        return {}

    async def get_public_user_rankings(self, account_id):
        return self.account


class ToolServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ranking = parse_boss_ranking(ranking_payload_with_rows())
        self.battles = {
            "btl_upload_abcdef123456": parse_battle_detail(battle_detail_payload()),
            "btl_upload_bbbbbbbbbbbb": parse_battle_detail(second_battle_payload()),
        }
        self.data = FakeData(
            ranking=self.ranking,
            battles=self.battles,
            account=parse_public_user_rankings(public_user_rankings_payload()),
        )
        self.renderer = FakeRenderer()
        self.matchers = MatcherCache()
        self.service = ToolService(
            client=None,
            data=self.data,
            renderer=lambda: self.renderer,
            board_matcher=lambda cards: self.matchers.matcher_for(
                cards, AliasConfig.empty()
            ),
            settings=PluginSettings(web_base_url=WEB),
            logger=logging.getLogger("test"),
        )

    def test_a_board_answer_carries_both_the_page_and_the_facts(self) -> None:
        answer = run(self.service.board("三位一体", limit=2))

        self.assertEqual(self.renderer.calls, ["ranking"])
        self.assertEqual(answer.image_path, "/tmp/ranking.png")
        # The text names the board and the records; the numbers are in both.
        self.assertIn("公开记录", answer.text)
        self.assertIn("battleId", answer.text)

    def test_a_weak_keyword_is_refused_rather_than_guessed(self) -> None:
        # A command shows the closest board and lets the reader judge. A tool
        # has no reader in the loop, so a guess becomes the model's answer.
        # "一体三位" scrambles a real board name into a below-threshold
        # similarity hit, which a command would have rendered anyway.
        answer = run(self.service.board("一体三位"))

        self.assertIsNone(answer.image_path)
        self.assertIn("没有可靠匹配", answer.text)
        self.assertIn("最接近的是", answer.text)
        self.assertEqual(self.renderer.calls, [])

    def test_a_name_that_matches_nothing_at_all_is_reported_plainly(self) -> None:
        answer = run(self.service.board("完全不沾边的名字"))

        self.assertIsNone(answer.image_path)
        self.assertIn("没有找到", answer.text)
        self.assertEqual(self.renderer.calls, [])

    def test_an_unknown_character_says_so_instead_of_drawing(self) -> None:
        answer = run(self.service.board("三位一体", character="不存在的角色"))

        self.assertIsNone(answer.image_path)
        self.assertIn("没有", answer.text)
        self.assertEqual(self.renderer.calls, [])

    def test_a_battle_needs_a_reference_not_a_rank(self) -> None:
        answer = run(self.service.battle("第一名"))

        self.assertIsNone(answer.image_path)
        self.assertIn("battleId", answer.text)
        self.assertEqual(self.renderer.calls, [])

    def test_a_battle_answer_reduces_the_telemetry_to_facts(self) -> None:
        answer = run(self.service.battle("btl_upload_abcdef123456"))

        self.assertEqual(answer.image_path, "/tmp/battle.png")
        self.assertIn("参战角色", answer.text)
        self.assertIn("配装与养成", answer.text)
        # Never the raw arrays behind those lines.
        self.assertNotIn("tsMsFromStart", answer.text)
        self.assertLess(len(answer.text), 3_000)

    def test_comparing_a_battle_with_itself_is_refused(self) -> None:
        answer = run(
            self.service.compare(
                "btl_upload_abcdef123456", "btl_upload_abcdef123456"
            )
        )

        self.assertIsNone(answer.image_path)
        self.assertIn("同一场", answer.text)

    def test_a_comparison_names_the_differences_but_not_a_cause(self) -> None:
        answer = run(
            self.service.compare(
                "btl_upload_abcdef123456", "btl_upload_bbbbbbbbbbbb"
            )
        )

        self.assertEqual(answer.image_path, "/tmp/compare.png")
        self.assertIn("用时", answer.text)
        self.assertIn("无法判定", answer.text)

    def test_an_account_answer_lists_its_records(self) -> None:
        answer = run(self.service.account("usr_1234567890abcdef"))

        self.assertEqual(answer.image_path, "/tmp/account.png")
        self.assertIn("上榜", answer.text)

    def test_a_page_that_will_not_draw_still_answers_in_text(self) -> None:
        self.renderer.fail = True
        answer = run(self.service.board("三位一体"))

        self.assertIsNone(answer.image_path)
        self.assertIn("公开记录", answer.text)


class FactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ranking = parse_boss_ranking(ranking_payload_with_rows())
        self.battle = parse_battle_detail(battle_detail_payload())

    def test_a_filter_that_matches_nothing_says_nothing_matched(self) -> None:
        text = facts.format_board_ranking(self.ranking, character="没有这个人")

        self.assertIn("没有符合的公开记录", text)

    def test_partner_counts_refuse_to_read_as_strength(self) -> None:
        name = self.ranking.rows[0].roster_entries[0].character_name
        text = facts.format_character_partners([self.ranking], name)

        self.assertIn("最常同队", text)
        # The disclaimer is the point of the function, not decoration.
        self.assertIn("不代表这些角色或组合更强", text)

    def test_a_character_nobody_played_is_reported_as_absent(self) -> None:
        text = facts.format_character_partners([self.ranking], "没有这个人")

        self.assertIn("没有", text)
        self.assertNotIn("最常同队", text)

    def test_row_limits_are_bounded_whatever_is_asked_for(self) -> None:
        for asked in (0, -5, 999):
            with self.subTest(asked=asked):
                text = facts.format_board_ranking(self.ranking, limit=asked)
                shown = text.count("battleId")
                self.assertGreaterEqual(shown, 1)
                self.assertLessEqual(shown, facts.MAX_ROW_LIMIT)

    def test_cross_boss_comparison_is_refused_with_a_reason(self) -> None:
        other = parse_battle_detail(battle_detail_payload())
        object.__setattr__(other, "boss_name", "别的首领")
        text = facts.format_battle_comparison(self.battle, other)

        self.assertIn("不是同一个首领", text)
        self.assertIn("没有可比性", text)


class ToolAnswerTests(unittest.TestCase):
    def test_an_answer_always_has_text(self) -> None:
        self.assertEqual(ToolAnswer("说点什么").image_path, None)


if __name__ == "__main__":
    unittest.main()
