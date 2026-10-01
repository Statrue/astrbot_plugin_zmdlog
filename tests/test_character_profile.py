"""角色档案: one character's builds, teammates and 通关名次, from its profile.

The page is the site's ``/character/{key}``: shares of the character's
public records (one per account per board, the fastest) and its clear-time
rank on every board. These tests follow it from the payload to the page.
"""

import asyncio
import logging
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from core import facts, messages
from core.buttons import pick_list_message, result_keyboard
from core.candidates import CandidateStore, CandidateView
from core.client import (
    InvalidBossSlugError,
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsClientError,
    ZmdLogsProtocolError,
)
from core.datasource import ZmdLogsDataSource
from core.matcher import AliasConfig, MatcherCache
from core.models import (
    CharacterType,
    ModelValidationError,
    parse_boss_ranking,
    parse_character_profile,
    parse_character_types,
)
from core.outcome import PageSubject, PageTarget
from core.queries import QueryService
from core.recipes import prepare_character_profile
from core.render import TemplateRenderer
from core.routing import (
    OPTION_USAGE,
    RouteKind,
    RouteParseError,
    parse_zmdlog_payload,
)
from core.settings import PluginSettings
from core.toolbox import ToolService
from tests.helpers import (
    character_profile_payload,
    make_card,
    ranking_payload_with_rows,
)
from tests.test_recipes import OfflineClient
from tests.test_tools import WEB, FakeData, FakeRenderer

ROOT = Path(__file__).resolve().parents[1]
GROUP = "aiocqhttp:GroupMessage:100"


class CatalogKeyTests(unittest.TestCase):
    def test_the_game_data_catalog_names_every_characters_key(self) -> None:
        # The profile is keyed by the character, of every rarity; the
        # six-star statistics catalog knows six-stars only, the game-data
        # catalog everyone. Read leniently like the rest of that catalog.
        types = parse_character_types(
            {
                "entries": [
                    {
                        "id": "chr_0016_laevat",
                        "name": "莱万汀",
                        "charTypeName": "灼热",
                        "rarity": 6,
                    },
                    {
                        "id": "chr_0019_karin",
                        "name": "秋栗",
                        "charTypeName": "物理",
                        "rarity": 4,
                    },
                    {"id": 7, "name": "无键", "charTypeName": "物理", "rarity": "6"},
                ]
            }
        )

        self.assertEqual(
            [(entry.name, entry.key, entry.rarity) for entry in types],
            [
                ("莱万汀", "chr_0016_laevat", 6),
                ("秋栗", "chr_0019_karin", 4),
                ("无键", "", None),
            ],
        )


class ProfileModelTests(unittest.TestCase):
    def test_the_profile_reads_its_shares_and_its_boards(self) -> None:
        profile = parse_character_profile(character_profile_payload())

        self.assertEqual(profile.character_key, "chr_0016_laevat")
        self.assertEqual(profile.range, "7d")
        self.assertIsNone(profile.boss_slug)
        self.assertEqual(
            (profile.sample_count, profile.account_count, profile.boss_count),
            (15, 9, 3),
        )
        top = profile.combinations[0]
        self.assertEqual(
            (top.key, top.name, top.count, top.percent, top.icon_url),
            ("(2, 1)", "2 + 1", 5, 33.33, None),
        )
        self.assertEqual(
            [entry.name for entry in profile.teammates], ["卡缪", "诀", "狼卫"]
        )
        self.assertEqual(
            profile.weapons[0].icon_url, "/images/weapon/icon/wpn_sword_0006.png"
        )
        self.assertEqual(len(profile.equipment), 2)
        board = profile.bosses[0]
        self.assertEqual(
            (
                board.boss_slug,
                board.boss_name,
                board.dungeon_name,
                board.sample_count,
                board.character_rank,
                board.best_duration_ms,
                board.ranked_character_count,
            ),
            (
                "indie_battletower001_ex", "白刃穿水·残酷", "战争回响", 8, 1,
                45_517, 15,
            ),
        )
        fastest = board.rows[0]
        self.assertEqual(
            (
                fastest.rank,
                fastest.battle_id,
                fastest.account_display_name,
                fastest.duration_ms,
                fastest.potential,
                fastest.refinement,
            ),
            (1, "btl_upload_cad50c180d36", "百合末莉", 45_517, 5, 6),
        )
        self.assertIsNone(profile.bosses[2].rows[0].refinement)

    def test_the_lists_the_page_never_draws_are_never_read(self) -> None:
        # characterRows (every character on a board) and bossOptions (every
        # statistics board) are out of scope; a change in them must not fail
        # a profile that draws without them. Nor do the separate potential
        # and refinement shares, which the combinations already hold.
        payload = character_profile_payload()
        payload["bossOptions"] = "not a list"
        payload["potentials"] = None
        payload["refinements"] = [{"key": 1}]
        payload["equipmentUnknownSamples"] = "?"
        for board in payload["bosses"]:
            board["characterRows"] = [{"rank": "first"}]

        profile = parse_character_profile(payload)

        self.assertEqual(len(profile.bosses), 3)

    def test_a_drawn_field_that_is_missing_or_mistyped_fails_the_profile(
        self,
    ) -> None:
        def drop(*keys):
            def change(payload):
                target = payload
                for key in keys[:-1]:
                    target = target[key]
                del target[keys[-1]]

            return change

        def put(value, *keys):
            def change(payload):
                target = payload
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value

            return change

        for label, change in (
            ("characterKey", drop("characterKey")),
            ("range", put("90d", "range")),
            ("sampleCount", drop("sampleCount")),
            ("accountCount", put(-1, "accountCount")),
            ("bossCount", put("3", "bossCount")),
            ("combinations", drop("combinations")),
            ("weapons", put({}, "weapons")),
            ("equipment entry", put(None, "equipment", 0)),
            ("teammate count", drop("teammates", 0, "count")),
            ("share percent", put("33%", "combinations", 0, "percent")),
            ("share name", put(None, "weapons", 0, "name")),
            ("characterRank", put(None, "bosses", 0, "characterRank")),
            ("bestDurationMs", drop("bosses", 0, "bestDurationMs")),
            ("rankedCharacterCount", put(1.5, "bosses", 0, "rankedCharacterCount")),
            ("board sampleCount", drop("bosses", 1, "sampleCount")),
            ("board rows", put(None, "bosses", 0, "rows")),
            ("record accountId", drop("bosses", 0, "rows", 0, "accountId")),
            ("record durationMs", put("45s", "bosses", 0, "rows", 0, "durationMs")),
            ("record potential", put("5", "bosses", 0, "rows", 0, "potential")),
        ):
            with self.subTest(field=label):
                payload = character_profile_payload()
                change(payload)

                with self.assertRaises(ModelValidationError):
                    parse_character_profile(payload)


class ProfileClientTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, respond) -> tuple[ZmdLogsClient, list[httpx.Request]]:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return respond(request)

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        return client, seen

    async def test_the_profile_is_asked_for_by_key_window_and_board(self) -> None:
        client, seen = self._client(
            lambda request: httpx.Response(200, json=character_profile_payload())
        )

        profile = await client.get_character_profile(
            "chr_0016_laevat", time_range="7d"
        )
        await client.get_character_profile(
            "chr_0016_laevat", time_range="all", boss_slug="indie_battletower001_ex"
        )

        self.assertEqual(profile.character_key, "chr_0016_laevat")
        self.assertEqual(
            [(request.url.path, dict(request.url.params)) for request in seen],
            [
                ("/api/characters/chr_0016_laevat/profile", {"range": "7d"}),
                (
                    "/api/characters/chr_0016_laevat/profile",
                    {"range": "all", "boss": "indie_battletower001_ex"},
                ),
            ],
        )

    async def test_refusals_are_api_errors_and_never_retried(self) -> None:
        for status, code in ((404, "character_not_found"), (404, "boss_not_found")):
            with self.subTest(code=code):
                client, seen = self._client(
                    lambda request, status=status, code=code: httpx.Response(
                        status, json={"error": {"code": code, "message": "x"}}
                    )
                )

                with self.assertRaises(ZmdLogsAPIError) as caught:
                    await client.get_character_profile("chr_0016_laevat")

                self.assertEqual(
                    (caught.exception.status_code, caught.exception.code),
                    (status, code),
                )
                self.assertEqual(len(seen), 1)
        # A window upstream does not know is FastAPI's 422 with no error body.
        client, _ = self._client(
            lambda request: httpx.Response(422, json={"detail": [{"type": "x"}]})
        )
        with self.assertRaises(ZmdLogsAPIError) as caught:
            await client.get_character_profile("chr_0016_laevat")
        self.assertEqual(caught.exception.code, "http_422")

    async def test_nothing_is_sent_for_an_unsafe_key_board_or_window(self) -> None:
        client, seen = self._client(
            lambda request: httpx.Response(200, json=character_profile_payload())
        )

        with self.assertRaises(InvalidBossSlugError):
            await client.get_character_profile("../bosses")
        with self.assertRaises(InvalidBossSlugError):
            await client.get_character_profile("chr_0016_laevat", boss_slug="a/b")
        with self.assertRaises(ValueError):
            await client.get_character_profile("chr_0016_laevat", time_range="90d")
        self.assertEqual(seen, [])

    async def test_a_payload_off_the_contract_is_a_protocol_error(self) -> None:
        client, _ = self._client(
            lambda request: httpx.Response(200, json={"characterKey": 1})
        )

        with self.assertRaises(ZmdLogsProtocolError):
            await client.get_character_profile("chr_0016_laevat")


class ProfileCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_read_per_character_window_and_board(self) -> None:
        reads: list[tuple] = []

        class Client:
            async def get_character_profile(
                self, character_key, *, time_range, boss_slug=None
            ):
                reads.append((character_key, time_range, boss_slug))
                return parse_character_profile(character_profile_payload())

        data = ZmdLogsDataSource(
            Client(),
            data_dir=None,
            logger=logging.getLogger("test"),
        )
        self.addAsyncCleanup(data.close)

        for _ in range(2):
            await data.get_character_profile("chr_0016_laevat", time_range="7d")
        await data.get_character_profile("chr_0016_laevat", time_range="all")
        await data.get_character_profile(
            "chr_0016_laevat", time_range="7d", boss_slug="indie_battletower001_ex"
        )
        await data.get_character_profile("chr_0033_camille", time_range="7d")

        self.assertEqual(
            reads,
            [
                ("chr_0016_laevat", "7d", None),
                ("chr_0016_laevat", "all", None),
                ("chr_0016_laevat", "7d", "indie_battletower001_ex"),
                ("chr_0033_camille", "7d", None),
            ],
        )


def catalog_entry(name, element, profession, key, rarity, icon=True):
    icon_path = f"/images/character/charremoteicon/icon_{key}.png" if icon else ""
    return CharacterType(
        name, element, "单手剑", profession, icon_path, key=key, rarity=rarity
    )


# The game-data catalog as the fakes serve it: every rarity, a key each.
CATALOG = (
    catalog_entry("莱万汀", "灼热", "突击", "chr_0016_laevat", 6),
    catalog_entry("卡缪", "电磁", "近卫", "chr_0033_camille", 6),
    catalog_entry("诀", "物理", "近卫", "chr_0032_lizhiyan", 6),
    catalog_entry("狼卫", "灼热", "先锋", "chr_0006_wolfgd", 5),
    catalog_entry("卡契尔", "寒冷", "重装", "chr_0020_meurs", 4, icon=False),
    catalog_entry("秋栗", "物理", "先锋", "chr_0019_karin", 4, icon=False),
)


CONTRACT = "indie_group_ccdg"
# The board list as the fakes serve it: the fixture's three boards, two of
# them one dungeon; a phase of two dungeons the character has no record in;
# and the crisis contract, which keeps no profile.
BOARDS = (
    make_card("indie_battletower001_ex", "白刃穿水·残酷", "战争回响"),
    make_card("indie_battletower012_ex", "无机狂热·残酷", "战争回响"),
    make_card("dung02_group_bossrush03", "危境再现·阿莱克琉斯", "危境再现"),
    make_card("tower04_fire", "撼山雾火·苦难", "影拓丰碑4期 · 山中见犼"),
    make_card("tower04_frost", "霜原残响·苦难", "影拓丰碑4期 · 霜原"),
    make_card(CONTRACT, "破潮之像", "危机合约"),
)


class ProfileData(FakeData):
    """The tools' fake data source, plus the catalog keys and the profile."""

    def __init__(self) -> None:
        super().__init__(ranking=parse_boss_ranking(ranking_payload_with_rows()))
        self.cards = BOARDS
        self.catalog = CATALOG
        # A character the catalog only learns on a re-read: new content.
        self.added_on_refresh: tuple[CharacterType, ...] = ()
        self.payload = character_profile_payload
        self.profile_reads: list[tuple] = []
        self.profile_error: Exception | None = None

    async def get_character_types(self, *, names=()):
        if any(name not in {e.name for e in self.catalog} for name in names):
            self.catalog = self.catalog + self.added_on_refresh
            self.added_on_refresh = ()
        return {entry.name: entry for entry in self.catalog}

    async def get_character_profile(self, character_key, *, time_range, boss_slug=None):
        self.profile_reads.append((character_key, time_range, boss_slug))
        if self.profile_error is not None:
            raise self.profile_error
        if boss_slug == CONTRACT:
            raise ZmdLogsAPIError(404, "boss_not_found", "x")
        payload = self.payload(time_range=time_range, boss_slug=boss_slug)
        payload["characterKey"] = character_key
        return parse_character_profile(payload)


class ProfileQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = ProfileData()
        self.renderer = FakeRenderer()
        self.store = CandidateStore()
        matchers = MatcherCache()
        self.queries = QueryService(
            client=OfflineClient(),
            data=self.data,
            renderer=lambda: self.renderer,
            candidates=self.store,
            board_matcher=lambda cards: matchers.matcher_for(
                cards, AliasConfig.empty()
            ),
            watcher=SimpleNamespace(history_for=lambda account_id: None),
            settings=PluginSettings(web_base_url=WEB),
            logger=logging.getLogger("test"),
        )

    def _ask(self, payload: str):
        route = parse_zmdlog_payload(payload)
        return asyncio.run(
            self.queries.dispatch(route, command_prefix="/", origin=GROUP)
        )

    def _pick(self, listed, number: str):
        entry, choice = self.store.resolve(
            listed.candidates.code, number, origin=GROUP
        )
        return asyncio.run(self.queries.render_pick(entry, choice))

    def test_a_name_draws_its_profile_over_the_window_asked_for(self) -> None:
        outcome = self._ask("角色档案 莱万汀 --范围 7d")

        self.assertEqual(outcome.image_path, "/tmp/character_profile.png")
        self.assertEqual(self.data.profile_reads, [("chr_0016_laevat", "7d", None)])
        (profile,) = self.renderer.args["character_profile"]
        kwargs = self.renderer.kwargs["character_profile"]
        self.assertEqual(profile.range, "7d")
        self.assertEqual(kwargs["character"].name, "莱万汀")
        self.assertEqual(kwargs["query"], "莱万汀")
        # The teammates' rings come from the catalog.
        self.assertEqual(kwargs["elements"]["卡缪"], "电磁")
        # The page is the site's character page, under that window.
        self.assertEqual(
            outcome.target,
            PageTarget(
                PageSubject.CHARACTER,
                "chr_0016_laevat",
                CandidateView.CHARACTER_PROFILE,
                stats_range="7d",
                name="莱万汀",
            ),
        )

    def test_the_whole_history_by_default(self) -> None:
        self._ask("角色档案 莱万汀")

        self.assertEqual(self.data.profile_reads, [("chr_0016_laevat", "all", None)])

    def test_names_resolve_as_every_character_command_does(self) -> None:
        # Pinyin initials, and every rarity: the profile is not a six-star
        # statistic, the site keeps one for every character.
        for typed, key in (("lwt", "chr_0016_laevat"), ("秋栗", "chr_0019_karin")):
            with self.subTest(typed=typed):
                self.data.profile_reads.clear()

                outcome = self._ask(f"角色档案 {typed}")

                self.assertEqual(outcome.target.key, key)
                self.assertEqual(self.data.profile_reads[0][0], key)

    def test_a_non_six_star_has_no_statistics_to_offer(self) -> None:
        # 角色统计 covers six-stars only, so its button would lead nowhere.
        four_star = self._ask("角色档案 秋栗").target
        six_star = self._ask("角色档案 莱万汀").target

        self.assertEqual(
            four_star.unavailable, frozenset({CandidateView.CHARACTER_STATS})
        )
        self.assertEqual(six_star.unavailable, frozenset())

    def test_a_name_the_catalog_lacks_is_looked_for_once_more(self) -> None:
        # A character released since the catalog was read looks exactly like
        # this; the catalog's bounded re-read finds it.
        self.data.added_on_refresh = (
            catalog_entry("新角色", "自然", "辅助", "chr_0099_newcomer", 6),
        )

        outcome = self._ask("角色档案 新角色")

        self.assertEqual(outcome.target.key, "chr_0099_newcomer")

    def test_an_unknown_or_ambiguous_name_is_answered_in_text(self) -> None:
        unknown = self._ask("角色档案 不存在的人")
        ambiguous = self._ask("角色档案 卡")

        self.assertIsNone(unknown.image_path)
        self.assertIn("不存在的人", unknown.message)
        self.assertIn("卡缪 / 卡契尔", ambiguous.message)
        self.assertEqual(self.data.profile_reads, [])
        self.assertEqual(self.renderer.calls, [])

    def test_a_window_without_records_is_answered_in_text(self) -> None:
        def empty(**options):
            payload = character_profile_payload(**options)
            payload.update(
                sampleCount=0, accountCount=0, bossCount=0, combinations=[],
                weapons=[], equipment=[], teammates=[], bosses=[],
            )
            return payload

        self.data.payload = empty

        outcome = self._ask("角色档案 秋栗 --范围 7d")
        whole = self._ask("角色档案 秋栗")

        self.assertEqual(outcome.message, "近 7 天没有带「秋栗」的公开通关记录。")
        self.assertEqual(whole.message, "没有带「秋栗」的公开通关记录。")
        self.assertEqual(self.renderer.calls, [])

    def test_a_character_the_site_keeps_no_profile_of_is_answered_in_text(
        self,
    ) -> None:
        self.data.profile_error = ZmdLogsAPIError(404, "character_not_found", "x")

        outcome = self._ask("角色档案 莱万汀")

        self.assertEqual(outcome.message, messages.CHARACTER_PROFILE_MISSING)

    def test_a_board_without_a_profile_is_refused_by_the_recipe(self) -> None:
        # 危机合约 answers 404 boss_not_found, the code an unknown board gets;
        # asked for a board the index listed, it means the board keeps none.
        self.data.profile_error = ZmdLogsAPIError(404, "boss_not_found", "x")
        character = CATALOG[0]

        refusal = asyncio.run(
            prepare_character_profile(
                self.data,
                character,
                time_range="all",
                boss_slug="indie_group_ccdg",
                query="莱万汀",
                web_base_url=WEB,
            )
        )

        self.assertEqual(refusal, "该榜不提供角色档案。")
        self.assertEqual(
            self.data.profile_reads, [("chr_0016_laevat", "all", "indie_group_ccdg")]
        )

    # --- --榜单: the profile cut to one board ------------------------------------

    def test_a_keyword_naming_one_board_cuts_the_profile_to_it(self) -> None:
        outcome = self._ask("角色档案 莱万汀 --榜单 白刃穿水 --范围 7d")

        self.assertEqual(outcome.image_path, "/tmp/character_profile.png")
        self.assertEqual(
            self.data.profile_reads,
            [("chr_0016_laevat", "7d", "indie_battletower001_ex")],
        )
        (profile,) = self.renderer.args["character_profile"]
        self.assertEqual(profile.boss_slug, "indie_battletower001_ex")
        self.assertEqual(self.renderer.kwargs["character_profile"]["query"], "莱万汀")
        # The site's character page reads the board too.
        self.assertEqual(
            outcome.target,
            PageTarget(
                PageSubject.CHARACTER,
                "chr_0016_laevat",
                CandidateView.CHARACTER_PROFILE,
                stats_range="7d",
                name="莱万汀",
                boss_slug="indie_battletower001_ex",
            ),
        )

    def test_a_keyword_naming_a_dungeon_lists_its_boards(self) -> None:
        outcome = self._ask("角色档案 莱万汀 --榜单 战争回响 --范围 7d")

        entry = outcome.candidates
        self.assertIsNone(outcome.image_path)
        self.assertEqual(self.data.profile_reads, [])
        self.assertIs(entry.view, CandidateView.CHARACTER_PROFILE)
        self.assertEqual(
            [choice.target.key for choice in entry.choices],
            ["indie_battletower001_ex", "indie_battletower012_ex"],
        )
        self.assertEqual((entry.profile_character, entry.stats_range), ("莱万汀", "7d"))
        # The list says whose profile a pick draws.
        self.assertTrue(
            outcome.message.startswith(
                "「战争回响」匹配到 2 个榜单，选一个看莱万汀的角色档案，"
            ),
            outcome.message,
        )

    def test_a_keyword_naming_a_phase_lists_the_boards_of_its_dungeons(
        self,
    ) -> None:
        outcome = self._ask("角色档案 莱万汀 --榜单 影拓丰碑4期")

        self.assertEqual(
            [choice.target.key for choice in outcome.candidates.choices],
            ["tower04_fire", "tower04_frost"],
        )
        self.assertEqual(self.data.profile_reads, [])

    def test_a_pick_draws_the_profile_on_the_board_picked(self) -> None:
        listed = self._ask("角色档案 lwt --榜单 战争回响 --范围 7d")

        outcome = self._pick(listed, "2")

        self.assertEqual(outcome.image_path, "/tmp/character_profile.png")
        self.assertEqual(
            self.data.profile_reads,
            [("chr_0016_laevat", "7d", "indie_battletower012_ex")],
        )
        self.assertEqual(outcome.target.boss_slug, "indie_battletower012_ex")
        self.assertEqual(outcome.target.stats_range, "7d")
        # The list keeps the catalog's name, not what was typed.
        self.assertEqual(self.renderer.kwargs["character_profile"]["query"], "莱万汀")

    def test_a_pick_lists_buttons_name_the_character_and_the_board(self) -> None:
        listed = self._ask("角色档案 莱万汀 --榜单 战争回响 --范围 7d")

        message = pick_list_message(
            listed.candidates, command="/zmdlog", ttl_seconds=600
        )

        commands = [
            button["action"]["data"]
            for row in message.keyboard["content"]["rows"]
            for button in row["buttons"]
        ]
        self.assertEqual(
            commands,
            [
                "/zmdlog 角色档案 莱万汀 --榜单 indie_battletower001_ex --范围 7d",
                "/zmdlog 角色档案 莱万汀 --榜单 indie_battletower012_ex --范围 7d",
            ],
        )
        # Each draws the page its pick does.
        for command, choice in zip(commands, listed.candidates.choices):
            with self.subTest(command=command):
                self.data.profile_reads.clear()

                outcome = self._ask(command.removeprefix("/zmdlog "))

                self.assertEqual(outcome.target.boss_slug, choice.target.key)
                self.assertEqual(
                    self.data.profile_reads,
                    [("chr_0016_laevat", "7d", choice.target.key)],
                )

    def test_the_crisis_contract_is_refused(self) -> None:
        # One board, so no list: upstream is asked, and refuses.
        outcome = self._ask("角色档案 莱万汀 --榜单 危机合约")

        self.assertEqual(outcome.message, "该榜不提供角色档案。")
        self.assertEqual(
            self.data.profile_reads, [("chr_0016_laevat", "all", CONTRACT)]
        )
        self.assertEqual(self.renderer.calls, [])

    def test_a_keyword_naming_no_board_is_answered_in_text(self) -> None:
        outcome = self._ask("角色档案 莱万汀 --榜单 不存在的榜")

        # Worded as every page that exists per board words it.
        self.assertEqual(outcome.message, "没有找到与「不存在的榜」匹配的榜单。")
        self.assertEqual(self.data.profile_reads, [])

    def test_a_slug_still_draws_while_the_board_list_is_unreachable(self) -> None:
        # The pick list's buttons name a board by its slug; without the
        # board list there is nothing to match a plain keyword against.
        async def unreachable():
            raise ZmdLogsClientError("down")

        self.data.list_hot_bosses = unreachable

        outcome = self._ask("角色档案 莱万汀 --榜单 indie_battletower012_ex")

        self.assertEqual(outcome.target.boss_slug, "indie_battletower012_ex")
        with self.assertRaises(ZmdLogsClientError):
            self._ask("角色档案 莱万汀 --榜单 战争回响")

    def test_the_character_is_resolved_before_the_board(self) -> None:
        outcome = self._ask("角色档案 不存在的人 --榜单 战争回响")

        self.assertIn("不存在的人", outcome.message)
        self.assertIsNone(outcome.candidates)

    def test_a_board_without_the_character_says_so_of_the_board(self) -> None:
        # The character has records, only not on this board: the whole
        # profile's wording would say it has none at all.
        outcome = self._ask("角色档案 秋栗 --榜单 撼山雾火 --范围 7d")

        self.assertEqual(outcome.message, "近 7 天该榜没有带「秋栗」的公开通关记录。")
        self.assertEqual(self.renderer.calls, [])


