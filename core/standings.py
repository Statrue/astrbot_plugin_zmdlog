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

    def boards_within(self, rank: int) -> tuple[BoardStanding, ...]:
        """Boards where the best record fielding the character ranks ≤ ``rank``."""

        return tuple(board for board in self.boards if board.best.rank <= rank)

    @property
    def first_places(self) -> int:
        return len(self.boards_within(1))

    @property
    def first_places_as_main(self) -> int:
        return sum(1 for board in self.boards_within(1) if board.best_as_main)


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


@dataclass(frozen=True, slots=True)
class CharacterTally:
    """One character's standings counted over every board.

    A first place — a 冠军 — is a board whose #1 record fields the
    character; the record's four members each get one (the user's
    definition, 2026-09-06). ``first_places_as_main`` counts the ones where
    the character was that record's main C, as a side note.
    """

    name: str
    profession: str
    avatar_url: str | None
    first_places: int
    first_places_as_main: int
    podiums: int
    top_tens: int
    boards: int
    appearances: int


def character_tallies(rankings: Iterable[BossRanking]) -> tuple[CharacterTally, ...]:
    """Every fielded character's counts, most first places first.

    "谁的冠军最多" is the standings turned the other way round: the same #1
    records, counted per character instead of listed per board.
    """

    first: dict[str, int] = {}
    first_main: dict[str, int] = {}
    podium: dict[str, int] = {}
    top_ten: dict[str, int] = {}
    boards: dict[str, int] = {}
    appearances: dict[str, int] = {}
    profession: dict[str, str] = {}
    avatar: dict[str, str | None] = {}
    for ranking in rankings:
        best: dict[str, int] = {}
        for row in ranking.rows:
            for entry in row.roster_entries:
                name = entry.character_name
                appearances[name] = appearances.get(name, 0) + 1
                profession.setdefault(name, entry.profession)
                if avatar.get(name) is None:
                    avatar[name] = entry.avatar_url
                if row.rank < best.get(name, row.rank + 1):
                    best[name] = row.rank
        for name, rank in best.items():
            boards[name] = boards.get(name, 0) + 1
            if rank <= 10:
                top_ten[name] = top_ten.get(name, 0) + 1
            if rank <= 3:
                podium[name] = podium.get(name, 0) + 1
            if rank == 1:
                first[name] = first.get(name, 0) + 1
        if ranking.rows:
            leader = ranking.rows[0].character_name
            first_main[leader] = first_main.get(leader, 0) + 1
    tallies = [
        CharacterTally(
            name=name,
            profession=profession.get(name, ""),
            avatar_url=avatar.get(name),
            first_places=first.get(name, 0),
            first_places_as_main=first_main.get(name, 0),
            podiums=podium.get(name, 0),
            top_tens=top_ten.get(name, 0),
            boards=boards.get(name, 0),
            appearances=appearances.get(name, 0),
        )
        for name in appearances
    ]
    tallies.sort(
        key=lambda tally: (
            -tally.first_places,
            -tally.first_places_as_main,
            -tally.podiums,
            -tally.top_tens,
            -tally.boards,
            tally.name,
        )
    )
    return tuple(tallies)
