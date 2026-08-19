"""Deterministic typed matching for boards, dungeons, and dungeon scopes."""

import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any

from .models import HotBossCard

_IGNORED_QUERY_WORDS = ("排行榜", "榜单", "排行", "排名", "副本", "榜", "dps")
_DIFFICULTY_SUFFIX_RE = re.compile(r"[·・\-–—\s]*(?:苦难|残酷|困难|噩梦|普通|简单)$")
_NAME_SEPARATOR_RE = re.compile(r"\s*[·・]\s*")
_ASCII_LETTERS_RE = re.compile(r"^[a-z][a-z0-9]*$")
_CHINESE_PHASE_RE = re.compile(r"[零〇一二两三四五六七八九十百]+(?=期)")
_PHASE_FAMILY_RE = re.compile(r"^(?P<family>.+?)(?P<phase>\d+)期")
_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100}


class TargetType(str, Enum):
    BOARD = "board"
    DUNGEON = "dungeon"
    DUNGEON_SCOPE = "dungeon_scope"


class MatchStatus(str, Enum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class MatchLevel(IntEnum):
    STRUCTURED_ID = 1
    STANDARD_EXACT = 2
    ALIAS_EXACT = 3
    NORMALIZED_EXACT = 4
    PINYIN_EXACT = 5
    PREFIX_SUFFIX = 6
    CONTAINS = 7
    SIMILARITY = 8


@dataclass(frozen=True, slots=True)
class AliasConfig:
    boards: dict[str, tuple[str, ...]]
    dungeons: dict[str, tuple[str, ...]]

    @classmethod
    def empty(cls) -> "AliasConfig":
        return cls(boards={}, dungeons={})

    @classmethod
    def load(cls, path: Path) -> "AliasConfig":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AliasConfigError(f"cannot load alias file: {path}") from exc

        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: Any) -> "AliasConfig":
        root = _mapping(payload, "aliases")
        return cls(
            boards=_parse_alias_group(root.get("boards", {}), "aliases.boards"),
            dungeons=_parse_alias_group(
                root.get("dungeons", {}),
                "aliases.dungeons",
            ),
        )

    def to_payload(self) -> dict[str, dict[str, list[str]]]:
        return {
            "boards": {key: list(values) for key, values in self.boards.items()},
            "dungeons": {
                key: list(values) for key, values in self.dungeons.items()
            },
        }

    def with_aliases(
        self,
        target_type: "TargetType",
        target_key: str,
        aliases: tuple[str, ...],
    ) -> "AliasConfig":
        """Return a copy with ``aliases`` added to one board or dungeon."""

        group = dict(
            self.boards if target_type is TargetType.BOARD else self.dungeons
        )
        current = list(group.get(target_key, ()))
        for alias in aliases:
            cleaned = alias.strip()
            if cleaned and cleaned not in current:
                current.append(cleaned)
        group[target_key] = tuple(current)
        if target_type is TargetType.BOARD:
            return AliasConfig(boards=group, dungeons=dict(self.dungeons))
        return AliasConfig(boards=dict(self.boards), dungeons=group)

    def without_alias(self, alias: str) -> tuple["AliasConfig", int]:
        """Return a copy with every occurrence of ``alias`` removed."""

        key = fold_text(alias)
        removed = 0

        def strip(group: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
            nonlocal removed
            result: dict[str, tuple[str, ...]] = {}
            for target, values in group.items():
                kept = tuple(v for v in values if fold_text(v) != key)
                removed += len(values) - len(kept)
                if kept:
                    result[target] = kept
            return result

        updated = AliasConfig(
            boards=strip(self.boards),
            dungeons=strip(self.dungeons),
        )
        return updated, removed

    def iter_entries(self):
        for key, values in self.boards.items():
            yield TargetType.BOARD, key, values
        for key, values in self.dungeons.items():
            yield TargetType.DUNGEON, key, values


class AliasConfigError(ValueError):
    """Raised when the local alias configuration is malformed."""


@dataclass(frozen=True, slots=True)
class MatchTarget:
    target_type: TargetType
    key: str
    name: str
    dungeon_names: tuple[str, ...]
    boss_slugs: tuple[str, ...]
    query_text: str
    aliases: tuple[str, ...] = ()
    pinyin: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MatchChoice:
    target: MatchTarget
    level: MatchLevel
    score: float
    matched_text: str


@dataclass(frozen=True, slots=True)
class MatchResult:
    status: MatchStatus
    query: str
    selected: MatchChoice | None = None
    candidates: tuple[MatchChoice, ...] = ()


@dataclass(frozen=True, slots=True)
class _SearchText:
    raw: str
    folded: str
    compact: str
    is_alias: bool


class RankingMatcher:
    """Build a live candidate index from one hot-bosses response."""

    def __init__(
        self,
        cards: tuple[HotBossCard, ...],
        aliases: AliasConfig,
        *,
        fuzzy_threshold: float = 0.65,
        ambiguity_score_gap: float = 0.08,
    ) -> None:
        self.cards = cards
        self.fuzzy_threshold = _ratio(fuzzy_threshold, "fuzzy_threshold")
        self.ambiguity_score_gap = _ratio(
            ambiguity_score_gap,
            "ambiguity_score_gap",
        )
        self.targets, self.issues = _build_targets(cards, aliases)
        self._boards_by_slug = {
            target.key: target
            for target in self.targets
            if target.target_type is TargetType.BOARD
        }

    def match(
        self,
        query: str,
        *,
        allowed_types: frozenset[TargetType] | None = None,
    ) -> MatchResult:
        stripped_query = query.strip()
        folded_query = fold_text(stripped_query)
        compact_query = normalize_search_text(stripped_query)
        if not compact_query:
            return MatchResult(MatchStatus.NOT_FOUND, stripped_query)

        enabled_types = (
            frozenset(TargetType) if allowed_types is None else allowed_types
        )

        choices = tuple(
            choice
            for target in self.targets
            if target.target_type in enabled_types
            if (
                choice := _score_target(
                    target,
                    folded_query,
                    compact_query,
                )
            )
            is not None
        )
        ranked = _rank_choices(choices)
        if not ranked:
            return MatchResult(MatchStatus.NOT_FOUND, stripped_query)

        exact = tuple(
            choice
            for choice in ranked
            if choice.level <= MatchLevel.PINYIN_EXACT
        )
        if exact:
            # "影拓丰碑1期" names several dungeons of one phase; treat an exact
            # hit on all of them as the whole phase rather than an ambiguity.
            exact_dungeons = tuple(
                choice
                for choice in exact
                if choice.target.target_type is TargetType.DUNGEON
            )
            if (
                TargetType.DUNGEON_SCOPE in enabled_types
                and len(exact_dungeons) >= 2
                and len(exact_dungeons) == len(exact)
            ):
                scope = self._build_scope(
                    stripped_query,
                    compact_query,
                    exact_dungeons,
                )
                if scope is not None:
                    return MatchResult(
                        MatchStatus.MATCHED,
                        stripped_query,
                        selected=scope,
                    )
            preferred = self._prefer_single_board_dungeons(
                exact,
                enabled_types,
            )
            return self._resolve_ranked(stripped_query, preferred)

        scope = None
        if TargetType.DUNGEON_SCOPE in enabled_types:
            scope = self._build_scope(
                stripped_query,
                compact_query,
                tuple(
                    choice
                    for choice in ranked
                    if choice.target.target_type is TargetType.DUNGEON
                    and choice.level
                    in {MatchLevel.PREFIX_SUFFIX, MatchLevel.CONTAINS}
                ),
            )
        if scope is not None:
            return MatchResult(
                MatchStatus.MATCHED,
                stripped_query,
                selected=scope,
            )

        preferred = self._prefer_single_board_dungeons(
            ranked,
            enabled_types,
        )
        return self._resolve_ranked(stripped_query, preferred)

    def _prefer_single_board_dungeons(
        self,
        choices: tuple[MatchChoice, ...],
        enabled_types: frozenset[TargetType],
    ) -> tuple[MatchChoice, ...]:
        """Resolve an exact one-board dungeon name to its concrete ranking."""

        if TargetType.BOARD not in enabled_types:
            return choices

        preferred: list[MatchChoice] = []
        for choice in choices:
            target = choice.target
            if (
                target.target_type is TargetType.DUNGEON
                and len(target.boss_slugs) == 1
                and choice.level <= MatchLevel.PINYIN_EXACT
            ):
                board = self._boards_by_slug.get(target.boss_slugs[0])
                if board is not None:
                    choice = MatchChoice(
                        target=board,
                        level=choice.level,
                        score=choice.score,
                        matched_text=choice.matched_text,
                    )
            preferred.append(choice)
        return _rank_choices(tuple(preferred))

    def _resolve_ranked(
        self,
        query: str,
        ranked: tuple[MatchChoice, ...],
    ) -> MatchResult:
        top = ranked[0]
        candidates = ranked[:5]
        if top.level is MatchLevel.SIMILARITY and top.score < self.fuzzy_threshold:
            return MatchResult(
                MatchStatus.AMBIGUOUS,
                query,
                candidates=candidates,
            )

        same_level = tuple(
            choice for choice in ranked if choice.level is top.level
        )
        if len(normalize_search_text(query)) == 1 and len(same_level) > 1:
            return MatchResult(
                MatchStatus.AMBIGUOUS,
                query,
                candidates=candidates,
            )

        if (
            len(same_level) > 1
            and top.score - same_level[1].score < self.ambiguity_score_gap
        ):
            return MatchResult(
                MatchStatus.AMBIGUOUS,
                query,
                candidates=candidates,
            )

        return MatchResult(MatchStatus.MATCHED, query, selected=top)

    def _build_scope(
        self,
        query: str,
        compact_query: str,
        dungeon_matches: tuple[MatchChoice, ...],
    ) -> MatchChoice | None:
        if len(dungeon_matches) < 2:
            return None

        matched_families = tuple(
            _phase_family(choice.target.name) for choice in dungeon_matches
        )
        if None in matched_families or len(set(matched_families)) != 1:
            return None

        family = matched_families[0]
        if family is None:
            return None
        matching_dungeon_names = {
            choice.target.name for choice in dungeon_matches
        }
        dungeon_names = tuple(
            target.name
            for target in self.targets
            if target.target_type is TargetType.DUNGEON
            and target.name in matching_dungeon_names
        )
        dungeon_name_set = set(dungeon_names)
        boss_slugs = tuple(
            card.boss_slug
            for card in self.cards
            if card.dungeon_name in dungeon_name_set
        )
        target = MatchTarget(
            target_type=TargetType.DUNGEON_SCOPE,
            key=f"scope:{family}:{compact_query}",
            name=_scope_name(family, dungeon_names),
            dungeon_names=dungeon_names,
            boss_slugs=boss_slugs,
            query_text=query,
        )
        return MatchChoice(
            target=target,
            level=min(choice.level for choice in dungeon_matches),
            score=max(choice.score for choice in dungeon_matches),
            matched_text=query,
        )


def fold_text(value: str) -> str:
    """NFKC/case-fold text while preserving internal punctuation and spacing."""

    return unicodedata.normalize("NFKC", value).casefold().strip()


def normalize_search_text(value: str) -> str:
    """Build the compact Chinese board-search key described by the MVP."""

    normalized = _replace_chinese_phase_numbers(fold_text(value))
    for ignored in _IGNORED_QUERY_WORDS:
        normalized = normalized.replace(ignored, "")
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith(("P", "Z"))
    )


