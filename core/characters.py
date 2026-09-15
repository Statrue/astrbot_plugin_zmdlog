"""Resolve a user-typed character name against the names seen in one ranking."""

from dataclasses import dataclass
from enum import Enum

from .matcher import fold_text, normalize_search_text, pinyin_keys
from .models import BossRanking, BossRankingRow, PublicUserRankings


class CharacterResolutionStatus(str, Enum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class CharacterResolution:
    status: CharacterResolutionStatus
    query: str
    name: str | None = None
    candidates: tuple[str, ...] = ()


def ranking_character_names(ranking: BossRanking) -> tuple[str, ...]:
    """Every character name that appears anywhere in a ranking response."""

    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    for row in ranking.rows:
        add(row.character_name)
        for entry in row.roster_entries:
            add(entry.character_name)
    for group in ranking.profession_groups:
        for entry in group.entries:
            add(entry.character_name)
    return tuple(names)


def account_roster_names(account: PublicUserRankings) -> tuple[str, ...]:
    """Every character named in an account's records, for the catalog reads."""

    names: dict[str, None] = {}
    for row in account.rankings:
        for name in row.roster_summary:
            names.setdefault(name, None)
    return tuple(names)


def resolve_character_name(
    query: str,
    names: tuple[str, ...],
) -> CharacterResolution:
    """Map ``query`` to exactly one of ``names`` or explain why it cannot.

    Resolution order: exact name, folded/compacted exact, pinyin (full or
    initials) exact, then a unique prefix or substring match. Several hits at
    the first level that produces any hit make the query ambiguous.
    """

    stripped = query.strip()
    if not stripped or not names:
        return CharacterResolution(CharacterResolutionStatus.NOT_FOUND, stripped)

    if stripped in names:
        return CharacterResolution(
            CharacterResolutionStatus.MATCHED, stripped, name=stripped
        )

    folded = fold_text(stripped)
    compact = normalize_search_text(stripped)
    levels: list[tuple[str, ...]] = []

    levels.append(
        tuple(
            name
            for name in names
            if fold_text(name) == folded
            or (compact and normalize_search_text(name) == compact)
        )
    )
    ascii_key = "".join(folded.split())
    if ascii_key.isascii() and ascii_key.isalpha():
        levels.append(
            tuple(name for name in names if ascii_key in pinyin_keys((name,)))
        )
    if compact:
        levels.append(
            tuple(
                name
                for name in names
                if normalize_search_text(name).startswith(compact)
            )
        )
        levels.append(
            tuple(name for name in names if compact in normalize_search_text(name))
        )
    if ascii_key.isascii() and ascii_key.isalpha():
        levels.append(
            tuple(
                name
                for name in names
                if any(key.startswith(ascii_key) for key in pinyin_keys((name,)))
            )
        )

    for hits in levels:
        if len(hits) == 1:
            return CharacterResolution(
                CharacterResolutionStatus.MATCHED, stripped, name=hits[0]
            )
        if len(hits) > 1:
            return CharacterResolution(
                CharacterResolutionStatus.AMBIGUOUS,
                stripped,
                candidates=hits[:6],
            )
    return CharacterResolution(CharacterResolutionStatus.NOT_FOUND, stripped)


class CharacterFilterScope(str, Enum):
    """Which row field a resolved ``--角色`` name is matched against."""

    MAIN = "main"
    ROSTER = "roster"
    NONE = "none"


def pick_character_filter_scope(
    ranking: BossRanking,
    name: str,
) -> CharacterFilterScope:
    """Prefer main-C rows, fall back to whole-roster rows when there are none.

    Supports and other off-carry characters are never a row's ``characterName``,
    so a main-C-only filter reports "no records" for exactly the characters
    people most need the ranking rows for. Falling back keeps one option instead
    of teaching a second one.
    """

    if any(row.character_name == name for row in ranking.rows):
        return CharacterFilterScope.MAIN
    if any(
        entry.character_name == name
        for row in ranking.rows
        for entry in row.roster_entries
    ):
        return CharacterFilterScope.ROSTER
    return CharacterFilterScope.NONE


def row_fields(
    row: BossRankingRow,
    names: tuple[str, ...],
    scope: CharacterFilterScope,
) -> bool:
    """Whether one ranking row passes a resolved ``--角色`` filter.

    MAIN wants the first name as the row's main C; ROSTER wants every name
    somewhere in the team; NONE passes nothing.
    """

    if scope is CharacterFilterScope.MAIN:
        return bool(names) and row.character_name == names[0]
    if scope is CharacterFilterScope.ROSTER:
        fielded = {entry.character_name for entry in row.roster_entries}
        return bool(names) and all(name in fielded for name in names)
    return False
