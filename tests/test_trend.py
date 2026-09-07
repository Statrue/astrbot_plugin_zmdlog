"""Tests for the 0.6.0 rank trend (趋势): the history trace, page geometry, routing."""

import copy
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.candidates import CandidateStore, CandidateView, format_candidates
from core.history import (
    AccountHistory,
    BoardHistory,
    RankPoint,
    history_payload,
    parse_history_payload,
    prune_points,
    record_rankings,
    trend_points,
    window_start,
)
from core.matcher import MatchChoice, MatchLevel, MatchTarget, TargetType
from core.models import parse_public_user_rankings
from core.presentation import build_trend_page
from core.render import TemplateRenderer
from core.routing import RouteKind, RouteParseError, parse_zmdlog_payload
from tests.helpers import public_user_rankings_payload

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


def stamp(days_ago: int, hours: int = 0) -> str:
    return (NOW - timedelta(days=days_ago, hours=hours)).isoformat()


def rankings(*entries: tuple[str, int], name: str = "CPU 0"):
    payload = public_user_rankings_payload()
    payload["accountId"] = "usr_watched"
    payload["accountDisplayName"] = name
    template = payload["rankings"][0]
    rows = []
    for slug, rank in entries:
        row = copy.deepcopy(template)
        row["bossSlug"] = slug
        row["bossName"] = f"首领 {slug}"
        row["dungeonName"] = f"副本 {slug}"
        row["rank"] = rank
        rows.append(row)
    payload["rankings"] = rows
    return parse_public_user_rankings(payload)


class HistoryTraceTests(unittest.TestCase):
    def test_points_are_recorded_only_when_a_rank_moves(self) -> None:
        history, changed = record_rankings(
            {}, rankings(("a", 3), ("b", 1)), checked_at=stamp(3)
        )

        self.assertTrue(changed)
        entry = history["usr_watched"]
        self.assertEqual(entry.display_name, "CPU 0")
        self.assertEqual(
            [
                (board.boss_slug, [p.rank for p in board.points])
                for board in entry.boards
            ],
            [("a", [3]), ("b", [1])],
        )
        self.assertEqual(entry.board("a").boss_name, "首领 a")

        same, changed = record_rankings(
            history, rankings(("a", 3), ("b", 1)), checked_at=stamp(2)
        )
        self.assertFalse(changed)
        self.assertIs(same, history)

        moved, changed = record_rankings(
            history, rankings(("a", 2), ("b", 1), ("c", 7)), checked_at=stamp(1)
        )
        self.assertTrue(changed)
        self.assertEqual(
            [(p.checked_at, p.rank) for p in moved["usr_watched"].board("a").points],
            [(stamp(3), 3), (stamp(1), 2)],
        )
        self.assertEqual(
            moved["usr_watched"].board("b").points, (RankPoint(stamp(3), 1),)
        )
        self.assertEqual(
            moved["usr_watched"].board("c").points, (RankPoint(stamp(1), 7),)
        )
        # A board missing from one response keeps its trace.
        gone, changed = record_rankings(moved, rankings(("a", 2)), checked_at=stamp(0))
        self.assertFalse(changed)
        self.assertIsNotNone(gone["usr_watched"].board("b"))
        # A renamed account changes the stored nickname without adding points.
        renamed, changed = record_rankings(
            moved, rankings(("a", 2), name="改名"), checked_at=stamp(0)
        )
        self.assertTrue(changed)
        self.assertEqual(renamed["usr_watched"].display_name, "改名")
        self.assertEqual(len(renamed["usr_watched"].board("a").points), 2)

    def test_pruning_keeps_the_newest_and_the_current_rank(self) -> None:
        points = tuple(RankPoint(stamp(200 - i), i + 1) for i in range(5)) + (
            RankPoint(stamp(100), 9),
        )

        pruned = prune_points(
            points, now=stamp(0), max_points=3, max_age_seconds=150 * 86_400
        )

        self.assertEqual(pruned, (RankPoint(stamp(100), 9),))
        recent = tuple(RankPoint(stamp(10 - i), i + 1) for i in range(10))
        capped = prune_points(recent, now=stamp(0), max_points=4)
        self.assertEqual([p.rank for p in capped], [7, 8, 9, 10])
        # The only point is the current rank, however old it is.
        single_old = (RankPoint(stamp(400), 2),)
        self.assertEqual(prune_points(single_old, now=stamp(0)), single_old)

    def test_trend_points_pin_the_carried_rank_to_the_window_start(self) -> None:
        board = BoardHistory(
            "a",
            "首领",
            "副本",
            (
                RankPoint(stamp(40), 4),
                RankPoint(stamp(20), 2),
                RankPoint("昨天", 1),
                RankPoint(stamp(3), 3),
            ),
        )
        start = window_start("30d", now=NOW)

        points = trend_points(board, start=start)

        self.assertEqual(
            [(p.checked_at, p.rank) for p in points],
            [(start.isoformat(), 4), (stamp(20), 2), (stamp(3), 3)],
        )
        self.assertEqual([p.rank for p in trend_points(board, start=None)], [4, 2, 3])
        self.assertIsNone(window_start("all", now=NOW))
        self.assertEqual(window_start("7d", now=NOW), NOW - timedelta(days=7))
        fresh = BoardHistory("b", "首领", "副本", (RankPoint(stamp(2), 5),))
        self.assertEqual(trend_points(fresh, start=start), (RankPoint(stamp(2), 5),))

    def test_payload_round_trip_and_garbage(self) -> None:
        history, _ = record_rankings({}, rankings(("a", 3)), checked_at=stamp(1))

        self.assertEqual(parse_history_payload(history_payload(history)), history)
        self.assertEqual(parse_history_payload(None), {})
        self.assertEqual(parse_history_payload({"accounts": []}), {})
        parsed = parse_history_payload(
            {
                "accounts": {
                    "usr_1": {
                        "displayName": "X",
                        "boards": {
                            "a": {
                                "bossName": "首领",
                                "points": [
                                    [stamp(1), 2],
                                    [stamp(0), "3"],
                                    [stamp(0), 0],
                                    "junk",
                                    [stamp(0)],
                                ],
                            },
                            "b": {"points": []},
                            "c": "junk",
                        },
                    },
                    "usr_2": {"boards": "junk"},
                    "": {"boards": {}},
                }
            }
        )
        self.assertEqual(list(parsed), ["usr_1"])
        self.assertEqual(parsed["usr_1"].board("a").points, (RankPoint(stamp(1), 2),))
        self.assertEqual(parsed["usr_1"].board("a").dungeon_name, "")
        self.assertIsNone(parsed["usr_1"].board("b"))


class TrendPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.history = AccountHistory(
            "usr_watched",
            "CPU 0",
            (
                BoardHistory(
                    "a",
                    "“碾骨之拳”罗丹",
                    "危境再现·罗丹",
                    (
                        RankPoint(stamp(40), 4),
                        RankPoint(stamp(20), 2),
                        RankPoint(stamp(12), 1),
                        RankPoint(stamp(3), 2),
                    ),
                ),
                BoardHistory(
                    "b", "三位一体", "危境再现·三位一体", (RankPoint(stamp(25), 1),)
                ),
                BoardHistory(
                    "c",
                    "山中见犼·苦难",
                    "影拓丰碑4期 · 山中见犼",
                    (RankPoint(stamp(28), 6), RankPoint(stamp(1), 12)),
                ),
            ),
        )

    def test_rows_are_stepped_and_sorted_by_current_rank(self) -> None:
        page = build_trend_page(
            self.history,
            query="趋势 CPU 0",
            web_base_url="https://zmdlogs.com",
            time_range="30d",
            now=NOW,
            last_checked=stamp(0, 1),
        )

        self.assertEqual(page.header.target_type, "名次趋势")
        self.assertEqual(page.tracked_since, "2026-07-25")
        self.assertEqual(page.tracked_days, "40 天")
        self.assertEqual(page.last_checked, "2026-09-03 17:00")
        self.assertEqual(page.best_rank, "#1")
        self.assertEqual((page.improved_count, page.declined_count), (1, 1))
        self.assertEqual(page.range_label, "近 30 天")
        self.assertEqual(page.account_url, "https://zmdlogs.com/records/usr_watched")
        self.assertEqual(
            [row.boss_name for row in page.rows],
            ["三位一体", "“碾骨之拳”罗丹", "山中见犼·苦难"],
        )
        rodan = page.rows[1]
        self.assertEqual(
            (rodan.start_rank, rodan.current_rank, rodan.delta_label, rodan.delta_kind),
            (4, 2, "上升 2", "up"),
        )
        self.assertEqual((rodan.best_rank, rodan.worst_rank), (1, 4))
        self.assertEqual(
            rodan.polyline,
            "0.0,38.0 33.33,38.0 33.33,16.67 60.0,16.67 60.0,6.0 "
            "90.0,6.0 90.0,16.67 100.0,16.67",
        )
        self.assertEqual(rodan.dots[0], (0.0, 86.36))
        self.assertEqual(len(rodan.dots), 4)
        # 月-日: the window is 30 days at most, so the year is noise and
        # the column used to clip it to "2026-0…".
        self.assertEqual(rodan.last_change, "08-31")
        self.assertEqual((rodan.axis_top, rodan.axis_bottom), ("#1", "#4"))
        flat = page.rows[0]
        self.assertEqual(flat.delta_kind, "flat")
        self.assertEqual(flat.polyline, "16.67,22.0 100.0,22.0")
        down = page.rows[2]
        self.assertEqual(
            (down.delta_label, down.delta_kind, down.start_rank), ("下降 6", "down", 6)
        )

    def test_all_time_axis_starts_at_the_first_record(self) -> None:
        page = build_trend_page(
            self.history,
            query="q",
            web_base_url="https://zmdlogs.com",
            time_range="all",
            now=NOW,
        )

        rodan = next(row for row in page.rows if row.boss_name.endswith("罗丹"))
        self.assertEqual(rodan.start_rank, 4)
        self.assertTrue(rodan.polyline.startswith("0.0,38.0"))
        self.assertEqual(page.range_label, "全部时间")

    def test_a_rank_held_since_before_the_window_is_a_flat_row(self) -> None:
        old = AccountHistory(
            "usr_x",
            "X",
            (BoardHistory("a", "首领", "副本", (RankPoint(stamp(60), 3),)),),
        )

        page = build_trend_page(
            old, query="q", web_base_url="https://zmdlogs.com", time_range="7d", now=NOW
        )

        self.assertEqual(len(page.rows), 1)
        self.assertEqual(page.rows[0].delta_kind, "flat")
        empty = build_trend_page(
            AccountHistory("usr_x", "X", ()),
            query="q",
            web_base_url="https://zmdlogs.com",
            now=NOW,
        )
        self.assertEqual(empty.rows, ())
        self.assertEqual(empty.best_rank, "—")
        self.assertEqual(empty.tracked_since, "—")