def _build_targets(
    cards: tuple[HotBossCard, ...],
    aliases: AliasConfig,
) -> tuple[tuple[MatchTarget, ...], tuple[str, ...]]:
    issues: list[str] = []
    targets: list[MatchTarget] = []
    cards_by_slug: dict[str, HotBossCard] = {}
    dungeon_cards: dict[str, list[HotBossCard]] = {}

    for card in cards:
        if card.boss_slug in cards_by_slug:
            issues.append(f"duplicate boss slug ignored: {card.boss_slug}")
            continue
        cards_by_slug[card.boss_slug] = card
        dungeon_cards.setdefault(card.dungeon_name, []).append(card)

    for slug, card in cards_by_slug.items():
        merged = _merge_aliases(
            card.boss_name,
            aliases.boards.get(slug, ()),
            derived_board_aliases(card.boss_name, card.dungeon_name),
        )
        targets.append(
            MatchTarget(
                target_type=TargetType.BOARD,
                key=slug,
                name=card.boss_name,
                dungeon_names=(card.dungeon_name,),
                boss_slugs=(slug,),
                query_text=slug,
                aliases=merged,
                pinyin=pinyin_keys((card.boss_name, *merged)),
            )
        )

    for dungeon_name, grouped_cards in dungeon_cards.items():
        merged = _merge_aliases(
            dungeon_name,
            aliases.dungeons.get(dungeon_name, ()),
            derived_dungeon_aliases(dungeon_name),
        )
        targets.append(
            MatchTarget(
                target_type=TargetType.DUNGEON,
                key=dungeon_name,
                name=dungeon_name,
                dungeon_names=(dungeon_name,),
                boss_slugs=tuple(card.boss_slug for card in grouped_cards),
                query_text=dungeon_name,
                aliases=merged,
                pinyin=pinyin_keys((dungeon_name, *merged)),
            )
        )

    for slug in aliases.boards.keys() - cards_by_slug.keys():
        issues.append(f"board alias target does not exist: {slug}")
    for dungeon_name in aliases.dungeons.keys() - dungeon_cards.keys():
        issues.append(f"dungeon alias target does not exist: {dungeon_name}")
    issues.extend(_alias_collision_issues(aliases))
    return tuple(targets), tuple(issues)


