"""The 顶屁股通告: one chat's batch of board notices as one picture.

A batch is every :class:`~core.board_changes.NoticeEntry` the board diff
found for one chat in one interval — a new record that entered a watched
board's top N, and whom it pushed down. The page draws one card a board,
so two new records on one board in one interval share a card, each with
its own record box and its own list of whom it pushed (both counted
against the board as it stood just before that record). The pill counts
the boards.

The cards stand two abreast, and the template cannot balance the columns
itself: a CSS grid would align them row by row, and CSS columns cut a card
in two. So the builder deals them, in batch order, each to the column whose
estimated height is the shorter, ties to the left. The estimate is the
card's parts at the 960 frame's sizes in ``notice.css``, with a name taken
to wrap at its column's width — names wrap, never truncate, so a long one
is a taller row. It only has to be close: a misjudged card costs a column
a little longer than the other, never a broken page.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..board_changes import NoticeEntry
from ..models import BossRankingRow
from .common import (
    _CRISIS_CONTRACT_BOSS_SLUG,
    PageHeader,
    PresentationError,
    _displayed,
    _initial,
    _safe_asset_url,
    format_duration,
    format_number,
)

NOTICE_COLUMNS = 2


@dataclass(frozen=True, slots=True)
class NoticeFaceView:
    """A main C's round face, its initial under it when the picture fails."""

    character_name: str
    initial: str
    avatar_url: str | None


@dataclass(frozen=True, slots=True)
class PushedRowView:
    """One account a new record pushed down: ``▼ before → after``."""

    name: str
    face: NoticeFaceView
    before: int
    after: int
    # Pushed past N: the grey 跌出前 N chip.
    fell_out: bool


@dataclass(frozen=True, slots=True)
class NoticeRecordView:
    """One new record: its place, who uploaded it, and how fast."""

    rank: int
    # A new #1: the magenta 新冠军! tag instead of the mint NEW!.
    champion: bool
    uploader: str
    face: NoticeFaceView
    time: str
    # The DPS on a plain board; on a 危机合约 board the score instead.
    dps: str | None
    contract_score: str | None
    pushed: tuple[PushedRowView, ...]


@dataclass(frozen=True, slots=True)
class NoticeCardView:
    """One board's card: its name, when it was seen, its new records."""

    boss_slug: str
    board_name: str
    # The dungeon, unless the board's name already says it.
    dungeon_label: str
    # ``10:38``, or ``10:38 – 10:44`` for records seen at two times.
    seen_at: str
    records: tuple[NoticeRecordView, ...]


@dataclass(frozen=True, slots=True)
class NoticePage:
    header: PageHeader
    board_count: int
    # ``10:30 – 10:45``: the interval the batch covers, in server time.
    window: str
    top_n: int
    # The cards dealt into the two columns, left first.
    columns: tuple[tuple[NoticeCardView, ...], ...]


def build_notice_page(
    entries: Sequence[NoticeEntry],
    *,
    window_start: str,
    window_end: str,
    top_n: int,
    web_base_url: str | None = None,
) -> NoticePage:
    """The picture of one chat's batch: ``entries`` in the order found.

    ``window_start`` / ``window_end`` are ISO stamps of the interval the
    batch covers, ``top_n`` the N whose changes it reports
    (``rank_watch_rank_threshold``).
    """

    if not entries:
        raise PresentationError("a notice needs at least one entry")
    boards: dict[str, list[NoticeEntry]] = {}
    for entry in entries:
        boards.setdefault(entry.boss_slug, []).append(entry)
    cards = [
        _card(board, top_n=top_n, web_base_url=web_base_url)
        for board in boards.values()
    ]
    window = f"{_clock(window_start)} – {_clock(window_end)}"
    return NoticePage(
        header=PageHeader(
            title="顶屁股通告",
            subtitle=window,
            query="",
            matched_name="",
            target_type="榜单通报",
        ),
        board_count=len(cards),
        window=window,
        top_n=top_n,
        columns=_deal(cards),
    )


def _card(
    entries: list[NoticeEntry], *, top_n: int, web_base_url: str | None
) -> NoticeCardView:
    first = entries[0]
    contract = first.boss_slug == _CRISIS_CONTRACT_BOSS_SLUG
    name = "危机合约" if contract else first.boss_name
    dungeon = "活动竞速" if contract else first.dungeon_name
    times = list(dict.fromkeys(_clock(entry.seen_at) for entry in entries))
    return NoticeCardView(
        boss_slug=first.boss_slug,
        board_name=name,
        dungeon_label="" if _normalised(dungeon) in _normalised(name) else dungeon,
        seen_at=times[0] if len(times) == 1 else f"{times[0]} – {times[-1]}",
        records=tuple(
            _record(entry, top_n=top_n, web_base_url=web_base_url)
            for entry in entries
        ),
    )


