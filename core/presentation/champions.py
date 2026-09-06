"""Who holds the most first places: every character counted over all boards."""

from collections.abc import Mapping
from dataclasses import dataclass

from ..elements import element_key
from ..standings import CharacterTally, ProfessionUsage, TeamTally
from .boards import RosterEntryView, _build_roster
from .common import PageHeader, _initial, _safe_asset_url
from .standings import _as_of_label


@dataclass(frozen=True, slots=True)
class TeamComboView:
    members: tuple[RosterEntryView, ...]
    count: int
    boards_label: str


@dataclass(frozen=True, slots=True)
class UsageChipView:
    name: str
    count: int
    share_label: str
    initial: str
    avatar_url: str | None
    element_key: str | None


@dataclass(frozen=True, slots=True)
class ChampionUsageView:
    profession: str
    chips: tuple[UsageChipView, ...]


@dataclass(frozen=True, slots=True)
class TallyRowView:
    position: int
    name: str
    profession: str
    initial: str
    avatar_url: str | None
    first_places: int
    first_places_as_main: int
    podiums: int
    top_tens: int
    boards: int
    # First places as a share of the leader's, for the in-row bar.
    bar_width: float
    element_key: str | None = None


@dataclass(frozen=True, slots=True)
class CharacterChampionsPage:
    header: PageHeader
    board_count: int
    as_of_label: str
    # The headline answer (most first places) and its side note (most first
    # places as main C). Both are None with no records.
    top_team: TallyRowView | None
    top_main: TallyRowView | None
    rows: tuple[TallyRowView, ...]
    # Characters fielded somewhere but never in a top-three record.
    others: tuple[str, ...]
    # The element the board was filtered to, if any.
    element: str | None = None
    # "近 7 天" and the like when the records were narrowed to a window.
    window_label: str = ""
    teams: tuple[TeamComboView, ...] = ()
    usage: tuple[ChampionUsageView, ...] = ()


def build_character_champions_page(
    tallies: tuple[CharacterTally, ...],
    *,
    board_count: int,
    query: str,
    web_base_url: str | None = None,
    age_seconds: float | None = None,
    element: str | None = None,
    elements: Mapping[str, str] | None = None,
    teams: tuple[TeamTally, ...] = (),
    usage: tuple[ProfessionUsage, ...] = (),
    window_label: str = "",
) -> CharacterChampionsPage:
    """One row per character with a podium, most first places first."""

    ranked = [tally for tally in tallies if tally.podiums]
    peak = board_count
    known = elements or {}
    rows = tuple(
        TallyRowView(
            position=index,
            name=tally.name,
            profession=tally.profession,
            initial=_initial(tally.name),
            avatar_url=_safe_asset_url(tally.avatar_url, base_url=web_base_url),
            first_places=tally.first_places,
            first_places_as_main=tally.first_places_as_main,
            podiums=tally.podiums,
            top_tens=tally.top_tens,
            boards=tally.boards,
            bar_width=(
                round(tally.first_places / peak * 100, 2) if peak else 0.0
            ),
            element_key=element_key(known.get(tally.name)),
        )
        for index, tally in enumerate(ranked, start=1)
    )
    top_main = max(rows, key=lambda row: row.first_places_as_main, default=None)
    top_team = max(rows, key=lambda row: row.first_places, default=None)
    team_views = tuple(
        TeamComboView(
            members=_build_roster(
                team.entries, team.names, web_base_url=web_base_url, elements=elements
            ),
            count=team.count,
            boards_label="、".join(team.boards[:6])
            + ("…" if len(team.boards) > 6 else ""),
        )
        for team in teams[:5]
    )
    usage_views = tuple(
        ChampionUsageView(
            profession=group.profession,
            chips=tuple(
                UsageChipView(
                    name=entry.name,
                    count=entry.count,
                    share_label=f"{entry.share:g}%",
                    initial=_initial(entry.name),
                    avatar_url=_safe_asset_url(entry.avatar_url, base_url=web_base_url),
                    element_key=element_key(known.get(entry.name)),
                )
                for entry in group.entries
            ),
        )
        for group in usage
    )
    scope = "全部榜单" if not window_label else f"{window_label}各榜最快记录"
    return CharacterChampionsPage(
        header=PageHeader(
            title=(
                "角色冠军榜"
                + (f" · {element}" if element else "")
                + (f" · {window_label}" if window_label else "")
            ),
            subtitle=(
                f"带该角色的队伍在{scope}拿下的第一名、前三与前十"
                if element is None
                else f"{element}属性角色的队伍在{scope}拿下的第一名、前三与前十"
            ),
            query=query,
            matched_name=f"全部 {board_count} 个榜单 · 角色冠军榜",
            target_type="角色排名",
            footer_note="公开榜单 · 队伍成绩 · 第一名队伍的四名角色各算一个冠军",
        ),
        board_count=board_count,
        as_of_label=_as_of_label(age_seconds),
        top_main=top_main,
        top_team=top_team,
        element=element,
        window_label=window_label,
        teams=team_views,
        usage=usage_views,
        rows=rows,
        others=tuple(tally.name for tally in tallies if not tally.podiums),
    )