def _merge_aliases(
    name: str,
    configured: tuple[str, ...],
    derived: tuple[str, ...],
) -> tuple[str, ...]:
    seen = {fold_text(name)}
    merged: list[str] = []
    for alias in (*configured, *derived):
        key = fold_text(alias)
        if key and key not in seen:
            seen.add(key)
            merged.append(alias)
    return tuple(merged)


def derived_board_aliases(boss_name: str, dungeon_name: str) -> tuple[str, ...]:
    """Names people actually type: no difficulty suffix, no dungeon prefix."""

    results: list[str] = []
    stripped = _DIFFICULTY_SUFFIX_RE.sub("", boss_name).strip()
    for candidate in (boss_name, stripped):
        parts = _NAME_SEPARATOR_RE.split(candidate, maxsplit=1)
        if len(parts) == 2 and fold_text(parts[0]) == fold_text(dungeon_name):
            results.append(parts[1].strip())
    results.append(stripped)
    return tuple(dict.fromkeys(alias for alias in results if alias))


def derived_dungeon_aliases(dungeon_name: str) -> tuple[str, ...]:
    """Split ``影拓丰碑4期 · 山中见犼`` into its parts plus ``丰碑4`` / ``影拓4``."""

    results: list[str] = []
    parts = [part.strip() for part in _NAME_SEPARATOR_RE.split(dungeon_name)]
    if len(parts) > 1:
        results.extend(part for part in parts if part)
    for part in parts:
        match = _PHASE_FAMILY_RE.match(normalize_search_text(part))
        if match is None:
            continue
        family, phase = match.group("family"), match.group("phase")
        results.append(f"{family}{phase}")
        if len(family) > 2:
            results.append(f"{family[:2]}{phase}")
            results.append(f"{family[-2:]}{phase}")
    return tuple(dict.fromkeys(alias for alias in results if alias))


