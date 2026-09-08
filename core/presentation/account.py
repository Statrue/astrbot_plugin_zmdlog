"""One public account's best records."""

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from ..models import BossRankingRow, PublicUserRankings
from .boards import RosterEntryView, _build_roster
from .common import (
    PageHeader,
    _format_date,
    format_duration,
    format_number,
    public_url,
)


@dataclass(frozen=True, slots=True)
class AccountRankingView:
    boss_name: str
    dungeon_name: str | None
    rank: int
    percentile: str
    duration: str
    total_dps: str
    battle_date: str
    battle_id: str
    # The record's main C when the ranking index holds its row, else "".
    character_name: str
    # False for a board the current board list no longer carries (retired
    # content): its roster can only be names, and the page says why.
    board_listed: bool
    roster: tuple[RosterEntryView, ...]
    contract_score: str | None


@dataclass(frozen=True, slots=True)
class AccountPage:
    header: PageHeader
    account_id: str
    account_url: str
    record_count: int
    best_rank: str
    best_percentile: str
    average_percentile: str
    rows: tuple[AccountRankingView, ...]


def build_account_page(
    account: PublicUserRankings,
    *,
    query: str,
    web_base_url: str,
    rows_by_battle: Mapping[str, BossRankingRow] | None = None,
    listed_boards: Collection[str] | None = None,
    elements: Mapping[str, str] | None = None,
    icons: Mapping[str, str] | None = None,
) -> AccountPage:
    """Build one exact public account's best-record overview.

    The user endpoint names each record's roster but carries no avatars,
    professions or main C; those are read off the ranking index row of the
    same battle when the index holds it (``rows_by_battle``), so the rows
    draw the same roster component as every board page. A board the index
    does not hold falls back to initials and names; when ``listed_boards``
    (the slugs of a complete index) is given, a board outside it is marked
    as retired rather than merely unread.
    """

    held = rows_by_battle or {}
    rows = sorted(account.rankings, key=lambda row: (row.rank, row.boss_name))
    record_count = len(rows)
    best_rank = f"#{min(row.rank for row in rows)}" if rows else "—"
    best_percentile = (
        f"{max(row.score_percent for row in rows)}%" if rows else "—"
    )
    average_percentile = (
        f"{round(sum(row.score_percent for row in rows) / record_count)}%"
        if rows
        else "—"
    )
    views: list[AccountRankingView] = []
    for row in rows:
        indexed = held.get(row.battle_id)
        views.append(
            AccountRankingView(
                boss_name=row.boss_name,
                dungeon_name=(
                    row.dungeon_name
                    if row.dungeon_name != row.boss_name
                    else None
                ),
                rank=row.rank,
                percentile=f"{format_number(row.score_percent)}%",
                duration=format_duration(row.duration_ms),
                total_dps=format_number(row.total_dps),
                battle_date=_format_date(row.battle_end_at),
                battle_id=row.battle_id,
                character_name=(
                    indexed.character_name if indexed is not None else ""
                ),
                board_listed=(
                    listed_boards is None or row.boss_slug in listed_boards
                ),
                roster=_build_roster(
                    indexed.roster_entries if indexed is not None else (),
                    row.roster_summary,
                    web_base_url=web_base_url,
                    elements=elements,
                    icons=icons,
                ),
                contract_score=(
                    format_number(row.contract_tag_score)
                    if row.contract_tag_score is not None
                    else None
                ),
            )
        )
    return AccountPage(
        header=PageHeader(
            title=account.account_display_name,
            subtitle="公开账号各首领最佳记录",
            query=query,
            matched_name=account.account_id,
            target_type="公开账号",
            footer_note="公开账号 · 当前最佳记录",
        ),
        account_id=account.account_id,
        account_url=public_url(
            web_base_url,
            "records",
            account.account_id,
        ),
        record_count=record_count,
        best_rank=best_rank,
        best_percentile=best_percentile,
        average_percentile=average_percentile,
        rows=tuple(views),
    )