def summary_lines(text: str, name: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(f"「{name}」最常见")]


class ProfileToolTests(unittest.TestCase):
    """The character tool: its two-line build summary, and its 档案 view."""

    def setUp(self) -> None:
        self.data = ProfileData()
        self.renderer = FakeRenderer()
        matchers = MatcherCache()
        self.tools = ToolService(
            client=None,
            data=self.data,
            renderer=lambda: self.renderer,
            board_matcher=lambda cards: matchers.matcher_for(
                cards, AliasConfig.empty()
            ),
            settings=PluginSettings(web_base_url=WEB),
            logger=logging.getLogger("test"),
        )

    def _character(self, name: str, **options):
        return asyncio.run(self.tools.character(name, **options))

    def test_one_character_is_headed_by_its_commonest_build(self) -> None:
        # Asked where 卡缪's teams rank, the answer still says how the
        # records build it: the commonest 养成 and weapon, out of the whole.
        answer = self._character("卡缪")

        self.assertEqual(answer.image_path, "/tmp/character_standings.png")
        self.assertEqual(self.data.profile_reads, [("chr_0033_camille", "all", None)])
        combination, weapon = summary_lines(answer.text, "卡缪")
        self.assertIn("养成组合", combination)
        self.assertIn("2+1", combination)
        self.assertIn("33.3%", combination)
        self.assertIn("5/15", combination)
        self.assertIn("全部榜单 · 全部时间", combination)
        self.assertIn("熔铸火焰", weapon)
        self.assertIn("86.7%", weapon)
        self.assertIn("13/15", weapon)
        # On top: a reply too long for the model is cut from the end.
        self.assertTrue(answer.text.startswith(f"{combination}\n{weapon}\n\n"))
        self.assertIn("最好名次", answer.text)
        self.assertTrue(answer.text.endswith(facts.SOURCE_NOTE))

    def _bare(self) -> tuple[str, str]:
        """The answer about 卡缪 without its build lines, and its picture."""

        full = self._character("卡缪")
        combination, weapon = summary_lines(full.text, "卡缪")
        self.setUp()
        return full.text.replace(f"{combination}\n{weapon}\n\n", ""), full.image_path

    def test_a_summary_that_cannot_be_read_leaves_the_answer_as_it_was(self) -> None:
        bare, picture = self._bare()

        async def unreachable(*, names=()):
            raise ZmdLogsClientError("down")

        for failure, fail in (
            ("profile refused", lambda: setattr(
                self.data, "profile_error", ZmdLogsAPIError(503, "http_503", "x")
            )),
            ("profile off contract", lambda: setattr(
                self.data, "profile_error", ZmdLogsProtocolError("x")
            )),
            ("catalog unreachable", lambda: setattr(
                self.data, "get_character_types", unreachable
            )),
        ):
            with self.subTest(failure=failure):
                self.setUp()
                fail()

                answer = self._character("卡缪")

                self.assertEqual(answer.text, bare)
                self.assertEqual(answer.image_path, picture)

    def test_a_fault_in_the_summary_costs_the_summary_alone(self) -> None:
        bare, picture = self._bare()
        self.data.profile_error = RuntimeError("bug")

        with self.assertLogs("test", level="WARNING") as logged:
            answer = self._character("卡缪")

        self.assertEqual((answer.text, answer.image_path), (bare, picture))
        # The type is logged, never the message.
        self.assertIn("RuntimeError", logged.output[0])
        self.assertNotIn("bug", logged.output[0])

    def test_a_slow_summary_does_not_hold_the_answer(self) -> None:
        bare, picture = self._bare()
        self.tools = ToolService(
            client=None,
            data=self.data,
            renderer=lambda: self.renderer,
            board_matcher=lambda cards: MatcherCache().matcher_for(
                cards, AliasConfig.empty()
            ),
            settings=PluginSettings(web_base_url=WEB),
            logger=logging.getLogger("test"),
            summary_grace_seconds=0.01,
        )
        read = self.data.get_character_profile

        async def slow(*args, **kwargs):
            await asyncio.sleep(5)
            return await read(*args, **kwargs)

        self.data.get_character_profile = slow

        started = time.monotonic()
        answer = self._character("卡缪")

        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual((answer.text, answer.image_path), (bare, picture))

    def test_a_refusal_carries_no_summary(self) -> None:
        # Builds under "no such board" would be answered instead.
        answer = self._character("莱万汀", board="不存在的榜")

        self.assertIn("没有找到与「不存在的榜」匹配", answer.text)
        self.assertIsNone(answer.image_path)
        self.assertEqual(summary_lines(answer.text, "莱万汀"), [])

    def test_the_summary_follows_any_question_about_one_character(self) -> None:
        # On a board too, and over the window asked for; the summary keeps
        # to every board, and says so, whatever board the question names.
        answer = self._character("卡缪", board="白刃穿水", time_range="7d")

        self.assertEqual(answer.image_path, "/tmp/ranking.png")
        self.assertEqual(self.data.profile_reads, [("chr_0033_camille", "7d", None)])
        combination, _weapon = summary_lines(answer.text, "卡缪")
        self.assertIn("全部榜单 · 近 7 天", combination)

    def test_the_commonest_build_is_the_commonest_recorded_one(self) -> None:
        # Old uploads left 养成 and weapons unrecorded; "most records do not
        # say" is no build, so the line names the commonest that is known,
        # its share still out of every record.
        def payload(**options):
            changed = character_profile_payload(**options)
            changed["combinations"].insert(
                0, {"key": "(None, 6)", "name": "未知 + 6", "count": 7,
                    "percent": 46.67, "iconUrl": None},
            )
            changed["weapons"][1]["count"] = 14
            return changed

        self.data.payload = payload

        combination, weapon = summary_lines(self._character("卡缪").text, "卡缪")

        self.assertIn(
            "（潜能+精炼，不计未记录）：2+1，占 33.3%（5/15 条，", combination
        )
        self.assertIn("（不计未记录）：熔铸火焰，占 86.7%（13/15 条）", weapon)

    def test_a_list_with_nothing_recorded_drops_its_line(self) -> None:
        def payload(**options):
            changed = character_profile_payload(**options)
            changed["weapons"] = [
                {"key": "unknown", "name": "未知", "count": 15, "percent": 100.0,
                 "iconUrl": None},
            ]
            return changed

        self.data.payload = payload

        (combination,) = summary_lines(self._character("卡缪").text, "卡缪")

        self.assertIn("2+1", combination)

        # Neither list knowing anything leaves no section at all.
        def nothing(**options):
            changed = payload(**options)
            changed["combinations"] = [
                {"key": "(None, None)", "name": "未知 + 未知", "count": 15,
                 "percent": 100.0, "iconUrl": None},
            ]
            return changed

        self.data.payload = nothing

        text = self._character("卡缪").text

        self.assertEqual(summary_lines(text, "卡缪"), [])
        self.assertNotIn("\n\n\n", text)
        self.assertTrue(text.endswith(f"不代表这些角色或组合更强。\n\n{facts.SOURCE_NOTE}"))

    def test_the_profile_view_draws_the_page_the_command_draws(self) -> None:
        for command, options in (
            ("角色档案 莱万汀 --范围 7d", {"time_range": "7d"}),
            ("角色档案 莱万汀 --榜单 白刃穿水", {"board": "白刃穿水"}),
        ):
            with self.subTest(command=command):
                self.setUp()
                by_command = FakeRenderer()
                queries = QueryService(
                    client=OfflineClient(),
                    data=self.data,
                    renderer=lambda by_command=by_command: by_command,
                    candidates=CandidateStore(),
                    board_matcher=lambda cards: MatcherCache().matcher_for(
                        cards, AliasConfig.empty()
                    ),
                    watcher=SimpleNamespace(history_for=lambda account_id: None),
                    settings=PluginSettings(web_base_url=WEB),
                    logger=logging.getLogger("test"),
                )
                asyncio.run(
                    queries.dispatch(
                        parse_zmdlog_payload(command), command_prefix="/", origin=GROUP
                    )
                )
                drawn_reads = list(self.data.profile_reads)
                self.data.profile_reads.clear()

                answer = self._character("莱万汀", view="档案", **options)

                self.assertEqual(answer.image_path, "/tmp/character_profile.png")
                self.assertEqual(self.data.profile_reads, drawn_reads)
                self.assertEqual(by_command.calls, self.renderer.calls)
                self.assertEqual(
                    by_command.args["character_profile"],
                    self.renderer.args["character_profile"],
                )
                self.assertEqual(
                    by_command.kwargs["character_profile"],
                    self.renderer.kwargs["character_profile"],
                )

    def test_the_profile_view_says_what_the_page_draws(self) -> None:
        text = self._character("莱万汀", view="档案", time_range="7d").text

        self.assertIn("「莱万汀」角色档案（全部榜单 · 近 7 天）", text)
        self.assertIn("15 条通关记录", text)
        self.assertIn("9 个账号", text)
        self.assertIn("同一账号在同一榜单只留最快一场", text)
        # The two lines every single-character answer carries head it.
        self.assertEqual(len(summary_lines(text, "莱万汀")), 2)
        # The four shares, labelled as the page labels them.
        self.assertIn(
            "养成组合（潜能+精炼）：2+1 33.3%、0+1 26.7%、3+6 26.7%、5+? 6.7%、"
            "养成未记录 6.7%",
            text,
        )
        self.assertIn("武器：熔铸火焰 86.7%、武器未记录 13.3%", text)
        self.assertIn("动火用手甲 100%、生物辅助护板 53.3%", text)
        self.assertIn("常见队友：卡缪 100%、诀 86.7%、狼卫 40%", text)
        # Its 通关名次 on every board, fastest-clearing first.
        boards = [line for line in text.splitlines() if line.startswith("#")]
        self.assertEqual(len(boards), 3)
        self.assertTrue(boards[0].startswith("#1/15 白刃穿水·残酷"), boards[0])
        self.assertIn("45.517 秒", boards[0])
        self.assertIn("百合末莉", boards[0])
        self.assertTrue(boards[1].startswith("#1/11 危境再现·阿莱克琉斯"), boards[1])
        self.assertTrue(boards[2].startswith("#10/13 无机狂热·残酷"), boards[2])

    def test_the_profile_view_on_one_board_lists_its_records(self) -> None:
        text = self._character("莱万汀", view="档案", board="白刃穿水").text

        self.assertIn("「莱万汀」角色档案（白刃穿水·残酷 · 战争回响 · 全部时间）", text)
        self.assertIn("该榜通关名次 #1/15", text)
        fastest, second = (
            line for line in text.splitlines() if line.startswith(("#1 ", "#2 "))
        )
        for part in ("百合末莉", "45.517 秒", "2026-09-24", "5+6",
                     "btl_upload_cad50c180d36"):
            self.assertIn(part, fastest)
        self.assertIn("镜花水月", second)
        self.assertIn("2+1", second)
        # Eight records, two listed: upstream lists twenty at most.
        self.assertIn("另有 6 条", text)
        self.assertNotIn("无机狂热", text)

    def test_the_profile_view_answers_in_text_what_it_cannot_draw(self) -> None:
        for name, options, expected in (
            ("莱万汀 卡缪", {}, "角色档案需要且只能填一个角色名"),
            ("", {}, "角色档案需要且只能填一个角色名"),
            ("不存在的人", {}, "不存在的人"),
            # No pick list on a tool call: the model asks which board.
            ("莱万汀", {"board": "战争回响"},
             "「战争回响」是副本，角色档案要按具体榜单看"),
            ("莱万汀", {"board": "危机合约"}, messages.BOARD_HAS_NO_PROFILE),
            ("莱万汀", {"board": "不存在的榜"}, "没有找到与「不存在的榜」匹配"),
            ("莱万汀", {"view": "装备"}, "「装备」不是可用的 view"),
        ):
            with self.subTest(name=name, **options):
                self.setUp()
                options.setdefault("view", "档案")

                answer = self._character(name, **options)

                self.assertIn(expected, answer.text)
                self.assertIsNone(answer.image_path)
                self.assertEqual(self.renderer.calls, [])

    def test_the_profile_view_over_every_board_is_the_whole_profile(self) -> None:
        answer = self._character("莱万汀", view="档案", board="全部")

        self.assertEqual(answer.image_path, "/tmp/character_profile.png")
        self.assertEqual(self.data.profile_reads, [("chr_0016_laevat", "all", None)])

    def test_the_profile_view_says_which_options_it_has_no_use_for(self) -> None:
        # The profile counts clears, by time: upstream ignores both.
        answer = self._character("莱万汀", view="档案", metric="rdps", potential="0")

        self.assertEqual(answer.image_path, "/tmp/character_profile.png")
        first = answer.text.splitlines()[0]
        self.assertIn("metric", first)
        self.assertIn("potential", first)
        self.assertIn("「莱万汀」角色档案", answer.text)
        # Neither said, or only the defaults, nothing to say.
        for options in ({}, {"metric": "dps", "potential": "all"}):
            with self.subTest(**options):
                text = self._character("莱万汀", view="档案", **options).text
                self.assertTrue(text.startswith("「莱万汀」角色档案"), text[:40])

    def test_no_summary_without_exactly_one_character(self) -> None:
        for name, options in (
            ("黎风 卡缪", {}),
            ("", {}),
            ("", {"board": "白刃穿水"}),
        ):
            with self.subTest(name=name, **options):
                self.setUp()

                answer = self._character(name, **options)

                self.assertNotIn("最常见的养成组合", answer.text)
                self.assertEqual(self.data.profile_reads, [])


