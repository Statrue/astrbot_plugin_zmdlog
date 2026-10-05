"""战报's 数据: the command, the view model and the page."""

import unittest
from pathlib import Path

from core.battle_views import BATTLE_VIEWS, unavailable_views, view_strip
from core.candidates import CandidateView
from core.crit import CritHit, build_crit_expectation
from core.models import parse_battle_detail
from core.presentation import build_battle_data_page
from core.render import PAGE_FRAMES, WIDE_FRAME, TemplateRenderer
from core.routing import RouteKind, parse_zmdlog_payload
from tests.helpers import battle_detail_payload, crisis_contract_tags
from tests.test_crit import crit_battle_payload

WEB = "https://zmdlogs.com"


def crit_of(battle):
    return build_crit_expectation(
        (
            CritHit(point.value, point.crit_roll, point.character_name)
            for point in battle.damage_points
        ),
        duration_ms=battle.duration_ms,
    )


def contract_payload() -> dict:
    payload = battle_detail_payload()
    tags = crisis_contract_tags()
    payload["battle"]["contractTags"] = tags
    payload["battle"]["contractTagScore"] = sum(tag["score"] for tag in tags)
    return payload


class DataRouteTests(unittest.TestCase):
    def test_data_takes_what_the_battle_command_takes(self) -> None:
        for payload, query, rank in (
            ("数据 btl_upload_abcdef123456", "btl_upload_abcdef123456", 1),
            (
                "数据 https://zmdlogs.com/battle/btl_upload_abcdef123456",
                "https://zmdlogs.com/battle/btl_upload_abcdef123456",
                1,
            ),
            ("数据 罗丹 3", "罗丹", 3),
        ):
            with self.subTest(payload=payload):
                route = parse_zmdlog_payload(payload)

                self.assertIs(route.kind, RouteKind.DATA_QUERY)
                self.assertEqual(route.query, query)
                self.assertEqual(route.battle_rank, rank)

        route = parse_zmdlog_payload("数据 罗丹 1 --口径 rdps")
        self.assertEqual(route.metric, "rdps")

    def test_the_old_skill_words_are_ordinary_keywords(self) -> None:
        # Dropped without an alias: they read as a board keyword like any
        # other word the router does not know.
        for payload in ("技能 罗丹", "技能统计 btl_upload_abcdef123456"):
            with self.subTest(payload=payload):
                route = parse_zmdlog_payload(payload)

                self.assertIs(route.kind, RouteKind.SMART_QUERY)
                self.assertEqual(route.query, payload)


class DataViewTests(unittest.TestCase):
    """数据 on the strip: second, and only where the upload has skill stats."""

    def test_data_follows_the_summary_on_the_strip(self) -> None:
        self.assertEqual(
            [(entry.view, entry.label, entry.word) for entry in BATTLE_VIEWS[:2]],
            [
                (CandidateView.BATTLE, "摘要", "战报"),
                (CandidateView.DATA, "数据", "数据"),
            ],
        )

    def test_a_battle_without_skill_stats_has_no_data_page(self) -> None:
        payload = battle_detail_payload()
        payload["roleSkillStats"] = []
        old = parse_battle_detail(payload)
        full = parse_battle_detail(battle_detail_payload())

        self.assertIn(CandidateView.DATA, unavailable_views(old))
        self.assertNotIn(CandidateView.DATA, unavailable_views(full))
        # A page that did not read the detail assumes it is there.
        self.assertNotIn(CandidateView.DATA, unavailable_views(None))
        self.assertNotIn(
            "数据", [label for label, _ in view_strip(CandidateView.BATTLE, old)]
        )


