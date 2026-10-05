import asyncio
import os
import re
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from core.matcher import MatchChoice, MatchLevel, MatchTarget, TargetType
from core.models import (
    BossRanking,
    BossRankingRosterEntry,
    BossRankingRow,
    ContractTag,
    parse_battle_detail,
    parse_public_user_rankings,
)
from core.presentation import (
    PresentationError,
    build_account_page,
    build_dungeon_top3_page,
    build_ranking_page,
    format_duration,
    format_number,
)
from core.presentation.build import PipState, star_view
from core.render import (
    FONT_ORIGIN,
    LEGACY_FRAME,
    LIST_FRAME,
    WIDE_FRAME,
    AssetCache,
    LongImageRenderer,
    RenderError,
    TemplateConfigurationError,
    TemplateRenderer,
    read_plugin_version,
)
from core.routing import ALL_PAGES
from tests.helpers import (
    battle_detail_payload,
    crisis_contract_tags,
    make_card,
    public_user_rankings_payload,
)


def dungeon_pick(name: str, *cards) -> MatchChoice:
    """``name`` as the dungeon a keyword or 榜单's list picked."""

    return MatchChoice(
        target=MatchTarget(
            target_type=TargetType.DUNGEON,
            key=name,
            name=name,
            dungeon_names=(name,),
            boss_slugs=tuple(card.boss_slug for card in cards),
        ),
        level=MatchLevel.STANDARD_EXACT,
        score=1.0,
        matched_text=name,
    )


class AssetCacheTests(unittest.TestCase):
    def _put(self, cache, url, size=4, now=0.0, status=200):
        cache.put(
            url,
            status=status,
            content_type="image/png",
            body=b"x" * size,
            now=now,
        )

    def test_entries_expire_by_ttl(self) -> None:
        cache = AssetCache(ttl_seconds=10)
        self._put(cache, "u", now=0.0)

        self.assertIsNotNone(cache.get("u", now=9.9))
        self.assertIsNone(cache.get("u", now=10.0))
        self.assertEqual(len(cache), 0)

    def test_total_byte_cap_evicts_oldest_first(self) -> None:
        cache = AssetCache(max_total_bytes=10, max_item_bytes=10)
        self._put(cache, "a", size=4, now=0.0)
        self._put(cache, "b", size=4, now=1.0)
        self._put(cache, "c", size=4, now=2.0)

        self.assertIsNone(cache.get("a", now=2.0))
        self.assertIsNotNone(cache.get("b", now=2.0))
        self.assertIsNotNone(cache.get("c", now=2.0))
        self.assertEqual(cache.total_bytes, 8)

    def test_an_oversized_item_is_not_stored(self) -> None:
        cache = AssetCache(max_item_bytes=3)
        self._put(cache, "big", size=4)

        self.assertIsNone(cache.get("big", now=0.0))
        self.assertEqual(cache.total_bytes, 0)

    def test_overwriting_a_url_replaces_its_bytes(self) -> None:
        cache = AssetCache()
        self._put(cache, "u", size=4, now=0.0)
        self._put(cache, "u", size=6, now=1.0)

        self.assertEqual(cache.total_bytes, 6)
        self.assertEqual(len(cache), 1)


class TemplateRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parents[1]
        self.renderer = TemplateRenderer.from_plugin_root(self.root)

    def test_help_is_self_contained_and_uses_manifest_version(self) -> None:
        html = self.renderer.render_help(command_prefix="!")

        # Each command in its shortest form beside one statement; the
        # options are explained once, in their own card.
        self.assertIn("!zmdlog 榜单</code>", html)
        self.assertIn("!zmdlog &lt;榜单关键词&gt;</code>", html)
        self.assertIn("!zmdlog 角色排名</code>", html)
        self.assertIn("查询某个榜单", html)
        self.assertNotIn("[--", html)
        self.assertIn("常用选项", html)
        self.assertIn("--页 N|全部", html)
        self.assertIn("对比 … 我", html)
        self.assertIn("17 条指令", html)
        self.assertIn("终末地·藕粉铺子", html)
        # Read from the manifest rather than repeating it: the version is
        # bumped every release, and the point of the test is that the page
        # shows whatever metadata.yaml says.
        version = read_plugin_version(self.root / "metadata.yaml")
        self.assertIn(f"v{version}", html)
        self.assertIn("@font-face", html)
        self.assertNotIn("astrbot_plugin_zmdlog", html)
        self.assertNotIn("/zmdlog", html)
        self.assertNotIn("固定口径", html)
        self.assertNotIn("影拓4", html)

    def test_help_is_a_wide_comic_page_with_its_chibis_embedded(self) -> None:
        from core.render import page_frame

        html = self.renderer.render_help(command_prefix="/")

        self.assertEqual(page_frame("help"), WIDE_FRAME)
        self.assertIn("--zmd-frame-width: 960;", html)
        self.assertIn('id="zmd-root"', html)
        self.assertIn('class="zmd-main', html)
        self.assertNotIn('id="zmd-page"', html)
        self.assertNotIn("scene-background", html)
        # The five chibis travel inside the page, never fetched at capture.
        chibis = re.findall(
            r'<img class="comic-chibi[^"]*" src="data:image/webp;base64,', html
        )
        self.assertEqual(len(chibis), 5)
        # About 106 KB of WebP; the fonts are linked for the capture.
        linked = self.renderer.render_help(command_prefix="/", embed_fonts=False)
        self.assertLess(len(linked), 300_000)
        # An outline from -webkit-text-stroke came out as spiky blobs and
        # hid the clipped fills; the letters are outlined by a shadow ring.
        self.assertNotIn("text-stroke", html)

    def test_help_shows_the_official_notes_only_when_asked(self) -> None:
        plain = self.renderer.render_help(command_prefix="/")
        official = self.renderer.render_help(command_prefix="/", official=True)

        self.assertNotIn('class="comic-note', plain)
        self.assertNotIn("获取群内全部消息", plain)
        self.assertIn('class="comic-note', official)
        self.assertIn("获取群内全部消息", official)
        self.assertIn("机器人主动在群聊内发言", official)

    def _notice_batch(self):
        """A #1 changing hands, a #3 entering, a contract board's #6."""

        from core.board_changes import NoticeEntry, PushedAccount

        def row(rank, name, **fields):
            return BossRankingRow(
                rank=rank,
                score_percent=100,
                battle_id=f"btl_upload_{rank:04d}",
                battle_end_at="2026-10-05T10:20:00+08:00",
                character_name="莱万汀",
                character_profession="术士",
                account_id=f"usr_{rank}",
                account_display_name=name,
                dps=fields.pop("dps", 61_204.77),
                duration_ms=fields.pop("duration_ms", 33_391),
                roster_summary=(),
                roster_entries=(),
                character_avatar_url="/images/character/charremoteicon/icon_x.png",
                **fields,
            )

        def entry(slug, boss, dungeon, rank, name, pushed, **fields):
            return NoticeEntry(
                boss_slug=slug,
                boss_name=boss,
                dungeon_name=dungeon,
                seen_at="2026-10-05T02:38:00+00:00",
                rank=rank,
                record=row(rank, name, **fields),
                pushed=tuple(
                    PushedAccount(record=row(after, who), before=before, after=after)
                    for who, before, after in pushed
                ),
            )

        return (
            entry("a", "白刃穿水·残酷", "战争回响", 1, "BlazeR",
                  (("華鳥風月", 1, 2), ("Yanxi", 2, 3))),
            entry("b", "危境再现·罗丹", "危境再现", 3, "dusk・&華鳥風月",
                  (("duck", 3, 4), ("吆一二三", 10, 11))),
            entry("indie_group_ccdg", "破潮之像", "危机合约", 6, "克劳德",
                  (("cordy5134", 9, 10),), contract_tag_score=47,
                  duration_ms=382_451),
        )

    def _render_notice(self, **kwargs) -> str:
        return self.renderer.render_notice(
            self._notice_batch(),
            window_start="2026-10-05T02:30:00+00:00",
            window_end="2026-10-05T02:45:00+00:00",
            top_n=10,
            web_base_url="https://zmdlogs.com",
            **kwargs,
        )

    def test_notice_is_a_wide_comic_page_with_its_chibis_embedded(self) -> None:
        from core.render import page_frame

        html = self._render_notice()

        self.assertEqual(page_frame("notice"), WIDE_FRAME)
        self.assertIn("--zmd-frame-width: 960;", html)
        self.assertIn('class="zmd-main comic-ground', html)
        self.assertIn('aria-label="顶屁股通告"', html)
        self.assertIn("3 个榜单有新纪录", html)
        self.assertIn("10:30 – 10:45 · 前 10 名的变动", html)
        self.assertIn("OOPS!", html)
        self.assertIn("数据来源 ZMDLogs", html)
        version = read_plugin_version(self.root / "metadata.yaml")
        self.assertIn(f"v{version}", html)
        # The surprised girl and the cat with her dream, one picture each,
        # travel inside the page.
        chibis = re.findall(
            r'<img class="comic-chibi[^"]*" src="data:image/webp;base64,', html
        )
        self.assertEqual(len(chibis), 2)
        linked = self._render_notice(embed_fonts=False)
        self.assertLess(len(linked), 300_000)

    def test_notice_draws_one_card_a_board_with_its_record_and_whom_it_pushed(
        self,
    ) -> None:
        html = self._render_notice()

        self.assertEqual(html.count('class="comic-card notice-card'), 3)
        self.assertIn('data-ch="白刃穿水·残酷"', html)
        self.assertIn('data-ch="危机合约"', html)
        self.assertIn('data-ch="#1"', html)
        self.assertIn('data-ch="#3"', html)
        # One new champion, two plain new records.
        self.assertEqual(html.count(">新冠军!<"), 1)
        self.assertEqual(html.count(">NEW!<"), 2)
        # 用时 and DPS; the contract board's score and 用时 instead.
        self.assertIn("0:33.391", html)
        self.assertIn("DPS 61,205", html)
        self.assertIn("47 分", html)
        self.assertIn("用时 6:22.451", html)
        # Whom each pushed down, and the one who fell out of the top 10.
        self.assertEqual(html.count("被顶下去的"), 3)
        self.assertIn("華鳥風月", html)
        self.assertIn("10 → 11", html)
        self.assertEqual(html.count("跌出前 10"), 1)
        # The uploader is named; the main C's face says who, not a word.
        self.assertIn("dusk・&amp;華鳥風月", html)
        self.assertNotIn("主 C", html)
        self.assertIn("https://zmdlogs.com/images/character/", html)

    def test_metadata_uses_current_plugin_identity(self) -> None:
        metadata = (self.root / "metadata.yaml").read_text(encoding="utf-8")

        self.assertIn("name: astrbot_plugin_zmdlog", metadata)
        self.assertIn("display_name: 终末地·藕粉铺子", metadata)
        self.assertIn(
            "repo: https://github.com/Statrue/astrbot_plugin_zmdlog",
            metadata,
        )
        self.assertNotIn("astrbot_plugin_zmdbot", metadata)

    def test_account_is_a_wide_table_of_every_record(self) -> None:
        payload = public_user_rankings_payload()
        payload["accountDisplayName"] = "测试<script>账号"
        base = payload["rankings"][0]
        long_board = "终末地藕粉铺子凹分研究所特别行动小组的超长首领名字"
        payload["rankings"] = [
            {
                **base,
                "rank": rank,
                "scorePercent": 100 - rank,
                "bossSlug": f"slug{rank}",
                "bossName": long_board if rank == 15 else f"首领{rank}",
                "battleId": f"btl_upload_{rank:012d}",
            }
            for rank in range(1, 16)
        ]
        account = parse_public_user_rankings(payload)

        page = build_account_page(
            account, query="usr_1234567890abcdef", web_base_url="https://zmdlogs.com"
        )
        html = self.renderer.render_account(
            account,
            query="usr_1234567890abcdef",
            web_base_url="https://zmdlogs.com",
        )

        # The figures card: 第 1 / 前 3 / 前 10 / 平均百分位.
        self.assertEqual(
            (page.first_places, page.top_three, page.top_ten), (1, 3, 10)
        )
        self.assertEqual(page.average_percentile, "92%")
        self.assertIn("--zmd-frame-width: 960;", html)
        self.assertIn('class="zmd-main"', html)
        self.assertIn(">ACCOUNT<", html)
        self.assertIn('<h1 class="i-title">测试&lt;script&gt;账号</h1>', html)
        self.assertIn("<dt>平均百分位</dt><dd>92%</dd>", html)
        # Never paged: every record is a row, a long board name whole.
        self.assertEqual(html.count('class="w-tr w-acc'), 15)
        self.assertIn(f"<strong>{long_board}</strong>", html)
        self.assertIn("<small>危境再现·罗丹</small>", html)
        # 用时 on yellow, DPS whole, the battle's date.
        self.assertIn('<span class="w-time"><b>0:20.833</b></span>', html)
        self.assertIn(">110,061<", html)
        self.assertNotIn("110,061.2", html)
        self.assertIn(">2026-07-13<", html)
        self.assertNotIn("scene-background", html)

    def test_account_rows_lead_with_the_main_c_and_a_contract_scores(self) -> None:
        payload = public_user_rankings_payload()
        payload["rankings"][0]["contractTagScore"] = 36
        account = parse_public_user_rankings(payload)
        (record,) = account.rankings
        held = replace(
            self._board(1).rows[0],
            battle_id=record.battle_id,
            character_name="卡缪",
        )

        page = build_account_page(
            account,
            query="q",
            web_base_url="https://zmdlogs.com",
            rows_by_battle={record.battle_id: held},
        )
        html = self.renderer.render_account(
            account,
            query="q",
            web_base_url="https://zmdlogs.com",
            rows_by_battle={record.battle_id: held},
        )

        self.assertEqual(
            [face.character_name for face in page.rows[0].roster],
            ["卡缪", "洛茜", "洁尔佩塔", "佩丽卡"],
        )
        self.assertEqual(html.count("i-face is-lead"), 1)
        # A 危机合约 record puts its score where the time goes, the time under.
        self.assertIn(
            '<span class="w-time"><b>36<small>分</small></b>'
            "<small>0:20.833</small></span>",
            html,
        )

    def test_a_board_outside_the_board_list_is_unlisted_never_retired(self) -> None:
        # Upstream cannot tell whether a board it no longer lists is retired:
        # every such board measured on 2026-10-01 still served its ranking.
        account = parse_public_user_rankings(public_user_rankings_payload())
        (record,) = account.rankings

        def drawn(*listed: str) -> str:
            return self.renderer.render_account(
                account,
                query=account.account_id,
                web_base_url="https://zmdlogs.com",
                listed_boards=frozenset(listed),
            )

        unlisted = drawn("dung01_group_bossrush02")
        listed = drawn("dung01_group_bossrush02", record.boss_slug)

        self.assertIn("<small>危境再现·罗丹 · 未收录榜单</small>", unlisted)
        self.assertNotIn("下线", unlisted)
        # The note explains the term only on a page that prints it.
        self.assertIn("不标主 C", unlisted)
        self.assertNotIn("未收录榜单", listed)
        self.assertNotIn("不标主 C", listed)

    def test_the_battle_summary_is_a_wide_page_on_the_new_shell(self) -> None:
        battle = parse_battle_detail(battle_detail_payload())

        html = self.renderer.render_battle(
            battle,
            query="https://zmdlogs.com/battle/btl_upload_abcdef123456",
            web_base_url="https://zmdlogs.com",
        )

        self.assertIn("--zmd-frame-width: 960;", html)
        self.assertIn('id="zmd-root"', html)
        self.assertIn("zmd-wide", html)
        self.assertIn('class="zmd-main"', html)
        self.assertIn(">BATTLE REPORT<", html)
        self.assertIn('<h1 class="i-title">“碾骨之拳”罗丹</h1>', html)
        self.assertIn("<span>危境再现·罗丹</span>", html)
        self.assertIn(f"v{self.renderer.version}", html)
        # The record band: who, when, and the three hero figures in whole
        # units; the main C's face first, its portrait resolved.
        self.assertIn("<strong>测试账号</strong>", html)
        self.assertIn('<span class="b-date">2026-07-13 22:00</span>', html)
        self.assertIn("<span>通关时间</span>", html)
        self.assertIn("<b>0:20.833</b>", html)
        self.assertIn("<span>总 DPS</span><b>110,061</b>", html)
        self.assertIn("<span>总伤害</span><b>2,292,905</b>", html)
        record = html[
            html.index('class="b-record"'):html.index("<strong>伤害构成</strong>")
        ]
        self.assertLess(
            record.index('class="i-face is-lead"'), record.index('class="i-face"')
        )
        self.assertIn("https://zmdlogs.com/images/character/luoxi.png", record)
        self.assertNotIn("合约分数", html)
        # Nothing of the old whole card, its shell or its tables.
        self.assertNotIn('id="zmd-page"', html)
        self.assertNotIn("scene-background", html)
        self.assertNotIn("战斗贡献", html)
        self.assertNotIn("ignored", html)

    def test_damage_shares_are_one_row_a_character(self) -> None:
        html = self.renderer.render_battle(
            parse_battle_detail(battle_detail_payload()),
            query="q",
            web_base_url="https://zmdlogs.com",
        )
        shares = html[html.index("<strong>伤害构成</strong>"):]

        # Highest DPS first, each in its colour; the main C's row is lead.
        self.assertLess(shares.index(">洛茜<"), shares.index(">卡缪<"))
        self.assertIn('class="b-sum-row c1 is-lead"', shares)
        self.assertIn('class="b-sum-row c2"', shares)
        self.assertIn("<small>DPS 97,325</small>", shares)
        self.assertIn('style="width: 88.43%;"', shares)
        self.assertIn("<b>88.4%</b>", shares)
        self.assertIn("<b>11.6%</b>", shares)

    def test_a_contract_record_leads_with_its_score_and_keeps_its_tags_off(
        self,
    ) -> None:
        payload = battle_detail_payload()
        tags = crisis_contract_tags()
        payload["battle"]["contractTags"] = tags
        payload["battle"]["contractTagScore"] = sum(tag["score"] for tag in tags)

        html = self.renderer.render_battle(
            parse_battle_detail(payload),
            query="btl_upload_526563531445",
            web_base_url="https://zmdlogs.com",
        )

        heroes = html[
            html.index('class="b-heroes"'):html.index("<strong>伤害构成</strong>")
        ]
        self.assertLess(
            heroes.index("<span>合约分数</span>"), heroes.index("<span>通关时间</span>")
        )
        self.assertIn('<div class="i-time"><b>14</b></div>', heroes)
        # The tags are 数据's; the 摘要 carries the score alone.
        self.assertNotIn("环境：禁锢", html)
        self.assertNotIn("contract-tag", html)

    def test_the_foot_lists_the_battles_pages_with_this_one_lit(self) -> None:
        battle = parse_battle_detail(battle_detail_payload())

        def strip(views) -> str:
            html = self.renderer.render_battle(
                battle, query="q", web_base_url="https://zmdlogs.com", views=views
            )
            foot = html[html.index('<footer class="i-foot">'):]
            self.assertIn("数据来源 ZMDLogs", foot)
            return foot

        foot = strip((("摘要", True), ("数据", False), ("养成", False)))
        self.assertIn(
            '<span class="is-on">摘要</span><i>·</i><span>数据</span>'
            "<i>·</i><span>养成</span>",
            foot,
        )
        # The strip sits above the source line.
        self.assertLess(foot.index('class="w-views"'), foot.index("数据来源"))
        self.assertIn('<span class="is-on">摘要</span>', strip((("摘要", True),)))
        # A page with no sibling list has no strip at all.
        self.assertNotIn('class="w-views"', strip(()))

    def test_the_shell_marks_a_build_as_the_game_does(self) -> None:
        macros = self.renderer.environment.from_string(
            '{% from "shell/wide-parts.html" import star, level_pips %}'
            "{{ star(lit) }}|{{ level_pips(pips) }}"
        )

        def star(lit: int) -> tuple[str, tuple[int, int, int]]:
            svg = macros.render(
                lit=star_view(lit, top=5), pips=()
            ).split("|")[0]
            return svg, (
                svg.count('class="is-lit"'),
                svg.count('class="is-lead"'),
                svg.count('class="is-base"'),
            )

        # Each blade drawn in the state the presentation gave it; a full
        # star glows.
        self.assertEqual(star(0)[1], (0, 1, 4))
        self.assertEqual(star(2)[1], (2, 1, 2))
        self.assertEqual(star(4)[1], (4, 1, 0))
        self.assertEqual(star(5)[1], (5, 0, 0))
        self.assertIn('class="b-star is-max"', star(5)[0])
        self.assertNotIn("is-max", star(4)[0])
        # The first blade to light is the upper-left one (A), painted
        # among the lit ones.
        self.assertIn(
            '<polygon class="is-lead" points="66,1 208,103 223,144 80,42"/>',
            star(0)[0],
        )

        # Level 3 of a skill capped at 5 by its 精炼: three filled, two
        # open, four crossed out.
        pips = macros.render(
            lit=star_view(0, top=5),
            pips=(PipState.ON,) * 3 + (PipState.OFF,) * 2 + (PipState.LOCKED,) * 4,
        ).split("|")[1]
        self.assertEqual(pips.count('class="is-on"'), 3)
        self.assertEqual(pips.count('<g class="is-x">'), 4)
        self.assertEqual(pips.count('class="is-ring"'), 2 + 4)

    def test_the_build_is_a_wide_page_of_one_card_a_character(self) -> None:
        payload = battle_detail_payload()
        weapon = payload["battle"]["roster"][0]["weapon"]
        weapon["weaponRefine"] = 1
        weapon["skills"] = [
            {"skillKey": "wpn_sp_attr_atk_high", "level": 9},
            {"skillKey": "sk_wpn_sword_0021", "level": 4},
            {"skillKey": "wpn_attr_agi_high", "level": 9},
        ]
        html = self.renderer.render_battle_build(
            parse_battle_detail(payload),
            query="养成 btl_upload_abcdef123456",
            web_base_url="https://zmdlogs.com",
            views=(("摘要", False), ("养成", True)),
        )

        self.assertIn("--zmd-frame-width: 960;", html)
        self.assertIn("zmd-root--battle-build", html)
        self.assertIn(">BATTLE REPORT<", html)
        self.assertIn('<h1 class="i-title">“碾骨之拳”罗丹</h1>', html)
        self.assertIn("<span><b>0:20.833</b> 通关</span>", html)
        # One card a character in roster order, the main C's slot lit.
        self.assertEqual(html.count('<article class="b-lo'), 2)
        self.assertIn('<article class="b-lo is-lead">', html)
        first = html.index("<strong>洛茜</strong>")
        second = html.index("<strong>卡缪</strong>")
        self.assertLess(first, second)
        self.assertIn("伤害占比 <b>88.4%</b>", html)
        # 潜能 5 is full: N over MAX, the star aglow.
        self.assertIn("<b>5</b><i></i><small>MAX</small>", html)
        self.assertIn('class="b-lo-pot is-max"', html)
        # The weapon panel: two named 词条 at MAX, then the weapon skill at
        # its 精炼 cap with five pips crossed out.
        luoxi = html[first:second]
        self.assertLess(luoxi.index("敏捷提升·大"), luoxi.index("攻击提升·大"))
        self.assertLess(luoxi.index("攻击提升·大"), luoxi.index("武器技能"))
        self.assertEqual(luoxi.count('class="b-affix-max"'), 2)
        self.assertIn('class="b-affix-lv is-max">4/4</b>', luoxi)
        self.assertEqual(luoxi.count('<g class="is-x">'), 5)
        self.assertIn("<span>精炼</span><b>1</b>", luoxi)
        # Gear: 护甲 → 护手 → 配件, never the raw item id as a name.
        self.assertLess(luoxi.index("<em>护甲</em>"), luoxi.index("<em>护手</em>"))
        self.assertLess(luoxi.index("<em>护手</em>"), luoxi.index("<em>配件</em>"))
        self.assertIn("<strong>名称未收录</strong>", luoxi)
        self.assertNotIn(">item_equip", html)
        self.assertNotIn("wpn_attr", html)
        self.assertIn('<small class="b-item-enh">+3 / +3 / +2</small>', luoxi)
        # 卡缪: no gear, no skill levels, 精炼 6 at MAX.
        kamiu = html[second:]
        self.assertEqual(kamiu.count('<div class="b-item is-empty">'), 4)
        self.assertEqual(kamiu.count('<div class="is-empty"><span>'), 4)
        self.assertIn("<span>精炼</span><b>MAX</b>", kamiu)
        # The strip with 养成 lit.
        self.assertIn(
            '<span>摘要</span><i>·</i><span class="is-on">养成</span>', html
        )

    def test_a_character_with_nothing_recorded_still_draws_its_card(self) -> None:
        payload = battle_detail_payload()
        entry = payload["battle"]["roster"][1]
        entry.update(characterPotential=None, characterLevel=None, weapon=None)

        html = self.renderer.render_battle_build(
            parse_battle_detail(payload), query="q", web_base_url="https://zmdlogs.com"
        )
        kamiu = html[html.index("<strong>卡缪</strong>"):]

        # Dashes for the level and the 潜能, a dark star, an empty weapon tile.
        self.assertIn('<div class="b-lo-level is-empty">', kamiu)
        self.assertIn('<div class="b-lo-pot is-empty">', kamiu)
        self.assertNotIn('class="is-lead"', kamiu[: kamiu.index("b-lo-levels")])
        self.assertIn('<div class="b-item b-item--wpn is-empty">', kamiu)
        self.assertNotIn("b-refine", kamiu)

    def test_the_character_pages_are_wide_pages_on_the_new_shell(self) -> None:
        from core.models import (
            CharacterType,
            parse_character_boss_statistics,
            parse_character_profile,
            parse_character_statistics,
        )
        from core.render import page_frame
        from tests.helpers import (
            character_boss_statistics_payload,
            character_profile_payload,
            character_statistics_payload,
        )

        laevat = CharacterType(
            "莱万汀", "灼热", "单手剑", "突击", "", key="chr_0016_laevat", rarity=6
        )
        web = "https://zmdlogs.com"
        forms = {
            "角色档案": (
                "character-profile",
                "OPERATOR",
                self.renderer.render_character_profile(
                    parse_character_profile(character_profile_payload()),
                    character=laevat,
                    query="角色档案 莱万汀",
                    web_base_url=web,
                ),
            ),
            "角色档案 --榜单": (
                "character-profile",
                "OPERATOR",
                self.renderer.render_character_profile(
                    parse_character_profile(
                        character_profile_payload(
                            boss_slug="indie_battletower001_ex"
                        )
                    ),
                    character=laevat,
                    query="角色档案 莱万汀 --榜单 白刃",
                    web_base_url=web,
                ),
            ),
            "角色统计": (
                "character-stats",
                "STATISTICS",
                self.renderer.render_character_stats(
                    parse_character_statistics(
                        character_statistics_payload(scope="all")
                    ),
                    query="角色统计",
                    web_base_url=web,
                ),
            ),
            "角色统计 角色": (
                "character-boss",
                "STATISTICS",
                self.renderer.render_character_boss(
                    parse_character_boss_statistics(
                        character_boss_statistics_payload()
                    ),
                    query="角色统计 洛茜",
                    web_base_url=web,
                ),
            ),
        }
        for form, (kind, giant, html) in forms.items():
            with self.subTest(form=form):
                self.assertEqual(page_frame(kind), WIDE_FRAME)
                self.assertIn("--zmd-frame-width: 960;", html)
                self.assertIn('id="zmd-root"', html)
                self.assertIn(f"zmd-root--{kind}", html)
                self.assertIn("zmd-wide", html)
                self.assertIn('class="zmd-main"', html)
                self.assertIn(f">{giant}<", html)
                self.assertIn(f"v{self.renderer.version}", html)
                # Nothing of the old shell.
                self.assertNotIn('id="zmd-page"', html)
                self.assertNotIn("scene-background", html)

    def test_every_font_size_is_a_scale_token(self) -> None:
        # Colours were tokens from the first commit; sizes drifted into
        # nineteen values with half-pixels between them. Each shell's
        # base.css now holds the only sizes its pages may use — the old
        # shell's under resources/common, the V2 shell's under
        # resources/shell — and every token a page uses must exist in the
        # scale of the shell it is built on.
        resources = self.root / "resources"

        def scale(base: Path) -> set[str]:
            css = base.read_text(encoding="utf-8")
            root_start = css.index(":root {")
            root_block = css[root_start : css.index("}", root_start)]
            return set(re.findall(r"--fs-[a-z0-9-]+(?=:)", root_block))

        old_scale = scale(resources / "common" / "base.css")
        new_scale = scale(resources / "shell" / "base.css")
        self.assertTrue(old_scale)
        self.assertTrue(new_scale)

        # A V2 file is the shell's own, or one a V2 page includes.
        v2_files = set((resources / "shell").glob("*.*"))
        for template in resources.rglob("*.html"):
            markup = template.read_text(encoding="utf-8")
            if '{% extends "shell/' in markup:
                v2_files.add(template)
                v2_files.update(
                    resources / name
                    for name in re.findall(r'{% include "([^"]+)" %}', markup)
                )

        raw_size = re.compile(r"font-size:\s*[0-9.]+(?:px|em|rem|%)")
        # The rule is about sizes, not about stylesheets. An inline
        # style="font-size: 13px" or an SVG font-size="13" would put an
        # untokenised size on the page and never be seen by a .css scan.
        # A token spelled inline is still a token, so only digits are refused.
        raw_inline = re.compile(r"font-size\s*[:=]\s*[\"']?\s*[0-9.]")
        token = re.compile(
            r"font-size\s*[:=]\s*[\"']?\s*var\((--fs-[a-z0-9-]+)\)"
        )
        used = {True: set(), False: set()}
        for path in sorted(resources.rglob("*")):
            if path.suffix not in {".css", ".html"}:
                continue
            text = path.read_text(encoding="utf-8")
            refused = raw_size if path.suffix == ".css" else raw_inline
            self.assertEqual(
                refused.findall(text), [], f"{path.name} sets a raw font-size"
            )
            used[path in v2_files].update(token.findall(text))

        self.assertTrue(used[True])
        self.assertTrue(used[False])
        self.assertEqual(used[True] - new_scale, set())
        self.assertEqual(used[False] - old_scale, set())

    def _board(self, count: int, **row_fields) -> BossRanking:
        return BossRanking(
            boss_slug="test-boss",
            boss_name="测试首领",
            dungeon_name="测试副本",
            profession_groups=(),
            rows=tuple(
                BossRankingRow(
                    rank=rank,
                    score_percent=100,
                    battle_id=f"battle-{rank}",
                    battle_end_at="2026-01-01T00:00:00Z",
                    character_name=f"角色{rank}",
                    character_profession="近卫",
                    account_id=f"account-{rank}",
                    account_display_name=f"公开账号{rank}号",
                    dps=100_000.6 - rank,
                    duration_ms=60_000 + rank,
                    roster_summary=(),
                    roster_entries=(),
                    **row_fields,
                )
                for rank in range(1, count + 1)
            ),
        )

    def test_a_ranking_page_holds_ten_rows_and_the_header_counts_them_all(
        self,
    ) -> None:
        ranking = self._board(35)

        first = build_ranking_page(ranking, query="测试")
        self.assertEqual([row.rank for row in first.rows], list(range(1, 11)))
        self.assertEqual((first.record_count, first.page_count), (35, 4))
        self.assertEqual(first.header.title, "测试首领")
        self.assertEqual(first.header.matched_name, "测试副本 · 测试首领")
        self.assertEqual(first.rows[0].dps, "100,000")

        last = build_ranking_page(ranking, query="测试", page=4)
        self.assertEqual([row.rank for row in last.rows], [31, 32, 33, 34, 35])

        html = self.renderer.render_ranking(ranking, query="测试", page=2)
        self.assertIn("公开账号11号", html)
        self.assertIn("公开账号20号", html)
        self.assertNotIn("公开账号10号", html)
        self.assertNotIn("公开账号21号", html)
        self.assertIn("<b>35</b> 条公开记录", html)
        self.assertIn("DPS 口径", html)
        # The picture never says a next page exists, and only 全部 has a strip.
        self.assertNotIn("下一页", html)
        self.assertNotIn("--页", html)
        self.assertNotIn("官网查看", html)

    def test_a_page_past_the_last_is_refused(self) -> None:
        ranking = self._board(35)
        for page in (0, 5, True, "2"):
            with self.subTest(page=page):
                with self.assertRaises(PresentationError):
                    build_ranking_page(ranking, query="测试", page=page)

    def test_all_shows_thirty_rows_and_sends_the_rest_to_the_site(self) -> None:
        html = self.renderer.render_ranking(
            self._board(35), query="测试", page=ALL_PAGES
        )
        self.assertIn("公开账号30号", html)
        self.assertNotIn("公开账号31号", html)
        self.assertIn("其余<b>5</b>条记录请到 ZMDLogs 官网查看", html)

        for count in (30, 12):
            with self.subTest(count=count):
                html = self.renderer.render_ranking(
                    self._board(count), query="测试", page=ALL_PAGES
                )
                self.assertIn(f"公开账号{count}号", html)
                self.assertNotIn("官网查看", html)

    def test_filters_hold_on_every_page_and_the_header_counts_what_they_keep(
        self,
    ) -> None:
        ranking = self._board(35)
        elements = {f"角色{rank}": "物理" for rank in range(1, 36, 2)}

        page = build_ranking_page(
            ranking, query="测试", page=2, element_filter="物理", elements=elements
        )
        self.assertEqual(page.record_count, 18)
        self.assertEqual(page.page_count, 2)
        self.assertEqual(
            [row.rank for row in page.rows], [21, 23, 25, 27, 29, 31, 33, 35]
        )

    def test_the_ranking_is_a_list_page_on_the_new_shell(self) -> None:
        html = self.renderer.render_ranking(self._board(3), query="测试")

        self.assertIn("--zmd-frame-width: 540;", html)
        self.assertIn('id="zmd-root"', html)
        self.assertIn('class="zmd-main"', html)
        self.assertIn("终末地·藕粉铺子", html)
        self.assertIn(">RANKING<", html)
        self.assertIn("TOP 1", html)
        self.assertIn(f"v{self.renderer.version}", html)
        self.assertNotIn("scene-background", html)
        self.assertNotIn('id="zmd-page"', html)

    def test_the_main_c_leads_the_faces_and_no_name_is_cut(self) -> None:
        roster = tuple(
            BossRankingRosterEntry(character_name=name, profession="近卫")
            for name in ("甲", "乙", "主", "丁")
        )
        long_name = "终末地藕粉铺子凹分研究所特别行动小组第一分队长期招募中欢迎"
        ranking = replace(
            self._board(1),
            rows=(
                replace(
                    self._board(1).rows[0],
                    rank=101,
                    character_name="主",
                    account_display_name=long_name,
                    roster_entries=roster,
                ),
            ),
        )

        page = build_ranking_page(ranking, query="测试")
        self.assertEqual(
            [face.character_name for face in page.rows[0].roster],
            ["主", "甲", "乙", "丁"],
        )
        html = self.renderer.render_ranking(ranking, query="测试")
        self.assertIn(f"<strong>{long_name}</strong>", html)
        self.assertIn(">101<", html)
        self.assertEqual(html.count("i-face is-lead"), 1)
        shell_css = (self.root / "resources" / "shell" / "parts.css").read_text(
            encoding="utf-8"
        )
        name_rule = re.search(r"\.i-name strong\s*\{([^}]*)\}", shell_css)
        self.assertIsNotNone(name_rule)
        self.assertNotIn("ellipsis", name_rule.group(1))
        self.assertNotIn("nowrap", name_rule.group(1))

    def test_rdps_board_counts_its_own_rows_and_says_nothing_more(self) -> None:
        ranking = replace(
            self._board(13, rdps=50_000.4), metric="rdps"
        )
        html = self.renderer.render_ranking(ranking, query="测试")

        self.assertIn("<b>13</b> 条公开记录", html)
        self.assertIn("rDPS 口径", html)
        self.assertIn("<span>rDPS</span>50,000", html)
        self.assertNotIn("DPS 榜", html)
        self.assertNotIn("只收录", html)

    def test_contract_ranking_only_shows_score(self) -> None:
        ranking = BossRanking(
            boss_slug="indie_group_ccdg",
            boss_name="破潮之像",
            dungeon_name="危机合约",
            profession_groups=(),
            rows=(
                BossRankingRow(
                    rank=1,
                    score_percent=100,
                    battle_id="secret-battle-id",
                    battle_end_at="2026-01-01T00:00:00Z",
                    character_name="狼卫",
                    character_profession="术士",
                    account_id="account-1",
                    account_display_name="公开账号",
                    dps=17_184.36,
                    duration_ms=385_648,
                    roster_summary=(),
                    roster_entries=(),
                    contract_tag_score=52,
                    contract_tags=(
                        ContractTag(
                            tag_id=1,
                            score=2,
                            name="队列：折刃",
                        ),
                    ),
                ),
            ),
        )

        page = build_ranking_page(ranking, query="危机合约")
        html = self.renderer.render_ranking(ranking, query="危机合约")

        self.assertEqual(page.header.title, "危机合约")
        self.assertEqual(page.header.subtitle, "活动竞速")
        self.assertEqual(page.header.matched_name, "危机合约")
        self.assertNotIn("破潮之像", html)
        # The score takes the time's place; the time moves under it, and no
        # DPS or 口径 is printed for a board ordered by score.
        self.assertIn("按合约分数", html)
        self.assertIn("<b>52</b><small>分</small>", html)
        self.assertIn("<span>用时</span>6:25.648", html)
        self.assertNotIn("按通关用时", html)
        self.assertNotIn("<span>DPS 口径</span>", html)
        self.assertNotIn("17,184", html)
        self.assertIn("<strong>公开账号</strong>", html)
        self.assertNotIn("队列：折刃", html)
        self.assertNotIn("secret-battle-id", html)
        self.assertNotIn("战斗详情", html)

    def test_top_three_template_does_not_leak_ranking_fields(self) -> None:
        card = make_card(
            "secret-slug",
            "首领<script>",
            "完整副本名",
            with_run=True,
        )
        # hot-bosses runs carry 养成 now; the top-3 card lists names and
        # times only.
        run = replace(
            card.top_speed_runs[0], character_potential=5, weapon_refine=6
        )
        card = replace(card, top_speed_runs=(run,))
        html = self.renderer.render_dungeon_top3(
            dungeon_pick("完整副本名", card), (card,), query="<副本>"
        )

        self.assertIn("首领&lt;script&gt;", html)
        self.assertIn("&lt;副本&gt;", html)
        self.assertNotIn("secret-slug", html)
        self.assertNotIn("battle-secret-slug", html)
        self.assertIn("1:01.234", html)
        # The bare "5+6" also occurs by chance in the embedded fonts' base64.
        self.assertNotIn(">5+6<", html)
        self.assertNotIn('class="investment"', html)

    def test_dungeon_scope_groups_cards_by_dungeon(self) -> None:
        cards = (
            make_card("a-1", "榜单甲", "影拓丰碑1期", with_run=True),
            make_card("b-1", "榜单乙", "影拓丰碑2期", with_run=True),
            make_card("a-2", "榜单丙", "影拓丰碑1期", with_run=True),
        )
        choice = MatchChoice(
            target=MatchTarget(
                target_type=TargetType.DUNGEON_SCOPE,
                key="scope:影拓丰碑",
                name="影拓丰碑1—2期",
                dungeon_names=("影拓丰碑1期", "影拓丰碑2期"),
                boss_slugs=tuple(card.boss_slug for card in cards),
            ),
            level=MatchLevel.NORMALIZED_EXACT,
            score=1.0,
            matched_text="丰碑",
        )

        page = build_dungeon_top3_page(choice, cards, query="丰碑")
        html = self.renderer.render_dungeon_top3(choice, cards, query="丰碑")

        self.assertTrue(page.group_by_dungeon)
        self.assertEqual(
            tuple(group.dungeon_name for group in page.card_groups),
            ("影拓丰碑1期", "影拓丰碑2期"),
        )
        self.assertEqual(
            tuple(len(group.cards) for group in page.card_groups),
            (2, 1),
        )
        self.assertEqual(html.count('class="dungeon-group"'), 2)
        self.assertNotIn('class="top3-card-dungeon"', html)
        self.assertLess(
            html.index("<h3>影拓丰碑1期</h3>"),
            html.index("<h3>影拓丰碑2期</h3>"),
        )
        self.assertIn("2 个榜单", html)
        self.assertIn("1 个榜单", html)

    def test_missing_background_is_a_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resources = root / "resources"
            resources.mkdir()
            metadata = root / "metadata.yaml"
            metadata.write_text('version: "1.0.0"\n', encoding="utf-8")

            with self.assertRaises(TemplateConfigurationError):
                TemplateRenderer(
                    resources,
                    metadata,
                    resources / "missing.jpg",
                )

    def test_relative_avatar_paths_resolve_against_web_base_url(self) -> None:
        ranking = BossRanking(
            boss_slug="test-boss",
            boss_name="测试首领",
            dungeon_name="测试副本",
            profession_groups=(),
            rows=(
                BossRankingRow(
                    rank=1,
                    score_percent=100,
                    battle_id="battle-1",
                    battle_end_at="2026-01-01T00:00:00Z",
                    character_name="洛茜",
                    character_profession="近卫",
                    account_id="account-1",
                    account_display_name="公开账号",
                    dps=1.0,
                    duration_ms=1_000,
                    roster_summary=(),
                    roster_entries=(),
                    character_avatar_url="/images/character/luoxi.png",
                ),
                BossRankingRow(
                    rank=2,
                    score_percent=90,
                    battle_id="battle-2",
                    battle_end_at="2026-01-01T00:00:00Z",
                    character_name="卡缪",
                    character_profession="重装",
                    account_id="account-2",
                    account_display_name="公开账号2",
                    dps=1.0,
                    duration_ms=1_000,
                    roster_summary=(),
                    roster_entries=(),
                    character_avatar_url="javascript:alert(1)",
                ),
            ),
        )

        page = build_ranking_page(
            ranking,
            query="测试",
            web_base_url="https://zmdlogs.com",
        )
        self.assertEqual(
            page.rows[0].roster[0].avatar_url,
            "https://zmdlogs.com/images/character/luoxi.png",
        )
        self.assertIsNone(page.rows[1].roster[0].avatar_url)

        # Without a base URL only absolute HTTP(S) URLs survive.
        page = build_ranking_page(ranking, query="测试")
        self.assertIsNone(page.rows[0].roster[0].avatar_url)

        card = make_card("a", "首领", "副本", with_run=True)
        html = self.renderer.render_dungeon_top3(
            dungeon_pick("副本", card),
            (card,),
            query="副本",
            web_base_url="https://zmdlogs.com",
        )
        self.assertNotIn("javascript:", html)

    def test_number_and_duration_formats(self) -> None:
        self.assertEqual(format_duration(61_234), "1:01.234")
        self.assertEqual(format_number(123_456.78), "123,456.78")


class LongImageValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parents[1]

    async def test_missing_page_frame_is_a_render_error(self) -> None:
        class MissingPage:
            async def evaluate(self, script):
                return None

        renderer = object.__new__(LongImageRenderer)
        with self.assertRaises(RenderError):
            await renderer._validate_page(MissingPage())

    async def test_a_v2_page_is_checked_against_its_declared_frame(self) -> None:
        class DrawnPage:
            def __init__(self, **metrics) -> None:
                self.metrics = {
                    "width": 540,
                    "boxWidth": 540,
                    "declaredWidth": 540,
                    "height": 2000,
                    "panelBottom": 1900,
                    **metrics,
                }

            async def evaluate(self, script):
                # The V2 anchors, never the old shell's.
                self.script = script
                return self.metrics

        renderer = object.__new__(LongImageRenderer)
        drawn = DrawnPage()
        self.assertEqual(
            await renderer._validate_page(drawn, LIST_FRAME), drawn.metrics
        )
        self.assertIn("#zmd-root", drawn.script)
        self.assertIn(".zmd-main", drawn.script)
        self.assertNotIn("#zmd-page", drawn.script)
        for wrong in (
            {"width": 560},  # a decoration spilling past the frame
            {"boxWidth": 1280},
            {"declaredWidth": 960},  # the template and the page kind disagree
            {"declaredWidth": 0},
            {"panelBottom": 2100},
            {"height": 0},
        ):
            with self.subTest(wrong=wrong):
                with self.assertRaises(RenderError):
                    await renderer._validate_page(DrawnPage(**wrong), LIST_FRAME)

        # The same page in another frame fails on its width.
        with self.assertRaises(RenderError):
            await renderer._validate_page(DrawnPage(), WIDE_FRAME)

    def test_each_page_kind_declares_its_frame(self) -> None:
        from core.render import page_frame

        self.assertEqual(page_frame("ranking"), LIST_FRAME)
        self.assertEqual((LIST_FRAME.width, LIST_FRAME.scale), (540, 2))
        self.assertEqual((WIDE_FRAME.width, WIDE_FRAME.scale), (960, 2))
        self.assertEqual(page_frame("battle"), WIDE_FRAME)
        self.assertEqual(page_frame("battle-cast"), WIDE_FRAME)
        self.assertEqual(page_frame("battle-build"), WIDE_FRAME)
        # 账号, 趋势 and 角色排名 <角色>: one wide table each, never paged.
        for kind in ("account", "trend", "character-standings"):
            self.assertEqual(page_frame(kind), WIDE_FRAME)
        # Pages not yet on the V2 shell keep the old frame.
        self.assertEqual(page_frame("compare"), LEGACY_FRAME)
        self.assertEqual((LEGACY_FRAME.width, LEGACY_FRAME.scale), (1280, 1))

    async def test_capture_failure_keeps_rendered_html_for_fallback(self) -> None:
        renderer = LongImageRenderer(self.root)

        async def failing_capture(html: str, page_kind: str) -> str:
            raise RenderError("browser capture failed")

        renderer._capture_once = failing_capture
        with self.assertRaises(RenderError) as context:
            await renderer.render_help(command_prefix="/")

        self.assertIn("/zmdlog", context.exception.html or "")
        self.assertIsNone(RenderError("renderer is unavailable").html)
        await renderer.close()

    async def test_default_output_directory_uses_current_plugin_name(self) -> None:
        renderer = LongImageRenderer(self.root)

        self.assertEqual(renderer.output_dir.name, "astrbot_plugin_zmdlog")
        await renderer.close()

    async def test_render_concurrency_is_bounded(self) -> None:
        renderer = LongImageRenderer(
            self.root,
            max_concurrent_renders=2,
        )
        release = asyncio.Event()
        two_started = asyncio.Event()
        active = 0
        peak = 0

        async def capture_once(html: str, page_kind: str) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
            try:
                await release.wait()
                return page_kind
            finally:
                active -= 1

        renderer._capture_once = capture_once
        tasks = tuple(
            asyncio.create_task(renderer._capture("", f"page-{index}"))
            for index in range(6)
        )
        await asyncio.wait_for(two_started.wait(), timeout=1)
        await asyncio.sleep(0)

        self.assertEqual(peak, 2)
        self.assertEqual(active, 2)

        release.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(len(results), 6)
        self.assertEqual(peak, 2)
        await renderer.close()

    async def test_output_pruning_removes_expired_and_excess_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            renderer = LongImageRenderer(
                self.root,
                output_dir=output_dir,
                output_ttl_seconds=60,
                max_output_files=2,
            )
            now = time.time()
            stale = output_dir / "zmd-help-stale.png"
            recent_paths = tuple(
                output_dir / f"zmd-help-recent-{index}.png"
                for index in range(3)
            )
            unrelated = output_dir / "keep.txt"
            for path in (stale, *recent_paths, unrelated):
                path.write_bytes(b"test")
            os.utime(stale, (now - 120, now - 120))
            for index, path in enumerate(recent_paths):
                modified_at = now - (3 - index)
                os.utime(path, (modified_at, modified_at))

            await renderer._prune_output_files()

            self.assertFalse(stale.exists())
            self.assertFalse(recent_paths[0].exists())
            self.assertTrue(recent_paths[1].exists())
            self.assertTrue(recent_paths[2].exists())
            self.assertTrue(unrelated.exists())
            await renderer.close()

    async def test_completed_output_expires_after_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            renderer = LongImageRenderer(
                self.root,
                output_dir=output_dir,
                output_ttl_seconds=0.01,
            )
            output_path = output_dir / "zmd-help-expiring.png"
            output_path.write_bytes(b"test")

            await renderer._complete_output(output_path)
            await asyncio.sleep(0.05)

            self.assertFalse(output_path.exists())
            self.assertNotIn(output_path, renderer._created_files)
            await renderer.close()