class ProfileTemplateTests(unittest.TestCase):
    """What the page prints, from the template the capture would load."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.renderer = TemplateRenderer.from_plugin_root(ROOT)

    def _html(self, payload=None, *, character=CATALOG[0], icons=None) -> str:
        profile = parse_character_profile(payload or character_profile_payload())
        return self.renderer.render_character_profile(
            profile,
            character=character,
            query="lwt",
            web_base_url=WEB,
            elements={entry.name: entry.element for entry in CATALOG},
            icons=icons or {},
        )

    def test_the_hero_names_the_character_window_and_sample(self) -> None:
        html = self._html()

        self.assertIn('<span class="title-text">莱万汀</span>', html)
        self.assertIn('<span class="title-tag">角色档案</span>', html)
        self.assertIn('<p class="page-subtitle">突击</p>', html)
        # Its portrait is the catalog's, in its element's ring.
        self.assertIn('class="avatar avatar--hero el-fire"', html)
        self.assertIn(
            f"{WEB}/images/character/charremoteicon/icon_chr_0016_laevat.png", html
        )
        self.assertIn("<span>范围</span><strong>近 7 天</strong>", html)
        self.assertIn("<span>样本</span><strong>15</strong>", html)
        self.assertIn("<span>公开账号</span><strong>9</strong>", html)
        # The boards it was fielded on; 榜单 alone is the board the page is
        # cut to, once it can be.
        self.assertIn("<span>上榜榜单</span><strong>3</strong>", html)
        # What the percentages are out of, stated where the page starts.
        self.assertIn(
            "只统计带该角色的公开有效通关记录，同一账号在同一榜单只留最快一场", html
        )

    def test_four_share_blocks_then_the_boards_in_that_order(self) -> None:
        html = self._html()

        headings = [
            html.index(f"<h2>{title}</h2>")
            for title in ("养成组合", "武器", "装备", "常见队友", "各榜通关名次")
        ]
        self.assertEqual(headings, sorted(headings))

    def test_a_combination_is_written_as_every_page_writes_its_investment(
        self,
    ) -> None:
        html = self._html()

        # 5+6 under an avatar elsewhere, so 5+6 here: not upstream's "5 + 6".
        self.assertIn('<span class="share-name">2+1</span>', html)
        self.assertIn('<span class="share-name">5+?</span>', html)
        self.assertIn('<span class="share-name">养成未记录</span>', html)
        self.assertNotIn("2 + 1", html)
        self.assertNotIn("未知", html)
        # Shares out of the sample, one decimal, with the count beside.
        self.assertIn("<b>33.3%</b><i>5 条</i>", html)
        self.assertIn('style="width: 33.33%;"', html)

    def test_gear_and_teammates_carry_their_pictures(self) -> None:
        html = self._html(icons={"卡缪": "/images/character/camille.png"})

        self.assertIn('<span class="share-name">熔铸火焰</span>', html)
        self.assertIn('<span class="share-name">武器未记录</span>', html)
        self.assertIn(f"{WEB}/images/weapon/icon/wpn_sword_0006.png", html)
        self.assertIn(
            f"{WEB}/images/equip/iconbig/item_equip_t4_suit_heal01_edc_03.png", html
        )
        self.assertIn("<b>100%</b><i>15 条</i>", html)
        # A teammate's face is the catalog's when it names one, else the
        # conventional path by key; its ring is its element.
        self.assertIn(f"{WEB}/images/character/camille.png", html)
        self.assertIn(
            f"{WEB}/images/character/charremoteicon/icon_chr_0006_wolfgd.png", html
        )
        self.assertIn('class="avatar el-pulse"', html)

    def test_a_raw_item_id_is_never_printed_as_a_name(self) -> None:
        payload = character_profile_payload()
        payload["weapons"][0]["name"] = "wpn_sword_0006"
        payload["equipment"][0]["name"] = "item_equip_t4_suit_fire_natr01_hand_02"

        html = self._html(payload)

        self.assertNotIn('<span class="share-name">wpn_sword_0006</span>', html)
        self.assertNotIn("share-name\">item_equip", html)
        self.assertEqual(html.count('<span class="share-name">名称未收录</span>'), 2)

    def test_a_long_list_shows_its_head_and_counts_the_rest(self) -> None:
        payload = character_profile_payload()
        payload["teammates"] = [
            {
                "key": f"chr_00{index:02d}_mate",
                "name": f"队友{index}",
                "count": 20 - index,
                "percent": float(20 - index),
                "iconUrl": None,
            }
            for index in range(12)
        ]

        html = self._html(payload)

        self.assertIn("队友7", html)
        self.assertNotIn("队友8", html)
        self.assertIn("另有 4 个未列出", html)

    def test_boards_run_best_clear_rank_first(self) -> None:
        html = self._html()

        # Upstream sends the most sampled board first; the page leads with
        # where the character clears fastest. Equal ranks keep that order.
        boards = [
            html.index(name)
            for name in ("白刃穿水·残酷", "危境再现·阿莱克琉斯", "无机狂热·残酷")
        ]
        self.assertEqual(boards, sorted(boards))
        self.assertRegex(html, r"<b>10</b>\s*<i>/ 13</i>")
        # The fastest clear, and whose record it is.
        self.assertIn("<b>0:45.517</b>", html)
        self.assertIn("<strong>百合末莉</strong>", html)
        self.assertIn("该榜带该角色的记录 8 条", html)

    def test_no_drawn_text_comes_from_the_unread_lists(self) -> None:
        payload = character_profile_payload()
        payload["bosses"][0]["characterRows"][0]["characterName"] = "只在角色行里"
        payload["bossOptions"][0]["bossName"] = "只在榜单选项里"
        payload["potentials"][0]["name"] = "只在潜能分布里"

        html = self._html(payload)

        for sentinel in ("只在角色行里", "只在榜单选项里", "只在潜能分布里"):
            self.assertNotIn(sentinel, html)

    # --- cut to one board ----------------------------------------------------------

    def _board_html(self, payload=None) -> str:
        return self._html(
            payload or character_profile_payload(boss_slug="indie_battletower001_ex")
        )

    def test_a_board_page_lists_that_boards_records_for_the_boards(self) -> None:
        html = self._board_html()

        self.assertIn("<h2>该榜记录</h2>", html)
        self.assertNotIn("<h2>各榜通关名次</h2>", html)
        self.assertNotIn('class="standings profile-boards"', html)
        # The four share blocks stay, ahead of the records.
        headings = [
            html.index(f"<h2>{title}</h2>")
            for title in ("养成组合", "武器", "装备", "常见队友", "该榜记录")
        ]
        self.assertEqual(headings, sorted(headings))

    def test_a_record_row_is_its_account_time_date_and_investment(self) -> None:
        html = self._board_html()

        first, second = (
            html.index(f"<strong>{name}</strong>") for name in ("百合末莉", "镜花水月")
        )
        self.assertLess(first, second)
        self.assertRegex(html, r'<div class="standings-rank">\s*<b>1</b>')
        self.assertIn("btl_upload_cad50c180d36", html)
        self.assertIn("<b>0:45.517</b>", html)
        self.assertIn("<b>0:58.871</b>", html)
        # The date under the word the account and 群榜 pages print it under.
        self.assertRegex(html, r"<span>战斗日期</span>\s*<b>2026-09-24</b>")
        # 养成 written as under every avatar.
        self.assertIn('<b class="investment-text">5+6</b>', html)
        self.assertIn('<b class="investment-text">2+1</b>', html)

    def test_an_unrecorded_weapon_or_potential_is_written_as_elsewhere(self) -> None:
        payload = character_profile_payload(boss_slug="indie_battletower001_ex")
        rows = payload["bosses"][0]["rows"]
        rows[0].update(potential=3, refinement=None)
        rows[1].update(potential=None, refinement=None)

        html = self._board_html(payload)

        self.assertIn('<b class="investment-text">3+?</b>', html)
        self.assertIn('<b class="investment-text">—</b>', html)

    def test_a_date_is_the_servers_day(self) -> None:
        # Upstream stamps each record in its uploader's offset.
        payload = character_profile_payload(boss_slug="indie_battletower001_ex")
        payload["bosses"][0]["rows"][0]["battleEndAt"] = "2026-07-23T10:29:31-04:00"

        html = self._board_html(payload)

        self.assertIn("<b>2026-07-23</b>", html)
        payload["bosses"][0]["rows"][0]["battleEndAt"] = "2026-07-23T13:29:31-04:00"
        self.assertIn("<b>2026-07-24</b>", self._board_html(payload))

    def test_a_board_page_names_its_board_and_its_clear_rank(self) -> None:
        html = self._board_html()

        self.assertIn(
            "<span>榜单</span><strong>白刃穿水·残酷 · 战争回响</strong>", html
        )
        self.assertIn(
            "<span>通关名次</span><strong>1</strong><em>上榜角色 15</em>", html
        )
        self.assertNotIn("上榜榜单", html)
        self.assertIn("莱万汀 · 角色档案 · 白刃穿水·残酷", html)
        # Its sample is the board's.
        self.assertIn("<span>样本</span><strong>8</strong>", html)

    def test_records_past_the_ones_listed_are_counted(self) -> None:
        # Upstream lists twenty records at most, one per account; the board's
        # sample counts them all.
        html = self._board_html()

        self.assertIn("只列最快 2 条，共 8 条", html)

        payload = character_profile_payload(boss_slug="indie_battletower001_ex")
        payload["bosses"][0]["sampleCount"] = 2
        self.assertNotIn("只列最快", self._board_html(payload))


def profile_page(**options) -> PageTarget:
    return PageTarget(
        PageSubject.CHARACTER,
        "chr_0016_laevat",
        CandidateView.CHARACTER_PROFILE,
        name="莱万汀",
        **options,
    )


def keyboard_buttons(target: PageTarget) -> list[tuple[int, str, str]]:
    """``(action type, label, data)`` of every button under ``target``'s picture."""

    keyboard = result_keyboard(target, web_base_url=WEB, command="/zmdlog")
    return [
        (
            button["action"]["type"],
            button["render_data"]["label"],
            button["action"]["data"],
        )
        for row in keyboard["content"]["rows"]
        for button in row["buttons"]
    ]


