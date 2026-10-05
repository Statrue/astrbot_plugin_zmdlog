"""Tests for the board watch: watch lists, their file, board diffs, notice text."""

import json
import tempfile
import unittest
from pathlib import Path

from core.candidates import CandidateStore
from core.models import HotBossCard, HotBossRun
from core.persistence import load_json, save_json
from core.rank_watch import WATCHLIST_FILE, RankWatcher
from core.settings import PluginSettings
from core.watch import (
    BoardSnapshot,
    BoardTopRun,
    Notice,
    NoticeLink,
    board_snapshot_is_usable,
    board_snapshot_payload,
    build_board_snapshot,
    find_top_run_changes,
    format_board_notice,
    join_board_notices,
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

GROUP = "aiocqhttp:GroupMessage:1"
OTHER_GROUP = "aiocqhttp:GroupMessage:2"
CHECKED = "2026-08-22T00:00:00+00:00"
AFTER = "2026-08-22T06:00:00+00:00"
ADDED_AT = "2026-08-22T10:00:00+00:00"
# What 隔离对话 made of GROUP for members 111 and 222.
MEMBER_111 = "aiocqhttp:GroupMessage:111_1"
MEMBER_222 = "aiocqhttp:GroupMessage:222_1"


def battle_url(index: int) -> str:
    return f"https://zmdlogs.com/battle/btl_upload_{index}"


def link(index: int) -> "NoticeLink":
    """The one link of a single-battle notice, numbered as its own message."""

    return NoticeLink(battle_url(index), "战报 1")


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


def card(slug: str, *runs: tuple[str, str, str, int]) -> HotBossCard:
    """A hot-bosses card from (battleId, nickname, character, durationMs) runs."""

    return HotBossCard(
        boss_slug=slug,
        boss_key=f"key-{slug}",
        boss_name=f"首领 {slug}",
        dungeon_name=f"副本 {slug}",
        top_speed_runs=tuple(
            HotBossRun(
                battle_id=battle_id,
                duration_ms=duration_ms,
                uploader_nickname=nickname,
                character_name=character,
            )
            for battle_id, nickname, character, duration_ms in runs
        ),
    )


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
        client=None,
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
        text = format_watchlist(ChatWatch(), command="/zmdlog")

        self.assertIn("还没有关注任何榜单", text)
        self.assertIn("/zmdlog 关注 <榜单关键词>", text)
        self.assertIn("/zmdlog 关注 全部", text)
        self.assertNotIn("账号", text)

    def test_an_explicit_list_is_numbered(self) -> None:
        chat, _ = ChatWatch().with_board(
            board_entry(
                "slug_a", boss="山中见犼·苦难", dungeon="影拓丰碑4期 · 山中见犼"
            )
        )

        text = format_watchlist(chat, command="/zmdlog")

        self.assertIn("当前关注的榜单", text)
        self.assertIn("1. 影拓丰碑4期 · 山中见犼 · 苦难", text)
        self.assertIn("取关 <序号或榜单关键词>", text)

    def test_every_board_shows_its_exclusions(self) -> None:
        self.assertIn("当前关注：全部榜单", format_watchlist(every_board()))
        self.assertNotIn("已排除", format_watchlist(every_board()))

        text = format_watchlist(every_board("slug_a", "slug_b"))

        self.assertIn(
            "已排除：\n- 副本 slug_a · 首领 slug_a\n- 副本 slug_b · 首领 slug_b",
            text,
        )


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


class BoardDiffTests(unittest.TestCase):
    def test_first_sighting_and_an_unchanged_top_are_silent(self) -> None:
        current = card(
            "slug_a", ("btl_a", "甲", "诀", 9771), ("btl_b", "乙", "洛茜", 10000)
        )
        baseline = build_board_snapshot(current, checked_at=CHECKED)

        self.assertIsNone(find_top_run_changes(None, current))
        self.assertIsNone(find_top_run_changes(baseline, current))
        self.assertEqual([run.battle_id for run in baseline.runs], ["btl_a", "btl_b"])
        self.assertEqual(baseline.runs[0].uploader_nickname, "甲")
        self.assertEqual(baseline.checked_at, CHECKED)

    def test_a_new_record_reports_the_entry_and_who_fell_out(self) -> None:
        previous = build_board_snapshot(
            card(
                "slug_a",
                ("btl_a", "甲", "诀", 9771),
                ("btl_b", "乙", "洛茜", 10000),
                ("btl_c", "丙", "卡缪", 11000),
            ),
            checked_at=CHECKED,
        )
        current = card(
            "slug_a",
            ("btl_new", "丁", "黎风", 9000),
            ("btl_a", "甲", "诀", 9771),
            ("btl_b", "乙", "洛茜", 10000),
        )

        change = find_top_run_changes(previous, current)

        self.assertEqual(
            [(entry.rank, entry.run.battle_id) for entry in change.new_runs],
            [(1, "btl_new")],
        )
        self.assertEqual(
            [
                (entry.rank, entry.run.uploader_nickname)
                for entry in change.dropped_runs
            ],
            [(3, "丙")],
        )
        self.assertEqual(change.boss_name, "首领 slug_a")

    def test_a_deleted_record_is_not_news(self) -> None:
        previous = build_board_snapshot(
            card("slug_a", ("btl_a", "甲", "诀", 9771), ("btl_b", "乙", "洛茜", 10000)),
            checked_at=CHECKED,
        )

        shrunken = card("slug_a", ("btl_b", "乙", "洛茜", 10000))

        self.assertIsNone(find_top_run_changes(previous, shrunken))

    def test_board_baseline_freshness(self) -> None:
        fresh = BoardSnapshot(runs=(), checked_at=CHECKED)

        self.assertTrue(
            board_snapshot_is_usable(fresh, now=AFTER, max_age_seconds=86_400)
        )
        self.assertFalse(
            board_snapshot_is_usable(fresh, now=AFTER, max_age_seconds=3_600)
        )
        self.assertFalse(
            board_snapshot_is_usable(None, now=AFTER, max_age_seconds=86_400)
        )
        self.assertFalse(
            board_snapshot_is_usable(
                BoardSnapshot(runs=(), checked_at=None),
                now=AFTER,
                max_age_seconds=86_400,
            )
        )


class BoardNoticeTests(unittest.TestCase):
    def test_notice_lists_new_runs_with_links_then_the_displaced(self) -> None:
        previous = build_board_snapshot(
            card(
                "slug_a",
                ("btl_a", "甲", "诀", 9771),
                ("btl_b", "乙", "洛茜", 10000),
                ("btl_c", "丙", "卡缪", 11000),
            ),
            checked_at=CHECKED,
        )
        current = card(
            "slug_a",
            ("btl_new1", "丁", "黎风", 9000),
            ("btl_new2", "戊", "余烬", 9500),
            ("btl_a", "甲", "诀", 9771),
        )

        notice = format_board_notice(
            find_top_run_changes(previous, current), web_base_url="https://zmdlogs.com"
        )

        text = notice.text
        self.assertIn("「副本 slug_a · 首领 slug_a」前三名有新纪录", text)
        self.assertIn("第 1 名 · 丁 · 主C 黎风 · 用时 0:09.000", text)
        self.assertIn("第 2 名 · 戊 · 主C 余烬 · 用时 0:09.500", text)
        self.assertIn(
            "跌出前三：乙（原第 2 · 主C 洛茜）、丙（原第 3 · 主C 卡缪）", text
        )
        self.assertNotIn("超", text)
        # Each new run's battle, as the text prints it; the displaced get none.
        urls = [
            "https://zmdlogs.com/battle/btl_new1",
            "https://zmdlogs.com/battle/btl_new2",
        ]
        self.assertEqual([link.url for link in notice.links], urls)
        self.assertEqual([link.label for link in notice.links], ["战报 1", "战报 2"])
        for url in urls:
            self.assertIn(url, text.splitlines())

    def test_a_new_record_on_an_empty_board_has_nobody_to_displace(self) -> None:
        previous = BoardSnapshot(runs=(), checked_at=CHECKED)
        notice = format_board_notice(
            find_top_run_changes(previous, card("slug_a", ("btl_a", "甲", "诀", 9771))),
            web_base_url="https://zmdlogs.com",
        )

        self.assertIn("第 1 名 · 甲", notice.text)
        self.assertNotIn("跌出前三", notice.text)

    def test_board_notices_merge_per_chat(self) -> None:
        notices = tuple(
            Notice(f"🏁 榜单{index}\n{battle_url(index)}", (link(index),))
            for index in range(5)
        )

        merged = join_board_notices(notices)

        self.assertEqual(merged.text.count("🏁"), 3)
        self.assertIn("另有 2 个关注的榜单也有新纪录。", merged.text)
        self.assertEqual(
            [entry.label for entry in merged.links], ["战报 1", "战报 2", "战报 3"]
        )
        self.assertEqual(join_board_notices(notices[:1]), notices[0])


class BoardSnapshotPayloadTests(unittest.TestCase):
    def test_round_trip_and_garbage(self) -> None:
        snapshots = {
            "slug_a": build_board_snapshot(
                card("slug_a", ("btl_a", "甲", "诀", 9771)), checked_at=CHECKED
            )
        }

        self.assertEqual(
            parse_board_snapshot_payload(board_snapshot_payload(snapshots)), snapshots
        )
        self.assertEqual(parse_board_snapshot_payload(None), {})
        self.assertEqual(parse_board_snapshot_payload({"boards": []}), {})
        parsed = parse_board_snapshot_payload(
            {
                "boards": {
                    "slug_a": {
                        "checkedAt": CHECKED,
                        "runs": [
                            {"battleId": "btl_a", "durationMs": "x"},
                            {"nope": 1},
                            "junk",
                        ],
                    },
                    "slug_b": {"runs": "junk"},
                    "slug_c": {"checkedAt": None, "runs": []},
                }
            }
        )
        self.assertEqual(parsed["slug_a"].runs, (BoardTopRun("btl_a", "", "", 0),))
        self.assertEqual(parsed["slug_a"].checked_at, CHECKED)
        self.assertNotIn("slug_b", parsed)
        self.assertEqual(parsed["slug_c"], BoardSnapshot(runs=(), checked_at=None))


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
