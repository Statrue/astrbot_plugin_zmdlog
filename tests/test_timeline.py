"""Tests for the 0.7.0 cast rail (技能轴 / 施法节奏) built from the export."""

import unittest
from pathlib import Path

import httpx

from core.candidates import CandidateStore, CandidateView, format_candidates
from core.client import ZmdLogsAPIError, ZmdLogsClient
from core.loadout import SkillCategory
from core.matcher import MatchChoice, MatchLevel, MatchTarget, TargetType
from core.models import (
    ModelValidationError,
    parse_battle_detail,
    parse_battle_export,
)
from core.presentation import (
    build_battle_page,
    build_timeline_page,
    build_timeline_view,
)
from core.render import TemplateRenderer
from core.routing import RouteKind, RouteParseError, parse_zmdlog_payload
from core.timeline import build_timeline, classify_cast
from tests.helpers import battle_detail_payload, battle_export_payload

WEB = "https://zmdlogs.com"


def cast(
    key: str,
    name: str,
    start: int,
    end: int | None,
    *,
    character: str = "chr_0028_wulfa",
    source: str | None = "unknown",
) -> dict:
    return {
        "tsMsFromStart": start,
        "endMsFromStart": end,
        "characterKey": character,
        "skillKey": key,
        "skillName": name,
        "skillSource": source,
        "recoversEnergy": False,
    }


def view_of(payload: dict, **kwargs):
    return build_timeline_view(parse_battle_export(payload), web_base_url=WEB, **kwargs)


class BattleExportModelTests(unittest.TestCase):
    def test_export_is_parsed_with_optional_fields(self) -> None:
        export = parse_battle_export(battle_export_payload())

        self.assertEqual(export.battle_id, "btl_upload_abcdef123456")
        self.assertEqual(export.boss_name, "“碾骨之拳”罗丹")
        self.assertEqual(export.boss_slug, "dung01_group_bossrush01")
        self.assertEqual(export.duration_ms, 25_000)
        self.assertEqual(
            [entry.character_name for entry in export.roster], ["卡缪", "洛茜"]
        )
        self.assertEqual(len(export.casts), 14)
        instant = next(cast for cast in export.casts if cast.skill_name == "A3")
        self.assertIsNone(instant.end_ms)
        summon = next(cast for cast in export.casts if cast.source == "Summon")
        self.assertEqual(summon.skill_name, "召唤 / pet")
        heavy = next(cast for cast in export.casts if cast.skill_name == "重击")
        self.assertTrue(heavy.recovers_energy)

    def test_typed_fields_stay_strict(self) -> None:
        payload = battle_export_payload()
        payload["casts"][0]["tsMsFromStart"] = "1000"
        with self.assertRaises(ModelValidationError):
            parse_battle_export(payload)
        payload = battle_export_payload()
        del payload["dungeon"]
        with self.assertRaises(ModelValidationError):
            parse_battle_export(payload)
        payload = battle_export_payload()
        payload["casts"][0]["skillName"] = None
        self.assertEqual(
            parse_battle_export(payload).casts[0].skill_name,
            "chr_0028_wulfa_ultimate_skill",
        )


class CastClassificationTests(unittest.TestCase):
    def test_names_then_end_anchored_keys(self) -> None:
        cases = {
            ("终结技", "chr_x_ultimate_skill"): SkillCategory.ULTIMATE,
            ("终结技", "chr_x_ultimate_skill2"): SkillCategory.ULTIMATE,
            ("战技", "chr_x_normal_skill"): SkillCategory.SKILL,
            ("连携技", "chr_x_combo_skill"): SkillCategory.COMBO,
            ("A1", "chr_x_attack1"): SkillCategory.NORMAL,
            ("雀跃扳机", "chr_x_attack3"): SkillCategory.NORMAL,
            ("重击", "chr_x_power_attack"): SkillCategory.HEAVY,
            ("噪点", "chr_x_heavy_attack"): SkillCategory.HEAVY,
            ("x", "chr_x_execution"): SkillCategory.HEAVY,
            ("x", "chr_x_ult_attack2"): SkillCategory.NORMAL,
            ("x", "chr_x_plunging_attack_start"): SkillCategory.NORMAL,
            ("协议α·突破", "chr_x_normal_skill_alpha"): SkillCategory.OTHER,
            # A mechanism entity: contains combo_skill but does not end in it.
            ("河水 / gene", "chr_x_combo_skill_water_gene"): SkillCategory.OTHER,
            ("绵羊持续伤害", "chr_x_normal_skill_sheep_dot"): SkillCategory.OTHER,
        }
        for (name, key), expected in cases.items():
            with self.subTest(name=name, key=key):
                self.assertIs(classify_cast(name, key), expected)

    def test_movement_noise_is_hidden(self) -> None:
        for key in (
            "chr_x_dash",
            "chr_x_dodge_back",
            "chr_x_sprint_loop",
            "chr_x_switch_in",
            "chr_x_plunging_attack_end",
        ):
            with self.subTest(key=key):
                self.assertIsNone(classify_cast("x", key))