class ProfileButtonTests(unittest.TestCase):
    def test_the_site_page_and_the_characters_other_pages(self) -> None:
        self.assertEqual(
            keyboard_buttons(profile_page()),
            [
                (0, "在 ZMDLogs 打开", f"{WEB}/character/chr_0016_laevat"),
                (2, "角色统计", "/zmdlog 角色统计 莱万汀"),
                (2, "角色排名", "/zmdlog 角色排名 莱万汀"),
            ],
        )

    def test_a_window_goes_wherever_it_means_the_same(self) -> None:
        # The site page and 角色统计 read the same window; 角色排名 with a
        # name takes none.
        self.assertEqual(
            keyboard_buttons(profile_page(stats_range="7d")),
            [
                (0, "在 ZMDLogs 打开", f"{WEB}/character/chr_0016_laevat?range=7d"),
                (2, "角色统计", "/zmdlog 角色统计 莱万汀 --范围 7d"),
                (2, "角色排名", "/zmdlog 角色排名 莱万汀"),
            ],
        )

    def test_a_character_without_statistics_offers_none(self) -> None:
        target = profile_page(unavailable=frozenset({CandidateView.CHARACTER_STATS}))

        self.assertEqual(
            [label for _, label, _ in keyboard_buttons(target)],
            ["在 ZMDLogs 打开", "角色排名"],
        )

    def test_every_command_draws_the_page_its_label_names(self) -> None:
        for stats_range in ("all", "14d"):
            for kind, label, data in keyboard_buttons(
                profile_page(stats_range=stats_range)
            ):
                if kind == 0:
                    continue
                with self.subTest(command=data):
                    route = parse_zmdlog_payload(data.removeprefix("/zmdlog "))

                    self.assertEqual(route.query, "莱万汀")
                    self.assertIs(
                        route.kind,
                        {
                            "角色统计": RouteKind.CHARACTER_STATS,
                            "角色排名": RouteKind.CHARACTER_STANDINGS,
                        }[label],
                    )
                    if route.kind is RouteKind.CHARACTER_STATS:
                        self.assertEqual(route.stats_range, stats_range)

    def test_a_board_page_opens_the_site_on_that_board(self) -> None:
        # The site's page reads both, in this order; the other pages of the
        # character have no board to keep.
        self.assertEqual(
            keyboard_buttons(
                profile_page(stats_range="7d", boss_slug="indie_battletower001_ex")
            ),
            [
                (
                    0,
                    "在 ZMDLogs 打开",
                    f"{WEB}/character/chr_0016_laevat"
                    "?range=7d&boss=indie_battletower001_ex",
                ),
                (2, "角色统计", "/zmdlog 角色统计 莱万汀 --范围 7d"),
                (2, "角色排名", "/zmdlog 角色排名 莱万汀"),
            ],
        )
        self.assertEqual(
            keyboard_buttons(profile_page(boss_slug="indie_group_x"))[0][2],
            f"{WEB}/character/chr_0016_laevat?boss=indie_group_x",
        )

    def test_a_board_cannot_leave_its_parameter(self) -> None:
        target = profile_page(boss_slug="a&range=30d#x")

        self.assertEqual(
            keyboard_buttons(target)[0][2],
            f"{WEB}/character/chr_0016_laevat?boss=a%26range%3D30d%23x",
        )

    def test_a_key_cannot_leave_its_path(self) -> None:
        target = PageTarget(
            PageSubject.CHARACTER,
            "../admin?x=1",
            CandidateView.CHARACTER_PROFILE,
            name="莱万汀",
        )

        self.assertEqual(
            keyboard_buttons(target)[0][2], f"{WEB}/character/..%2Fadmin%3Fx%3D1"
        )


