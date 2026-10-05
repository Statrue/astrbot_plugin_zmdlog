"""Board changes: one read of a board in, the rank trend points out.

The seam is :func:`core.board_changes.board_changes` — two copies of a
board and each account's last trend point in, what to record out. The
trend book and the data source's refresh hook are driven the same way the
index drives them: a board read, then another.
"""

import asyncio
import copy
import json
import logging
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from core.board_changes import BoardSnapshot, NoticeWatch, board_changes
from core.candidates import CandidateStore
from core.datasource import RANK_HISTORY_FILE, ZmdLogsDataSource
from core.history import MAX_POINT_AGE_SECONDS, RankPoint
from core.models import parse_boss_ranking
from core.persistence import JsonStore, save_json
from core.rank_trend import RankTrend
from core.rank_watch import WATCHLIST_FILE, RankWatcher
from core.settings import PluginSettings
from core.watchlist import ChatWatch, WatchedBoard, WatchList
from tests.helpers import ranking_payload_with_rows, standing

SLUG = "dung01_group_bossrush02"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def stamp(days_ago: float = 0.0) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def board(
    *accounts: str,
    metric: str = "dps",
    slug: str = SLUG,
    nickname: str = "昵称",
    ended: str = "2026-07-13T22:00:32+08:00",
):
    """A ranking whose rows belong to ``accounts``, in rank order.

    An account named twice has two records; its rank is its better one.
    Every row carries ``nickname`` and was fought at ``ended``.
    """

    payload = ranking_payload_with_rows()
    payload["bossSlug"] = slug
    payload["metric"] = metric
    template = payload["rows"][0]
    rows = []
    for rank, account in enumerate(accounts, start=1):
        row = copy.deepcopy(template)
        row["rank"] = rank
        row["battleId"] = f"btl_upload_{account}{rank:04d}"
        row["accountId"] = f"usr_{account}"
        row["accountDisplayName"] = f"{nickname} {account}"
        row["battleEndAt"] = ended
        rows.append(row)
    payload["rows"] = rows
    return parse_boss_ranking(payload, metric=metric)


def ranks(changes) -> dict[str, int]:
    return {point.account_id: point.rank for point in changes.trend_points}


class BoardChangeTests(unittest.TestCase):
    """The seam: before/after plus the last trend points → points to record."""

    def test_a_rank_that_moved_is_recorded_and_one_that_did_not_is_not(self) -> None:
        changes = board_changes(
            board("a", "b"),
            board("new", "a", "b"),
            seen_at=stamp(),
            last_ranks={"usr_a": 1, "usr_b": 2, "usr_new": 1},
        )

        self.assertEqual(ranks(changes), {"usr_a": 2, "usr_b": 3})
        self.assertEqual((changes.boss_slug, changes.seen_at), (SLUG, stamp()))
        # Every account on the board is named, moved or not.
        self.assertEqual(
            {account: name.name for account, name in changes.nicknames.items()},
            {"usr_new": "昵称 new", "usr_a": "昵称 a", "usr_b": "昵称 b"},
        )

        unchanged = board_changes(
            board("a", "b"),
            board("a", "b"),
            seen_at=stamp(),
            last_ranks={"usr_a": 1, "usr_b": 2},
        )
        self.assertEqual(unchanged.trend_points, ())

    def test_an_account_that_vanished_records_nothing(self) -> None:
        # b's record fell under the median threshold or was deleted.
        changes = board_changes(
            board("a", "b", "c"),
            board("a", "c"),
            seen_at=stamp(),
            last_ranks={"usr_a": 1, "usr_b": 2, "usr_c": 3},
        )

        self.assertEqual(ranks(changes), {"usr_c": 2})

    def test_a_first_read_compares_with_the_trend_not_with_nothing(self) -> None:
        # After a restart the index holds no copy; the trend still holds
        # every account's last rank, so only a real move is recorded.
        changes = board_changes(
            None,
            board("a", "c", "b"),
            seen_at=stamp(),
            last_ranks={"usr_a": 1, "usr_b": 2, "usr_c": 3},
        )

        self.assertEqual(ranks(changes), {"usr_c": 2, "usr_b": 3})
        quiet = board_changes(
            None, board("a", "b"), seen_at=stamp(), last_ranks={"usr_a": 1, "usr_b": 2}
        )
        self.assertEqual(quiet.trend_points, ())

    def test_an_account_seen_for_the_first_time_starts_its_trace(self) -> None:
        changes = board_changes(None, board("a", "b"), seen_at=stamp(), last_ranks={})

        self.assertEqual(ranks(changes), {"usr_a": 1, "usr_b": 2})

    def test_an_account_ranks_by_its_best_record(self) -> None:
        changes = board_changes(
            None, board("b", "a", "b"), seen_at=stamp(), last_ranks={}
        )

        self.assertEqual(ranks(changes), {"usr_b": 1, "usr_a": 2})

    def test_an_rdps_read_records_nothing(self) -> None:
        self.assertIsNone(
            board_changes(
                board("a", metric="rdps"),
                board("new", "a", metric="rdps"),
                seen_at=stamp(),
                last_ranks={"usr_a": 1},
            )
        )


