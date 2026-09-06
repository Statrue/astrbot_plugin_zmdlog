"""The LLM tool surface: what the model is handed, and what it is not."""

import asyncio
import logging
import unittest

from core import facts
from core.datasource import CharacterCatalogEntry
from core.matcher import AliasConfig, MatcherCache
from core.models import (
    parse_battle_detail,
    parse_boss_ranking,
    parse_character_boss_statistics,
    parse_character_statistics,
    parse_hot_bosses,
    parse_public_user_rankings,
)
from core.ranking_index import IndexEntry
from core.settings import PluginSettings
from core.toolbox import ToolAnswer, ToolService
from tests.helpers import (
    battle_detail_payload,
    character_boss_statistics_payload,
    character_statistics_payload,
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


class FakeIndex:
    """The ranking index as the tools see it: already filled, one board."""

    def __init__(self, ranking) -> None:
        self._entries = () if ranking is None else (IndexEntry(ranking, 0.0),)

    async def ensure_filled(self) -> None:
        return None

    def entries(self):
        return self._entries

    def oldest_age_seconds(self):
        return 42.0 if self._entries else None


class FakeData:
    def __init__(self, ranking=None, battles=None, account=None) -> None:
        self.cards = parse_hot_bosses(hot_bosses_payload())
        self.ranking = ranking
        self.battles = battles or {}
        self.account = account
        self.ranking_index = FakeIndex(ranking)

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

    async def get_character_types(self):
        from core.models import CharacterType

        lead = self.ranking.rows[0].character_name if self.ranking else "洛茜"
        return {
            lead: CharacterType(lead, "自然", "手铳", "近卫"),
            # A catalog character of the same class that no record fields.
            "未上榜者": CharacterType("未上榜者", "物理", "长枪", "近卫"),
        }

    async def index_rows_for(self, battle_ids):
        return {}, None

    async def character_elements(self):
        types = await self.get_character_types()
        return {name: entry.element for name, entry in types.items()}

    async def character_professions(self):
        types = await self.get_character_types()
        return {name: entry.profession for name, entry in types.items()}

    catalog_refreshes = 0

    async def get_character_catalog(self, *, refresh=False):
        if refresh:
            self.catalog_refreshes += 1
        return (
            CharacterCatalogEntry("洛茜", "chr_0001_luoxi"),
            CharacterCatalogEntry("提弗洛斯", "chr_0002_tifu"),
        )

    async def get_character_boss_statistics(self, key, *, time_range, potential):
        return parse_character_boss_statistics(character_boss_statistics_payload())

    class _EmptyLog:
        def recent(self, *, kind=None, since=None):
            return ()

        def oldest_seen_at(self):
            return None

    event_log = _EmptyLog()


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
        # Team counts ride along instead of being a tool of their own.
        self.assertIn("常见阵容", answer.text)

    def test_a_character_filter_also_counts_that_characters_partners(self) -> None:
        name = self.ranking.rows[0].roster_entries[0].character_name
        answer = run(self.service.board("三位一体", character=name))

        self.assertEqual(answer.image_path, "/tmp/ranking.png")
        self.assertIn(f"阵容包含「{name}」", answer.text)
        self.assertIn("最常同队", answer.text)
        self.assertNotIn("常见阵容（全榜", answer.text)

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
            self.service.battle(
                "btl_upload_abcdef123456", compare_with="btl_upload_abcdef123456"
            )
        )

        self.assertIsNone(answer.image_path)
        self.assertIn("同一场", answer.text)

    def test_a_comparison_names_the_differences_but_not_a_cause(self) -> None:
        # The second reference turns the battle tool into the comparison.
        answer = run(
            self.service.battle(
                "btl_upload_abcdef123456", compare_with="btl_upload_bbbbbbbbbbbb"
            )
        )

        self.assertEqual(answer.image_path, "/tmp/compare.png")
        self.assertIn("用时", answer.text)
        self.assertIn("无法判定", answer.text)

    def test_an_account_answer_lists_its_records(self) -> None:
        answer = run(self.service.account("usr_1234567890abcdef"))

        self.assertEqual(answer.image_path, "/tmp/account.png")
        self.assertIn("上榜", answer.text)

    def test_a_six_star_without_records_gets_the_distribution_page(self) -> None:
        # 提弗洛斯 is in the catalog but in no fixture roster: no standings to
        # draw, so the answer is the DPS distribution alone.
        answer = run(self.service.character("提弗洛斯"))

        self.assertEqual(answer.image_path, "/tmp/character_boss.png")
        self.assertIn("各榜单表现", answer.text)
        # The catalog knew the name, so no refresh was asked for.
        self.assertEqual(self.data.catalog_refreshes, 0)

    def test_a_fielded_character_gets_the_standings_page_first(self) -> None:
        name = self.ranking.rows[0].roster_entries[0].character_name

        answer = run(self.service.character(name))

        self.assertEqual(answer.image_path, "/tmp/character_standings.png")
        self.assertIn("各榜单的最好名次", answer.text)
        self.assertIn("battleId", answer.text)
        # Not a six-star in the catalog: no distribution lines are appended.
        self.assertNotIn("各榜单表现", answer.text)

    def test_an_element_narrows_the_board_and_the_champions(self) -> None:
        lead = self.ranking.rows[0].character_name

        board = run(self.service.board("三位一体", element="自然"))
        champions = run(self.service.character("", element="自然"))
        nobody = run(self.service.board("三位一体", element="雷"))
        nonsense = run(self.service.board("三位一体", element="光"))

        self.assertIn("主C 为自然属性", board.text)
        self.assertEqual(board.image_path, "/tmp/ranking.png")
        self.assertIn("自然属性角色", champions.text)
        self.assertIn(lead, champions.text)
        self.assertIsNone(nobody.image_path)
        self.assertIn("没有主C 为电磁属性", nobody.text)
        self.assertIn("不是属性", nonsense.text)

    def test_a_profession_lists_the_whole_class_with_its_zeros(self) -> None:
        # 2026-09-07: 谁是冠军最少的突击 was answered 没法拍板 while two 突击
        # sat at zero, unnamed by the text.
        lead = self.ranking.rows[0].character_name

        guards = run(self.service.character("", profession="近卫"))
        casters = run(self.service.character("", profession="术师"))
        nonsense = run(self.service.character("", profession="刺客"))

        self.assertIn("近卫角色", guards.text)
        self.assertIn(lead, guards.text)
        self.assertIn("冠军最少：", guards.text)
        self.assertIn("从未出现在公开记录里的近卫角色：未上榜者", guards.text)
        self.assertEqual(guards.image_path, "/tmp/character_champions.png")
        self.assertIn("术士角色", casters.text)
        self.assertIn("不是职业", nonsense.text)

    def test_a_range_narrows_the_champions_board_to_a_window(self) -> None:
        windowed = run(self.service.character("", time_range="7d"))
        odd = run(self.service.character("", time_range="上个世纪"))

        self.assertIn("近 7 天各榜最快记录", windowed.text)
        self.assertIn("全部", odd.text)

    def test_no_character_name_answers_who_holds_the_most_first_places(self) -> None:
        answer = run(self.service.character(""))

        self.assertEqual(answer.image_path, "/tmp/character_champions.png")
        self.assertIn("第一名记录里各角色各占几个", answer.text)
        self.assertIn("冠军最多：", answer.text)

    def test_an_unknown_character_triggers_one_catalog_refresh(self) -> None:
        # A name the catalog lacks might be a character added since the last
        # read; the tool asks for one refresh, then reports the miss.
        answer = run(self.service.character("新角色"))

        self.assertIsNone(answer.image_path)
        self.assertIn("没有「新角色」出场", answer.text)
        self.assertEqual(self.data.catalog_refreshes, 1)

    def test_every_answer_names_where_the_numbers_come_from(self) -> None:
        # A model not told the source calls a leaderboard count a server-wide
        # statistic; the page footer says it, so the text must too.
        answers = [
            run(self.service.board("三位一体")),
            run(self.service.battle("btl_upload_abcdef123456")),
            run(self.service.character("")),
            run(self.service.character("提弗洛斯")),
            run(self.service.account("usr_1234567890abcdef")),
        ]
        for answer in answers:
            with self.subTest(text=answer.text[:30]):
                self.assertIn("ZMDLogs", answer.text)
                # Once, even when the answer is several sections joined.
                self.assertEqual(answer.text.count("不是全服统计"), 1)

    def test_no_account_answers_which_player_holds_the_most_first_places(self) -> None:
        answer = run(self.service.account(""))

        self.assertEqual(answer.image_path, "/tmp/player_champions.png")
        self.assertIn("各玩家各占几个", answer.text)
        self.assertIn("冠军最多：", answer.text)

    def test_no_board_answers_what_is_new(self) -> None:
        answer = run(self.service.board(""))

        self.assertEqual(answer.image_path, "/tmp/records.png")
        self.assertIn("新纪录", answer.text)
        self.assertIn("第一名易主", answer.text)

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

    def test_null_quartiles_print_as_missing_not_as_a_crash(self) -> None:
        # 提弗洛斯 on 呼吼炽焰·苦难: 13 records, 4 survive the IQR filter, so
        # upstream returns rank null and no quartiles — for the board's top
        # DPS character. The text must say so rather than fail the tool.
        payload = character_statistics_payload()
        row = _first_statistics_row(payload)
        row.update(
            {
                "rank": None,
                "insufficientSamples": True,
                "p25": None,
                "p75": None,
                "sampleCount": 13,
                "normalSampleCount": 4,
            }
        )
        stats = parse_character_statistics(payload)

        text = facts.format_character_statistics(stats)

        self.assertIn("样本不足", text)
        self.assertIn("—", text)
        self.assertIn("样本 13（去极值后 4）", text)

    def test_cross_boss_comparison_is_refused_with_a_reason(self) -> None:
        other = parse_battle_detail(battle_detail_payload())
        object.__setattr__(other, "boss_name", "别的首领")
        text = facts.format_battle_comparison(self.battle, other)

        self.assertIn("不是同一个首领", text)
        self.assertIn("没有可比性", text)


def _first_statistics_row(payload: dict) -> dict:
    """The first per-character row of a statistics payload, wherever it lives."""

    for value in payload.values():
        if isinstance(value, list) and value and "p25" in value[0]:
            return value[0]
    raise AssertionError("statistics payload carries no character rows")


class ToolAnswerTests(unittest.TestCase):
    def test_an_answer_always_has_text(self) -> None:
        self.assertEqual(ToolAnswer("说点什么").image_path, None)


if __name__ == "__main__":
    unittest.main()