if __name__ == "__main__":
    unittest.main()


class AssetRoutingTests(unittest.TestCase):
    """The capture-time image gate: what it fetches, caches and refuses."""

    class FakeRequest:
        def __init__(self, url, resource_type="image"):
            self.url = url
            self.resource_type = resource_type

    class FakeRoute:
        def __init__(self, request, responses):
            self.request = request
            self._responses = responses
            self.fetched: list[str] = []
            self.fulfilled = None
            self.aborted = False

        async def fetch(self, url=None, max_redirects=None):
            target = url or self.request.url
            self.fetched.append(target)
            reply = self._responses.get(target)
            if reply is None:
                raise RuntimeError("no such asset")
            return reply

        async def fulfill(self, *, status, content_type, body):
            self.fulfilled = (status, content_type, body)

        async def abort(self):
            self.aborted = True

        async def continue_(self):
            self.aborted = False

    class FakeResponse:
        def __init__(self, status, body, content_type="image/png"):
            self.status = status
            self._body = body
            self.headers = {"content-type": content_type}

        async def body(self):
            return self._body

    ORIGIN = "https://zmdlogs.com"
    PATH = "/images/character/charremoteicon/icon_chr_0032.png"

    def _renderer(self):
        return LongImageRenderer(
            Path(__file__).parents[1],
            allowed_image_origins=(self.ORIGIN,),
        )

    def _thumb(self):
        from urllib.parse import quote

        return (
            f"{self.ORIGIN}/_next/image?url={quote(self.PATH, safe='')}&w=128&q=75"
        )

    def test_a_pages_images_are_fetched_before_the_capture(self) -> None:
        # One image per route-handler fetch, and those do not overlap: on a
        # link where an image takes a second, a dozen avatars cannot finish
        # inside the settle budget and come out as initials. Prefetching
        # them together is what keeps that off a slow host.
        renderer = self._renderer()
        other = "/images/character/charremoteicon/icon_chr_0028.png"
        renderer._asset_cache.put(
            self.ORIGIN + other,
            status=200,
            content_type="image/png",
            body=b"already here",
        )
        asked: list[str] = []

        async def fetch(url):
            asked.append(url)
            return "image/png", b"prefetched"

        renderer._fetch_image = fetch
        html = (
            f'<img src="{self.ORIGIN}{self.PATH}" alt="">'
            f'<img src="{self.ORIGIN}{self.PATH}" alt="">'
            f'<img src="{self.ORIGIN}{other}" alt="">'
            '<img src="https://evil.example/x.png" alt="">'
        )

        asyncio.run(renderer._prefetch_images(html))

        # The resized copy, once for the repeated image; the cached one and
        # the foreign origin are never asked for.
        self.assertEqual(asked, [self._thumb()])
        cached = renderer._asset_cache.get(self.ORIGIN + self.PATH)
        self.assertEqual(cached.body, b"prefetched")
        self.assertEqual(
            renderer._asset_cache.get(self.ORIGIN + other).body, b"already here"
        )

    def test_a_prefetched_image_costs_the_capture_no_request(self) -> None:
        renderer = self._renderer()

        async def fetch(url):
            return "image/png", b"prefetched"

        renderer._fetch_image = fetch
        asyncio.run(
            renderer._prefetch_images(f'<img src="{self.ORIGIN}{self.PATH}">')
        )
        route = self.FakeRoute(self.FakeRequest(self.ORIGIN + self.PATH), {})

        asyncio.run(renderer._route_asset_request(route))

        self.assertEqual(route.fetched, [])
        self.assertEqual(route.fulfilled, (200, "image/png", b"prefetched"))

    def test_prefetch_keeps_the_original_when_there_is_no_resized_copy(self) -> None:
        renderer = self._renderer()
        asked: list[str] = []

        async def fetch(url):
            asked.append(url)
            if url == self._thumb():
                return None
            return "image/png", b"full size"

        renderer._fetch_image = fetch

        asyncio.run(
            renderer._prefetch_images(f'<img src="{self.ORIGIN}{self.PATH}">')
        )

        self.assertEqual(asked, [self._thumb(), self.ORIGIN + self.PATH])
        self.assertEqual(
            renderer._asset_cache.get(self.ORIGIN + self.PATH).body, b"full size"
        )

    def test_an_unreachable_image_is_left_to_the_route_handler(self) -> None:
        renderer = self._renderer()

        async def fetch(url):
            return None

        renderer._fetch_image = fetch

        asyncio.run(
            renderer._prefetch_images(f'<img src="{self.ORIGIN}{self.PATH}">')
        )

        self.assertIsNone(renderer._asset_cache.get(self.ORIGIN + self.PATH))
        route = self.FakeRoute(
            self.FakeRequest(self.ORIGIN + self.PATH),
            {self._thumb(): self.FakeResponse(200, b"late")},
        )
        asyncio.run(renderer._route_asset_request(route))
        self.assertEqual(route.fulfilled, (200, "image/png", b"late"))

    def test_a_site_image_is_fetched_resized(self) -> None:
        # Upstream portraits are about 1 MB each; a page of them cannot load
        # inside the capture's image budget.
        renderer = self._renderer()
        route = self.FakeRoute(
            self.FakeRequest(self.ORIGIN + self.PATH),
            {self._thumb(): self.FakeResponse(200, b"small")},
        )

        asyncio.run(renderer._route_asset_request(route))

        self.assertEqual(route.fetched, [self._thumb()])
        self.assertEqual(route.fulfilled, (200, "image/png", b"small"))
        # Cached under the URL the page asked for, not the resized one.
        self.assertIsNotNone(renderer._asset_cache.get(self.ORIGIN + self.PATH))

    def test_the_original_is_used_when_there_is_no_resized_copy(self) -> None:
        renderer = self._renderer()
        route = self.FakeRoute(
            self.FakeRequest(self.ORIGIN + self.PATH),
            {
                self._thumb(): self.FakeResponse(404, b"gone"),
                self.ORIGIN + self.PATH: self.FakeResponse(200, b"whole"),
            },
        )

        asyncio.run(renderer._route_asset_request(route))

        self.assertEqual(route.fulfilled, (200, "image/png", b"whole"))

    def test_a_foreign_origin_is_refused(self) -> None:
        renderer = self._renderer()
        route = self.FakeRoute(self.FakeRequest("https://evil.example/a.png"), {})

        asyncio.run(renderer._route_asset_request(route))

        self.assertTrue(route.aborted)
        self.assertEqual(route.fetched, [])

    def test_a_redirect_is_refused(self) -> None:
        renderer = self._renderer()
        route = self.FakeRoute(
            self.FakeRequest(self.ORIGIN + self.PATH),
            {
                self._thumb(): self.FakeResponse(302, b""),
                self.ORIGIN + self.PATH: self.FakeResponse(302, b""),
            },
        )

        asyncio.run(renderer._route_asset_request(route))

        self.assertTrue(route.aborted)
        self.assertIsNone(route.fulfilled)