GROUP = "aiocqhttp:GroupMessage:1"
OTHER_GROUP = "aiocqhttp:GroupMessage:2"
CONTRACT_SLUG = "indie_group_ccdg"
def watching(slug: str = SLUG, *, origin: str = GROUP) -> WatchList:
    chat, _ = ChatWatch().with_board(
        WatchedBoard(slug, "首领", "副本", "aiocqhttp:111", stamp(1))
    )
    return WatchList.empty().with_chat(origin, chat)


def snapshot(*records: str, days_ago: float = 0.01, whole: bool = False):
    return BoardSnapshot(
        battle_ids=tuple(f"btl_{record}" for record in records),
        checked_at=stamp(days_ago),
        whole=whole,
    )


def notices(
    previous,
    current,
    *,
    watchlist: WatchList | None = None,
    top_n: int = 10,
    snap: BoardSnapshot | None = None,
    announced=None,
):
    """What one read gives every chat, through the seam."""

    changes = board_changes(
        previous,
        current,
        seen_at=stamp(),
        last_ranks={},
        watch=NoticeWatch(
            watchlist=watching(current.boss_slug) if watchlist is None else watchlist,
            top_n=top_n,
            snapshot=snap,
            snapshot_max_age_seconds=3600.0,
        ),
        announced=announced,
    )
    return changes.notices


def told(previous, current, **kwargs):
    """The one chat's entries as (rank, record, champion, pushed) tuples."""

    found = notices(previous, current, **kwargs)
    if not found:
        return []
    (entries,) = found.values()
    return [
        (
            entry.rank,
            entry.record.battle_id.removeprefix("btl_"),
            entry.champion,
            [
                (
                    pushed.record.account_id.removeprefix("usr_"),
                    pushed.before,
                    pushed.after,
                )
                for pushed in entry.pushed
            ],
        )
        for entry in entries
    ]


