"""Who holds the most first places: every character counted over all boards.

The 角色冠军榜 (bare 角色排名) is one wide table, never paged: a row a
character with a podium (every member when cut to a 职业), then the
compositions holding the most first places, the usage by 职业 and the
characters left out of the table. A 冠军 is a board's #1 record, credited
to all four of its members.
"""

from dataclasses import dataclass

from ..metrics import metric_label
from ..standings import CharacterTally, ProfessionUsage, TeamTally
from .boards import RosterEntryView, _build_roster
from .common import (
    PageHeader,
    _bar_width,
    _initial,
    _safe_asset_url,
)


@dataclass(frozen=True, slots=True)
class TeamComboView:
    members: tuple[RosterEntryView, ...]
    count: int


@dataclass(frozen=True, slots=True)
class UsageChipView:
    name: str
    count: int
    share_label: str
    character_initial: str
    avatar_url: str | None


@dataclass(frozen=True, slots=True)
class ChampionUsageView:
    profession: str
    chips: tuple[UsageChipView, ...]


@dataclass(frozen=True, slots=True)
class TallyRowView:
    position: int
    name: str
    profession: str
    character_initial: str
    avatar_url: str | None
    first_places: int
    first_places_as_main: int
    podiums: int
    top_tens: int
    boards: int
    # First places as a share of the leader's, for the in-row bar.
    bar_width: float


@dataclass(frozen=True, slots=True)
class CharacterChampionsPage:
    header: PageHeader
    board_count: int
    # DPS or rDPS, the ranking the first places were counted on.
    metric_label: str
    rows: tuple[TallyRowView, ...]
    # Characters fielded somewhere but never in a top-three record.
    others: tuple[str, ...]
    # The element the board was filtered to, if any.
    # The profession the board was restricted to, if any: then every member
    # is a row, zeros included, and ``others`` is empty.
    profession: str | None = None
    # "近 7 天" and the like when the records were narrowed to a window.
    window_label: str = ""
    teams: tuple[TeamComboView, ...] = ()
    usage: tuple[ChampionUsageView, ...] = ()
    # Catalog characters (of the profession, when restricted) that no public
    # record in the window fields; the template says 近 7 天未上榜 rather
    # than 从未上榜 under a window, since they may well have before it.
    unseen: tuple[str, ...] = ()


def build_character_champions_page(
    tallies: tuple[CharacterTally, ...],
    *,
    board_count: int,
    query: str,
    web_base_url: str | None = None,
    element: str | None = None,
    profession: str | None = None,
    teams: tuple[TeamTally, ...] = (),
    usage: tuple[ProfessionUsage, ...] = (),
    window_label: str = "",
    unseen: tuple[str, ...] = (),
    metric: str = "dps",
) -> CharacterChampionsPage:
    """One row per character with a podium, most first places first.

    Restricted to a profession, every member is a row, zeros included: the
    class is a handful, and the zeros are the answer to 谁冠军最少.
    """

    ranked = (
        list(tallies) if profession else [tally for tally in tallies if tally.podiums]
    )
    peak = board_count
    rows = tuple(
        TallyRowView(
            position=index,
            name=tally.name,
            profession=tally.profession,
            character_initial=_initial(tally.name),
            avatar_url=_safe_asset_url(tally.avatar_url, base_url=web_base_url),
            first_places=tally.first_places,
            first_places_as_main=tally.first_places_as_main,
            podiums=tally.podiums,
            top_tens=tally.top_tens,
            boards=tally.boards,
            bar_width=_bar_width(tally.first_places, peak),
        )
        for index, tally in enumerate(ranked, start=1)
    )
    team_views = tuple(
        TeamComboView(
            members=_build_roster(
                team.entries, team.names, web_base_url=web_base_url
            ),
            count=team.count,
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
                    character_initial=_initial(entry.name),
                    avatar_url=_safe_asset_url(entry.avatar_url, base_url=web_base_url),
                )
                for entry in group.entries
            ),
        )
        for group in usage
    )
    scope = "全部榜单" if not window_label else f"{window_label}各榜最快记录"
    who = (
        "带该角色"
        if not (element or profession)
        else (f"{element}属性" if element else "") + (profession or "") + "角色"
    )
    return CharacterChampionsPage(
        header=PageHeader(
            title=(
                "角色冠军榜"
                + (f" · {element}" if element else "")
                + (f" · {profession}" if profession else "")
                + (f" · {window_label}" if window_label else "")
            ),
            subtitle=f"{who}的队伍在{scope}拿下的第一名、前三与前十",
            query=query,
            metric=metric,
            matched_name=f"全部 {board_count} 个榜单 · 角色冠军榜",
            target_type="角色排名",
            footer_note="公开榜单 · 队伍成绩",
        ),
        board_count=board_count,
        metric_label=metric_label(metric),
        profession=profession,
        window_label=window_label,
        teams=team_views,
        usage=usage_views,
        rows=rows,
        others=(
            ()
            if profession
            else tuple(tally.name for tally in tallies if not tally.podiums)
        ),
        unseen=unseen,
    )
