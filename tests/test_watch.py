"""Tests for the board watch: watch lists, their file, the notice cycle.

The cycle is driven the way the data source drives it — a board read goes
through :func:`core.board_changes.board_changes` with what the watcher
brings, and :meth:`RankWatcher.collect` queues what comes out — with a fake
clock and a fake sender standing in for the interval and the platform.
"""

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.board_changes import BoardSnapshot, board_changes
from core.candidates import CandidateStore
from core.persistence import load_json, save_json
from core.rank_watch import BOARD_SNAPSHOT_FILE, WATCHLIST_FILE, RankWatcher
from core.settings import PluginSettings
from core.watch import (
    NoticeBatch,
    board_snapshot_payload,
    parse_board_snapshot_payload,
)
from core.watchlist import (
    AllBoards,
    ChatWatch,
    WatchedBoard,
    WatchList,
    board_label,
    format_watchlist,
    parse_watchlist,
)
from tests.helpers import standing

GROUP = "aiocqhttp:GroupMessage:1"
OTHER_GROUP = "aiocqhttp:GroupMessage:2"
CHECKED = "2026-08-22T00:00:00+00:00"
AFTER = "2026-08-22T06:00:00+00:00"
ADDED_AT = "2026-08-22T10:00:00+00:00"
# What 隔离对话 made of GROUP for members 111 and 222.
MEMBER_111 = "aiocqhttp:GroupMessage:111_1"
MEMBER_222 = "aiocqhttp:GroupMessage:222_1"


def board_entry(
    slug: str,
    boss: str | None = None,
    dungeon: str | None = None,
    added_by: str = "aiocqhttp:111",
) -> WatchedBoard:
    return WatchedBoard(
        boss_slug=slug,
        boss_name=boss or f"首领 {slug}",
        dungeon_name=dungeon or f"副本 {slug}",
        added_by=added_by,
        added_at=ADDED_AT,
    )


def explicit(*slugs: str, added_by: str = "aiocqhttp:111") -> ChatWatch:
    chat = ChatWatch()
    for slug in slugs:
        chat, _ = chat.with_board(board_entry(slug, added_by=added_by))
    return chat


def every_board(*excluded: str, added_by: str = "aiocqhttp:111") -> ChatWatch:
    chat = ChatWatch().following_all(added_by=added_by, added_at=ADDED_AT)
    for slug in excluded:
        chat = chat.excluding(board_entry(slug, added_by=added_by))
    return chat


class _ListLogger:
    """A LogSink that keeps what it was told to warn about."""

    def __init__(self, warnings: list[str]) -> None:
        self.warnings = warnings

    def warning(self, message, *args) -> None:
        self.warnings.append(message % args if args else message)

    def debug(self, message, *args) -> None:
        pass

    def info(self, message, *args) -> None:
        pass

    def error(self, message, *args) -> None:
        self.warnings.append(message % args if args else message)

    def exception(self, message, *args) -> None:
        self.warnings.append(message % args if args else message)


def watcher_on(root: Path, warnings: list[str] | None = None) -> RankWatcher:
    """A watcher that only loads its files; the collaborators stay idle."""

    return RankWatcher(
        data=None,
        settings=PluginSettings(),
        data_dir=root,
        board_matcher=None,
        candidates=CandidateStore(),
        notify=None,
        logger=_ListLogger([] if warnings is None else warnings),
    )