class RenderQueueTests(unittest.IsolatedAsyncioTestCase):
    """The semaphore bounds concurrency; the queue behind it needs bounds too."""

    def setUp(self) -> None:
        self.root = Path(__file__).parents[1]

    async def test_a_full_queue_is_refused_instead_of_joined(self) -> None:
        # The cap counts callers waiting for a slot, not the one rendering.
        renderer = LongImageRenderer(
            self.root,
            render_timeout_ms=1_000,
            max_concurrent_renders=1,
            max_queued_renders=2,
        )
        release = asyncio.Event()

        async def capture_once(html: str, page_kind: str) -> str:
            await release.wait()
            return page_kind

        renderer._capture_once = capture_once
        rendering = asyncio.create_task(renderer._capture("", "rendering"))
        waiting = [
            asyncio.create_task(renderer._capture("", f"waiting-{index}"))
            for index in range(2)
        ]
        for _ in range(4):
            await asyncio.sleep(0)
        self.assertEqual(renderer._queued_renders, 2)

        with self.assertRaises(RenderError) as refused:
            await renderer._capture("", "refused")
        self.assertIn("queued", str(refused.exception))

        release.set()
        self.assertEqual(await rendering, "rendering")
        self.assertEqual(await asyncio.gather(*waiting), ["waiting-0", "waiting-1"])
        # The slots are given back, so the next caller is served normally.
        self.assertEqual(await renderer._capture("", "later"), "later")
        await renderer.close()

    async def test_waiting_for_a_slot_is_under_a_timeout(self) -> None:
        renderer = LongImageRenderer(
            self.root, render_timeout_ms=1_000, max_concurrent_renders=1
        )
        # The wait is what is under test, not a real second of it.
        renderer.render_timeout_ms = 50
        release = asyncio.Event()

        async def capture_once(html: str, page_kind: str) -> str:
            await release.wait()
            return page_kind

        renderer._capture_once = capture_once
        held = asyncio.create_task(renderer._capture("", "held"))
        await asyncio.sleep(0)

        with self.assertRaises(RenderError) as timed_out:
            await renderer._capture("", "waiting")
        self.assertIn("render slot", str(timed_out.exception))

        release.set()
        await held
        await renderer.close()