class TrendTemplateTests(unittest.TestCase):
    def test_trend_page_renders(self) -> None:
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])
        points = (RankPoint(stamp(5), 3), RankPoint(stamp(1), 1))
        history = AccountHistory(
            "usr_watched",
            "CPU<b>0",
            (BoardHistory("a", "首领", "副本", points),),
        )

        html = renderer.render_trend(
            history,
            query="趋势 CPU",
            web_base_url="https://zmdlogs.com",
            time_range="7d",
        )

        self.assertIn("CPU&lt;b&gt;0", html)
        self.assertIn("上升 2", html)
        self.assertIn("<polyline points=", html)
        self.assertIn("trend-dot", html)
        self.assertIn("https://zmdlogs.com/records/usr_watched", html)
        empty = renderer.render_trend(
            AccountHistory("usr_x", "X", ()), query="q", web_base_url="https://zmdlogs.com"
        )
        self.assertIn("这段时间没有名次记录", empty)


class TrendRouteTests(unittest.TestCase):
    def test_trend_route_defaults_to_a_month(self) -> None:
        route = parse_zmdlog_payload("趋势 CPU 0")
        self.assertEqual(route.kind, RouteKind.TREND_QUERY)
        self.assertEqual(route.query, "CPU 0")
        self.assertEqual(route.stats_range, "30d")
        self.assertEqual(
            parse_zmdlog_payload("名次趋势 usr_x --范围 all").stats_range, "all"
        )
        self.assertEqual(
            parse_zmdlog_payload("趋势 usr_x --range 7天").stats_range, "7d"
        )
        rejected = (
            "趋势",
            "趋势 CPU --top 3",
            "趋势 CPU --潜能 0",
            "趋势 CPU --范围 3d",
        )
        for payload in rejected:
            with self.subTest(payload=payload):
                with self.assertRaises(RouteParseError):
                    parse_zmdlog_payload(payload)


class TrendCandidateTests(unittest.TestCase):
    def test_pick_list_names_the_trend_view(self) -> None:
        choice = MatchChoice(
            target=MatchTarget(
                target_type=TargetType.ACCOUNT,
                key="usr_a",
                name="CPU 0",
                dungeon_names=(),
                boss_slugs=(),
            ),
            level=MatchLevel.STANDARD_EXACT,
            score=1.0,
            matched_text="CPU 0",
        )
        store = CandidateStore()

        entry = store.remember(
            "cpu", (choice, choice), view=CandidateView.TREND, stats_range="7d"
        )

        self.assertIn("选一个看名次趋势", format_candidates(entry))
        resolved, _ = store.resolve(entry.code, "2")
        self.assertIs(resolved.view, CandidateView.TREND)
        self.assertEqual(resolved.stats_range, "7d")


if __name__ == "__main__":
    unittest.main()
