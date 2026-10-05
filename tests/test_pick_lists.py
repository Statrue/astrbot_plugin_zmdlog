"""A pick list travels as the list itself, not only as its text.

Every path that posts a candidate list hands back the ``PendingCandidates``
it remembered alongside the formatted text, so the host can build buttons
from the entries without re-parsing the message. The text stays exactly what
``format_candidates`` writes for that entry.
"""

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.candidates import CandidateStore, CandidateView, format_candidates
from core.matcher import AliasConfig, MatcherCache
from core.messages import MORE_NICKNAME_HITS
from core.models import HotBossCard
from core.queries import QueryService
from core.rank_watch import RankWatcher
from core.routing import RouteKind, RouteRequest
from core.settings import PluginSettings
from tests.helpers import make_card

GROUP = "aiocqhttp:GroupMessage:100"
DUNGEON = "影拓丰碑4期 · 山中见犼"


def run(coro):
    return asyncio.run(coro)


class TwoHitClient:
    async def search_public_accounts(self, query, *, limit):
        return SimpleNamespace(
            query=query,
            has_more=False,
            accounts=(
                SimpleNamespace(account_id="usr_a", account_display_name="CPU 0"),
                SimpleNamespace(account_id="usr_b", account_display_name="cpu0"),
            ),
        )


class TwoBoardData:
    """One dungeon with two boards: a board-only view must ask which."""

    def __init__(self) -> None:
        self.cards: tuple[HotBossCard, ...] = (
            make_card("dung04_boss_hard", "山中见犼·苦难", DUNGEON),
            make_card("dung04_boss_normal", "山中见犼·寻常", DUNGEON),
        )

    async def list_hot_bosses(self):
        return self.cards


class NoHistory:
    def history_by_name(self, query):
        return ()


class TwoHistories:
    def history_by_name(self, query):
        return (
            SimpleNamespace(account_id="usr_a", display_name="CPU 0"),
            SimpleNamespace(account_id="usr_b", display_name="CPU 1"),
        )


def board_matcher_factory():
    matchers = MatcherCache()

    def board_matcher(cards):
        return matchers.matcher_for(cards, AliasConfig.empty())

    return board_matcher


class QueryPickListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = CandidateStore()

    def _service(self, *, watcher=None) -> QueryService:
        return QueryService(
            client=TwoHitClient(),
            data=TwoBoardData(),
            renderer=lambda: None,
            candidates=self.store,
            board_matcher=board_matcher_factory(),
            watcher=watcher or NoHistory(),
            settings=PluginSettings(),
            logger=logging.getLogger("test"),
        )

    def _dispatch(self, kind: RouteKind, query: str, *, watcher=None, **options):
        route = RouteRequest(kind, query=query, **options)
        return run(
            self._service(watcher=watcher).dispatch(
                route, command_prefix="/", origin=GROUP, requester_key="qq:1"
            )
        )

    def _assert_carries_its_list(self, outcome, *, view: CandidateView) -> None:
        entry = outcome.candidates
        self.assertIsNotNone(entry)
        self.assertIsNone(outcome.image_path)
        self.assertEqual(entry.view, view)
        self.assertEqual(entry.origin, GROUP)
        # The entry is the one a quoted reply resolves, not a copy.
        resolved = self.store.resolve(entry.code, "1", origin=GROUP)
        self.assertIsNotNone(resolved)
        self.assertIs(resolved[0], entry)
        self.assertEqual(
            outcome.message,
            format_candidates(entry, ttl_seconds=self.store.ttl_seconds),
        )

    def test_board_only_views_carry_the_board_list(self) -> None:
        cases = {
            RouteKind.BATTLE_QUERY: CandidateView.BATTLE,
            RouteKind.LOADOUT_QUERY: CandidateView.LOADOUT,
            RouteKind.SKILL_QUERY: CandidateView.SKILLS,
            RouteKind.TIMELINE_QUERY: CandidateView.TIMELINE,
            RouteKind.COMPARE_QUERY: CandidateView.COMPARE,
            RouteKind.ROSTER_QUERY: CandidateView.ROSTER,
            RouteKind.CHARACTER_STATS: CandidateView.CHARACTER_STATS,
        }
        for kind, view in cases.items():
            with self.subTest(kind=kind):
                outcome = self._dispatch(kind, DUNGEON)
                self._assert_carries_its_list(outcome, view=view)
                self.assertEqual(len(outcome.candidates.choices), 2)

    def test_the_list_keeps_the_route_options(self) -> None:
        outcome = self._dispatch(
            RouteKind.BATTLE_QUERY, DUNGEON, battle_rank=3, metric="rdps"
        )

        self.assertEqual(outcome.candidates.battle_rank, 3)
        self.assertEqual(outcome.candidates.metric, "rdps")

    def test_an_account_nickname_carries_the_account_list(self) -> None:
        outcome = self._dispatch(RouteKind.ACCOUNT_QUERY, "CPU")

        self._assert_carries_its_list(outcome, view=CandidateView.RANKING)
        self.assertEqual(
            [choice.target.key for choice in outcome.candidates.choices],
            ["usr_a", "usr_b"],
        )

    def test_a_keyword_that_misses_every_board_carries_the_account_list(self) -> None:
        outcome = self._dispatch(RouteKind.SMART_QUERY, "CPU")

        self._assert_carries_its_list(outcome, view=CandidateView.RANKING)
        self.assertEqual(len(outcome.candidates.choices), 2)

    def test_a_trend_nickname_carries_the_account_list(self) -> None:
        searched = self._dispatch(RouteKind.TREND_QUERY, "CPU", stats_range="7d")
        self._assert_carries_its_list(searched, view=CandidateView.TREND)
        self.assertEqual(searched.candidates.stats_range, "7d")

        local = self._dispatch(RouteKind.TREND_QUERY, "CPU", watcher=TwoHistories())
        self._assert_carries_its_list(local, view=CandidateView.TREND)

    def test_the_list_carries_its_note_too(self) -> None:
        class MoreHitsClient(TwoHitClient):
            async def search_public_accounts(self, query, *, limit):
                search = await super().search_public_accounts(query, limit=limit)
                return SimpleNamespace(**{**vars(search), "has_more": True})

        service = self._service()
        service._client = MoreHitsClient()
        outcome = run(
            service.dispatch(
                RouteRequest(RouteKind.ACCOUNT_QUERY, query="CPU"),
                command_prefix="/",
                origin=GROUP,
            )
        )

        self.assertEqual(outcome.candidate_note, MORE_NICKNAME_HITS)
        self.assertEqual(
            outcome.message,
            format_candidates(
                outcome.candidates,
                ttl_seconds=self.store.ttl_seconds,
                note=MORE_NICKNAME_HITS,
            ),
        )

    def test_an_answer_without_a_list_carries_none(self) -> None:
        outcome = self._dispatch(RouteKind.ACCOUNT_QUERY, "")

        self.assertIsNone(outcome.candidates)
        self.assertIsNotNone(outcome.message)


class WatchPickListTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = CandidateStore()
        self.watcher = RankWatcher(
            client=TwoHitClient(),
            data=TwoBoardData(),
            settings=PluginSettings(rank_watch_enabled=True),
            data_dir=Path(directory.name),
            board_matcher=board_matcher_factory(),
            candidates=self.store,
            notify=None,
            logger=logging.getLogger("test"),
        )

    def _handle(self, kind: RouteKind, query: str):
        return run(
            self.watcher.handle_route(
                RouteRequest(kind, query=query),
                origin=GROUP,
                requester_key="qq:1",
                is_admin=False,
                command="/zmdlog",
            )
        )

    def _assert_carries_its_list(self, outcome, *, view: CandidateView) -> None:
        entry = outcome.candidates
        self.assertIsNotNone(entry)
        self.assertEqual(entry.view, view)
        resolved = self.store.resolve(entry.code, "1", origin=GROUP)
        self.assertIs(resolved[0], entry)
        self.assertEqual(
            outcome.message,
            format_candidates(entry, ttl_seconds=self.store.ttl_seconds),
        )

    def test_watching_a_nickname_carries_the_account_list(self) -> None:
        outcome = self._handle(RouteKind.WATCH_ADD, "CPU")

        self._assert_carries_its_list(outcome, view=CandidateView.WATCH)

    def test_watching_a_dungeon_carries_the_board_list(self) -> None:
        outcome = self._handle(RouteKind.WATCH_BOARD_ADD, DUNGEON)

        self._assert_carries_its_list(outcome, view=CandidateView.WATCH_BOARD)

    def test_a_watch_reply_without_a_list_carries_none(self) -> None:
        outcome = self._handle(RouteKind.WATCH_LIST, "")

        self.assertIsNone(outcome.candidates)
        self.assertIsNotNone(outcome.message)


if __name__ == "__main__":
    unittest.main()
