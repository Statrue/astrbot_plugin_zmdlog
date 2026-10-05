"""One public account's best records: 账号, one wide table drawn whole."""

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from ..models import BossRankingRow, PublicUserRankings
from .boards import RosterEntryView, _build_roster
from .common import (
    PageHeader,
    _format_date,
    format_duration,
    format_number,
)


@dataclass(frozen=True, slots=True)
class AccountRankingView:
    boss_name: str
    dungeon_name: str | None
    rank: int
    duration: str
    # The team's DPS as a whole number.
    total_dps: str
    battle_date: str
    battle_id: str
    # The record's main C when the ranking index holds its row, else "".
    character_name: str
    # False for a 未收录榜单, a board outside 全部榜单: the index never reads
    # it, so its main C is unknown, and the page says why.
    board_listed: bool
    # The four faces, the main C's first when it is known.
    roster: tuple[RosterEntryView, ...]
    contract_score: str | None


@dataclass(frozen=True, slots=True)
class AccountPage:
    header: PageHeader
    account_id: str
    record_count: int
    # The figures card: records at 第 1, in the 前 3 and the 前 10.
    first_places: int
    top_three: int
    top_ten: int
    average_percentile: str
    rows: tuple[AccountRankingView, ...]
    # Some row is a 未收录榜单, which the legend then explains; only a page
    # drawn from the endpoint has one, since the index reads 全部榜单 alone.
    has_unlisted_boards: bool


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

    The user endpoint carries each record's roster, 养成 included, but no
    main C; that is read off the ranking index row of the same battle when
    the index holds it (``rows_by_battle``), whose roster also stands in for
    a record that arrives without one. Either way the rows draw the same
    roster component as every board page; with neither, initials and names.
    When ``listed_boards`` (全部榜单's slugs) is given, a board outside it
    is marked 未收录榜单 rather than left with an unknown main C — never as
    retired, which nothing upstream can tell.
    """

    held = rows_by_battle or {}
    rows = sorted(account.rankings, key=lambda row: (row.rank, row.boss_name))
    record_count = len(rows)
    average_percentile = (
        f"{round(sum(row.score_percent for row in rows) / record_count)}%"
        if rows
        else "—"
    )
    views: list[AccountRankingView] = []
    for row in rows:
        indexed = held.get(row.battle_id)
        main_c = indexed.character_name if indexed is not None else ""
        roster = _build_roster(
            row.roster_entries
            or (indexed.roster_entries if indexed is not None else ()),
            row.roster_summary,
            web_base_url=web_base_url,
            elements=elements,
            icons=icons,
        )
        views.append(
            AccountRankingView(
                boss_name=row.boss_name,
                dungeon_name=(
                    row.dungeon_name
                    if row.dungeon_name != row.boss_name
                    else None
                ),
                rank=row.rank,
                duration=format_duration(row.duration_ms),
                total_dps=format_number(round(row.total_dps)),
                battle_date=_format_date(row.battle_end_at),
                battle_id=row.battle_id,
                character_name=main_c,
                board_listed=(
                    listed_boards is None or row.boss_slug in listed_boards
                ),
                # A stable sort: the main C first, the rest in record order.
                roster=tuple(
                    sorted(roster, key=lambda face: face.character_name != main_c)
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
        record_count=record_count,
        first_places=sum(1 for row in rows if row.rank == 1),
        top_three=sum(1 for row in rows if row.rank <= 3),
        top_ten=sum(1 for row in rows if row.rank <= 10),
        average_percentile=average_percentile,
        rows=tuple(views),
        has_unlisted_boards=any(not view.board_listed for view in views),
    )
