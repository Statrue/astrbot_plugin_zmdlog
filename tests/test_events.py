"""新纪录: the difference between two reads of a board, kept bounded."""

import asyncio
import copy
import logging
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core import facts
from core.events import (
    CHAMPION_CHANGE,
    MAX_EVENTS,
    NEW_RECORD,
    EventLog,
    RecordEvent,
    board_activity,
    diff_rankings,
    events_payload,
    parse_events_payload,
    prune_events,
)
from core.models import parse_boss_ranking, parse_hot_bosses
from core.persistence import JsonStore
from core.presentation import build_records_page
from core.ranking_index import RankingIndex
from core.render import TemplateRenderer
from core.routing import RouteKind, RouteParseError, parse_zmdlog_payload
from core.timestamps import parse_timestamp
from tests.helpers import hot_bosses_payload, ranking_payload_with_rows

WEB = "https://zmdlogs.com"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
STAMP = "2026-09-06T12:00:00+00:00"


def run(coro):
    return asyncio.run(coro)


def board(*, extra_first: bool = False, extra_last: bool = False):
    payload = ranking_payload_with_rows()
    rows = payload["rows"]
    if extra_first:
        new = copy.deepcopy(rows[0])
        new["battleId"] = "btl_upload_newchampion1"
        new["accountDisplayName"] = "新人"
        new["durationMs"] = rows[0]["durationMs"] - 500
        rows.insert(0, new)
    if extra_last:
        new = copy.deepcopy(rows[-1])
        new["battleId"] = "btl_upload_newlast00001"
        rows.append(new)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return parse_boss_ranking(payload)


class DiffTests(unittest.TestCase):
    def test_a_record_lower_down_is_new_but_changes_no_champion(self) -> None:
        events = diff_rankings(board(), board(extra_last=True), seen_at=STAMP)

        self.assertEqual([event.kind for event in events], [NEW_RECORD])
        self.assertEqual(events[0].battle_id, "btl_upload_newlast00001")
        self.assertEqual(events[0].seen_at, STAMP)

    def test_a_new_first_place_is_a_new_record_and_a_champion_change(self) -> None:
        before = board()
        events = diff_rankings(before, board(extra_first=True), seen_at=STAMP)

        self.assertEqual(
            [event.kind for event in events], [NEW_RECORD, CHAMPION_CHANGE]
        )
        change = events[1]
        self.assertEqual(change.rank, 1)
        self.assertEqual(change.account_display_name, "新人")
        self.assertEqual(change.previous_battle_id, before.rows[0].battle_id)
        self.assertEqual(
            change.previous_account_display_name, before.rows[0].account_display_name
        )
        self.assertEqual(change.previous_duration_ms, before.rows[0].duration_ms)

    def test_an_unchanged_board_yields_nothing(self) -> None:
        self.assertEqual(diff_rankings(board(), board(), seen_at=STAMP), ())


class PruneAndPayloadTests(unittest.TestCase):
    def _event(self, seen_at: str, battle_id: str = "btl_upload_x") -> RecordEvent:
        return RecordEvent(
            seen_at=seen_at,
            kind=NEW_RECORD,
            boss_slug="s",
            boss_name="b",
            dungeon_name="d",
            battle_id=battle_id,
            rank=5,
            character_name="洛茜",
            account_id="usr_1",
            account_display_name="someone",
            duration_ms=20_000,
            dps=1000.0,
            battle_end_at=seen_at,
            roster=("洛茜", "诀"),
        )

    def test_old_events_and_the_overflow_are_dropped(self) -> None:
        old = self._event((NOW - timedelta(days=31)).isoformat())
        fresh = [
            self._event(
                (NOW - timedelta(minutes=i)).isoformat(), f"btl_upload_{i:012d}"
            )
            for i in range(MAX_EVENTS + 5)
        ]

        kept = prune_events([old, *fresh], now=NOW)

        self.assertEqual(len(kept), MAX_EVENTS)
        self.assertNotIn(old, kept)
        # Newest survive; the oldest of the fresh ones are the ones dropped.
        self.assertEqual(kept[-1].battle_id, "btl_upload_000000000000")

    def test_the_payload_round_trips_and_tolerates_junk(self) -> None:
        event = self._event(STAMP)

        payload = events_payload((event,))
        payload["events"].append("junk")
        payload["events"].append({"seen_at": STAMP})

        self.assertEqual(parse_events_payload(payload), (event,))
        self.assertEqual(parse_events_payload(None), ())


