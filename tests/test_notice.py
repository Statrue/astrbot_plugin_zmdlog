"""The 顶屁股通告 page's view model: a batch of notice entries in, a page out.

The seam is :func:`core.presentation.build_notice_page` — the entries the
board diff found for one chat in one interval, the interval, and N; what
the picture shows out: one card a board, each new record with whom it
pushed down, the cards dealt into two columns by their estimated height.
"""

import unittest

from core.board_changes import NoticeEntry, PushedAccount
from core.models import BossRankingRow
from core.presentation import PresentationError, build_notice_page

WEB = "https://zmdlogs.com"
CONTRACT_SLUG = "indie_group_ccdg"


def row(
    rank: int,
    name: str,
    *,
    dps: float = 61_204.77,
    duration_ms: int = 33_391,
    contract_score: int | None = None,
    avatar: str | None = "/images/character/charremoteicon/icon_chr_0001.png",
) -> BossRankingRow:
    return BossRankingRow(
        rank=rank,
        score_percent=100,
        battle_id=f"btl_upload_{rank:04d}{name.encode().hex()[:8]}",
        battle_end_at="2026-10-05T10:20:00+08:00",
        character_name="莱万汀",
        character_profession="术士",
        account_id=f"usr_{name.encode().hex()[:12]}",
        account_display_name=name,
        dps=dps,
        duration_ms=duration_ms,
        roster_summary=(),
        roster_entries=(),
        character_avatar_url=avatar,
        contract_tag_score=contract_score,
    )


def entry(
    rank: int,
    name: str = "BlazeR",
    *,
    slug: str = "dung01_group_bossrush02",
    boss: str = "白刃穿水·残酷",
    dungeon: str = "战争回响",
    seen_at: str = "2026-10-05T02:38:00+00:00",
    pushed: tuple[tuple[str, int, int], ...] = (),
    **record_fields,
) -> NoticeEntry:
    return NoticeEntry(
        boss_slug=slug,
        boss_name=boss,
        dungeon_name=dungeon,
        seen_at=seen_at,
        rank=rank,
        record=row(rank, name, **record_fields),
        pushed=tuple(
            PushedAccount(record=row(after, who), before=before, after=after)
            for who, before, after in pushed
        ),
    )


def build(*entries: NoticeEntry, top_n: int = 10):
    return build_notice_page(
        entries,
        window_start="2026-10-05T02:30:00+00:00",
        window_end="2026-10-05T02:45:00+00:00",
        top_n=top_n,
        web_base_url=WEB,
    )


def cards(page):
    return [card for column in page.columns for card in column]


def only_record(page):
    (card,) = cards(page)
    (record,) = card.records
    return record