class ChatWatchTests(unittest.TestCase):
    def test_boards_keep_insertion_order_and_their_first_adder(self) -> None:
        chat, added = ChatWatch().with_board(board_entry("slug_a"))
        self.assertTrue(added)
        chat, _ = chat.with_board(board_entry("slug_b"))
        chat, added = chat.with_board(
            board_entry("slug_a", boss="改名", added_by="aiocqhttp:999")
        )

        self.assertFalse(added)
        self.assertEqual(
            [entry.boss_slug for entry in chat.boards], ["slug_a", "slug_b"]
        )
        self.assertEqual(chat.boards[0].boss_name, "改名")
        self.assertEqual(chat.boards[0].added_by, "aiocqhttp:111")
        self.assertTrue(chat.covers("slug_b"))
        self.assertFalse(chat.covers("slug_c"))
        self.assertEqual(
            [entry.boss_slug for entry in chat.without_board("slug_a").boards],
            ["slug_b"],
        )
        self.assertTrue(
            chat.without_board("slug_a").without_board("slug_b").is_empty
        )

    def test_every_board_replaces_the_list_and_covers_unseen_boards(self) -> None:
        chat = explicit("slug_a").following_all(
            added_by="aiocqhttp:222", added_at=ADDED_AT
        )

        self.assertEqual(chat.boards, ())
        self.assertEqual(chat.all_boards, AllBoards("aiocqhttp:222", ADDED_AT))
        self.assertTrue(chat.covers("slug_a"))
        self.assertTrue(chat.covers("a_board_that_opens_next_season"))
        # Asked again, the first adder and the exclusions stay.
        excluded = chat.excluding(board_entry("slug_a"))
        again = excluded.following_all(added_by="aiocqhttp:333", added_at=AFTER)
        self.assertIs(again, excluded)

    def test_an_exclusion_is_recorded_once_and_can_be_lifted(self) -> None:
        chat = every_board("slug_a")
        chat = chat.excluding(board_entry("slug_a", added_by="aiocqhttp:999"))

        self.assertEqual([entry.boss_slug for entry in chat.excluded], ["slug_a"])
        self.assertEqual(chat.excluded[0].added_by, "aiocqhttp:111")
        self.assertFalse(chat.covers("slug_a"))
        self.assertTrue(chat.covers("slug_b"))
        lifted = chat.including("slug_a")
        self.assertEqual(lifted.excluded, ())
        self.assertTrue(lifted.covers("slug_a"))

    def test_board_selectors_accept_index_slug_and_names(self) -> None:
        chat, _ = ChatWatch().with_board(
            board_entry("slug_a", boss="“碾骨之拳”罗丹", dungeon="危境再现·罗丹")
        )
        chat, _ = chat.with_board(
            board_entry(
                "slug_b", boss="山中见犼·苦难", dungeon="影拓丰碑4期 · 山中见犼"
            )
        )

        def resolved(selector: str) -> list[str]:
            return [
                entry.boss_slug for entry in chat.resolve_board_matches(selector)
            ]

        self.assertEqual(resolved("2"), ["slug_b"])
        self.assertEqual(resolved("slug_a"), ["slug_a"])
        self.assertEqual(resolved("山中见犼·苦难"), ["slug_b"])
        self.assertEqual(resolved("罗丹"), ["slug_a"])
        self.assertEqual(resolved("见犼"), ["slug_b"])
        self.assertEqual(resolved("3"), [])
        self.assertEqual(resolved("查无此榜"), [])
        self.assertEqual(resolved("9" * 4400), [])

    def test_only_the_adder_or_an_admin_may_remove(self) -> None:
        entry = board_entry("slug_a")
        self.assertTrue(entry.removable_by("aiocqhttp:111", is_admin=False))
        self.assertFalse(entry.removable_by("aiocqhttp:222", is_admin=False))
        self.assertFalse(entry.removable_by("", is_admin=False))
        self.assertTrue(entry.removable_by("aiocqhttp:222", is_admin=True))

    def test_dropping_everything_needs_the_right_to_drop_every_entry(self) -> None:
        mixed, _ = explicit("slug_a").with_board(
            board_entry("slug_b", added_by="aiocqhttp:222")
        )

        self.assertFalse(
            mixed.removable_entirely_by("aiocqhttp:111", is_admin=False)
        )
        self.assertTrue(mixed.removable_entirely_by("aiocqhttp:111", is_admin=True))
        self.assertTrue(
            explicit("slug_a").removable_entirely_by("aiocqhttp:111", is_admin=False)
        )
        everything = every_board()
        self.assertTrue(
            everything.removable_entirely_by("aiocqhttp:111", is_admin=False)
        )
        self.assertFalse(
            everything.removable_entirely_by("aiocqhttp:222", is_admin=False)
        )


