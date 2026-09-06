"""Where the teams fielding one character stand on every board. Pure.

The question is "带 X 的阵容在各榜排第几，打了多少时间", and the answer is
a team's standing, not the character's: the best-ranked public record on
each board whose four-man roster includes X, with the rank it holds among
every record on that board. Everything here is counting over rankings the
index already holds; nothing is fetched.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .models import BossRanking, BossRankingRosterEntry, BossRankingRow
from .timestamps import parse_timestamp

PROFESSION_ORDER = ("近卫", "重装", "辅助", "突击", "术士", "先锋")


def window_rows(
    ranking: BossRanking, since: datetime | None
) -> tuple[BossRankingRow, ...]:
    """The board's records fought at or after ``since``, still in rank order.

    ``None`` is the whole board. Inside a window the positions are counted
    again from one, so "the first place of the last seven days" is the
    fastest record of those seven days, whatever its rank overall.
    """

    if since is None:
        return ranking.rows
    kept = []
    for row in ranking.rows:
        when = parse_timestamp(row.battle_end_at)
        if when is not None and when >= since:
            kept.append(row)
    return tuple(kept)


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


def character_tallies(
    rankings: Iterable[BossRanking],
    *,
    since: datetime | None = None,
) -> tuple[CharacterTally, ...]:
    """Every fielded character's counts, most first places first.

    "谁的冠军最多" is the standings turned the other way round: the same #1
    records, counted per character instead of listed per board. ``since``
    narrows every board to the records of a window first.
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
        rows = window_rows(ranking, since)
        best: dict[str, int] = {}
        for position, row in enumerate(rows, start=1):
            for entry in row.roster_entries:
                name = entry.character_name
                appearances[name] = appearances.get(name, 0) + 1
                profession.setdefault(name, entry.profession)
                if avatar.get(name) is None:
                    avatar[name] = entry.avatar_url
                if position < best.get(name, position + 1):
                    best[name] = position
        for name, rank in best.items():
            boards[name] = boards.get(name, 0) + 1
            if rank <= 10:
                top_ten[name] = top_ten.get(name, 0) + 1
            if rank <= 3:
                podium[name] = podium.get(name, 0) + 1
            if rank == 1:
                first[name] = first.get(name, 0) + 1
        if rows:
            leader = rows[0].character_name
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


@dataclass(frozen=True, slots=True)
class TeamTally:
    """One four-man composition and the boards whose first place fields it."""

    names: tuple[str, ...]
    # The roster of one of those records, for avatars and professions.
    entries: tuple[BossRankingRosterEntry, ...]
    count: int
    boards: tuple[str, ...]


def first_place_teams(
    rankings: Iterable[BossRanking],
    *,
    since: datetime | None = None,
) -> tuple[TeamTally, ...]:
    """The compositions holding first places, most boards first."""

    groups: dict[tuple[str, ...], list[tuple[str, BossRankingRow]]] = {}
    for ranking in rankings:
        rows = window_rows(ranking, since)
        if not rows:
            continue
        top = rows[0]
        names = tuple(sorted(entry.character_name for entry in top.roster_entries))
        if not names:
            names = tuple(sorted(top.roster_summary))
        if not names:
            continue
        groups.setdefault(names, []).append((ranking.boss_name, top))
    tallies = [
        TeamTally(
            names=names,
            entries=members[0][1].roster_entries,
            count=len(members),
            boards=tuple(board for board, _ in members),
        )
        for names, members in groups.items()
    ]
    tallies.sort(key=lambda tally: (-tally.count, tally.names))
    return tuple(tallies)


@dataclass(frozen=True, slots=True)
class UsageEntry:
    name: str
    count: int
    # Records fielding the character, as a percentage of every record counted.
    share: float
    avatar_url: str | None


@dataclass(frozen=True, slots=True)
class ProfessionUsage:
    profession: str
    entries: tuple[UsageEntry, ...]