class NoticeEntryTests(unittest.TestCase):
    """The seam: before/after, the watch list and the snapshot → notice entries."""

    def test_a_new_record_entering_the_top_n_is_one_entry(self) -> None:
        found = notices(standing("a1", "b1", "c1"), standing("a1", "n1", "b1", "c1"))

        ((origin, (entry,)),) = found.items()
        self.assertEqual(origin, GROUP)
        self.assertEqual(
            (entry.boss_slug, entry.seen_at, entry.rank, entry.record.battle_id),
            (SLUG, stamp(), 2, "btl_n1"),
        )
        self.assertFalse(entry.champion)
        # Everyone it moved down, best first, before and after it.
        self.assertEqual(
            [(p.record.account_display_name, p.before, p.after) for p in entry.pushed],
            [("昵称 b", 2, 3), ("昵称 c", 3, 4)],
        )

    def test_first_place_changing_hands_is_a_new_champion(self) -> None:
        self.assertEqual(
            told(standing("a1", "b1"), standing("n1", "a1", "b1")),
            [(1, "n1", True, [("a", 1, 2), ("b", 2, 3)])],
        )
        # The holder beating their own #1 is a plain new record: nobody's
        # place changed hands, and their own account did not move.
        self.assertEqual(
            told(standing("a1", "b1"), standing("a2", "a1", "b1")),
            [(1, "a2", False, [("b", 2, 3)])],
        )
        # A board's first record: the place had no holder, now it has one.
        self.assertEqual(told(standing(), standing("n1")), [(1, "n1", True, [])])

    def test_a_new_record_outside_the_top_n_is_not_news(self) -> None:
        self.assertEqual(
            told(standing("a1", "b1"), standing("a1", "b1", "n1"), top_n=2), []
        )
        self.assertEqual(
            told(standing("a1", "b1"), standing("a1", "b1", "n1"), top_n=3),
            [(3, "n1", False, [])],
        )

    def test_whom_it_pushed_and_who_fell_out_of_the_top_n(self) -> None:
        # c was third, outside the top 2, so it was not pushed out of it.
        self.assertEqual(
            told(standing("a1", "b1", "c1"), standing("n1", "a1", "b1", "c1"), top_n=2),
            [(1, "n1", True, [("a", 1, 2), ("b", 2, 3)])],
        )
        # An account is pushed once, by its best record.
        self.assertEqual(
            told(
                standing("a1", "b1", "a2", "c1"),
                standing("n1", "a1", "b1", "a2", "c1"),
            ),
            [(1, "n1", True, [("a", 1, 2), ("b", 2, 3), ("c", 4, 5)])],
        )

    def test_off_a_snapshot_only_what_was_announced_before_it_is_back(
        self,
    ) -> None:
        held = snapshot("a1", "c1", whole=True)
        # b was announced long before the snapshot: it came back.
        self.assertEqual(
            told(
                None,
                standing("a1", "b1", "c1"),
                snap=held,
                announced={"btl_b1": stamp(1)},
            ),
            [],
        )
        # b was announced after it: found, never sent, then a restart.
        self.assertEqual(
            told(
                None,
                standing("a1", "b1", "c1"),
                snap=held,
                announced={"btl_b1": stamp(0.001)},
            ),
            [(2, "b1", False, [("c", 2, 3)])],
        )

    def test_a_threshold_return_or_a_deletion_is_no_record(self) -> None:
        # b was deleted (or fell under 60% of the median): c moved up.
        self.assertEqual(told(standing("a1", "b1", "c1"), standing("a1", "c1")), [])
        # b came back over the threshold: it was announced when it was new.
        self.assertEqual(
            told(
                standing("a1", "c1"),
                standing("a1", "b1", "c1"),
                announced={"btl_b1": stamp(1)},
            ),
            [],
        )
        # A deletion and an upload in one read: counted on the board as it
        # stood after the deletion.
        self.assertEqual(
            told(standing("a1", "b1", "c1"), standing("a1", "n1", "c1")),
            [(2, "n1", False, [("c", 2, 3)])],
        )

    def test_an_rdps_read_gives_no_entry(self) -> None:
        self.assertIsNone(
            board_changes(
                standing("a1", metric="rdps"),
                standing("n1", "a1", metric="rdps"),
                seen_at=stamp(),
                last_ranks={},
                watch=NoticeWatch(watching(), top_n=10),
            )
        )

    def test_several_new_records_on_one_board_are_one_entry_each(self) -> None:
        # In rank order, best first; each counts what it pushed on the board
        # as it stood just before it, the better new record already there.
        self.assertEqual(
            told(
                standing("a1", "b1", "c1", "d1"),
                standing("n1", "a1", "m1", "b1", "c1", "d1"),
                top_n=4,
            ),
            [
                (1, "n1", True, [("a", 1, 2), ("b", 2, 3), ("c", 3, 4), ("d", 4, 5)]),
                (3, "m1", False, [("b", 3, 4), ("c", 4, 5)]),
            ],
        )

    def test_a_contract_board_entry_carries_the_score(self) -> None:
        found = notices(
            standing("a1", slug=CONTRACT_SLUG, score=40),
            standing("n1", "a1", slug=CONTRACT_SLUG, score=47),
        )

        ((entry,),) = found.values()
        self.assertEqual(entry.boss_slug, CONTRACT_SLUG)
        self.assertEqual(entry.record.contract_tag_score, 47)
        self.assertEqual(entry.rank, 1)

    def test_only_the_chats_watching_the_board_are_told(self) -> None:
        before, after = standing("a1"), standing("n1", "a1")
        self.assertEqual(notices(before, after, watchlist=watching("other")), {})
        self.assertEqual(notices(before, after, watchlist=WatchList.empty()), {})

        every = ChatWatch().following_all(added_by="aiocqhttp:111", added_at=stamp(1))
        excluding = every.excluding(
            WatchedBoard(SLUG, "首领", "副本", "aiocqhttp:111", stamp(1))
        )
        watchlist = (
            WatchList.empty()
            .with_chat(GROUP, every)
            .with_chat(OTHER_GROUP, excluding)
            .with_chat("aiocqhttp:GroupMessage:3", watching("other").chat(GROUP))
        )
        self.assertEqual(list(notices(before, after, watchlist=watchlist)), [GROUP])

    def test_a_first_read_without_a_snapshot_is_no_change(self) -> None:
        changes = board_changes(
            None,
            standing("n1", "a1", "b1"),
            seen_at=stamp(),
            last_ranks={},
            watch=NoticeWatch(watching(), top_n=2),
        )

        self.assertEqual(changes.notices, {})
        # What the watch keeps from the read: its top N, to compare a
        # restart against.
        self.assertEqual(
            changes.snapshot,
            BoardSnapshot(("btl_n1", "btl_a1"), checked_at=stamp(), whole=False),
        )
        whole = board_changes(
            None,
            standing("a1"),
            seen_at=stamp(),
            last_ranks={},
            watch=NoticeWatch(watching(), top_n=2),
        )
        self.assertTrue(whole.snapshot.whole)

    def test_a_restart_compares_its_first_read_with_the_snapshot(self) -> None:
        self.assertEqual(
            told(
                None,
                standing("n1", "a1", "b1", "c1"),
                top_n=2,
                snap=snapshot("a1", "b1"),
            ),
            [(1, "n1", True, [("a", 1, 2), ("b", 2, 3)])],
        )
        # The snapshot holds the top N only: c, below it, is not new for
        # having risen into the top N when b was deleted.
        self.assertEqual(
            told(None, standing("a1", "c1"), top_n=2, snap=snapshot("a1", "b1")), []
        )
        # The top three the old file kept are the same kind of baseline: a
        # record below them is not known to be new.
        self.assertEqual(
            told(
                None,
                standing("a1", "b1", "c1", "d1"),
                snap=snapshot("a1", "b1", "c1"),
            ),
            [],
        )
        # A snapshot that held the whole board knows every record on it.
        self.assertEqual(
            told(None, standing("a1", "n1"), snap=snapshot("a1", whole=True)),
            [(2, "n1", False, [])],
        )

    def test_a_stale_snapshot_is_a_silent_new_start(self) -> None:
        changes = board_changes(
            None,
            standing("n1", "a1"),
            seen_at=stamp(),
            last_ranks={},
            watch=NoticeWatch(
                watching(),
                top_n=10,
                snapshot=snapshot("a1", days_ago=1),
                snapshot_max_age_seconds=3600.0,
            ),
        )

        self.assertEqual(changes.notices, {})
        self.assertEqual(changes.snapshot.battle_ids, ("btl_n1", "btl_a1"))