class EventLogTests(unittest.TestCase):
    def test_a_record_that_leaves_and_re_enters_is_new_once(self) -> None:
        store = JsonStore(None, label="record events", warn=lambda _: None)
        log = EventLog(store, clock=lambda: NOW, stamp=lambda: STAMP)

        log.record(board(), board(extra_first=True))
        # Upstream drops a borderline record as the median moves (not an
        # event) and it comes back: new once, though it leads again.
        log.record(board(extra_first=True), board())
        log.record(board(), board(extra_first=True))

        self.assertEqual(
            [e.kind for e in log.events],
            [NEW_RECORD, CHAMPION_CHANGE, CHAMPION_CHANGE],
        )

    def test_the_log_records_saves_and_reloads(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "record-events.json"
        store = JsonStore(path, label="record events", warn=lambda _: None)
        log = EventLog(store, clock=lambda: NOW, stamp=lambda: STAMP)

        log.record(board(), board(extra_first=True))

        self.assertEqual([e.kind for e in log.events], [NEW_RECORD, CHAMPION_CHANGE])
        reloaded = EventLog(store, clock=lambda: NOW)
        self.assertEqual(reloaded.events, log.events)
        self.assertEqual(reloaded.oldest_seen_at(), STAMP)
        # Newest first, filtered by kind and by window.
        self.assertEqual(
            [e.kind for e in reloaded.recent(kind=CHAMPION_CHANGE)], [CHAMPION_CHANGE]
        )
        self.assertEqual(reloaded.recent(since=NOW + timedelta(seconds=1)), ())

    def test_the_index_hands_the_log_every_re_read(self) -> None:
        seen: list[tuple[str, str]] = []
        rankings = {"a": board(), "b": board()}

        async def fetch_ranking(slug, metric):
            return rankings[slug]

        async def fetch_boards():
            cards = hot_bosses_payload()
            second = copy.deepcopy(cards[0])
            cards[0]["bossSlug"], second["bossSlug"] = "a", "b"
            return parse_hot_bosses([cards[0], second])

        index = RankingIndex(
            fetch_ranking=fetch_ranking,
            fetch_boards=fetch_boards,
            logger=logging.getLogger("t"),
            on_refresh=lambda old, new: seen.append(
                (old.rows[0].battle_id, new.rows[0].battle_id)
            ),
        )

        async def scenario():
            await index.ensure_filled()  # first reads: nothing to compare
            rankings["a"] = board(extra_first=True)
            await index.get("a", max_age=0)

        run(scenario())

        self.assertEqual(seen, [(board().rows[0].battle_id, "btl_upload_newchampion1")])


class ActivityTests(unittest.TestCase):
    def test_activity_counts_records_fought_inside_the_window(self) -> None:
        ranking = board()
        fought = parse_timestamp(ranking.rows[0].battle_end_at)

        inside = board_activity((ranking,), since=fought)
        outside = board_activity((ranking,), since=fought + timedelta(seconds=1))

        self.assertEqual(inside[0].count, len(ranking.rows))
        self.assertEqual(outside[0].count, 0)
        self.assertEqual(outside[0].total, len(ranking.rows))


class RecordsPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events = tuple(
            reversed(diff_rankings(board(), board(extra_first=True), seen_at=STAMP))
        )
        self.activity = board_activity((board(),), since=None)

    def test_the_page_splits_changes_from_records_and_names_the_log_start(self) -> None:
        page = build_records_page(
            self.events,
            self.activity,
            query="新纪录",
            window_label="近 7 天",
            log_since=STAMP,
        )

        self.assertEqual(page.change_count, 1)
        self.assertEqual(page.record_count, 1)
        self.assertEqual(page.changes[0].account_display_name, "新人")
        self.assertTrue(page.log_since_label)
        self.assertEqual(page.activity[0].bar_width, 100.0)

    def test_the_text_and_the_template_carry_all_three_sections(self) -> None:
        text = facts.format_records(
            self.events, self.activity, window_label="近 7 天", log_since=STAMP
        )
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])
        html = renderer.render_records(
            self.events, self.activity, query="新纪录", window_label="近 7 天"
        )

        self.assertIn("第一名易主 1 次", text)
        self.assertIn("顶掉", text)
        self.assertIn("新上传的记录 1 条", text)
        self.assertIn("打出的记录数", text)
        self.assertIn("ZMDLogs", text)
        self.assertIn("第一名易主", html)
        self.assertIn("新人", html)
        self.assertIn("新增记录", html)


class RecordsRouteTests(unittest.TestCase):
    def test_the_command_defaults_to_a_week_and_takes_only_a_range(self) -> None:
        route = parse_zmdlog_payload("新纪录")
        self.assertEqual(route.kind, RouteKind.RECORDS_QUERY)
        self.assertEqual(route.stats_range, "7d")
        self.assertEqual(parse_zmdlog_payload("新纪录 --范围 30d").stats_range, "30d")
        self.assertEqual(parse_zmdlog_payload("最近纪录").kind, RouteKind.RECORDS_QUERY)
        with self.assertRaises(RouteParseError):
            parse_zmdlog_payload("新纪录 罗丹")


if __name__ == "__main__":
    unittest.main()