def pinyin_keys(texts: tuple[str, ...]) -> tuple[str, ...]:
    """Full pinyin and initials for every CJK text; empty without pypinyin."""

    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:  # pragma: no cover - optional dependency
        return ()
    keys: list[str] = []
    for text in texts:
        cjk = "".join(
            ch for ch in text if "一" <= ch <= "鿿" or ch.isdigit()
        )
        if not any("一" <= ch <= "鿿" for ch in cjk):
            continue
        full = "".join(lazy_pinyin(cjk))
        initials = "".join(
            item[:1] for item in lazy_pinyin(cjk, style=Style.FIRST_LETTER)
        )
        for key in (full, initials):
            if len(key) >= 2 and key not in keys:
                keys.append(key)
    return tuple(keys)


def _score_target(
    target: MatchTarget,
    folded_query: str,
    compact_query: str,
) -> MatchChoice | None:
    if (
        target.target_type is TargetType.BOARD
        and fold_text(target.key) == folded_query
    ):
        return MatchChoice(
            target,
            MatchLevel.STRUCTURED_ID,
            1.0,
            target.key,
        )

    if (
        target.pinyin
        and _ASCII_LETTERS_RE.match(compact_query)
        and compact_query in target.pinyin
    ):
        return MatchChoice(target, MatchLevel.PINYIN_EXACT, 1.0, target.name)

    texts = (
        _search_text(target.name, is_alias=False),
        *(_search_text(alias, is_alias=True) for alias in target.aliases),
    )
    matches = tuple(
        choice
        for text in texts
        if (
            choice := _score_search_text(
                target,
                text,
                folded_query,
                compact_query,
            )
        )
        is not None
    )
    return _rank_choices(matches)[0] if matches else None


def _score_search_text(
    target: MatchTarget,
    text: _SearchText,
    folded_query: str,
    compact_query: str,
) -> MatchChoice | None:
    if text.folded == folded_query:
        level = MatchLevel.ALIAS_EXACT if text.is_alias else MatchLevel.STANDARD_EXACT
        return MatchChoice(target, level, 1.0, text.raw)
    if text.compact == compact_query:
        return MatchChoice(target, MatchLevel.NORMALIZED_EXACT, 1.0, text.raw)

    if not text.compact:
        return None
    coverage = min(1.0, len(compact_query) / len(text.compact))
    if text.compact.startswith(compact_query) or text.compact.endswith(compact_query):
        return MatchChoice(
            target,
            MatchLevel.PREFIX_SUFFIX,
            0.90 + 0.09 * coverage,
            text.raw,
        )
    if compact_query in text.compact:
        return MatchChoice(
            target,
            MatchLevel.CONTAINS,
            0.80 + 0.09 * coverage,
            text.raw,
        )

    similarity = _partial_similarity(compact_query, text.compact)
    if similarity <= 0:
        return None
    return MatchChoice(
        target,
        MatchLevel.SIMILARITY,
        similarity,
        text.raw,
    )


