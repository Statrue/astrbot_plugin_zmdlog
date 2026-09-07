"""Tests for the 0.5.0 rank watch: watch lists, rank diffing, notice text."""

import copy
import unittest
from dataclasses import replace

from core.models import (
    HotBossCard,
    HotBossRun,
    parse_boss_ranking,
    parse_public_user_rankings,
)
from core.timestamps import parse_timestamp
from core.watch import (
    AccountSnapshot,
    BoardSnapshot,
    BoardTopRun,
    board_snapshot_is_usable,
    board_snapshot_payload,
    build_board_snapshot,
    build_snapshot,
    find_new_record_above,
    find_rank_drops,
    find_top_run_changes,
    format_board_notice,
    format_rank_drop_notice,
    join_board_notices,
    join_rank_drop_notices,
    parse_board_snapshot_payload,
    parse_snapshot_payload,
    snapshot_is_usable,
    snapshot_payload,
    snapshot_ranks,
)
from core.watchlist import (
    WatchedAccount,
    WatchedBoard,
    WatchList,
    board_label,
    format_watchlist,
    parse_watchlist,
)
from tests.helpers import (
    public_user_rankings_payload,
    ranking_payload_with_rows,
)

GROUP = "aiocqhttp:GroupMessage:1"
OTHER_GROUP = "aiocqhttp:GroupMessage:2"
BEFORE = "2026-08-20T00:00:00+00:00"
CHECKED = "2026-08-22T00:00:00+00:00"
AFTER = "2026-08-22T06:00:00+00:00"
LATER = "2026-08-22T09:00:00+00:00"


def account(
    account_id: str = "usr_1",
    display_name: str = "CPU 0",
    added_by: str = "aiocqhttp:111",
) -> WatchedAccount:
    return WatchedAccount(
        account_id=account_id,
        display_name=display_name,
        added_by=added_by,
        added_at="2026-08-22T10:00:00+00:00",
    )


def rankings(*entries: tuple[str, int]):
    """Build a public account response with one row per (slug, rank) pair."""

    payload = public_user_rankings_payload()
    payload["accountId"] = "usr_watched"
    payload["accountDisplayName"] = "CPU 0"
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


def board(*rows: tuple[int, str, str]):
    """Build a ranking from (rank, accountId, battleEndAt) triples."""

    payload = ranking_payload_with_rows()
    template = payload["rows"][0]
    built = []
    for rank, account_id, battle_end_at in rows:
        row = copy.deepcopy(template)
        row["rank"] = rank
        row["accountId"] = account_id
        row["accountDisplayName"] = f"玩家-{account_id}"
        row["battleId"] = f"btl_upload_{account_id}"
        row["battleEndAt"] = battle_end_at
        row["dps"] = 200_000 - rank * 1000
        built.append(row)
    payload["rows"] = built
    return parse_boss_ranking(payload)


def snapshot(ranks: dict[str, int], checked_at: str | None = CHECKED):
    return AccountSnapshot(ranks=ranks, checked_at=checked_at)


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
        added_at="2026-08-22T10:00:00+00:00",
    )


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