class WatchListTests(unittest.TestCase):
    def test_every_chat_has_its_own_list(self) -> None:
        watchlist = WatchList.empty().with_chat(GROUP, explicit("slug_a"))
        watchlist = watchlist.with_chat(OTHER_GROUP, explicit("slug_a", "slug_b"))

        self.assertEqual(watchlist.chat(GROUP), explicit("slug_a"))
        self.assertEqual(watchlist.chat("nobody"), ChatWatch())
        self.assertEqual(
            watchlist.origins_by_board(()),
            {"slug_a": (GROUP, OTHER_GROUP), "slug_b": (OTHER_GROUP,)},
        )
        emptied = watchlist.with_chat(GROUP, ChatWatch())
        self.assertEqual([origin for origin, _ in emptied.chats], [OTHER_GROUP])
        self.assertTrue(watchlist.watches("slug_b"))
        self.assertFalse(watchlist.watches("slug_c"))

    def test_every_board_follows_the_catalog_minus_its_exclusions(self) -> None:
        watchlist = WatchList.empty().with_chat(GROUP, every_board("slug_b"))
        watchlist = watchlist.with_chat(OTHER_GROUP, explicit("slug_b"))

        self.assertEqual(
            watchlist.origins_by_board(("slug_a", "slug_b", "slug_new")),
            {
                "slug_a": (GROUP,),
                "slug_b": (OTHER_GROUP,),
                "slug_new": (GROUP,),
            },
        )
        self.assertTrue(watchlist.watches("anything"))
        alone = WatchList.empty().with_chat(GROUP, every_board("slug_b"))
        self.assertFalse(alone.watches("slug_b"))

    def test_entries_kept_under_isolation_move_to_their_group(self) -> None:
        theirs = "aiocqhttp:222"
        watchlist = WatchList.empty().with_chat(
            MEMBER_111, explicit("slug_a", "slug_b")
        )
        watchlist = watchlist.with_chat(
            MEMBER_222, explicit("slug_b", "slug_c", added_by=theirs)
        )
        watchlist = watchlist.with_chat(
            GROUP, explicit("slug_d", added_by="aiocqhttp:333")
        )
        watchlist = watchlist.with_chat(OTHER_GROUP, explicit("slug_a"))

        moved, count = watchlist.with_group_origins()

        self.assertEqual(count, 4)
        grouped = moved.chat(GROUP).boards
        self.assertEqual(
            [entry.boss_slug for entry in grouped],
            ["slug_a", "slug_b", "slug_c", "slug_d"],
        )
        # Watched by both members: the first one seen keeps it.
        self.assertEqual(grouped[1].added_by, "aiocqhttp:111")
        self.assertEqual(moved.chat(MEMBER_111), ChatWatch())
        self.assertEqual(moved.origins_by_board(())["slug_a"], (GROUP, OTHER_GROUP))
        again, count = moved.with_group_origins()
        self.assertIs(again, moved)
        self.assertEqual(count, 0)

    def test_a_board_moved_into_a_group_watching_everything_is_covered(
        self,
    ) -> None:
        watchlist = WatchList.empty().with_chat(GROUP, every_board("slug_x"))
        watchlist = watchlist.with_chat(MEMBER_111, explicit("slug_a"))

        moved, count = watchlist.with_group_origins()

        self.assertEqual(count, 1)
        self.assertEqual(moved.chat(GROUP), every_board("slug_x"))
        self.assertEqual([origin for origin, _ in moved.chats], [GROUP])