class DataPageModelTests(unittest.TestCase):
    def page(self, payload=None, **kwargs):
        battle = parse_battle_detail(payload or battle_detail_payload())
        return build_battle_data_page(battle, query="q", web_base_url=WEB, **kwargs)

    def test_characters_run_highest_dps_first_in_whole_units(self) -> None:
        luoxi, kamiu = self.page().characters

        self.assertEqual(
            (luoxi.character_name, luoxi.colour_index, luoxi.profession),
            ("洛茜", 1, "近卫"),
        )
        self.assertEqual((luoxi.dps, luoxi.rdps), ("97,325", "26,428"))
        self.assertEqual((kamiu.dps, kamiu.rdps), ("12,736", "83,633"))
        self.assertEqual(luoxi.total_damage, "2,027,572")
        self.assertEqual(luoxi.max_hit, "381,016")
        self.assertEqual(luoxi.crit_rate, "81.2%")
        self.assertEqual((kamiu.max_hit, kamiu.crit_rate), ("—", "—"))
        self.assertEqual(luoxi.avatar_url, f"{WEB}/images/character/luoxi.png")
        self.assertIsNone(kamiu.avatar_url)

    def test_contribution_puts_direct_damage_beside_team_contribution(self) -> None:
        luoxi, kamiu = self.page().characters

        # DPS: each one's share of the damage; rDPS: of the team's rDPS.
        self.assertEqual((luoxi.damage_share, kamiu.damage_share), ("88.4%", "11.6%"))
        self.assertEqual((luoxi.rdps_share, kamiu.rdps_share), ("24%", "76%"))
        self.assertEqual(luoxi.damage_percent, 88.43)
        self.assertEqual(kamiu.rdps_percent, 75.99)

    def test_skill_damage_is_grouped_by_character_in_their_colours(self) -> None:
        page = self.page()

        luoxi, kamiu = page.skill_groups
        self.assertEqual((luoxi.character_name, luoxi.colour_index), ("洛茜", 1))
        self.assertEqual((kamiu.character_name, kamiu.colour_index), ("卡缪", 2))
        self.assertEqual(luoxi.total_damage, "2,027,572")
        self.assertEqual((luoxi.team_share, kamiu.team_share), ("88.4%", "11.6%"))
        top = luoxi.rows[0]
        self.assertEqual(
            (top.category, top.name, top.cast_count, top.total_damage, top.share),
            ("终结技", "终结技", 2, "1,200,000", "59.2%"),
        )
        self.assertEqual(luoxi.hidden_count, 0)
        self.assertEqual(kamiu.rows[0].name, "连携技")
        self.assertEqual(luoxi.avatar_url, f"{WEB}/images/character/luoxi.png")

    def test_a_long_list_of_skills_is_cut_and_counted(self) -> None:
        payload = battle_detail_payload()
        payload["roleSkillStats"] += [
            {
                "characterName": "卡缪",
                "skillKey": f"chr_0031_kamiu_skill_{index}",
                "skillName": f"招式{index}",
                "castCount": 1,
                "totalDamage": 100 + index,
                "avgDamage": 100.0 + index,
                "maxDamage": 100 + index,
            }
            for index in range(14)
        ]

        kamiu = self.page(payload).skill_groups[1]

        self.assertEqual(len(kamiu.rows), 12)
        self.assertEqual(kamiu.hidden_count, 4)

    def test_crit_has_the_team_figures_and_a_row_a_character(self) -> None:
        battle = parse_battle_detail(crit_battle_payload())
        page = build_battle_data_page(
            battle, query="q", web_base_url=WEB, crit=crit_of(battle)
        )

        crit = page.crit
        self.assertEqual(
            (crit.actual_damage, crit.expected_damage, crit.expected_dps),
            ("2,292,905", "2,012,905", "96,621"),
        )
        # Signed, as the site prints it.
        self.assertEqual(crit.deviation, "+13.91%")
        self.assertIsNone(crit.coverage_note)
        self.assertIsNotNone(crit.bell)
        self.assertEqual(
            [(row.label, row.colour_index) for row in crit.rows],
            [("洛茜", 1), ("卡缪", 2)],
        )

    def test_no_crit_rolls_no_crit(self) -> None:
        self.assertIsNone(self.page().crit)

    def test_the_curve_keys_carry_each_lines_final_dps(self) -> None:
        page = self.page()

        self.assertIsNotNone(page.dps_curve)
        self.assertEqual(
            [(key.label, key.colour_index) for key in page.curve_keys],
            [("全队", None), ("洛茜", 1), ("卡缪", 2)],
        )

    def test_a_contract_record_carries_its_tags_by_family(self) -> None:
        page = self.page(contract_payload())

        self.assertEqual(page.contract_score, "14")
        self.assertEqual(
            [(group.family, group.count) for group in page.contract_groups],
            [("队列", 2), ("改写", 2), ("环境", 2)],
        )
        self.assertEqual(self.page().contract_groups, ())
        self.assertIsNone(self.page().contract_score)

    def test_the_strip_is_the_one_given(self) -> None:
        page = self.page(views=(("摘要", False), ("数据", True)))

        self.assertEqual(
            [(view.label, view.current) for view in page.views],
            [("摘要", False), ("数据", True)],
        )


class DataTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])

    def html(self, payload=None, **kwargs) -> str:
        battle = parse_battle_detail(payload or battle_detail_payload())
        return self.renderer.render_battle_data(
            battle, query="q", web_base_url=WEB, **kwargs
        )

    def test_data_is_a_wide_page_on_the_new_shell(self) -> None:
        self.assertIs(PAGE_FRAMES["battle-data"], WIDE_FRAME)
        html = self.html()

        self.assertIn("--zmd-frame-width: 960;", html)
        self.assertIn('class="zmd-main"', html)
        self.assertIn(">BATTLE REPORT<", html)
        self.assertIn('<h1 class="i-title">“碾骨之拳”罗丹</h1>', html)
        # The meta line: who, how long, when.
        self.assertIn("<span>测试账号</span>", html)
        self.assertIn("<span><b>0:20.833</b> 通关</span>", html)
        self.assertIn("<span>2026-07-13 22:00</span>", html)

    def test_the_sections_run_in_the_approved_order(self) -> None:
        battle = parse_battle_detail(contract_payload_with_crit())
        html = self.renderer.render_battle_data(
            battle, query="q", web_base_url=WEB, crit=crit_of(battle)
        )

        titles = (
            "战斗贡献", "本场角色", "暴击期望", "DPS 曲线", "技能伤害", "危机合约"
        )
        positions = [html.index(f"<strong>{title}</strong>") for title in titles]
        self.assertEqual(positions, sorted(positions))

    def test_the_table_prints_every_dps_whole(self) -> None:
        html = self.html()
        start = html.index("<strong>本场角色</strong>")
        table = html[start:html.index("<strong>DPS 曲线</strong>")]

        self.assertIn("<b>97,325</b>", table)
        self.assertIn("<span>26,428</span>", table)
        self.assertNotIn("97,325.01", html)
        self.assertNotIn("26,428.42", html)

    def test_contribution_draws_both_bars_a_character(self) -> None:
        html = self.html()
        contribution = html[
            html.index("<strong>战斗贡献</strong>"):html.index("<strong>本场角色</strong>")
        ]

        self.assertIn('class="d-contrib-row c1 is-lead"', contribution)
        self.assertIn('class="d-contrib-row c2"', contribution)
        self.assertIn('style="width: 88.43%;"', contribution)
        self.assertIn("<b>24%</b>", contribution)
        self.assertIn("<b>76%</b>", contribution)

    def test_crit_is_the_figures_the_bell_and_the_table(self) -> None:
        battle = parse_battle_detail(crit_battle_payload())
        html = self.renderer.render_battle_data(
            battle, query="q", web_base_url=WEB, crit=crit_of(battle)
        )
        start = html.index("<strong>暴击期望</strong>")
        crit = html[start:html.index("<strong>DPS 曲线</strong>")]

        self.assertIn("<b>+13.91%</b>", crit)
        self.assertIn('class="b-dist-mark is-expected"', crit)
        self.assertIn('class="b-dist-mark is-actual"', crit)
        self.assertEqual(crit.count('class="d-tr"'), 2)

    def test_a_battle_without_crit_rolls_has_no_crit_block(self) -> None:
        html = self.html()

        self.assertNotIn("<strong>暴击期望</strong>", html)
        self.assertNotIn('class="b-dist-plot', html)

    def test_skill_damage_is_drawn_per_character(self) -> None:
        html = self.html()
        skills = html[html.index("<strong>技能伤害</strong>"):]

        self.assertIn("普攻 · 绯红刃舞", skills)
        self.assertIn("1,200,000", skills)
        self.assertIn("59.2%", skills)
        self.assertIn("全队 88.4%", skills)
        # Raw keys never print as a name.
        self.assertNotIn("chr_0028_wulfa_skill_3090<", skills)

    def test_the_contract_tags_are_drawn_by_family(self) -> None:
        html = self.html(contract_payload())
        contract = html[html.index("<strong>危机合约</strong>"):]

        self.assertIn("14 分", contract)
        self.assertIn("环境：禁锢", contract)
        self.assertIn("contract-tag/icon_activity_contract_tag_208.png", contract)
        self.assertNotIn("<strong>危机合约</strong>", self.html())

    def test_the_foot_lists_the_battles_pages_with_data_lit(self) -> None:
        html = self.html(views=(("摘要", False), ("数据", True)))
        foot = html[html.index('<footer class="i-foot">'):]

        self.assertIn(
            '<span>摘要</span><i>·</i><span class="is-on">数据</span>', foot
        )


def contract_payload_with_crit() -> dict:
    payload = crit_battle_payload()
    tags = crisis_contract_tags()
    payload["battle"]["contractTags"] = tags
    payload["battle"]["contractTagScore"] = sum(tag["score"] for tag in tags)
    return payload


if __name__ == "__main__":
    unittest.main()