class RankTrendTests(unittest.TestCase):
    """The book the points go into, and the one file it keeps."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / RANK_HISTORY_FILE
        self.warnings: list[str] = []
        self.clock = [0.0]

    def _trend(self, **kwargs) -> RankTrend:
        store = JsonStore(
            self.path, label="rank history", warn=self.warnings.append, compact=True
        )
        return RankTrend(store, clock=lambda: self.clock[0], now=lambda: NOW, **kwargs)

    def _read(self, trend: RankTrend, current, previous=None, *, at: str) -> None:
        changes = board_changes(
            previous,
            current,
            seen_at=at,
            last_ranks=trend.last_ranks(current.boss_slug),
        )
        if changes is not None:
            trend.apply(changes)

    def test_reads_build_one_trace_per_account_and_board(self) -> None:
        trend = self._trend()

        self._read(trend, board("a", "b"), at=stamp(2))
        self._read(trend, board("new", "a", "b"), board("a", "b"), at=stamp(1))
        self._read(trend, board("new", "a", "b"), board("new", "a", "b"), at=stamp())

        trace = trend.history_for("usr_a")
        self.assertEqual(trace.display_name, "昵称 a")
        self.assertEqual(
            trace.board(SLUG).points, (RankPoint(stamp(2), 1), RankPoint(stamp(1), 2))
        )
        self.assertEqual(trend.last_checked("usr_a"), stamp())
        self.assertIsNone(trend.history_for("usr_nobody"))
        self.assertEqual(
            [entry.account_id for entry in trend.by_name("昵称 new")], ["usr_new"]
        )

    def test_points_older_than_90_days_go_but_each_board_keeps_its_newest(
        self,
    ) -> None:
        trend = self._trend()
        self._read(trend, board("a", "b"), at=stamp(120))
        self._read(trend, board("b", "a"), at=stamp(100))
        self._read(trend, board("a", "b"), at=stamp(1))

        self.assertEqual(
            trend.history_for("usr_a").board(SLUG).points, (RankPoint(stamp(1), 1),)
        )
        trend.flush()
        # A trace that stopped moving keeps its last point, however old: it
        # is the rank the account still holds.
        reloaded = self._trend()
        self.assertEqual(
            reloaded.history_for("usr_b").board(SLUG).points,
            (RankPoint(stamp(1), 2),),
        )
        self.assertEqual(MAX_POINT_AGE_SECONDS, 90 * 86_400)

    def test_the_watched_accounts_old_file_is_read_on_and_joined_up(self) -> None:
        # rank-history.json as the rank watch's poll wrote it, indented.
        old = {
            "version": 1,
            "accounts": {
                "usr_a": {
                    "displayName": "旧昵称",
                    "boards": {
                        SLUG: {
                            "bossName": "三位一体",
                            "dungeonName": "危境再现",
                            "points": [[stamp(30), 4], [stamp(10), 1]],
                        },
                        "outside_the_index": {
                            "bossName": "别的榜",
                            "dungeonName": "",
                            "points": [[stamp(5), 7]],
                        },
                    },
                }
            },
        }
        self.path.write_text(json.dumps(old, indent=2), encoding="utf-8")
        trend = self._trend()

        self._read(trend, board("a", "b"), at=stamp())

        trace = trend.history_for("usr_a")
        self.assertEqual(
            trace.board(SLUG).points, (RankPoint(stamp(30), 4), RankPoint(stamp(10), 1))
        )
        self.assertEqual(trace.board("outside_the_index").points[0].rank, 7)
        # The read names the account as its newest upload there does.
        self.assertEqual(trace.display_name, "昵称 a")
        self.assertEqual(trend.history_for("usr_b").board(SLUG).points[0].rank, 2)

    def test_a_missing_file_starts_empty_and_a_corrupt_one_is_set_aside(
        self,
    ) -> None:
        self.assertIsNone(self._trend().history_for("usr_a"))
        self.assertEqual(self.warnings, [])

        self.path.write_text("{not json", encoding="utf-8")
        trend = self._trend()

        self.assertIsNone(trend.history_for("usr_a"))
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("rank history", self.warnings[0])
        aside = list(self.path.parent.glob(f"{RANK_HISTORY_FILE}.corrupt-*"))
        self.assertEqual(len(aside), 1)
        self.assertEqual(aside[0].read_text(encoding="utf-8"), "{not json")

    def test_the_file_is_written_compact_and_at_most_once_an_interval(self) -> None:
        trend = self._trend(save_interval_seconds=300)

        self._read(trend, board("a", "b"), at=stamp(2))
        first = self.path.read_text(encoding="utf-8")
        self.assertNotIn("\n ", first)
        self.clock[0] = 60.0
        self._read(trend, board("b", "a"), at=stamp(1))
        self.assertEqual(self.path.read_text(encoding="utf-8"), first)

        # The next read past the interval writes what was held back, even
        # one that changed nothing itself.
        self.clock[0] = 301.0
        self._read(trend, board("b", "a"), at=stamp())
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["accounts"]["usr_a"]["boards"][SLUG]["points"],
            [[stamp(2), 1], [stamp(1), 2]],
        )

    def test_a_nickname_from_an_older_upload_never_replaces_a_newer_one(
        self,
    ) -> None:
        trend = self._trend()
        later, earlier = "2026-10-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"

        self._read(trend, board("a", nickname="新名", ended=later), at=stamp(1))
        # Another board, last played before the rename: still the old name.
        self._read(
            trend,
            board("a", slug="other", nickname="旧名", ended=earlier),
            at=stamp(),
        )

        self.assertEqual(trend.history_for("usr_a").display_name, "新名 a")
        self.assertEqual(trend.by_name("旧名 a"), ())
        # A rename with no rank change is still taken.
        self._read(trend, board("a", nickname="更新", ended=later), at=stamp())
        self.assertEqual(trend.history_for("usr_a").display_name, "更新 a")

    def test_flush_writes_what_the_interval_held_back(self) -> None:
        trend = self._trend(save_interval_seconds=300)
        self._read(trend, board("a"), at=stamp(1))
        self._read(trend, board("b", "a"), at=stamp())

        trend.flush()

        self.assertEqual(
            self._trend().history_for("usr_a").board(SLUG).points[-1].rank, 2
        )


class RefreshHookTests(unittest.TestCase):
    """The data source records the trend from the index's own reads."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.boards = {
            "dps": board("a", "b"),
            # The rDPS board lists only b, first: a rank never to be recorded.
            "rdps": board("b", metric="rdps"),
        }

    def _life(self) -> RankTrend:
        """One process: a fresh index reads the board once in each metric."""

        async def get_boss_rankings(slug, *, metric):
            return self.boards[metric]

        async def scenario():
            source = ZmdLogsDataSource(
                SimpleNamespace(get_boss_rankings=get_boss_rankings),
                data_dir=self.data_dir,
                logger=logging.getLogger("test"),
            )
            await source.ranking_index.get(SLUG)
            await source.ranking_index.get(SLUG, metric="rdps")
            await source.close()
            return source.rank_trend

        return run_async(scenario())

    def _ranks(self, trend: RankTrend, account: str) -> list[int]:
        return [point.rank for point in trend.history_for(account).board(SLUG).points]

    def test_dps_reads_are_recorded_and_a_restart_joins_up(self) -> None:
        trend = self._life()
        self.assertEqual(self._ranks(trend, "usr_a"), [1])
        self.assertEqual(self._ranks(trend, "usr_b"), [2])

        # Restarted with nothing held: the first read compares with the file.
        self.assertEqual(self._ranks(self._life(), "usr_b"), [2])

        self.boards["dps"] = board("new", "a", "b")
        moved = self._life()
        self.assertEqual(self._ranks(moved, "usr_a"), [1, 2])
        self.assertEqual(self._ranks(moved, "usr_b"), [2, 3])
        self.assertEqual(self._ranks(moved, "usr_new"), [1])