class WatchListTests(unittest.TestCase):
    def test_adding_is_per_group_and_keeps_insertion_order(self) -> None:
        watchlist, added = WatchList.empty().with_account(GROUP, account())
        self.assertTrue(added)
        watchlist, added = watchlist.with_account(
            GROUP, account("usr_2", "CPU 1")
        )
        self.assertTrue(added)
        watchlist, added = watchlist.with_account(
            OTHER_GROUP, account("usr_3", "CPU 2")
        )

        self.assertTrue(added)
        self.assertEqual(
            [entry.account_id for entry in watchlist.accounts_for(GROUP)],
            ["usr_1", "usr_2"],
        )
        self.assertEqual(
            [entry.account_id for entry in watchlist.accounts_for(OTHER_GROUP)],
            ["usr_3"],
        )
        self.assertEqual(watchlist.total_accounts, 3)

    def test_re_adding_refreshes_the_nickname_without_moving_or_stealing(
        self,
    ) -> None:
        watchlist, _ = WatchList.empty().with_account(GROUP, account())
        watchlist, _ = watchlist.with_account(GROUP, account("usr_2", "CPU 1"))
        watchlist, added = watchlist.with_account(
            GROUP,
            account("usr_1", "改名了", added_by="aiocqhttp:999"),
        )

        self.assertFalse(added)
        first = watchlist.accounts_for(GROUP)[0]
        self.assertEqual(first.account_id, "usr_1")
        self.assertEqual(first.display_name, "改名了")
        self.assertEqual(first.added_by, "aiocqhttp:111")

    def test_origins_by_account_inverts_the_list(self) -> None:
        watchlist, _ = WatchList.empty().with_account(GROUP, account())
        watchlist, _ = watchlist.with_account(OTHER_GROUP, account())
        watchlist, _ = watchlist.with_account(GROUP, account("usr_2", "CPU 1"))

        self.assertEqual(
            watchlist.origins_by_account(),
            {"usr_1": (GROUP, OTHER_GROUP), "usr_2": (GROUP,)},
        )

    def test_resolve_accepts_index_id_and_nickname(self) -> None:
        watchlist, _ = WatchList.empty().with_account(GROUP, account())
        watchlist, _ = watchlist.with_account(GROUP, account("usr_2", "阿米娅"))

        self.assertEqual(watchlist.resolve(GROUP, "2").account_id, "usr_2")
        self.assertEqual(watchlist.resolve(GROUP, "usr_1").account_id, "usr_1")
        self.assertEqual(watchlist.resolve(GROUP, "阿米娅").account_id, "usr_2")
        self.assertEqual(watchlist.resolve(GROUP, "cpu 0").account_id, "usr_1")
        self.assertIsNone(watchlist.resolve(GROUP, "3"))
        self.assertIsNone(watchlist.resolve(GROUP, "查无此人"))
        self.assertIsNone(watchlist.resolve(OTHER_GROUP, "1"))

    def test_an_absurd_selector_is_rejected_instead_of_raising(self) -> None:
        watchlist, _ = WatchList.empty().with_account(GROUP, account())

        # int() refuses digit strings this long; the handler must not see that.
        self.assertEqual(watchlist.resolve_matches(GROUP, "9" * 4400), ())
        self.assertIsNone(watchlist.resolve(GROUP, "9" * 4400))
        self.assertEqual(watchlist.resolve_matches(GROUP, "0"), ())

    def test_only_the_adder_or_an_admin_may_remove(self) -> None:
        entry = account()

        self.assertTrue(entry.removable_by("aiocqhttp:111", is_admin=False))
        self.assertFalse(entry.removable_by("aiocqhttp:222", is_admin=False))
        self.assertTrue(entry.removable_by("aiocqhttp:222", is_admin=True))
        # An entry whose adder was not recorded must not become everyone's.
        self.assertFalse(account(added_by="").removable_by("", is_admin=False))

    def test_removing_the_last_account_drops_the_group(self) -> None:
        watchlist, _ = WatchList.empty().with_account(GROUP, account())
        watchlist, _ = watchlist.with_account(OTHER_GROUP, account("usr_2"))
        watchlist = watchlist.without_account(GROUP, "usr_1")

        self.assertEqual(watchlist.accounts_for(GROUP), ())
        self.assertEqual(len(watchlist.groups), 1)

    def test_payload_round_trip_and_tolerance_for_garbage(self) -> None:
        watchlist, _ = WatchList.empty().with_account(GROUP, account())

        self.assertEqual(parse_watchlist(watchlist.to_payload()), watchlist)
        self.assertEqual(parse_watchlist(None), WatchList.empty())
        self.assertEqual(parse_watchlist({"groups": []}), WatchList.empty())
        salvaged = parse_watchlist(
            {
                "groups": {
                    GROUP: [
                        {"accountId": "usr_1"},
                        {"accountId": "usr_1", "displayName": "重复"},
                        {"displayName": "没有 id"},
                        "不是字典",
                    ],
                    "": [{"accountId": "usr_9"}],
                }
            }
        )
        self.assertEqual(
            [entry.account_id for entry in salvaged.accounts_for(GROUP)],
            ["usr_1"],
        )
        self.assertEqual(salvaged.accounts_for(GROUP)[0].display_name, "usr_1")
        self.assertEqual(salvaged.total_accounts, 1)

    def test_list_text_numbers_the_entries(self) -> None:
        watchlist, _ = WatchList.empty().with_account(GROUP, account())
        text = format_watchlist(watchlist.accounts_for(GROUP))

        self.assertIn("1. CPU 0（usr_1）", text)
        self.assertIn("关注", format_watchlist(()))


