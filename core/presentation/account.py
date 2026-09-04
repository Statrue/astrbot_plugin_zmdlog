"""One public account's best records."""

from dataclasses import dataclass

from ..models import (
    PublicUserRankings,
)
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
    roster: tuple[str, ...]
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
    show_contract_score: bool
    rows: tuple[AccountRankingView, ...]


def build_account_page(
    account: PublicUserRankings,
    *,
    query: str,
    web_base_url: str,
) -> AccountPage:
    """Build one exact public account's best-record overview."""

    rows = account.rankings
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
        show_contract_score=any(
            row.contract_tag_score is not None for row in rows
        ),
        rows=tuple(
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
                roster=row.roster_summary,
                contract_score=(
                    format_number(row.contract_tag_score)
                    if row.contract_tag_score is not None
                    else None
                ),
            )
            for row in rows
        ),
    )