class RailFoldingTests(unittest.TestCase):
    def test_bursts_fold_into_single_events(self) -> None:
        export = parse_battle_export(battle_export_payload())

        timeline = build_timeline(export)

        self.assertEqual(timeline.duration_ms, 25_000)
        self.assertEqual(timeline.hidden_count, 1)
        # The overlong cast and the pre-timer cast.
        self.assertEqual(timeline.clipped_count, 2)
        self.assertEqual(len(timeline.blocks), 12)
        luoxi, kamiu = timeline.lanes
        self.assertEqual((luoxi.character_name, luoxi.cast_count), ("洛茜", 8))
        self.assertEqual(
            [(event.name, event.count) for event in luoxi.events],
            [
                ("终结技", 1),
                ("普攻", 2),
                ("重击", 1),
                ("战技", 1),
                ("连携技", 1),
                ("普攻", 1),
                ("血红之影", 1),
            ],
        )
        run = luoxi.events[1]
        self.assertIs(run.category, SkillCategory.NORMAL)
        self.assertEqual((run.start_ms, run.end_ms), (4_000, 5_100))
        self.assertTrue(luoxi.events[2].recovers_energy)
        lone = luoxi.events[5]
        self.assertTrue(lone.instant)
        self.assertEqual((lone.start_ms, lone.end_ms), (12_000, 12_000))
        self.assertEqual(luoxi.events[-1].end_ms, 25_000)
        self.assertEqual(luoxi.summon_events, ())
        self.assertEqual((kamiu.character_name, kamiu.cast_count), ("卡缪", 4))
        self.assertEqual(
            [event.name for event in kamiu.events],
            ["河水 / water / gene", "战技", "终结技"],
        )
        self.assertIs(kamiu.events[0].category, SkillCategory.OTHER)
        self.assertEqual(
            [(event.name, event.summon) for event in kamiu.summon_events],
            [("召唤 / pet", True)],
        )

    def test_repeated_moves_and_named_attacks_keep_their_name(self) -> None:
        payload = battle_export_payload()
        payload["casts"] = [
            cast("chr_0028_wulfa_normal_skill_x", "秘杖·束能技艺", 1_000, 1_400),
            cast("chr_0028_wulfa_normal_skill_x", "秘杖·束能技艺", 1_500, 1_900),
            cast("chr_0028_wulfa_normal_skill_x", "秘杖·束能技艺", 2_000, 2_400),
            # A gap wider than the merge window starts a new event.
            cast("chr_0028_wulfa_normal_skill_x", "秘杖·束能技艺", 5_000, 5_400),
            cast("chr_0028_wulfa_attack1", "岩石的轻语", 6_000, 6_300),
            cast("chr_0028_wulfa_attack2", "岩石的轻语", 6_300, 6_700),
            cast("chr_0028_wulfa_attack3", "岩石的轻语", 6_700, 7_200),
            # A different move interrupts the run even inside the window.
            cast("chr_0028_wulfa_combo_skill", "连携技", 7_300, 8_000),
            cast("chr_0028_wulfa_attack1", "岩石的轻语", 8_100, 8_400),
        ]

        events = build_timeline(parse_battle_export(payload)).lanes[0].events

        self.assertEqual(
            [(event.name, event.count) for event in events],
            [
                ("秘杖·束能技艺", 3),
                ("秘杖·束能技艺", 1),
                ("岩石的轻语", 3),
                ("连携技", 1),
                ("岩石的轻语", 1),
            ],
        )
        self.assertEqual((events[0].start_ms, events[0].end_ms), (1_000, 2_400))

    def test_a_caster_missing_from_the_roster_gets_a_lane(self) -> None:
        payload = battle_export_payload()
        payload["casts"][0]["characterKey"] = "chr_9999_ghost"

        timeline = build_timeline(parse_battle_export(payload))

        self.assertEqual(
            [lane.character_name for lane in timeline.lanes],
            ["洛茜", "卡缪", "chr_9999_ghost"],
        )