class RankDiffTests(unittest.TestCase):
    def test_a_first_sighting_never_announces_anything(self) -> None:
        current = rankings(("boss_a", 1), ("boss_b", 4))

        self.assertEqual(find_rank_drops(None, current), ())
        self.assertEqual(find_rank_drops(snapshot({}), current), ())
        self.assertEqual(snapshot_ranks(current), {"boss_a": 1, "boss_b": 4})
        self.assertEqual(
            build_snapshot(current, checked_at=CHECKED),
            snapshot({"boss_a": 1, "boss_b": 4}),
        )

    def test_only_drops_from_inside_the_threshold_count(self) -> None:
        previous = snapshot(
            {"kept": 3, "dropped": 1, "improved": 8, "deep": 12, "gone": 5}
        )
        current = rankings(
            ("kept", 3),
            ("dropped", 2),
            ("improved", 4),
            ("deep", 20),
            ("fresh", 1),
        )

        drops = find_rank_drops(previous, current, rank_threshold=10)

        self.assertEqual([drop.boss_slug for drop in drops], ["dropped"])
        self.assertEqual(drops[0].previous_rank, 1)
        self.assertEqual(drops[0].current_rank, 2)
        self.assertIsNone(drops[0].new_record_above)

    def test_drops_are_ordered_by_how_high_the_account_was(self) -> None:
        previous = snapshot({"boss_a": 5, "boss_b": 1, "boss_c": 3})
        current = rankings(("boss_a", 9), ("boss_b", 2), ("boss_c", 4))

        drops = find_rank_drops(previous, current)

        self.assertEqual([drop.previous_rank for drop in drops], [1, 3, 5])

    def test_threshold_is_applied_to_the_old_rank(self) -> None:
        previous = snapshot({"boss_a": 10, "boss_b": 11})
        current = rankings(("boss_a", 30), ("boss_b", 12))

        drops = find_rank_drops(previous, current, rank_threshold=10)

        self.assertEqual([drop.boss_slug for drop in drops], ["boss_a"])


