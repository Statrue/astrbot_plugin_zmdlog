"""角色排名: where the teams fielding a character stand, and who holds the firsts."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .. import messages
from ..history import window_label, window_start
from ..standings import (
    CharacterStandings,
    CharacterTally,
    ProfessionUsage,
    TeamTally,
    by_profession,
    character_standings,
    character_tallies,
    first_place_teams,
    profession_usage,
    unseen_characters,
)
from .index import IndexSnapshot

if TYPE_CHECKING:
    from ..datasource import ZmdLogsDataSource
    from ..render import LongImageRenderer


@dataclass(frozen=True, slots=True)
class StandingsRecipe:
    """角色排名 <角色名…>: the best record fielding the named characters, per board."""

    standings: CharacterStandings
    query: str
    web_base_url: str | None
    age_seconds: float | None
    missing_count: int
    elements: Mapping[str, str]

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_character_standings(
            self.standings,
            query=self.query,
            web_base_url=self.web_base_url,
            age_seconds=self.age_seconds,
            elements=self.elements,
        )


async def prepare_standings(
    data: "ZmdLogsDataSource",
    snapshot: IndexSnapshot,
    names: tuple[str, ...],
    *,
    query: str,
    web_base_url: str | None,
) -> StandingsRecipe | str:
    """``names`` are already resolved against ``snapshot.fielded``.

    The refusal is a team no record fields: each name is fielded somewhere,
    or resolving it would have refused, and an empty page reads as a broken
    render, not as an answer.
    """

    standings = character_standings(snapshot.rankings, *names)
    if standings.is_team and not standings.boards:
        return messages.NO_TEAM_FIELDING.format(names="」「".join(names))
    return StandingsRecipe(
        standings=standings,
        query=query,
        web_base_url=web_base_url,
        age_seconds=snapshot.age_seconds,
        missing_count=snapshot.missing_count,
        elements=await data.character_elements(names=snapshot.fielded),
    )


@dataclass(frozen=True, slots=True)
class ChampionsRecipe:
    """角色排名 without a name: every character's first places over all boards."""

    tallies: tuple[CharacterTally, ...]
    board_count: int
    query: str
    web_base_url: str | None
    age_seconds: float | None
    missing_count: int
    # The element the board was cut to, if any; ``elements`` is the catalog
    # map every row's ring is drawn from.
    element: str | None
    elements: Mapping[str, str]
    profession: str | None
    teams: tuple[TeamTally, ...]
    usage: tuple[ProfessionUsage, ...]
    window_label: str
    # Catalog characters (of the profession, when restricted) no record in
    # the window fields.
    unseen: tuple[str, ...]

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_character_champions(
            self.tallies,
            board_count=self.board_count,
            query=self.query,
            web_base_url=self.web_base_url,
            age_seconds=self.age_seconds,
            element=self.element,
            elements=self.elements,
            profession=self.profession,
            teams=self.teams,
            usage=self.usage,
            window_label=self.window_label,
            unseen=self.unseen,
        )


async def prepare_champions(
    data: "ZmdLogsDataSource",
    snapshot: IndexSnapshot,
    *,
    web_base_url: str | None,
    element: str | None = None,
    profession: str | None = None,
    time_range: str = "all",
    query: str = "角色排名",
) -> ChampionsRecipe:
    """The champions board, cut to an element or a profession, over a window.

    ``element`` keeps the characters of that element, which is how 物理队有
    什么冠军 is answered. ``profession`` keeps one class and lists every
    member, zeros included, which is how 谁是冠军最少的突击 is answered.
    """

    since = window_start(time_range, now=datetime.now(UTC))
    elements = await data.character_elements(names=snapshot.fielded)
    tallies = character_tallies(snapshot.rankings, since=since)
    if element is not None:
        tallies = tuple(t for t in tallies if elements.get(t.name) == element)
    if profession is not None:
        tallies = by_profession(tallies, profession)
    # Who the catalog knows and no record fields. Not worked out after an
    # element cut: every member of another element would read as unseen.
    unseen: tuple[str, ...] = ()
    if element is None:
        professions = await data.character_professions(names=snapshot.fielded)
        unseen = unseen_characters(tallies, professions, profession=profession)
    return ChampionsRecipe(
        tallies=tallies,
        board_count=snapshot.board_count,
        query=query,
        web_base_url=web_base_url,
        age_seconds=snapshot.age_seconds,
        missing_count=snapshot.missing_count,
        element=element,
        elements=elements,
        profession=profession,
        teams=first_place_teams(snapshot.rankings, since=since),
        usage=profession_usage(snapshot.rankings, since=since),
        window_label=window_label(time_range),
        unseen=unseen,
    )