class RailViewTests(unittest.TestCase):
    def test_page_scale_ticks_blocks_and_labels(self) -> None:
        view = view_of(battle_export_payload())

        # 25 s at the 40 px/s page ceiling.
        self.assertEqual(view.chart_height, 1_000)
        self.assertEqual(view.scale_label, "每格 1 秒")
        self.assertEqual(view.duration_label, "0:25.000")
        self.assertEqual(len(view.ticks), 26)
        self.assertEqual(
            (view.ticks[0].label, view.ticks[-1].label), ("0s", "25s")
        )
        self.assertEqual(view.ticks[-1].top, 1_000)
        self.assertEqual(
            [tick.major for tick in view.ticks[:6]],
            [True, False, False, False, False, True],
        )
        self.assertEqual((view.cast_count, view.ultimate_count), (12, 2))
        self.assertEqual((view.hidden_count, view.clipped_count), (1, 2))
        self.assertTrue(view.has_summon)
        self.assertTrue(view.has_energy)
        self.assertEqual(
            [(item.css, item.count) for item in view.legend],
            [
                ("ultimate", 2),
                ("skill", 2),
                ("combo", 1),
                ("heavy", 1),
                ("normal", 3),
                ("other", 2),
            ],
        )
        luoxi, kamiu = view.lanes
        self.assertEqual(luoxi.cast_count, 8)
        self.assertEqual(
            luoxi.character_avatar_url,
            f"{WEB}/images/character/charremoteicon/icon_chr_0028_wulfa.png",
        )
        self.assertEqual(luoxi.label_left, 44)
        ultimate, run, heavy, skill, combo, lone, other = luoxi.events
        self.assertEqual((ultimate.top, ultimate.height), (40, 120))
        # The widest bar, with the diamond landmark on top.
        self.assertEqual((ultimate.left, ultimate.width), (0, 20))
        self.assertEqual((ultimate.shape, ultimate.category), ("bar", "ultimate"))
        self.assertTrue(ultimate.landmark)
        self.assertEqual(ultimate.time_label, "1.0s")
        self.assertTrue(ultimate.label_visible)
        self.assertEqual(ultimate.label_top, 33)
        # Too close to the label column to need a leader.
        self.assertEqual(ultimate.lead_width, 0)
        self.assertEqual((run.top, run.height, run.shape), (160, 44, "bar"))
        self.assertEqual((run.category, run.name, run.count), ("normal", "普攻", 2))
        self.assertEqual(run.width, 8)
        self.assertEqual((run.lead_top, run.lead_left, run.lead_width), (160, 25, 17))
        self.assertTrue(heavy.energy)
        self.assertEqual((skill.category, skill.width), ("skill", 16))
        self.assertEqual((skill.lead_left, skill.lead_width), (33, 9))
        self.assertEqual(combo.category, "combo")
        # An instant cast is a dot, not a bar.
        self.assertEqual((lone.top, lone.height, lone.shape), (480, 0, "dot"))
        self.assertFalse(lone.landmark)
        self.assertEqual((lone.lead_left, lone.lead_width), (22, 20))
        self.assertEqual((other.top, other.height, other.category), (720, 280, "other"))
        self.assertEqual(other.width, 12)
        # 卡缪: the entity overlaps her own casts, so her moves step right into
        # a second column and her labels move with them. The stream is in
        # time order, so the summon (3.0 s) precedes the ultimate (21.0 s).
        self.assertEqual(kamiu.label_left, 66)
        entity, own_skill, pet, own_ultimate = kamiu.events
        self.assertEqual((entity.left, entity.width), (0, 12))
        self.assertEqual((own_skill.left, own_skill.width), (22, 16))
        self.assertEqual((own_ultimate.left, own_ultimate.width), (22, 20))
        self.assertEqual(entity.category, "other")
        self.assertTrue(pet.summon)
        # The summon appears on the same instant as her 战技, whose label wins
        # the line; a summon label tolerates only a short push.
        self.assertFalse(pet.label_visible)
        self.assertEqual((pet.top, pet.height), (120, 480))

    def test_card_scale_is_shorter_and_ticks_widen(self) -> None:
        view = view_of(
            battle_export_payload(), target_height=560, min_pps=6, max_pps=24
        )

        self.assertEqual(view.chart_height, 560)
        self.assertEqual(view.scale_label, "每格 2 秒")
        self.assertEqual(len(view.ticks), 13)

    def test_labels_slide_down_and_drop_when_pushed_too_far(self) -> None:
        payload = battle_export_payload()
        payload["casts"] = [
            cast("chr_0028_wulfa_normal_skill", "战技", 1_000, 1_200),
            cast("chr_0028_wulfa_combo_skill", "连携技", 1_100, 1_300),
            cast("chr_0028_wulfa_attack1", "A1", 1_300, 1_500),
            cast("chr_0028_wulfa_ultimate_skill", "终结技", 1_400, 3_000),
        ]

        skill, combo, run, ultimate = view_of(payload).lanes[0].events

        self.assertTrue(skill.label_visible)
        self.assertEqual(skill.label_top, 33)
        # 0.1 s later is 4 px down; the label takes the next free line.
        self.assertTrue(combo.label_visible)
        self.assertEqual(combo.label_top, 48)
        # A normal-attack run only tolerates a short push, so it loses its label.
        self.assertFalse(run.label_visible)
        # An ultimate is never dropped, however far it slides.
        self.assertTrue(ultimate.label_visible)
        self.assertEqual(ultimate.label_top, 63)

    def test_labels_stay_inside_the_lane(self) -> None:
        payload = battle_export_payload()
        payload["casts"] = [
            cast("chr_0028_wulfa_ultimate_skill", "终结技", 0, 2_000),
            cast("chr_0028_wulfa_normal_skill", "战技", 24_900, 25_000),
        ]

        first, last = view_of(payload).lanes[0].events

        self.assertEqual(first.label_top, 0)
        self.assertEqual((last.top, last.height), (996, 4))
        self.assertEqual(last.label_top, 985)
        self.assertTrue(last.label_visible)

    def test_long_fights_compress_the_scale(self) -> None:
        payload = battle_export_payload()
        payload["durationMs"] = 385_648

        view = view_of(payload)

        self.assertEqual(view.chart_height, 3_857)
        self.assertEqual(view.scale_label, "每格 5 秒")
        self.assertEqual(view.ticks[12].label, "1:00")
        self.assertEqual(view.ticks[-1].label, "6:25")

    def test_a_very_short_fight_keeps_a_minimum_height(self) -> None:
        payload = battle_export_payload()
        payload["durationMs"] = 2_000
        payload["casts"] = payload["casts"][:1]
        payload["casts"][0]["endMsFromStart"] = 1_500

        view = view_of(payload)

        self.assertEqual(view.chart_height, 200)
        # 1.0 s to 1.5 s at the 40 px/s ceiling.
        first = view.lanes[0].events[0]
        self.assertEqual((first.top, first.height), (40, 20))


class BattleCardIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])
        self.battle = parse_battle_detail(battle_detail_payload())
        self.export = parse_battle_export(battle_export_payload())

    def test_card_embeds_the_rail_when_the_export_is_available(self) -> None:
        page = build_battle_page(
            self.battle, query="q", web_base_url=WEB, export=self.export
        )
        self.assertIsNotNone(page.timeline)
        self.assertIsNone(page.timeline_note)
        self.assertEqual(page.timeline.chart_height, 560)

        html = self.renderer.render_battle(
            self.battle, query="q", web_base_url=WEB, export=self.export
        )
        self.assertIn("<h2>施法节奏</h2>", html)
        self.assertIn('class="rail-chart"', html)
        self.assertIn("rail-bar is-ultimate", html)
        self.assertIn("rail-bar is-normal", html)
        self.assertIn('class="rail-summon"', html)
        self.assertIn('class="rail-dot is-normal"', html)
        self.assertIn('class="rail-mark"', html)
        self.assertIn('class="rail-lead"', html)
        self.assertIn('class="rail-energy"', html)
        self.assertIn("瞬时动作", html)
        self.assertIn("<small>×2</small>", html)
        self.assertIn("已隐藏 1 条冲刺、闪避等移动动作", html)
        self.assertNotIn("chr_0028_wulfa_dash", html)

    def test_card_prints_the_reason_or_nothing_without_an_export(self) -> None:
        html = self.renderer.render_battle(
            self.battle, query="q", web_base_url=WEB, export_note="旧版客户端上传"
        )
        self.assertIn("<h2>施法节奏</h2>", html)
        self.assertIn('class="rail-note">旧版客户端上传', html)
        self.assertNotIn('class="rail-chart"', html)

        html = self.renderer.render_battle(self.battle, query="q", web_base_url=WEB)
        self.assertNotIn("<h2>施法节奏</h2>", html)
        self.assertNotIn('class="rail-chart"', html)

    def test_standalone_page_renders_the_same_rail_at_full_height(self) -> None:
        page = build_timeline_page(self.export, query="技能轴 罗丹", web_base_url=WEB)
        self.assertEqual(page.header.target_type, "技能轴")
        self.assertEqual(page.timeline.chart_height, 1_000)

        html = self.renderer.render_timeline(
            self.export, query="技能轴 罗丹", web_base_url=WEB
        )
        self.assertIn('class="rail-chart"', html)
        self.assertIn("每格 1 秒", html)
        self.assertIn("icon_chr_0028_wulfa.png", html)
        self.assertIn(">START<", html)
        self.assertIn(">END<", html)