class NoticePageTests(unittest.TestCase):
    def test_one_card_a_board_holding_each_of_its_new_records(self) -> None:
        page = build(
            entry(1, "BlazeR", slug="a", boss="白刃穿水·残酷"),
            entry(3, "小小猪", slug="b", boss="危境再现·罗丹", dungeon="危境再现"),
            entry(5, "Ryu", slug="a", boss="白刃穿水·残酷",
                  seen_at="2026-10-05T02:44:00+00:00"),
        )

        self.assertEqual(page.board_count, 2)
        by_board = {card.board_name: card for card in cards(page)}
        self.assertEqual(set(by_board), {"白刃穿水·残酷", "危境再现·罗丹"})
        self.assertEqual(
            [record.uploader for record in by_board["白刃穿水·残酷"].records],
            ["BlazeR", "Ryu"],
        )
        # Its two records were seen at two times, and the card says both.
        self.assertEqual(by_board["白刃穿水·残酷"].seen_at, "10:38 – 10:44")
        self.assertEqual(by_board["危境再现·罗丹"].seen_at, "10:38")

    def test_the_header_names_the_interval_and_n_in_server_time(self) -> None:
        page = build(entry(2), top_n=8)

        self.assertEqual(page.window, "10:30 – 10:45")
        self.assertEqual(page.top_n, 8)
        self.assertEqual(page.header.title, "顶屁股通告")

    def test_a_new_first_place_is_a_new_champion(self) -> None:
        page = build(entry(1, slug="a"), entry(4, slug="b"))

        champion, other = (card.records[0] for card in cards(page))
        self.assertTrue(champion.champion)
        self.assertFalse(other.champion)
        self.assertEqual((champion.rank, other.rank), (1, 4))

    def test_a_record_shows_its_time_and_dps_and_its_main_c_face(self) -> None:
        record = only_record(build(entry(3, dps=179_934.87, duration_ms=12_743)))

        self.assertEqual(record.time, "0:12.743")
        self.assertEqual(record.dps, "179,935")
        self.assertIsNone(record.contract_score)
        self.assertEqual(
            record.face.avatar_url,
            f"{WEB}/images/character/charremoteicon/icon_chr_0001.png",
        )
        self.assertEqual(record.face.initial, "莱")

    def test_a_contract_board_shows_the_score_and_its_own_name(self) -> None:
        card = build(
            entry(
                6,
                "克劳德",
                slug=CONTRACT_SLUG,
                boss="破潮之像",
                dungeon="危机合约",
                contract_score=47,
                duration_ms=382_451,
            )
        ).columns[0][0]

        self.assertEqual(card.board_name, "危机合约")
        self.assertEqual(card.dungeon_label, "活动竞速")
        record = card.records[0]
        self.assertEqual(record.contract_score, "47")
        self.assertEqual(record.time, "6:22.451")
        self.assertIsNone(record.dps)

    def test_the_dungeon_is_left_out_when_the_board_name_says_it(self) -> None:
        page = build(
            entry(3, slug="a", boss="危境再现·罗丹", dungeon="危境再现"),
            entry(3, slug="b", boss="白刃穿水·残酷", dungeon="战争回响"),
        )

        labels = {card.board_name: card.dungeon_label for card in cards(page)}
        self.assertEqual(labels, {"危境再现·罗丹": "", "白刃穿水·残酷": "战争回响"})

    def test_whoever_was_pushed_down_and_whoever_fell_out_of_the_top_n(self) -> None:
        record = only_record(
            build(entry(9, pushed=(("duck", 9, 10), ("Tasuimaki", 10, 11))))
        )

        self.assertEqual(
            [(p.name, p.before, p.after, p.fell_out) for p in record.pushed],
            [("duck", 9, 10, False), ("Tasuimaki", 10, 11, True)],
        )
        self.assertEqual(record.pushed[0].face.initial, "莱")

    def test_an_unsafe_face_url_draws_no_picture(self) -> None:
        record = only_record(build(entry(2, avatar="javascript:alert(1)")))

        self.assertIsNone(record.face.avatar_url)

    def test_an_empty_batch_has_no_page(self) -> None:
        with self.assertRaises(PresentationError):
            build()


class NoticeColumnTests(unittest.TestCase):
    """Each card goes to the shorter column, by its estimated height."""

    @staticmethod
    def pushed(count: int, name: str = "duck") -> tuple[tuple[str, int, int], ...]:
        return tuple((f"{name}{i}", i + 1, i + 2) for i in range(count))

    def test_cards_keep_their_order_and_go_to_the_shorter_column(self) -> None:
        page = build(
            entry(1, slug="tall", pushed=self.pushed(9)),
            entry(9, slug="s1"),
            entry(9, slug="s2"),
            entry(9, slug="s3"),
        )

        self.assertEqual(len(page.columns), 2)
        # The tall card fills the left column; the short ones stack on the
        # right, which stays the shorter one for all three.
        self.assertEqual(
            [[card.boss_slug for card in column] for column in page.columns],
            [["tall"], ["s1", "s2", "s3"]],
        )

    def test_a_tie_goes_left(self) -> None:
        page = build(entry(9, slug="a"), entry(9, slug="b"), entry(9, slug="c"))

        self.assertEqual(
            [[card.boss_slug for card in column] for column in page.columns],
            [["a", "c"], ["b"]],
        )

    def test_one_card_leaves_the_right_column_empty(self) -> None:
        page = build(entry(1))

        self.assertEqual([len(column) for column in page.columns], [1, 0])

    def test_names_that_wrap_make_a_card_taller(self) -> None:
        long_name = "名字很长很长很长很长很长很长的一位玩家"
        short = build(
            entry(5, slug="a", pushed=self.pushed(2)),
            entry(5, slug="b", pushed=self.pushed(2)),
            entry(5, slug="c"),
        )
        wrapped = build(
            entry(5, slug="a", pushed=self.pushed(2, long_name)),
            entry(5, slug="b", pushed=self.pushed(2)),
            entry(5, slug="c"),
        )

        self.assertEqual([card.boss_slug for card in short.columns[0]], ["a", "c"])
        self.assertEqual([card.boss_slug for card in wrapped.columns[1]], ["b", "c"])

    def test_more_new_records_on_a_board_make_its_card_taller(self) -> None:
        page = build(
            entry(5, slug="a"),
            entry(6, slug="a"),
            entry(5, slug="b"),
            entry(5, slug="c"),
        )

        self.assertEqual(
            [[card.boss_slug for card in column] for column in page.columns],
            [["a"], ["b", "c"]],
        )


if __name__ == "__main__":
    unittest.main()