def profession_usage(
    rankings: Iterable[BossRanking],
    *,
    since: datetime | None = None,
    limit: int = 6,
) -> tuple[ProfessionUsage, ...]:
    """Who fills each profession slot across every record, in slot order.

    Counted from the rosters themselves rather than from upstream's per-board
    percentages, so the share is exact: records fielding the character over
    all records counted.
    """

    counts: dict[str, dict[str, int]] = {}
    avatar: dict[str, str | None] = {}
    total = 0
    for ranking in rankings:
        for row in window_rows(ranking, since):
            total += 1
            for entry in row.roster_entries:
                bucket = counts.setdefault(entry.profession, {})
                bucket[entry.character_name] = bucket.get(entry.character_name, 0) + 1
                if avatar.get(entry.character_name) is None:
                    avatar[entry.character_name] = entry.avatar_url
    order = {profession: index for index, profession in enumerate(PROFESSION_ORDER)}
    result = []
    for profession in sorted(counts, key=lambda p: (order.get(p, len(order)), p)):
        ranked = sorted(counts[profession].items(), key=lambda kv: (-kv[1], kv[0]))
        result.append(
            ProfessionUsage(
                profession=profession,
                entries=tuple(
                    UsageEntry(
                        name=name,
                        count=count,
                        share=round(count / total * 100, 1) if total else 0.0,
                        avatar_url=avatar.get(name),
                    )
                    for name, count in ranked[:limit]
                ),
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class AccountTally:
    """One public account's standings counted over every board.

    A first place is a board whose #1 record this account uploaded; podiums
    and top tens count boards by the account's best record there.
    """

    account_id: str
    display_name: str
    first_places: int
    podiums: int
    top_tens: int
    boards: int
    records: int
    main_c: str
    main_c_count: int
    team: tuple[str, ...]
    team_count: int


def account_tallies(
    rankings: Iterable[BossRanking],
    *,
    since: datetime | None = None,
) -> tuple[AccountTally, ...]:
    """Every uploading account's counts, most first places first."""

    first: dict[str, int] = {}
    podium: dict[str, int] = {}
    top_ten: dict[str, int] = {}
    boards: dict[str, int] = {}
    records: dict[str, int] = {}
    mains: dict[str, dict[str, int]] = {}
    teams: dict[str, dict[tuple[str, ...], int]] = {}
    display: dict[str, tuple[str, str]] = {}
    for ranking in rankings:
        rows = window_rows(ranking, since)
        best: dict[str, int] = {}
        for position, row in enumerate(rows, start=1):
            account = row.account_id
            records[account] = records.get(account, 0) + 1
            if position < best.get(account, position + 1):
                best[account] = position
            bucket = mains.setdefault(account, {})
            bucket[row.character_name] = bucket.get(row.character_name, 0) + 1
            names = tuple(sorted(entry.character_name for entry in row.roster_entries))
            if names:
                combos = teams.setdefault(account, {})
                combos[names] = combos.get(names, 0) + 1
            # Nicknames change; the most recent upload carries the current one.
            newest = display.get(account)
            if newest is None or row.battle_end_at >= newest[0]:
                display[account] = (row.battle_end_at, row.account_display_name)
        for account, position in best.items():
            boards[account] = boards.get(account, 0) + 1
            if position <= 10:
                top_ten[account] = top_ten.get(account, 0) + 1
            if position <= 3:
                podium[account] = podium.get(account, 0) + 1
            if position == 1:
                first[account] = first.get(account, 0) + 1
    tallies = []
    for account, count in records.items():
        main_c, main_count = max(
            mains.get(account, {}).items(),
            key=lambda kv: (kv[1], kv[0]),
            default=("", 0),
        )
        team, team_count = max(
            teams.get(account, {}).items(),
            key=lambda kv: (kv[1], kv[0]),
            default=((), 0),
        )
        tallies.append(
            AccountTally(
                account_id=account,
                display_name=display[account][1],
                first_places=first.get(account, 0),
                podiums=podium.get(account, 0),
                top_tens=top_ten.get(account, 0),
                boards=boards.get(account, 0),
                records=count,
                main_c=main_c,
                main_c_count=main_count,
                team=team,
                team_count=team_count,
            )
        )
    tallies.sort(
        key=lambda tally: (
            -tally.first_places,
            -tally.podiums,
            -tally.top_tens,
            -tally.boards,
            -tally.records,
            tally.display_name,
        )
    )
    return tuple(tallies)


def account_tally(
    rankings: Iterable[BossRanking], account_id: str
) -> AccountTally | None:
    """One account's counts, or None when it uploaded nothing the index holds."""

    for tally in account_tallies(rankings):
        if tally.account_id == account_id:
            return tally
    return None