class TimelineRouteTests(unittest.TestCase):
    def test_timeline_commands_share_the_battle_shape(self) -> None:
        route = parse_zmdlog_payload("技能轴 罗丹 2")
        self.assertEqual(route.kind, RouteKind.TIMELINE_QUERY)
        self.assertEqual((route.query, route.battle_rank), ("罗丹", 2))
        self.assertEqual(
            parse_zmdlog_payload("排轴 btl_upload_abcdef123456").kind,
            RouteKind.TIMELINE_QUERY,
        )
        self.assertEqual(
            parse_zmdlog_payload("时间轴 罗丹").kind, RouteKind.TIMELINE_QUERY
        )
        for payload in ("技能轴", "技能轴 罗丹 --top 3"):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)

    def test_pick_list_names_the_timeline_view(self) -> None:
        choice = MatchChoice(
            target=MatchTarget(
                target_type=TargetType.BOARD,
                key="a",
                name="榜单甲",
                dungeon_names=("副本",),
                boss_slugs=("a",),
                query_text="榜",
            ),
            level=MatchLevel.NORMALIZED_EXACT,
            score=1.0,
            matched_text="榜",
        )
        store = CandidateStore()

        entry = store.remember("榜", (choice, choice), view=CandidateView.TIMELINE)

        self.assertIn("技能轴查询匹配到 2 个榜单", format_candidates(entry))


class ExportClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_path_and_unsupported_error(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if "old" in request.url.path:
                return httpx.Response(
                    422,
                    json={
                        "error": {
                            "code": "battle_export_unsupported",
                            "message": "旧版客户端",
                        }
                    },
                )
            return httpx.Response(200, json=battle_export_payload())

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)

        export = await client.get_battle_export("btl_upload_abcdef123456")
        self.assertEqual(export.battle_id, "btl_upload_abcdef123456")
        self.assertEqual(seen, ["/api/v1/battles/btl_upload_abcdef123456/export"])
        with self.assertRaises(ZmdLogsAPIError) as context:
            await client.get_battle_export("btl_upload_old000000001")
        self.assertEqual(context.exception.status_code, 422)
        self.assertEqual(context.exception.code, "battle_export_unsupported")


if __name__ == "__main__":
    unittest.main()