def _rank_choices(choices: tuple[MatchChoice, ...]) -> tuple[MatchChoice, ...]:
    best_by_target: dict[tuple[TargetType, str], MatchChoice] = {}
    for choice in choices:
        identity = (choice.target.target_type, choice.target.key)
        current = best_by_target.get(identity)
        if current is None or (choice.level, -choice.score) < (
            current.level,
            -current.score,
        ):
            best_by_target[identity] = choice
    return tuple(
        sorted(
            best_by_target.values(),
            key=lambda choice: (
                choice.level,
                -choice.score,
                choice.target.target_type.value,
                choice.target.name,
            ),
        )
    )


def _search_text(value: str, *, is_alias: bool) -> _SearchText:
    return _SearchText(
        raw=value,
        folded=fold_text(value),
        compact=normalize_search_text(value),
        is_alias=is_alias,
    )


def _partial_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) == len(longer):
        return SequenceMatcher(None, shorter, longer, autojunk=False).ratio()
    return max(
        SequenceMatcher(
            None,
            shorter,
            longer[index : index + len(shorter)],
            autojunk=False,
        ).ratio()
        for index in range(len(longer) - len(shorter) + 1)
    )


def _phase_family(value: str) -> str | None:
    match = _PHASE_FAMILY_RE.match(normalize_search_text(value))
    return match.group("family") if match else None


def _scope_name(family: str, dungeon_names: tuple[str, ...]) -> str:
    phases = sorted(
        {
            int(match.group("phase"))
            for name in dungeon_names
            if (match := _PHASE_FAMILY_RE.match(normalize_search_text(name)))
        }
    )
    if not phases:
        return family
    if phases == list(range(phases[0], phases[-1] + 1)) and len(phases) > 1:
        return f"{family}{phases[0]}—{phases[-1]}期"
    return f"{family}{'、'.join(str(phase) for phase in phases)}期"


def _replace_chinese_phase_numbers(value: str) -> str:
    return _CHINESE_PHASE_RE.sub(
        lambda match: str(_chinese_number(match.group())),
        value,
    )


def _chinese_number(value: str) -> int:
    total = 0
    current = 0
    for character in value:
        if character in _CHINESE_DIGITS:
            current = _CHINESE_DIGITS[character]
        elif character in _CHINESE_UNITS:
            unit = _CHINESE_UNITS[character]
            total += (current or 1) * unit
            current = 0
    return total + current


def _alias_collision_issues(aliases: AliasConfig) -> tuple[str, ...]:
    owners: dict[str, set[tuple[str, str]]] = {}
    labels: dict[str, str] = {}
    groups = (("board", aliases.boards), ("dungeon", aliases.dungeons))
    for kind, group in groups:
        for target_key, target_aliases in group.items():
            for alias in target_aliases:
                key = fold_text(alias)
                owners.setdefault(key, set()).add((kind, target_key))
                labels.setdefault(key, alias)
    return tuple(
        f"alias is assigned to multiple targets: {labels[key]}"
        for key, targets_for_alias in owners.items()
        if len(targets_for_alias) > 1
    )


def _parse_alias_group(value: Any, path: str) -> dict[str, tuple[str, ...]]:
    group = _mapping(value, path)
    result: dict[str, tuple[str, ...]] = {}
    for target, aliases in group.items():
        if not isinstance(target, str) or not target.strip():
            raise AliasConfigError(f"{path} target names must be non-empty strings")
        if not isinstance(aliases, list):
            raise AliasConfigError(f"{path}.{target} must be an array")
        cleaned: list[str] = []
        for alias in aliases:
            if not isinstance(alias, str) or not alias.strip():
                raise AliasConfigError(
                    f"{path}.{target} aliases must be non-empty strings"
                )
            normalized_alias = alias.strip()
            if normalized_alias not in cleaned:
                cleaned.append(normalized_alias)
        result[target.strip()] = tuple(cleaned)
    return result


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AliasConfigError(f"{path} must be an object")
    return value


def _ratio(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return result