class OvertakerTests(unittest.TestCase):
    def test_a_new_record_above_the_account_is_the_overtaker(self) -> None:
        # usr_new landed after the last check and pushed the watched account
        # from rank 1 to rank 2.
        ranking = board(
            (1, "usr_new", AFTER),
            (2, "usr_watched", BEFORE),
            (3, "usr_old", BEFORE),
        )

        record = find_new_record_above(
            ranking,
            account_id="usr_watched",
            fallback_rank=2,
            since=CHECKED,
        )

        self.assertEqual(record.account_display_name, "玩家-usr_new")
        self.assertEqual(record.record_count, 1)

    def test_a_bystander_shifted_down_is_never_blamed(self) -> None:
        # The regression this function exists for: usr_new inserts at rank 1 so
        # everyone shifts down. The watched account went 3 -> 4, and the row now
        # sitting at its old rank 3 is usr_bystander, who uploaded nothing and
        # overtook nobody.
        ranking = board(
            (1, "usr_new", AFTER),
            (2, "usr_ahead", BEFORE),
            (3, "usr_bystander", BEFORE),
            (4, "usr_watched", BEFORE),
        )

        record = find_new_record_above(
            ranking,
            account_id="usr_watched",
            fallback_rank=4,
            since=CHECKED,
        )

        self.assertEqual(record.account_display_name, "玩家-usr_new")

    def test_several_new_records_are_counted_not_pinned_on_one(self) -> None:
        ranking = board(
            (1, "usr_new_a", AFTER),
            (2, "usr_new_b", LATER),
            (3, "usr_watched", BEFORE),
        )

        record = find_new_record_above(
            ranking,
            account_id="usr_watched",
            fallback_rank=3,
            since=CHECKED,
        )

        self.assertEqual(record.record_count, 2)
        # The newest of them, not the best ranked.
        self.assertEqual(record.account_display_name, "玩家-usr_new_b")

    def test_nothing_is_claimed_without_a_baseline_or_a_new_record(
        self,
    ) -> None:
        ranking = board((1, "usr_new", AFTER), (2, "usr_watched", BEFORE))

        self.assertIsNone(
            find_new_record_above(
                ranking,
                account_id="usr_watched",
                fallback_rank=2,
                since=None,
            )
        )
        self.assertIsNone(
            find_new_record_above(
                board((1, "usr_old", BEFORE), (2, "usr_watched", BEFORE)),
                account_id="usr_watched",
                fallback_rank=2,
                since=CHECKED,
            )
        )

    def test_a_stale_ranking_never_names_the_account_itself(self) -> None:
        # The shared board cache can still hold a ranking from before the drop,
        # in which the watched account itself occupies a rank above its current
        # one. It must never be named, and the cutoff must come from this same
        # response rather than from the separate account read.
        ranking = board((1, "usr_watched", LATER), (2, "usr_other", AFTER))

        self.assertIsNone(
            find_new_record_above(
                ranking,
                account_id="usr_watched",
                fallback_rank=3,
                since=CHECKED,
            )
        )

    def test_unusable_timestamps_are_rejected_rather_than_guessed(self) -> None:
        self.assertIsNone(parse_timestamp(None))
        self.assertIsNone(parse_timestamp(""))
        self.assertIsNone(parse_timestamp("昨天"))
        # No offset means it cannot be compared against an aware stamp.
        self.assertIsNone(parse_timestamp("2026-08-22T00:00:00"))
        self.assertIsNotNone(parse_timestamp("2026-08-22T00:00:00Z"))
        self.assertIsNotNone(parse_timestamp("2026-08-22T08:00:00+08:00"))


class NoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.drops = find_rank_drops(
            snapshot({"dung01_group_bossrush02": 1}),
            rankings(("dung01_group_bossrush02", 2)),
        )

    def test_notice_names_the_board_both_ranks_and_the_new_record(self) -> None:
        ranking = board((1, "usr_new", AFTER), (2, "usr_watched", BEFORE))
        drop = replace(
            self.drops[0],
            new_record_above=find_new_record_above(
                ranking,
                account_id="usr_watched",
                fallback_rank=2,
                since=CHECKED,
            ),
        )

        notice = format_rank_drop_notice(
            "CPU 0", (drop,), web_base_url="https://zmdlogs.com"
        )

        self.assertIn("CPU 0", notice)
        self.assertIn("第 1 → 第 2", notice)
        self.assertIn("期间上方新增纪录：玩家-usr_new", notice)
        # The wording must never claim this record is what overtook the account.
        self.assertNotIn("超过", notice)
        self.assertIn("199,000 DPS", notice)
        self.assertIn("https://zmdlogs.com/battle/btl_upload_usr_new", notice)

    def test_notice_says_so_when_several_records_landed(self) -> None:
        ranking = board(
            (1, "usr_new_a", AFTER),
            (2, "usr_new_b", LATER),
            (3, "usr_watched", BEFORE),
        )
        drop = replace(
            self.drops[0],
            new_record_above=find_new_record_above(
                ranking,
                account_id="usr_watched",
                fallback_rank=3,
                since=CHECKED,
            ),
        )

        notice = format_rank_drop_notice(
            "CPU 0", (drop,), web_base_url="https://zmdlogs.com"
        )

        self.assertIn("期间上方新增 2 条纪录", notice)

    def test_notice_without_an_overtaker_keeps_the_rank_line(self) -> None:
        notice = format_rank_drop_notice(
            "CPU 0", self.drops, web_base_url="https://zmdlogs.com"
        )

        self.assertIn("第 1 → 第 2", notice)
        self.assertNotIn("期间上方", notice)

    def test_a_flood_of_drops_is_summarised(self) -> None:
        previous = snapshot({f"boss_{index}": 1 for index in range(8)})
        current = rankings(*((f"boss_{index}", 2) for index in range(8)))

        notice = format_rank_drop_notice(
            "CPU 0",
            find_rank_drops(previous, current),
            web_base_url="https://zmdlogs.com",
        )

        self.assertEqual(notice.count("第 1 → 第 2"), 5)
        self.assertIn("另有 3 个榜单也掉了名次。", notice)

    def test_one_cycle_sends_a_chat_a_single_merged_message(self) -> None:
        notices = tuple(f"📉 账号{index} 被顶屁股了" for index in range(5))

        merged = join_rank_drop_notices(notices)

        self.assertEqual(merged.count("被顶屁股了"), 3)
        self.assertIn("另有 2 个关注的账号也掉了名次。", merged)
        self.assertEqual(
            join_rank_drop_notices(notices[:1]), "📉 账号0 被顶屁股了"
        )