class FakeRoute:
    def __init__(self, url: str, resource_type: str = "font") -> None:
        self.request = SimpleNamespace(url=url, resource_type=resource_type)
        self.fulfilled: dict | None = None
        self.aborted = False

    async def fulfill(self, **kwargs) -> None:
        self.fulfilled = kwargs

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        raise AssertionError("must not continue")

    async def fetch(self, **kwargs):
        raise AssertionError("must not fetch")


class FontDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """Chromium gets linked fonts served from memory; fallbacks get them embedded."""

    def setUp(self) -> None:
        self.root = Path(__file__).parents[1]
        self.templates = TemplateRenderer.from_plugin_root(self.root)
        if not self.templates.font_files:
            self.skipTest("bundled fonts are not present")

    def test_linked_fonts_keep_the_document_small(self) -> None:
        linked = self.templates.render_help(command_prefix="/", embed_fonts=False)
        embedded = self.templates.render_help(command_prefix="/")

        self.assertIn(f"{FONT_ORIGIN}/NotoSansSC-Regular.woff2", linked)
        self.assertNotIn("data:font", linked)
        self.assertIn("data:font/woff2;base64,", embedded)
        self.assertNotIn(FONT_ORIGIN, embedded)
        self.assertLess(len(linked), len(embedded) // 10)

    async def test_capture_error_carries_the_self_contained_copy(self) -> None:
        renderer = LongImageRenderer(self.root)
        captured: list[str] = []

        async def failing_capture(html: str, page_kind: str) -> str:
            captured.append(html)
            raise RenderError("browser capture failed")

        renderer._capture_once = failing_capture
        with self.assertRaises(RenderError) as context:
            await renderer.render_help(command_prefix="/")

        # Chromium was handed the small document; the AstrBot fallback cannot
        # reach the route handler, so its copy embeds the fonts.
        self.assertIn(FONT_ORIGIN, captured[0])
        self.assertIn("data:font/woff2", context.exception.html or "")
        await renderer.close()

    async def test_font_requests_are_served_from_memory(self) -> None:
        renderer = LongImageRenderer(
            self.root, allowed_image_origins=("https://zmdlogs.com",)
        )

        known = FakeRoute(f"{FONT_ORIGIN}/NotoSansSC-Regular.woff2")
        await renderer._route_asset_request(known)
        self.assertIsNotNone(known.fulfilled)
        self.assertEqual(
            known.fulfilled["body"],
            renderer.templates.font_files["NotoSansSC-Regular.woff2"],
        )

        unknown = FakeRoute(f"{FONT_ORIGIN}/other.woff2")
        await renderer._route_asset_request(unknown)
        self.assertTrue(unknown.aborted)

        # Only the reserved origin serves fonts; the image origins stay images.
        elsewhere = FakeRoute("https://zmdlogs.com/NotoSansSC-Regular.woff2")
        await renderer._route_asset_request(elsewhere)
        self.assertTrue(elsewhere.aborted)
        await renderer.close()
