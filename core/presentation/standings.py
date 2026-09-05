"""Where the teams fielding one character stand on every board."""

from dataclasses import dataclass

from ..standings import CharacterStandings
from .boards import RosterEntryView, _build_roster
from .common import (
    PageHeader,
    _initial,
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
    dps: str
    account_display_name: str
    # The record's main C, and whether that is the character asked about.
    character_name: str
    is_main: bool
    roster: tuple[RosterEntryView, ...]
    appearances: int
    battle_id: str


@dataclass(frozen=True, slots=True)
class CharacterStandingsPage:
    header: PageHeader
    character_name: str
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
) -> CharacterStandingsPage:
    """One row per board the character appeared on, best rank first.

    The rank is the record's rank among every record on that board, which
    is what the board page shows; the page says so, and says how old the
    index behind it is, because it is drawn from memory, not from upstream.
    """

    name = standings.character
    rows: list[StandingRowView] = []
    avatar_url: str | None = None
    for board in standings.boards:
        row = board.best
        roster = _build_roster(
            row.roster_entries, row.roster_summary, web_base_url=web_base_url
        )
        if avatar_url is None:
            avatar_url = next(
                (
                    member.avatar_url
                    for member in roster
                    if member.character_name == name and member.avatar_url
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
            title=name,
            subtitle="带该角色的队伍在各榜单的最好记录",
            query=query,
            matched_name=f"{name} · 各榜单最好名次",
            target_type="角色排名",
            footer_note="公开榜单 · 队伍成绩 · 名次为该记录在全榜的名次",
        ),
        character_name=name,
        character_initial=_initial(name),
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


def _as_of_label(age_seconds: float | None) -> str:
    if age_seconds is None:
        return ""
    minutes = int(age_seconds // 60)
    if minutes < 1:
        return "数据刚刚更新"
    if minutes < 60:
        return f"数据截至 {minutes} 分钟前"
    hours, minutes = divmod(minutes, 60)
    return f"数据截至 {hours} 小时 {minutes} 分钟前"
