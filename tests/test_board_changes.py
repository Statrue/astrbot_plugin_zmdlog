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

from core.board_changes import board_changes
from core.datasource import RANK_HISTORY_FILE, ZmdLogsDataSource
from core.history import MAX_POINT_AGE_SECONDS, RankPoint
from core.models import parse_boss_ranking
from core.persistence import JsonStore
from core.rank_trend import RankTrend
from tests.helpers import ranking_payload_with_rows

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


def run_async(coro):
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