class SnapshotFreshnessTests(unittest.TestCase):
    def test_a_baseline_from_before_a_long_outage_is_not_usable(self) -> None:
        fresh = AccountSnapshot(ranks={"boss_a": 1}, checked_at=CHECKED)

        self.assertTrue(
            snapshot_is_usable(fresh, now=AFTER, max_age_seconds=86_400)
        )
        # 6 hours old against a 1 hour bound.
        self.assertFalse(
            snapshot_is_usable(fresh, now=AFTER, max_age_seconds=3_600)
        )

    def test_unusable_shapes_are_rejected(self) -> None:
        for candidate in (
            None,
            AccountSnapshot(ranks={}, checked_at=CHECKED),
            AccountSnapshot(ranks={"boss_a": 1}, checked_at=None),
            AccountSnapshot(ranks={"boss_a": 1}, checked_at="昨天"),
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(
                    snapshot_is_usable(
                        candidate, now=AFTER, max_age_seconds=86_400
                    )
                )

    def test_a_baseline_stamped_in_the_future_is_not_trusted(self) -> None:
        ahead = AccountSnapshot(ranks={"boss_a": 1}, checked_at=LATER)

        self.assertFalse(
            snapshot_is_usable(ahead, now=AFTER, max_age_seconds=86_400)
        )


class WatchListMaintenanceTests(unittest.TestCase):
    def test_resolve_reports_every_match_so_ambiguity_can_be_explained(
        self,
    ) -> None:
        watchlist, _ = WatchList.empty().with_account(
            GROUP, account("usr_1", "同名")
        )
        watchlist, _ = watchlist.with_account(GROUP, account("usr_2", "同名"))

        self.assertEqual(len(watchlist.resolve_matches(GROUP, "同名")), 2)
        self.assertIsNone(watchlist.resolve(GROUP, "同名"))
        self.assertEqual(watchlist.resolve_matches(GROUP, "查无此人"), ())
        self.assertEqual(watchlist.resolve(GROUP, "2").account_id, "usr_2")

    def test_live_nicknames_refresh_across_every_group(self) -> None:
        watchlist, _ = WatchList.empty().with_account(GROUP, account())
        watchlist, _ = watchlist.with_account(OTHER_GROUP, account())
        watchlist, _ = watchlist.with_account(GROUP, account("usr_2", "CPU 1"))

        renamed, changed = watchlist.with_display_names({"usr_1": "改名了"})

        self.assertTrue(changed)
        self.assertEqual(renamed.accounts_for(GROUP)[0].display_name, "改名了")
        self.assertEqual(
            renamed.accounts_for(OTHER_GROUP)[0].display_name, "改名了"
        )
        self.assertEqual(renamed.accounts_for(GROUP)[1].display_name, "CPU 1")
        self.assertEqual(
            watchlist.with_display_names({"usr_1": "CPU 0"}), (watchlist, False)
        )
        self.assertEqual(watchlist.with_display_names({}), (watchlist, False))


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


class BoardWatchListTests(unittest.TestCase):
    def test_boards_are_kept_apart_from_accounts(self) -> None:
        watchlist, added = WatchList.empty().with_board(GROUP, board_entry("slug_a"))
        self.assertTrue(added)
        watchlist, added = watchlist.with_board(
            GROUP, board_entry("slug_a", boss="改名", added_by="aiocqhttp:999")
        )
        self.assertFalse(added)
        self.assertEqual(watchlist.boards_for(GROUP)[0].boss_name, "改名")
        self.assertEqual(watchlist.boards_for(GROUP)[0].added_by, "aiocqhttp:111")
        watchlist, _ = watchlist.with_board(OTHER_GROUP, board_entry("slug_a"))
        watchlist, _ = watchlist.with_account(GROUP, account())

        self.assertEqual(watchlist.origins_by_board(), {"slug_a": (GROUP, OTHER_GROUP)})
        self.assertEqual(watchlist.total_boards, 2)
        self.assertEqual(watchlist.total_accounts, 1)
        without = watchlist.without_board(GROUP, "slug_a")
        self.assertEqual(without.boards_for(GROUP), ())
        self.assertEqual(without.accounts_for(GROUP)[0].account_id, "usr_1")
        self.assertEqual(without.boards_for(OTHER_GROUP)[0].boss_slug, "slug_a")
        self.assertEqual(without.origins_by_board(), {"slug_a": (OTHER_GROUP,)})

    def test_board_selectors_accept_index_slug_and_names(self) -> None:
        watchlist, _ = WatchList.empty().with_board(
            GROUP, board_entry("slug_a", boss="“碾骨之拳”罗丹", dungeon="危境再现·罗丹")
        )
        watchlist, _ = watchlist.with_board(
            GROUP,
            board_entry(
                "slug_b", boss="山中见犼·苦难", dungeon="影拓丰碑4期 · 山中见犼"
            ),
        )

        self.assertEqual(watchlist.resolve_board(GROUP, "2").boss_slug, "slug_b")
        self.assertEqual(watchlist.resolve_board(GROUP, "slug_a").boss_slug, "slug_a")
        self.assertEqual(
            watchlist.resolve_board(GROUP, "山中见犼·苦难").boss_slug, "slug_b"
        )
        self.assertEqual(watchlist.resolve_board(GROUP, "罗丹").boss_slug, "slug_a")
        self.assertIsNone(watchlist.resolve_board(GROUP, "3"))
        self.assertIsNone(watchlist.resolve_board(GROUP, "查无此榜"))
        self.assertIsNone(watchlist.resolve_board(OTHER_GROUP, "1"))
        self.assertEqual(watchlist.resolve_board_matches(GROUP, "9" * 4400), ())
        self.assertEqual(len(watchlist.resolve_board_matches(GROUP, "见犼")), 1)

    def test_payload_round_trip_and_version_one_files_still_load(self) -> None:
        watchlist, _ = WatchList.empty().with_account(GROUP, account())
        watchlist, _ = watchlist.with_board(GROUP, board_entry("slug_a"))
        watchlist, _ = watchlist.with_board(OTHER_GROUP, board_entry("slug_b"))

        payload = watchlist.to_payload()

        self.assertEqual(payload["version"], 2)
        self.assertEqual(parse_watchlist(payload), watchlist)
        legacy = parse_watchlist(
            {"groups": {GROUP: [{"accountId": "usr_1", "displayName": "CPU 0"}]}}
        )
        self.assertEqual(legacy.accounts_for(GROUP)[0].display_name, "CPU 0")
        self.assertEqual(legacy.boards_for(GROUP), ())
        salvaged = parse_watchlist(
            {
                "groups": {
                    GROUP: {
                        "accounts": "不是列表",
                        "boards": [
                            {"bossSlug": "s"},
                            {"bossSlug": "s", "bossName": "重复"},
                            {"bossName": "没有 slug"},
                            1,
                        ],
                    }
                }
            }
        )
        self.assertEqual(
            [board.boss_slug for board in salvaged.boards_for(GROUP)], ["s"]
        )
        self.assertEqual(salvaged.boards_for(GROUP)[0].boss_name, "s")
        self.assertEqual(salvaged.accounts_for(GROUP), ())

    def test_list_text_shows_both_sections(self) -> None:
        watchlist, _ = WatchList.empty().with_account(GROUP, account())
        watchlist, _ = watchlist.with_board(
            GROUP,
            board_entry(
                "slug_a", boss="山中见犼·苦难", dungeon="影拓丰碑4期 · 山中见犼"
            ),
        )

        text = format_watchlist(
            watchlist.accounts_for(GROUP), boards=watchlist.boards_for(GROUP)
        )

        self.assertIn("1. CPU 0（usr_1）", text)
        self.assertIn("1. 影拓丰碑4期 · 山中见犼 · 苦难", text)
        boards_only = format_watchlist((), boards=watchlist.boards_for(GROUP))
        self.assertNotIn("当前关注的账号", boards_only)
        self.assertIn("榜单", format_watchlist(()))


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

        self.assertIn("「副本 slug_a · 首领 slug_a」前三名有新纪录", notice)
        self.assertIn("第 1 名 · 丁 · 主C 黎风 · 用时 0:09.000", notice)
        self.assertIn("第 2 名 · 戊 · 主C 余烬 · 用时 0:09.500", notice)
        self.assertIn("https://zmdlogs.com/battle/btl_new1", notice)
        self.assertIn("https://zmdlogs.com/battle/btl_new2", notice)
        self.assertIn(
            "跌出前三：乙（原第 2 · 主C 洛茜）、丙（原第 3 · 主C 卡缪）", notice
        )
        self.assertNotIn("超", notice)

    def test_a_new_record_on_an_empty_board_has_nobody_to_displace(self) -> None:
        previous = BoardSnapshot(runs=(), checked_at=CHECKED)
        notice = format_board_notice(
            find_top_run_changes(previous, card("slug_a", ("btl_a", "甲", "诀", 9771))),
            web_base_url="https://zmdlogs.com",
        )

        self.assertIn("第 1 名 · 甲", notice)
        self.assertNotIn("跌出前三", notice)

    def test_board_notices_merge_per_chat(self) -> None:
        notices = tuple(f"🏁 榜单{index}" for index in range(5))

        merged = join_board_notices(notices)

        self.assertEqual(merged.count("🏁"), 3)
        self.assertIn("另有 2 个关注的榜单也有新纪录。", merged)
        self.assertEqual(join_board_notices(notices[:1]), "🏁 榜单0")


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


class SnapshotPayloadTests(unittest.TestCase):
    def test_round_trip_keeps_the_ranks_and_the_check_time(self) -> None:
        snapshots = {"usr_1": snapshot({"boss_a": 1, "boss_b": 7})}

        restored = parse_snapshot_payload(snapshot_payload(snapshots))

        self.assertEqual(restored, snapshots)
        self.assertEqual(restored["usr_1"].checked_at, CHECKED)

    def test_garbage_is_dropped_instead_of_raising(self) -> None:
        self.assertEqual(parse_snapshot_payload(None), {})
        self.assertEqual(parse_snapshot_payload({"accounts": []}), {})
        # The version 1 shape had no "ranks" key, so it re-seeds rather than
        # being read as a baseline, which is the safe direction.
        self.assertEqual(
            parse_snapshot_payload({"accounts": {"usr_1": {"boss_a": 1}}}), {}
        )
        self.assertEqual(
            parse_snapshot_payload(
                {
                    "accounts": {
                        "usr_1": {
                            "checkedAt": CHECKED,
                            "ranks": {"boss_a": 1, "boss_b": "二", "boss_c": 0},
                        },
                        "usr_2": {"ranks": {"boss_a": True}},
                        "usr_3": {"ranks": "不是字典"},
                        "usr_4": {"ranks": {"boss_a": 2}},
                    }
                }
            ),
            {
                "usr_1": snapshot({"boss_a": 1}),
                # "ranks" present but empty is a real state (no public records),
                # not garbage, so it survives the round trip.
                "usr_2": snapshot({}, checked_at=None),
                "usr_4": snapshot({"boss_a": 2}, checked_at=None),
            },
        )


if __name__ == "__main__":
    unittest.main()


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