class ProfileRouteTests(unittest.TestCase):
    def test_the_command_takes_a_name_and_a_window(self) -> None:
        route = parse_zmdlog_payload("角色档案 莱万汀 --范围 两周")

        self.assertEqual(route.kind, RouteKind.CHARACTER_PROFILE)
        self.assertEqual((route.query, route.stats_range), ("莱万汀", "14d"))
        with self.assertRaisesRegex(RouteParseError, "角色名"):
            parse_zmdlog_payload("角色档案")

    def test_every_other_option_says_where_it_works(self) -> None:
        for payload, option in (
            ("角色档案 莱万汀 --口径 rdps", "metric"),
            ("角色档案 莱万汀 --潜能 0", "potential"),
            ("角色档案 莱万汀 --top 5", "top"),
            ("角色档案 莱万汀 --角色 卡缪", "character"),
            ("角色档案 莱万汀 --属性 灼热", "element"),
            ("角色档案 莱万汀 --职业 突击", "profession"),
        ):
            with self.subTest(option=option):
                with self.assertRaises(RouteParseError) as caught:
                    parse_zmdlog_payload(payload)

                self.assertEqual(str(caught.exception), OPTION_USAGE[option])

    def test_one_board_is_named_by_a_keyword(self) -> None:
        # A board keyword can run to several words, as typed after zmdlog:
        # everything up to the next option is the keyword.
        for payload, keyword in (
            ("角色档案 莱万汀 --榜单 罗丹", "罗丹"),
            ("角色档案 莱万汀 --榜单 白刃穿水 残酷 --范围 7d", "白刃穿水 残酷"),
            ("角色档案 莱万汀 --board indie_group_ccdg", "indie_group_ccdg"),
        ):
            with self.subTest(payload=payload):
                route = parse_zmdlog_payload(payload)

                self.assertEqual(route.kind, RouteKind.CHARACTER_PROFILE)
                self.assertEqual((route.query, route.board_query), ("莱万汀", keyword))
        self.assertIsNone(parse_zmdlog_payload("角色档案 莱万汀").board_query)

    def test_the_board_option_belongs_to_the_profile_alone(self) -> None:
        for payload in (
            "罗丹 --榜单 罗丹",
            "角色统计 莱万汀 --榜单 罗丹",
            "角色排名 莱万汀 --榜单 罗丹",
            "榜单 --榜单 罗丹",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError) as caught:
                    parse_zmdlog_payload(payload)

                self.assertEqual(str(caught.exception), OPTION_USAGE["board"])
        self.assertIn("角色档案", OPTION_USAGE["board"])
        with self.assertRaisesRegex(RouteParseError, "--榜单 后需要填写取值"):
            parse_zmdlog_payload("角色档案 莱万汀 --榜单")

    def test_the_window_option_names_the_profile_where_it_works(self) -> None:
        self.assertIn("角色档案", OPTION_USAGE["range"])


if __name__ == "__main__":
    unittest.main()
