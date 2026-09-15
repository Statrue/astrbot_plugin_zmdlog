"""群榜: one chat's bound accounts on one board, drawn as standings rows."""

from collections.abc import Mapping
from dataclasses import dataclass

from ..models import BossRanking, BossRankingRow
from .boards import RosterEntryView, _build_roster
from .common import (
    PageHeader,
    _as_of_label,
    _format_date,
    format_duration,
    format_number,
)


@dataclass(frozen=True, slots=True)
class GroupRowView:
    # Position among the chat's members, and the record's rank on the board.
    position: int
    rank: int
    total_rows: int
    percentile: str
    account_display_name: str
    character_name: str
    roster: tuple[RosterEntryView, ...]
    dps: str
    duration: str
    battle_date: str
    battle_id: str


@dataclass(frozen=True, slots=True)
class GroupBoardPage:
    header: PageHeader
    boss_name: str
    dungeon_name: str
    total_rows: int
    # Bound users who used a binding command in the chat, their accounts,
    # and how many of those accounts have a record on this board.
    member_count: int
    account_count: int
    listed_count: int
    shown_count: int
    # True when the chat has more bound accounts than the board reads.
    truncated: bool
    as_of_label: str
    rows: tuple[GroupRowView, ...]


def build_group_board_page(
    ranking: BossRanking,
    rows: tuple[BossRankingRow, ...],
    *,
    query: str,
    web_base_url: str | None = None,
    member_count: int,
    account_count: int,
    display_limit: int,
    truncated: bool = False,
    elements: Mapping[str, str] | None = None,
    age_seconds: float | None = None,
) -> GroupBoardPage:
    """The chat's members in board order; ``rows`` come from ``group_standings``.

    The rank shown is the record's rank among every record on the board —
    the number the board page shows — and the position is only where that
    puts the member among the chat. Public nicknames throughout: whose
    binding an account came from is never drawn.
    """

    shown = rows[: max(1, display_limit)]
    views = tuple(
        GroupRowView(
            position=index,
            rank=row.rank,
            total_rows=len(ranking.rows),
            percentile=f"{format_number(row.score_percent)}%",
            account_display_name=row.account_display_name,
            character_name=row.character_name,
            roster=_build_roster(
                row.roster_entries,
                row.roster_summary,
                web_base_url=web_base_url,
                elements=elements,
            ),
            dps=format_number(row.dps),
            duration=format_duration(row.duration_ms),
            battle_date=_format_date(row.battle_end_at),
            battle_id=row.battle_id,
        )
        for index, row in enumerate(shown, start=1)
    )
    return GroupBoardPage(
        header=PageHeader(
            title=ranking.boss_name,
            subtitle="本群绑定账号在该榜的最好记录",
            query=query,
            matched_name=f"{ranking.dungeon_name} · {ranking.boss_name}",
            target_type="群榜",
            footer_note="公开榜单 · 本群绑定账号 · 名次为该记录在全榜的名次",
        ),
        boss_name=ranking.boss_name,
        dungeon_name=ranking.dungeon_name,
        total_rows=len(ranking.rows),
        member_count=member_count,
        account_count=account_count,
        listed_count=len(rows),
        shown_count=len(views),
        truncated=truncated,
        as_of_label=_as_of_label(age_seconds),
        rows=views,
    )
