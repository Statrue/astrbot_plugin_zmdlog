"""Where the teams fielding one character (or several) stand on every board.

角色排名 <角色> is one wide table, never paged: a row a board, its best
record of such a team, that record's own main C on the yellow ring — the
names asked about are marked nowhere else — and the boards without one as
chips at the end.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from ..metrics import metric_label
from ..standings import CharacterStandings
from .boards import RosterEntryView, _build_roster
from .common import (
    PageHeader,
    format_duration,
    format_number,
)


@dataclass(frozen=True, slots=True)
class StandingRowView:
    rank: int
    total_rows: int
    boss_name: str
    dungeon_name: str
    duration: str
    # The board's figure as a whole number.
    dps: str
    account_display_name: str
    # The record's main C, whose face leads the roster.
    character_name: str
    roster: tuple[RosterEntryView, ...]
    battle_id: str


@dataclass(frozen=True, slots=True)
class CharacterStandingsPage:
    header: PageHeader
    # ``A`` or ``A · B``: the label every sentence on the page uses.
    character_name: str
    character_names: tuple[str, ...]
    # 带 A / 同时带 A · B — the phrase the notes complete.
    team_label: str
    metric_label: str
    appearances: int
    main_appearances: int
    first_places: int
    board_count: int
    absent_count: int
    rows: tuple[StandingRowView, ...]
    absent: tuple[str, ...]


def build_character_standings_page(
    standings: CharacterStandings,
    *,
    query: str,
    web_base_url: str | None = None,
    elements: Mapping[str, str] | None = None,
    metric: str = "dps",
) -> CharacterStandingsPage:
    """One row per board the character appeared on, best rank first.

    The rank is the record's rank among every record on that board, which
    is what the board page shows, and the page says so. Several characters
    draw the same page for the teams fielding all of them.
    """

    label = standings.character
    names = standings.characters
    team_label = f"同时带 {label}" if standings.is_team else f"带 {label}"
    rows: list[StandingRowView] = []
    for board in standings.boards:
        row = board.best
        roster = _build_roster(
            row.roster_entries,
            row.roster_summary,
            web_base_url=web_base_url,
            elements=elements,
        )
        rows.append(
            StandingRowView(
                rank=row.rank,
                total_rows=board.total_rows,
                boss_name=board.boss_name,
                dungeon_name=board.dungeon_name,
                duration=format_duration(row.duration_ms),
                dps=format_number(round(row.dps)),
                account_display_name=row.account_display_name,
                character_name=row.character_name,
                # A stable sort: the main C first, the rest in record order.
                roster=tuple(
                    sorted(
                        roster,
                        key=lambda face: face.character_name != row.character_name,
                    )
                ),
                battle_id=row.battle_id,
            )
        )
    return CharacterStandingsPage(
        header=PageHeader(
            title=label,
            subtitle=(
                "同时带这些角色的队伍在各榜单的最好记录"
                if standings.is_team
                else "带该角色的队伍在各榜单的最好记录"
            ),
            query=query,
            matched_name=f"{label} · 各榜单最好名次",
            target_type="角色排名",
            # The masthead names the metric; the foot what the rows are.
            footer_note="公开榜单 · 队伍成绩",
            metric=metric,
        ),
        character_name=label,
        character_names=names,
        team_label=team_label,
        metric_label=metric_label(metric),
        appearances=standings.appearances,
        main_appearances=sum(board.main_appearances for board in standings.boards),
        first_places=standings.first_places,
        board_count=len(standings.boards),
        absent_count=len(standings.absent),
        rows=tuple(rows),
        absent=tuple(board.boss_name for board in standings.absent),
    )