class NoticeHookTests(unittest.TestCase):
    """The data source hands every DPS read to the board watch as well."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        save_json(self.data_dir / WATCHLIST_FILE, watching().to_payload())
        self.boards = {"dps": standing("a1", "b1"), "rdps": standing(metric="rdps")}

    def _source(self, get_boss_rankings) -> ZmdLogsDataSource:
        return ZmdLogsDataSource(
            SimpleNamespace(get_boss_rankings=get_boss_rankings),
            data_dir=self.data_dir,
            logger=logging.getLogger("test"),
        )

    def _watcher(self, source: ZmdLogsDataSource) -> RankWatcher:
        """The plugin's watcher, which attaches itself to the data source."""

        return RankWatcher(
            data=source,
            settings=PluginSettings(),
            data_dir=self.data_dir,
            board_matcher=None,
            candidates=CandidateStore(),
            notify=None,
            logger=logging.getLogger("test"),
        )

    def _life(self) -> RankWatcher:
        """One process: a fresh index reads the board once in each metric."""

        async def get_boss_rankings(slug, *, metric):
            return self.boards[metric]

        async def scenario():
            source = self._source(get_boss_rankings)
            watcher = self._watcher(source)
            await source.ranking_index.get(SLUG)
            await source.ranking_index.get(SLUG, metric="rdps")
            await source.close()
            return watcher

        return run_async(scenario())

    @staticmethod
    def _queued(watcher: RankWatcher) -> list[str]:
        return [entry.record.battle_id for entry in watcher.pending(GROUP)]

    def test_a_record_found_by_a_re_read_is_queued(self) -> None:
        reads = iter((standing("a1", "b1"), standing("n1", "a1", "b1")))

        async def get_boss_rankings(slug, *, metric):
            return next(reads)

        async def scenario():
            source = self._source(get_boss_rankings)
            watcher = self._watcher(source)
            await source.ranking_index.get(SLUG)
            await source.ranking_index._refresh(SLUG, "dps")
            await source.close()
            return source, watcher

        source, watcher = run_async(scenario())

        # The event log announced n1 on that same read; the notice still
        # counts it, having asked before the read was recorded.
        self.assertEqual(self._queued(watcher), ["btl_n1"])
        self.assertIn("btl_n1", source.event_log.announced("dps"))

    def test_a_restart_compares_its_first_read_with_the_snapshot(self) -> None:
        first = self._life()
        self.assertEqual(self._queued(first), [])
        # The interval ends: the board's top N is kept on disk.
        run_async(first.run_notice_cycle())

        self.boards["dps"] = standing("n1", "a1", "b1")
        # rDPS reads are never news, a new record there included.
        self.boards["rdps"] = standing("r1", metric="rdps")
        restarted = self._life()

        self.assertEqual(self._queued(restarted), ["btl_n1"])

    def test_a_record_found_and_never_sent_is_found_again_after_a_restart(
        self,
    ) -> None:
        first = self._life()
        run_async(first.run_notice_cycle())

        # A re-read finds n1 — the event log announces it on disk — and the
        # send fails, so the snapshot stays where it was.
        reads = iter((standing("a1", "b1"), standing("n1", "a1", "b1")))

        async def get_boss_rankings(slug, *, metric):
            return next(reads)

        async def failing(origin, batch):
            return False

        async def scenario():
            source = self._source(get_boss_rankings)
            watcher = self._watcher(source)
            watcher.notify = failing
            await source.ranking_index.get(SLUG)
            await source.ranking_index._refresh(SLUG, "dps")
            await watcher.run_notice_cycle()
            await source.close()
            return watcher

        self.assertEqual(self._queued(run_async(scenario())), ["btl_n1"])

        # Announced after the snapshot was kept, n1 is still news to it.
        self.boards["dps"] = standing("n1", "a1", "b1")
        self.assertEqual(self._queued(self._life()), ["btl_n1"])


def run_async(coro):
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