def _record(
    entry: NoticeEntry, *, top_n: int, web_base_url: str | None
) -> NoticeRecordView:
    record = entry.record
    score = record.contract_tag_score
    return NoticeRecordView(
        rank=entry.rank,
        champion=entry.champion,
        uploader=record.account_display_name,
        face=_face(record, web_base_url=web_base_url),
        time=format_duration(record.duration_ms),
        dps=format_number(round(record.dps)) if score is None else None,
        contract_score=format_number(score) if score is not None else None,
        pushed=tuple(
            PushedRowView(
                name=pushed.record.account_display_name,
                face=_face(pushed.record, web_base_url=web_base_url),
                before=pushed.before,
                after=pushed.after,
                fell_out=pushed.after > top_n,
            )
            for pushed in entry.pushed
        ),
    )


def _face(row: BossRankingRow, *, web_base_url: str | None) -> NoticeFaceView:
    return NoticeFaceView(
        character_name=row.character_name,
        initial=_initial(row.character_name),
        avatar_url=_safe_asset_url(row.character_avatar_url, base_url=web_base_url),
    )


def _clock(stamp: str) -> str:
    """``HH:MM`` in server time (UTC+8), as every page prints a stamp."""

    try:
        return _displayed(stamp).strftime("%H:%M")
    except ValueError:
        return stamp


def _normalised(text: str) -> str:
    return "".join(text.split()).replace("·", "").replace("・", "")


# The estimate, in CSS px at the 960 frame (notice.css). A card's chrome is
# its border, padding and the gap under it; a head line is the outlined
# name; a record box is its padding and its tallest part (the face, or the
# uploader's name in as many lines as it wraps to); a pushed row is its
# padding and its face, or its name's lines. The name columns are what is
# left of a card's 389 px of content beside the other parts of the row.
_CARD_CHROME = 76
_HEAD_LINE = 38
_HEAD_GAP = 10
_RECORD_PADDING = 38
_RECORD_FACE = 60
_RECORD_NAME_LINE = 26
_RECORD_NAME_WIDTH = 105
_RECORD_NAME_SIZE = 20
_PUSHED_HEAD = 40
_PUSHED_PADDING = 16
_PUSHED_FACE = 32
_PUSHED_NAME_LINE = 22
_PUSHED_NAME_WIDTH = 230
_PUSHED_NAME_WIDTH_WITH_CHIP = 150
_PUSHED_NAME_SIZE = 17
_HEAD_NAME_WIDTH = 280
_HEAD_NAME_SIZE = 32


def _deal(
    cards: list[NoticeCardView],
) -> tuple[tuple[NoticeCardView, ...], ...]:
    """The cards in order, each to the shorter column, ties to the left."""

    columns: list[list[NoticeCardView]] = [[] for _ in range(NOTICE_COLUMNS)]
    heights = [0] * NOTICE_COLUMNS
    for card in cards:
        shortest = heights.index(min(heights))
        columns[shortest].append(card)
        heights[shortest] += _estimated_height(card)
    return tuple(tuple(column) for column in columns)


def _estimated_height(card: NoticeCardView) -> int:
    height = _CARD_CHROME + _HEAD_GAP + _HEAD_LINE * _lines(
        card.board_name, _HEAD_NAME_SIZE, _HEAD_NAME_WIDTH
    )
    for record in card.records:
        name_lines = _lines(record.uploader, _RECORD_NAME_SIZE, _RECORD_NAME_WIDTH)
        height += _RECORD_PADDING + max(_RECORD_FACE, _RECORD_NAME_LINE * name_lines)
        if record.pushed:
            height += _PUSHED_HEAD
        for pushed in record.pushed:
            width = (
                _PUSHED_NAME_WIDTH_WITH_CHIP if pushed.fell_out else _PUSHED_NAME_WIDTH
            )
            lines = _lines(pushed.name, _PUSHED_NAME_SIZE, width)
            height += _PUSHED_PADDING + max(_PUSHED_FACE, _PUSHED_NAME_LINE * lines)
    return height


def _lines(text: str, size: int, width: int) -> int:
    """How many lines ``text`` takes at ``size`` px in ``width`` px.

    A CJK or full-width character is an em wide, anything else about half
    of one — close enough to tell a name that wraps from one that does not.
    """

    ems = sum(1.0 if ord(char) >= 0x2E80 else 0.56 for char in text)
    return max(1, math.ceil(ems * size / width))