class WatchListFileTests(unittest.TestCase):
    def test_round_trip_of_both_shapes(self) -> None:
        watchlist = WatchList.empty().with_chat(GROUP, explicit("slug_a", "slug_b"))
        watchlist = watchlist.with_chat(OTHER_GROUP, every_board("slug_c"))

        payload = watchlist.to_payload()

        self.assertEqual(payload["version"], 3)
        self.assertEqual(parse_watchlist(payload), watchlist)
        self.assertEqual(
            payload["groups"][OTHER_GROUP]["allBoards"],
            {"addedBy": "aiocqhttp:111", "addedAt": ADDED_AT},
        )
        self.assertNotIn("boards", payload["groups"][OTHER_GROUP])
        self.assertNotIn("allBoards", payload["groups"][GROUP])
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_old_files_keep_their_boards_and_drop_their_accounts(self) -> None:
        version_2 = {
            "version": 2,
            "groups": {
                GROUP: {
                    "accounts": [{"accountId": "usr_1", "displayName": "CPU 0"}],
                    "boards": [
                        {
                            "bossSlug": "slug_a",
                            "bossName": "首领 slug_a",
                            "dungeonName": "副本 slug_a",
                            "addedBy": "aiocqhttp:111",
                            "addedAt": ADDED_AT,
                        }
                    ],
                },
                OTHER_GROUP: {
                    "accounts": [{"accountId": "usr_2", "displayName": "CPU 1"}],
                    "boards": [],
                },
            },
        }
        # Version 1 kept a bare account list per chat.
        version_1 = {"groups": {GROUP: [{"accountId": "usr_1"}]}}

        upgraded = parse_watchlist(version_2)

        self.assertEqual(
            upgraded, WatchList.empty().with_chat(GROUP, explicit("slug_a"))
        )
        self.assertNotIn("accounts", json.dumps(upgraded.to_payload()))
        self.assertEqual(parse_watchlist(version_1), WatchList.empty())

    def test_garbage_is_dropped_instead_of_raising(self) -> None:
        salvaged = parse_watchlist(
            {
                "groups": {
                    GROUP: {
                        "boards": [
                            {"bossSlug": "s"},
                            {"bossSlug": "s", "bossName": "重复"},
                            {"bossName": "没有 slug"},
                            1,
                        ],
                    },
                    OTHER_GROUP: {"allBoards": "是", "excludedBoards": "不是列表"},
                    "": {"boards": [{"bossSlug": "t"}]},
                }
            }
        )

        self.assertEqual([origin for origin, _ in salvaged.chats], [GROUP])
        (entry,) = salvaged.chat(GROUP).boards
        self.assertEqual((entry.boss_slug, entry.boss_name), ("s", "s"))
        for payload in (None, [], {"groups": []}):
            with self.subTest(payload=payload):
                self.assertEqual(parse_watchlist(payload), WatchList.empty())

    def test_every_board_wins_over_a_list_beside_it(self) -> None:
        # Only a hand-edited file holds both; the broader reading is kept.
        parsed = parse_watchlist(
            {
                "groups": {
                    GROUP: {
                        "allBoards": {"addedBy": "aiocqhttp:111", "addedAt": ADDED_AT},
                        "boards": [{"bossSlug": "slug_a"}],
                        "excludedBoards": [
                            {
                                "bossSlug": "slug_c",
                                "bossName": "首领 slug_c",
                                "dungeonName": "副本 slug_c",
                                "addedBy": "aiocqhttp:111",
                                "addedAt": ADDED_AT,
                            }
                        ],
                    }
                }
            }
        )

        self.assertEqual(parsed.chat(GROUP), every_board("slug_c"))

    def test_the_watcher_upgrades_an_old_file_when_it_loads(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        save_json(
            root / WATCHLIST_FILE,
            {
                "version": 2,
                "groups": {
                    GROUP: {
                        "accounts": [{"accountId": "usr_1", "displayName": "CPU 0"}],
                        "boards": [
                            {"bossSlug": "slug_a", "addedBy": "aiocqhttp:111"}
                        ],
                    }
                },
            },
        )
        warnings: list[str] = []

        watcher = watcher_on(root, warnings)

        self.assertEqual(
            [entry.boss_slug for entry in watcher.watchlist.chat(GROUP).boards],
            ["slug_a"],
        )
        self.assertEqual(warnings, [])

    def test_a_missing_file_is_empty_and_a_corrupt_one_is_set_aside(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        warnings: list[str] = []

        missing = watcher_on(root, warnings)

        self.assertEqual(missing.watchlist, WatchList.empty())
        self.assertEqual(warnings, [])
        self.assertEqual(list(root.iterdir()), [])

        (root / WATCHLIST_FILE).write_text("{not json", encoding="utf-8")
        corrupt = watcher_on(root, warnings)

        self.assertEqual(corrupt.watchlist, WatchList.empty())
        self.assertEqual(len(warnings), 1)
        self.assertIn("watch list", warnings[0])
        (aside,) = [path.name for path in root.iterdir()]
        self.assertTrue(aside.startswith(f"{WATCHLIST_FILE}.corrupt-"))

    def test_a_list_written_under_isolation_is_moved_when_loaded(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        stored = WatchList.empty().with_chat(MEMBER_111, explicit("slug_a"))
        save_json(root / WATCHLIST_FILE, stored.to_payload())

        watcher = watcher_on(root)

        self.assertEqual(watcher.watchlist.chat(GROUP), explicit("slug_a"))
        self.assertEqual(list(load_json(root / WATCHLIST_FILE)["groups"]), [GROUP])


class WatchListTextTests(unittest.TestCase):
    def test_an_empty_list_says_how_to_follow(self) -> None:
        text = format_watchlist(ChatWatch(), command="/zmdlog", top_n=10)

        self.assertIn("还没有关注任何榜单", text)
        self.assertIn("/zmdlog 关注 <榜单关键词>", text)
        self.assertIn("/zmdlog 关注 全部", text)
        self.assertIn("前 10 名", text)
        self.assertNotIn("账号", text)

    def test_an_explicit_list_is_numbered(self) -> None:
        chat, _ = ChatWatch().with_board(
            board_entry(
                "slug_a", boss="山中见犼·苦难", dungeon="影拓丰碑4期 · 山中见犼"
            )
        )

        text = format_watchlist(chat, command="/zmdlog", top_n=10)

        self.assertIn("当前关注的榜单", text)
        self.assertIn("1. 影拓丰碑4期 · 山中见犼 · 苦难", text)
        self.assertIn("取关 <序号或榜单关键词>", text)

    def test_every_board_shows_its_exclusions(self) -> None:
        self.assertIn("当前关注：全部榜单", format_watchlist(every_board(), top_n=10))
        self.assertNotIn("已排除", format_watchlist(every_board(), top_n=10))

        text = format_watchlist(every_board("slug_a", "slug_b"), top_n=8)

        self.assertIn(
            "已排除：\n- 副本 slug_a · 首领 slug_a\n- 副本 slug_b · 首领 slug_b",
            text,
        )
        self.assertIn("新纪录进入前 8 名时通报", text)


class BoardLabelTests(unittest.TestCase):
    def test_the_shared_segment_is_not_repeated(self) -> None:
        self.assertEqual(
            board_label("影拓丰碑4期 · 山中见犼", "山中见犼·苦难"),
            "影拓丰碑4期 · 山中见犼 · 苦难",
        )
        self.assertEqual(
            board_label("危境再现·罗丹", "“碾骨之拳”罗丹"),
            "危境再现 · 罗丹 · “碾骨之拳”罗丹",
        )
        self.assertEqual(board_label("危机合约", "破潮之像"), "危机合约 · 破潮之像")


SLUG_A = "dung01_group_bossrush02"
SLUG_B = "dung01_group_bossrush01"
START = datetime(2026, 10, 5, 2, 30, tzinfo=UTC)


class NoticeCycleTests(unittest.TestCase):
    """The interval: entries queued per chat, one picture each, sent or kept.

    GROUP watches boards A and B, OTHER_GROUP board A alone. Every read is
    a board read by the index, at the fake clock's time; every cycle the
    interval's end.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        watchlist = (
            WatchList.empty()
            .with_chat(GROUP, explicit(SLUG_A, SLUG_B))
            .with_chat(OTHER_GROUP, explicit(SLUG_A))
        )
        save_json(self.root / WATCHLIST_FILE, watchlist.to_payload())
        self.clock = [START]
        self.sent: list[tuple[str, NoticeBatch]] = []
        self.refuse: set[str] = set()
        self.warnings: list[str] = []
        self.watcher = self._start()

    def _start(self) -> RankWatcher:
        """A watcher on the test's directory, as a (re)start loads one."""

        async def send(origin: str, batch: NoticeBatch) -> bool:
            self.sent.append((origin, batch))
            return origin not in self.refuse

        return RankWatcher(
            data=None,
            settings=PluginSettings(),
            data_dir=self.root,
            board_matcher=None,
            candidates=CandidateStore(),
            notify=send,
            logger=_ListLogger(self.warnings),
            now=lambda: self.clock[0],
        )

    def _later(self, minutes: float) -> None:
        self.clock[0] += timedelta(minutes=minutes)

    def _at(self, minutes: float) -> str:
        return (START + timedelta(minutes=minutes)).isoformat()

    def _read(self, previous, current, watcher: RankWatcher | None = None) -> None:
        """One read of a board, handed on the way the data source does."""

        watcher = watcher or self.watcher
        changes = board_changes(
            previous,
            current,
            seen_at=self.clock[0].isoformat(),
            last_ranks={},
            watch=watcher.notice_watch(current.boss_slug),
        )
        watcher.collect(changes)

    def _cycle(self, watcher: RankWatcher | None = None) -> dict[str, NoticeBatch]:
        """One interval's end: what each chat was sent."""

        before = len(self.sent)
        asyncio.run((watcher or self.watcher).run_notice_cycle())
        return dict(self.sent[before:])

    @staticmethod
    def _records(batch: NoticeBatch) -> list[str]:
        return [entry.record.battle_id.removeprefix("btl_") for entry in batch.entries]

    def _snapshot(self, watcher: RankWatcher | None = None) -> tuple[str, ...]:
        return (watcher or self.watcher).board_snapshots[SLUG_A].battle_ids

    def test_one_picture_a_chat_an_interval(self) -> None:
        a0, a1 = standing("a1", "b1"), standing("n1", "a1", "b1")
        a2 = standing("n1", "a1", "m1", "b1")
        b0, b1 = standing("c1", slug=SLUG_B), standing("c1", "x1", slug=SLUG_B)
        self._later(3)
        self._read(a0, a1)
        self._later(3)
        self._read(a1, a2)
        self._later(3)
        self._read(b0, b1)
        self._later(6)

        sent = self._cycle()

        self.assertEqual(list(sent), [GROUP, OTHER_GROUP])
        self.assertEqual(self._records(sent[GROUP]), ["n1", "m1", "x1"])
        self.assertEqual(self._records(sent[OTHER_GROUP]), ["n1", "m1"])
        self.assertEqual(
            (sent[GROUP].window_start, sent[GROUP].window_end),
            (self._at(0), self._at(15)),
        )
        self.assertEqual(sent[GROUP].top_n, 10)
        # Delivered: nothing waits, the snapshot moved to the latest read,
        # and the next interval has nothing to send.
        self.assertEqual(self.watcher.pending(GROUP), ())
        self.assertEqual(self._snapshot(), ("btl_n1", "btl_a1", "btl_m1", "btl_b1"))
        self.assertEqual(
            self.watcher.board_snapshots[SLUG_A].checked_at, self._at(15)
        )
        self._later(15)
        self.assertEqual(self._cycle(), {})

    def test_a_failed_send_is_sent_again_and_holds_the_snapshot(self) -> None:
        self._read(None, standing("a1", "b1"))
        self._later(15)
        self.assertEqual(self._cycle(), {})
        self.assertEqual(self._snapshot(), ("btl_a1", "btl_b1"))

        self.refuse.add(GROUP)
        self._later(5)
        self._read(standing("a1", "b1"), standing("n1", "a1", "b1"))
        self._later(10)
        failed = self._cycle()

        self.assertEqual(list(failed), [GROUP, OTHER_GROUP])
        self.assertEqual(
            [entry.record.battle_id for entry in self.watcher.pending(GROUP)],
            ["btl_n1"],
        )
        # GROUP still waits for n1, so the board's snapshot stays put: a
        # restart now would find n1 again.
        self.assertEqual(self._snapshot(), ("btl_a1", "btl_b1"))

        self.refuse.clear()
        self._later(5)
        self._read(standing("n1", "a1", "b1"), standing("n1", "m1", "a1", "b1"))
        self._later(10)
        resent = self._cycle()

        self.assertEqual(self._records(resent[GROUP]), ["n1", "m1"])
        self.assertEqual(self._records(resent[OTHER_GROUP]), ["m1"])
        # The picture covers the oldest entry it carries.
        self.assertEqual(resent[GROUP].window_start, self._at(20))
        self.assertEqual(resent[OTHER_GROUP].window_start, self._at(30))
        self.assertEqual(self._snapshot(), ("btl_n1", "btl_m1", "btl_a1", "btl_b1"))
        self._later(15)
        self.assertEqual(self._cycle(), {})

    def test_a_restart_finds_what_was_waiting_through_the_snapshot(self) -> None:
        self._read(None, standing("a1", "b1"))
        self._later(15)
        self._cycle()
        self._later(5)
        self._read(standing("a1", "b1"), standing("n1", "a1", "b1"))
        # The bot stops before the interval ends: the queue is gone.
        self._later(5)
        restarted = self._start()
        self.assertEqual(restarted.pending(GROUP), ())

        self._read(None, standing("n1", "a1", "b1"), watcher=restarted)
        self._later(5)
        sent = self._cycle(restarted)

        self.assertEqual(self._records(sent[GROUP]), ["n1"])
        self.assertEqual(self._records(sent[OTHER_GROUP]), ["n1"])
        (entry,) = sent[GROUP].entries
        self.assertTrue(entry.champion)
        self.assertEqual(
            [(pushed.before, pushed.after) for pushed in entry.pushed],
            [(1, 2), (2, 3)],
        )

    def test_after_a_long_stop_a_board_starts_over_silently(self) -> None:
        self._read(None, standing("a1", "b1"))
        self._later(15)
        self._cycle()
        # Longer than max(3 × 15 minutes, 1 hour).
        self._later(61)
        restarted = self._start()

        self._read(None, standing("n1", "a1", "b1"), watcher=restarted)
        self._later(15)

        self.assertEqual(self._cycle(restarted), {})
        self.assertEqual(self._snapshot(restarted), ("btl_n1", "btl_a1", "btl_b1"))

    def test_a_snapshot_no_read_renews_is_dropped_once_too_old(self) -> None:
        # Board B is watched but has not been read since the start: its
        # snapshot is kept while it can still be compared with, no longer.
        self.watcher._save_board_snapshots(
            {SLUG_B: BoardSnapshot(("btl_c1",), self._at(0))}
        )
        self._later(60)
        self._cycle()
        self.assertIn(SLUG_B, self.watcher.board_snapshots)
        self._later(15)
        self._cycle()
        self.assertNotIn(SLUG_B, self.watcher.board_snapshots)

    def test_a_board_unwatched_before_the_send_is_not_sent(self) -> None:
        self._read(standing("a1"), standing("n1", "a1"))
        self.assertTrue(self.watcher._save_chat(OTHER_GROUP, ChatWatch()))
        self._later(15)

        self.assertEqual(list(self._cycle()), [GROUP])
        self.assertEqual(self.watcher.pending(OTHER_GROUP), ())

    def test_a_chat_unreachable_too_long_is_dropped_and_the_snapshot_moves(
        self,
    ) -> None:
        self.refuse.add(GROUP)
        self._read(standing("a1"), standing("n1", "a1"))
        attempts = 0
        for _ in range(5):
            self._later(15)
            attempts += GROUP in self._cycle()

        # Tried for the snapshot age, an hour, then given up on.
        self.assertEqual(attempts, 4)
        self.assertEqual(self.watcher.pending(GROUP), ())
        self.assertIn("could not deliver", self.warnings[-1])
        self.assertEqual(self._snapshot(), ("btl_n1", "btl_a1"))


class BoardSnapshotFileTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_round_trip_and_garbage(self) -> None:
        snapshots = {"slug_a": BoardSnapshot(("btl_a", "btl_b"), CHECKED, whole=True)}

        payload = board_snapshot_payload(snapshots)

        self.assertEqual(payload["version"], 2)
        self.assertEqual(parse_board_snapshot_payload(payload), snapshots)
        self.assertEqual(parse_board_snapshot_payload(None), {})
        self.assertEqual(parse_board_snapshot_payload({"boards": []}), {})
        parsed = parse_board_snapshot_payload(
            {
                "boards": {
                    "slug_a": {
                        "checkedAt": CHECKED,
                        "battleIds": ["btl_a", 3, "", None],
                    },
                    "slug_b": {"battleIds": "junk"},
                    "slug_c": {"checkedAt": None, "battleIds": []},
                }
            }
        )
        self.assertEqual(parsed["slug_a"], BoardSnapshot(("btl_a",), CHECKED))
        self.assertNotIn("slug_b", parsed)
        # A stamp that does not read: a snapshot too old to compare with.
        self.assertEqual(parsed["slug_c"], BoardSnapshot((), ""))

    def test_the_old_top_three_file_is_read_as_a_partial_snapshot(self) -> None:
        run = {"uploaderNickname": "甲", "characterName": "诀", "durationMs": 9771}
        old = {
            "version": 1,
            "boards": {
                "slug_a": {
                    "checkedAt": CHECKED,
                    "runs": [
                        dict(run, battleId="btl_a"),
                        dict(run, battleId="btl_b"),
                        "junk",
                    ],
                },
                "slug_b": {"runs": "junk"},
            },
        }
        (self.root / BOARD_SNAPSHOT_FILE).write_text(json.dumps(old), encoding="utf-8")

        watcher = watcher_on(self.root)

        # Its top three: never the whole board, whatever their number.
        self.assertEqual(
            watcher.board_snapshots,
            {"slug_a": BoardSnapshot(("btl_a", "btl_b"), CHECKED, whole=False)},
        )

    def test_a_missing_file_is_empty_and_a_corrupt_one_is_set_aside(self) -> None:
        warnings: list[str] = []

        self.assertEqual(watcher_on(self.root, warnings).board_snapshots, {})
        self.assertEqual(warnings, [])

        (self.root / BOARD_SNAPSHOT_FILE).write_text("{not json", encoding="utf-8")
        corrupt = watcher_on(self.root, warnings)

        self.assertEqual(corrupt.board_snapshots, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("board snapshot", warnings[0])
        (aside,) = [path.name for path in self.root.iterdir()]
        self.assertTrue(aside.startswith(f"{BOARD_SNAPSHOT_FILE}.corrupt-"))


class TimestampOrderTests(unittest.TestCase):
    def test_stamps_with_different_offsets_compare_as_instants(self) -> None:
        from core.timestamps import later_or_same

        # 05 00:00 +08:00 is 04 16:00 Z: earlier than 04 20:00 Z, whatever
        # the text order says.
        self.assertFalse(
            later_or_same("2026-09-05T00:00:00+08:00", "2026-09-04T20:00:00Z")
        )
        self.assertTrue(
            later_or_same("2026-09-05T00:00:00+08:00", "2026-09-04T15:00:00Z")
        )
        # Stamps that do not parse fall back to text order.
        self.assertTrue(later_or_same("b", "a"))


if __name__ == "__main__":
    unittest.main()
