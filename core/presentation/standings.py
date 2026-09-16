"""Where the teams fielding one character (or several) stand on every board."""

from collections.abc import Mapping
from dataclasses import dataclass

from ..standings import CharacterStandings
from .boards import RosterEntryView, _build_roster
from .common import (
    PageHeader,
    _as_of_label,
    _initial,
    format_duration,
    format_number,
    metric_footer,
)


@dataclass(frozen=True, slots=True)
class StandingRowView:
    rank: int
    total_rows: int
    boss_name: str
    dungeon_name: str
    duration: str
    dps: str
    account_display_name: str
    # The record's main C, and whether that is a character asked about.
    character_name: str
    is_main: bool
    roster: tuple[RosterEntryView, ...]
    appearances: int
    battle_id: str


@dataclass(frozen=True, slots=True)
class CharacterStandingsPage:
    header: PageHeader
    # ``A`` or ``A · B``: the label every sentence on the page uses.
    character_name: str
    # The names themselves, for highlighting each one in a roster.
    character_names: tuple[str, ...]
    # 带 A / 同时带 A · B — the phrase the notes complete.
    team_label: str
    character_initial: str
    character_avatar_url: str | None
    appearances: int
    main_appearances: int
    first_places: int
    first_places_as_main: int
    board_count: int
    absent_count: int
    as_of_label: str
    rows: tuple[StandingRowView, ...]
    absent: tuple[str, ...]


def build_character_standings_page(
    standings: CharacterStandings,
    *,
    query: str,
    web_base_url: str | None = None,
    age_seconds: float | None = None,
    elements: Mapping[str, str] | None = None,
    metric: str = "dps",
) -> CharacterStandingsPage:
    """One row per board the character appeared on, best rank first.

    The rank is the record's rank among every record on that board, which
    is what the board page shows; the page says so, and says how old the
    index behind it is, because it is drawn from memory, not from upstream.
    Several characters draw the same page for the teams fielding all of them.
    """

    label = standings.character
    names = standings.characters
    team_label = f"同时带 {label}" if standings.is_team else f"带 {label}"
    rows: list[StandingRowView] = []
    avatar_url: str | None = None
    for board in standings.boards:
        row = board.best
        roster = _build_roster(
            row.roster_entries,
            row.roster_summary,
            web_base_url=web_base_url,
            elements=elements,
        )
        if avatar_url is None:
            avatar_url = next(
                (
                    member.avatar_url
                    for member in roster
                    if member.character_name == names[0] and member.avatar_url
                ),
                None,
            )
        rows.append(
            StandingRowView(
                rank=row.rank,
                total_rows=board.total_rows,
                boss_name=board.boss_name,
                dungeon_name=board.dungeon_name,
                duration=format_duration(row.duration_ms),
                dps=format_number(row.dps),
                account_display_name=row.account_display_name,
                character_name=row.character_name,
                is_main=board.best_as_main,
                roster=roster,
                appearances=board.appearances,
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
            footer_note=(
                metric_footer(metric) + " · 队伍成绩 · 名次为该记录在全榜的名次"
            ),
            metric=metric,
        ),
        character_name=label,
        character_names=names,
        team_label=team_label,
        character_initial=_initial(names[0]),
        character_avatar_url=avatar_url,
        appearances=standings.appearances,
        main_appearances=sum(board.main_appearances for board in standings.boards),
        first_places=standings.first_places,
        first_places_as_main=standings.first_places_as_main,
        board_count=len(standings.boards),
        absent_count=len(standings.absent),
        as_of_label=_as_of_label(age_seconds),
        rows=tuple(rows),
        absent=tuple(board.boss_name for board in standings.absent),
    )
