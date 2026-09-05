"""Where the teams fielding one character stand on every board. Pure.

The question is "带 X 的阵容在各榜排第几，打了多少时间", and the answer is
a team's standing, not the character's: the best-ranked public record on
each board whose four-man roster includes X, with the rank it holds among
every record on that board. Everything here is counting over rankings the
index already holds; nothing is fetched.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from .models import BossRanking, BossRankingRow


@dataclass(frozen=True, slots=True)
class BoardStanding:
    """One board: the best record fielding the character, and how many did."""

    boss_slug: str
    boss_name: str
    dungeon_name: str
    total_rows: int
    appearances: int
    main_appearances: int
    best: BossRankingRow | None
    # The best record has the character as its main C, not just in the team.
    best_as_main: bool


@dataclass(frozen=True, slots=True)
class CharacterStandings:
    """A character's standing on every board, best rank first."""

    character: str
    boards: tuple[BoardStanding, ...]
    absent: tuple[BoardStanding, ...]

    @property
    def appearances(self) -> int:
        return sum(board.appearances for board in self.boards)


def character_standings(
    rankings: Iterable[BossRanking],
    character: str,
) -> CharacterStandings:
    """Count one character's teams over ``rankings``, best rank first.

    A board where the character never appears goes to ``absent`` in board
    order, so the page can fold them into one line instead of forty rows
    saying nothing.
    """

    present: list[BoardStanding] = []
    absent: list[BoardStanding] = []
    for ranking in rankings:
        fielding = [
            row
            for row in ranking.rows
            if any(entry.character_name == character for entry in row.roster_entries)
        ]
        best = min(fielding, key=lambda row: row.rank) if fielding else None
        standing = BoardStanding(
            boss_slug=ranking.boss_slug,
            boss_name=ranking.boss_name,
            dungeon_name=ranking.dungeon_name,
            total_rows=len(ranking.rows),
            appearances=len(fielding),
            main_appearances=sum(
                1 for row in fielding if row.character_name == character
            ),
            best=best,
            best_as_main=best is not None and best.character_name == character,
        )
        (present if best is not None else absent).append(standing)
    present.sort(key=lambda item: (item.best.rank, item.boss_name))
    return CharacterStandings(
        character=character,
        boards=tuple(present),
        absent=tuple(absent),
    )


def roster_character_names(rankings: Iterable[BossRanking]) -> tuple[str, ...]:
    """Every character fielded in any record, for resolving what was typed.

    The six-star catalog is not enough here: rosters carry every rarity,
    and a four-star support is exactly the kind of name people ask about.
    """

    names: dict[str, None] = {}
    for ranking in rankings:
        for row in ranking.rows:
            for entry in row.roster_entries:
                names.setdefault(entry.character_name, None)
    return tuple(names)
