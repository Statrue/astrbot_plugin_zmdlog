"""养成: the 潜能 + 精炼 pair, from the payload to the pages and the tools."""

import unittest
from dataclasses import replace
from pathlib import Path

from core import facts
from core.models import (
    parse_boss_ranking,
    parse_hot_bosses,
    parse_public_user_rankings,
)
from core.render import TemplateRenderer
from tests.helpers import (
    hot_bosses_payload,
    make_card,
    public_user_rankings_payload,
    ranking_payload_with_rows,
)

ROOT = Path(__file__).parents[1]
# The line on every page that prints 养成, saying what the numbers are.
LEGEND = "头像下的数字是 潜能+精炼"


def pair(potential, refine) -> dict:
    return {"characterPotential": potential, "weaponRefine": refine}


def ranking_with_pairs(*pairs, row: int = 0, payload: dict | None = None):
    """The rows fixture with one row's four roster entries given these fields.

    Each pair is a dict of the raw upstream fields for one entry, applied in
    roster order; an empty dict leaves the entry as the fixture wrote it.
    """

    payload = payload or ranking_payload_with_rows()
    for entry, fields in zip(payload["rows"][row]["rosterEntries"], pairs):
        entry.update(fields)
    return payload


def account_with_entries(*pairs):
    """The account fixture's one record with a roster in the board rows' shape."""

    payload = public_user_rankings_payload()
    record = payload["rankings"][0]
    record["rosterEntries"] = [
        {
            "characterKey": f"chr_{name}",
            "characterName": name,
            "profession": "近卫",
            "avatarUrl": None,
            **fields,
        }
        for name, fields in zip(record["rosterSummary"], pairs)
    ]
    return payload


def investments(html: str, marker: str = 'class="investment">') -> list[str]:
    """Every 养成 printed on a page, in page order.

    ``marker`` is how the page tags one: the old shell's ``investment``
    span, or a V2 page's own chip.
    """

    return [
        chunk.split("<", 1)[0] for chunk in html.split(marker)[1:]
    ]


class InvestmentParseTests(unittest.TestCase):
    def test_roster_entries_read_the_pair_and_tolerate_null_missing_and_odd(
        self,
    ) -> None:
        payload = ranking_with_pairs(
            {"characterPotential": 5, "weaponRefine": 6},
            {"characterPotential": 2, "weaponRefine": None},
            {},
            {"characterPotential": "5", "weaponRefine": True},
        )

        entries = parse_boss_ranking(payload).rows[0].roster_entries

        self.assertEqual(
            [(entry.character_potential, entry.weapon_refine) for entry in entries],
            [(5, 6), (2, None), (None, None), (None, None)],
        )

    def test_account_records_read_their_roster_with_its_pairs(self) -> None:
        account = parse_public_user_rankings(
            account_with_entries(pair(5, 6), pair(0, None), {}, {})
        )

        entries = account.rankings[0].roster_entries
        self.assertEqual(
            [entry.character_name for entry in entries],
            ["洛茜", "卡缪", "洁尔佩塔", "佩丽卡"],
        )
        self.assertEqual(
            [(entry.character_potential, entry.weapon_refine) for entry in entries],
            [(5, 6), (0, None), (None, None), (None, None)],
        )

    def test_an_account_roster_missing_or_unreadable_is_no_roster(self) -> None:
        unreadable = account_with_entries({}, {}, {}, {})
        del unreadable["rankings"][0]["rosterEntries"][2]["characterName"]

        for payload in (public_user_rankings_payload(), unreadable):
            with self.subTest(has_entries="rosterEntries" in payload["rankings"][0]):
                account = parse_public_user_rankings(payload)
                self.assertEqual(account.rankings[0].roster_entries, ())

    def test_hot_boss_runs_read_the_pair_both_nullable(self) -> None:
        payload = hot_bosses_payload()
        run = {
            "battleId": "btl_upload_000000000001",
            "durationMs": 61_234,
            "uploaderNickname": "公开账户",
            "characterName": "余烬",
        }
        payload[0]["topSpeedRuns"] = [
            {**run, **pair(5, 6)},
            {**run, **pair(None, None)},
            run,
        ]

        runs = parse_hot_bosses(payload)[0].top_speed_runs

        self.assertEqual(
            [(run.character_potential, run.weapon_refine) for run in runs],
            [(5, 6), (None, None), (None, None)],
        )


class InvestmentPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = TemplateRenderer.from_plugin_root(ROOT)
        # Row 1 is 黎风, 卡缪, 佩丽卡, 洁尔佩塔; row 3 is 洛茜 and the same three.
        payload = ranking_with_pairs(pair(5, 6), pair(3, 4), pair(0, 1), {})
        payload = ranking_with_pairs(pair(1, None), row=2, payload=payload)
        self.ranking = parse_boss_ranking(payload)

    def test_the_ranking_page_prints_no_pair(self) -> None:
        # The V2 ranking row is faces, time and DPS only (the prototype's
        # ruling); 养成 is the battle's 养成 view's to show.
        html = self.renderer.render_ranking(self.ranking, query="三位一体")

        self.assertEqual(investments(html), [])
        self.assertNotIn(LEGEND, html)

    def test_the_roster_page_prints_each_combo_members_pair_from_its_best_run(
        self,
    ) -> None:
        html = self.renderer.render_roster(self.ranking, query="阵容 三位一体")

        # Combo A's best run is row 1, its members in profession order
        # 黎风 洁尔佩塔 佩丽卡 卡缪; combo B's is row 3, where only 洛茜 has one.
        # A chip under each name (the V2 阵容), with no legend line: the
        # prototype's ruling, as on the V2 ranking.
        self.assertEqual(
            investments(html, 'class="r-inv">'), ["5+6", "0+1", "3+4", "1+?"]
        )
        self.assertNotIn(LEGEND, html)

    def test_the_account_page_prints_no_pair_and_borrows_the_index_faces(
        self,
    ) -> None:
        # The V2 account row is faces, time, DPS and date, like the board's.
        own = parse_public_user_rankings(
            account_with_entries(pair(5, 6), pair(0, None), {}, {})
        )
        # Without a roster of its own, the index's row of the same battle
        # (here the fixture's row 1) is where the faces come from.
        bare = parse_public_user_rankings(public_user_rankings_payload())
        held_row = self.ranking.rows[0]
        held = {bare.rankings[0].battle_id: held_row}

        own_html = self.renderer.render_account(
            own, query="账号", web_base_url="https://zmdlogs.com"
        )
        held_html = self.renderer.render_account(
            bare, query="账号", web_base_url="https://zmdlogs.com",
            rows_by_battle=held,
        )

        self.assertEqual(investments(own_html), [])
        self.assertNotIn(LEGEND, own_html)
        self.assertEqual(investments(held_html), [])
        portrait = next(e.avatar_url for e in held_row.roster_entries if e.avatar_url)
        self.assertIn(portrait.rsplit("/", 1)[-1], held_html)


class InvestmentTextTests(unittest.TestCase):
    """The tools name 养成 after a character, and say once what it is."""

    KEY = "潜能+精炼"

    def test_board_rows_name_the_main_c_and_every_members_pair(self) -> None:
        payload = ranking_with_pairs(pair(5, 6), pair(3, 4), pair(0, 1), {})
        payload = ranking_with_pairs(pair(1, None), row=2, payload=payload)

        text = facts.format_board_ranking(parse_boss_ranking(payload))

        self.assertIn("主 C 黎风 5+6 · 公开账号1", text)
        self.assertIn("阵容 黎风 5+6、卡缪 3+4、佩丽卡 0+1、洁尔佩塔 · battleId", text)
        self.assertIn("主 C 洛茜 1+? · 公开账号3", text)
        self.assertIn("主 C 黎风 · 公开账号2", text)
        self.assertIn(self.KEY, text)

    def test_a_board_without_any_pair_does_not_explain_one(self) -> None:
        text = facts.format_board_ranking(
            parse_boss_ranking(ranking_payload_with_rows())
        )

        self.assertIn("阵容 黎风、卡缪、佩丽卡、洁尔佩塔 · battleId", text)
        self.assertNotIn(self.KEY, text)

    def test_account_records_name_every_members_pair(self) -> None:
        account = parse_public_user_rankings(
            account_with_entries(pair(5, 6), pair(0, None), {}, {})
        )
        bare = parse_public_user_rankings(public_user_rankings_payload())

        text = facts.format_account(account)
        bare_text = facts.format_account(bare)

        self.assertIn("阵容 洛茜 5+6、卡缪 0+?、洁尔佩塔、佩丽卡 · battleId", text)
        self.assertIn(self.KEY, text)
        self.assertIn("阵容 洛茜、卡缪、洁尔佩塔、佩丽卡 · battleId", bare_text)
        self.assertNotIn(self.KEY, bare_text)

    def test_an_account_record_without_a_roster_borrows_the_index_row(self) -> None:
        # The page's fallback, so the tool and the page name the same pairs.
        bare = parse_public_user_rankings(public_user_rankings_payload())
        payload = ranking_with_pairs(pair(5, 6), pair(3, 4), pair(0, 1), {})
        held = {bare.rankings[0].battle_id: parse_boss_ranking(payload).rows[0]}

        text = facts.format_account(bare, rows_by_battle=held)

        self.assertIn("阵容 黎风 5+6、卡缪 3+4、佩丽卡 0+1、洁尔佩塔 · battleId", text)
        self.assertIn(self.KEY, text)

    def test_the_overview_names_each_leaders_pair(self) -> None:
        card = make_card("a-1", "榜单甲", "副本", with_run=True)
        run = replace(
            card.top_speed_runs[0], character_potential=5, weapon_refine=None
        )
        card = replace(card, top_speed_runs=(run,))

        text = facts.format_boards_overview((card,), title="全部榜单")

        self.assertIn("主 C 余烬 5+? · 公开账户", text)
        self.assertIn(self.KEY, text)


if __name__ == "__main__":
    unittest.main()
