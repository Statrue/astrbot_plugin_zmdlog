"""Derived facts about a battle's roster loadout and per-skill damage.

Nothing here formats or fetches: the functions turn the raw roster and
``roleSkillStats`` rows of a public battle into labels, categories and groups
that the presentation layer can print. Skill naming mirrors the site's own
``skill-display`` helper so both surfaces call the same thing by the same name.
"""

import re
from dataclasses import dataclass
from enum import Enum

from .models import (
    BattleRosterEntry,
    BattleSkillStat,
    BattleWeapon,
)

_ATTACK_INDEX_RE = re.compile(r"_attack(\d+)(?:$|_)", re.IGNORECASE)
_COMBO_SKILL_KEY_RE = re.compile(r"(?:^|_)combo(?:_\d+)?_skill(?:$|_)", re.IGNORECASE)
_NORMAL_ATTACK_NAME_RE = re.compile(r"^A[1-9]$", re.IGNORECASE)
# ``chr_0032_lizhiyan_skill_3090``: the character prefix says nothing to a
# reader, the tail is all that distinguishes one unnamed skill from another.
_CHARACTER_KEY_PREFIX_RE = re.compile(r"^chr_\d+_[a-z0-9]+_", re.IGNORECASE)
_CJK_RE = re.compile(r"[㐀-鿿]")
# ``item_equip_t4_suit_usp02_body_03``: the middle segment is the suit id
# the game data catalog is keyed by. ``parts`` in place of ``suit`` marks a
# piece that belongs to no suit at all.
_SUIT_TOKEN_RE = re.compile(
    r"^item_equip_t\d+_(?P<kind>suit|parts)_(?P<token>[a-z0-9_]+?)"
    r"_(?:hand|body|edc)_\d+$",
    re.IGNORECASE,
)
_WEAPON_OWN_SKILL_PREFIX = "sk_wpn_"

# Same overrides as the site's ``lib/format/skill-display.ts``.
_SKILL_KEY_OVERRIDES = {
    "buff_common_cryst_triggered_physical_break": "寒冷击破触发",
}
_SKILL_NAME_OVERRIDES = {
    "cryst triggered physical break": "寒冷击破触发",
}
# Upstream element keys as the game names them; unknown keys are shown raw.
_ELEMENT_LABELS = {
    "physical": "物理",
    "fire": "灼热",
    "cryst": "寒冷",
    "natural": "自然",
    "pulse": "电磁",
}
# Stat names the parser sometimes leaves untranslated.
_STAT_LABELS = {"main": "主能力", "sub": "副能力"}
_SKILL_LEVEL_SLOTS = (
    ("普攻", "_attack1"),
    ("战技", "_normal_skill"),
    ("连携", "_combo_skill"),
    ("终结", "_ultimate_skill"),
)


class SkillCategory(str, Enum):
    """Where a damage row comes from; the value is the printed tag."""

    NORMAL = "普攻"
    SKILL = "战技"
    COMBO = "连携"
    HEAVY = "重击"
    ULTIMATE = "终结技"
    # Element reactions, statuses and other engine-side damage sources.
    MECHANIC = "机制"
    SUIT = "套装"
    WEAPON = "武器"
    OTHER = "其他"


@dataclass(frozen=True, slots=True)
class SkillDamageRow:
    category: SkillCategory
    name: str
    cast_count: int
    total_damage: int
    avg_damage: float
    max_damage: int
    # How many upstream rows were folded into this one (normal attack chains).
    merged_count: int = 1


@dataclass(frozen=True, slots=True)
class CharacterSkillDamage:
    character_name: str
    total_damage: int
    rows: tuple[SkillDamageRow, ...]


@dataclass(frozen=True, slots=True)
class SkillLevel:
    label: str
    level: int


def skill_display_name(
    skill_name: str,
    skill_key: str | None,
    *,
    combo_from_key: bool = True,
) -> str:
    """The name a reader should see for one skill-stat row.

    Follows the site's rules (key override, name override, combo detection),
    then tidies raw keys the parser could not name: the character prefix is
    dropped and underscores become spaces. ``combo_from_key`` is the loose
    "anything with combo_skill in the key" rule; the cast timeline turns it
    off because mechanism entities carry such keys too.
    """

    if skill_key:
        override = _SKILL_KEY_OVERRIDES.get(skill_key.lower())
        if override:
            return override
    name = skill_name.strip()
    override = _SKILL_NAME_OVERRIDES.get(name.lower())
    if override:
        return override
    if combo_from_key and skill_key and _COMBO_SKILL_KEY_RE.search(skill_key):
        return "连携技"
    if not name:
        return skill_key or "未命名技能"
    if _looks_like_raw_key(name):
        stripped = _CHARACTER_KEY_PREFIX_RE.sub("", name).replace("_", " ").strip()
        return stripped or name
    return name


def skill_category(skill_name: str, skill_key: str | None) -> SkillCategory:
    """Bucket one skill-stat row, mirroring the site's category rules."""

    display = skill_display_name(skill_name, skill_key)
    if _NORMAL_ATTACK_NAME_RE.match(display):
        return SkillCategory.NORMAL
    if display == "战技":
        return SkillCategory.SKILL
    if display == "连携技":
        return SkillCategory.COMBO
    if display in {"重击", "处决"}:
        return SkillCategory.HEAVY
    if display == "终结技":
        return SkillCategory.ULTIMATE
    if not skill_key:
        return SkillCategory.OTHER
    lowered = skill_key.lower()
    if "_ult_attack" in lowered or "_ultimate_skill" in lowered:
        return SkillCategory.ULTIMATE
    if _COMBO_SKILL_KEY_RE.search(lowered):
        return SkillCategory.COMBO
    if "_normal_skill" in lowered:
        return SkillCategory.SKILL
    if "execute" in lowered or "execution" in lowered:
        return SkillCategory.HEAVY
    if any(
        token in lowered
        for token in ("power_attack", "plunging_attack", "heavy_attack")
    ):
        return SkillCategory.HEAVY
    if _ATTACK_INDEX_RE.search(lowered):
        return SkillCategory.NORMAL
    if lowered.startswith("buff_equipsuit_"):
        return SkillCategory.SUIT
    if lowered.startswith("buff_wpn_"):
        return SkillCategory.WEAPON
    if lowered.startswith("buff_"):
        return SkillCategory.MECHANIC
    return SkillCategory.OTHER


def is_normal_attack_segment(skill_key: str | None) -> bool:
    """Whether the row is one segment (A1, A2, ...) of the basic attack chain."""

    return bool(skill_key) and _ATTACK_INDEX_RE.search(skill_key) is not None


def group_skill_damage(
    stats: tuple[BattleSkillStat, ...],
    *,
    merge_normal_attacks: bool = True,
) -> tuple[CharacterSkillDamage, ...]:
    """Per-character damage rows, heaviest character and heaviest row first.

    The basic attack chain arrives as one row per segment (A1 to A5); a chat
    image reads better with the chain folded into one row, which is what the
    flag does. Rows that end up with the same tag and name (three "终结技"
    sub-hits, two "噪点" heavy attacks) are folded as well: a reader could not
    tell them apart on the page anyway.
    """

    per_character: dict[str, list[BattleSkillStat]] = {}
    for stat in stats:
        per_character.setdefault(stat.character_name, []).append(stat)
    groups: list[CharacterSkillDamage] = []
    for character_name, rows in per_character.items():
        built: list[SkillDamageRow] = []
        segments: list[BattleSkillStat] = []
        for stat in rows:
            category = skill_category(stat.skill_name, stat.skill_key)
            if (
                merge_normal_attacks
                and category is SkillCategory.NORMAL
                and is_normal_attack_segment(stat.skill_key)
            ):
                segments.append(stat)
                continue
            built.append(
                SkillDamageRow(
                    category=category,
                    name=skill_display_name(stat.skill_name, stat.skill_key),
                    cast_count=stat.cast_count,
                    total_damage=stat.total_damage,
                    avg_damage=stat.avg_damage,
                    max_damage=stat.max_damage,
                )
            )
        if segments:
            built.append(_merge_normal_attacks(segments))
        folded: dict[tuple[SkillCategory, str], SkillDamageRow] = {}
        for row in built:
            key = (row.category, row.name)
            existing = folded.get(key)
            folded[key] = row if existing is None else _fold_rows(existing, row)
        ordered = sorted(
            folded.values(), key=lambda row: (-row.total_damage, row.name)
        )
        groups.append(
            CharacterSkillDamage(
                character_name=character_name,
                total_damage=sum(row.total_damage for row in ordered),
                rows=tuple(ordered),
            )
        )
    groups.sort(key=lambda group: (-group.total_damage, group.character_name))
    return tuple(groups)


def _fold_rows(first: SkillDamageRow, second: SkillDamageRow) -> SkillDamageRow:
    casts = first.cast_count + second.cast_count
    total = first.total_damage + second.total_damage
    return SkillDamageRow(
        category=first.category,
        name=first.name,
        cast_count=casts,
        total_damage=total,
        avg_damage=total / casts if casts else 0.0,
        max_damage=max(first.max_damage, second.max_damage),
        merged_count=first.merged_count + second.merged_count,
    )


def _merge_normal_attacks(segments: list[BattleSkillStat]) -> SkillDamageRow:
    casts = sum(stat.cast_count for stat in segments)
    total = sum(stat.total_damage for stat in segments)
    names = {
        skill_display_name(stat.skill_name, stat.skill_key) for stat in segments
    }
    shared = next(iter(names)) if len(names) == 1 else None
    named = (
        shared
        if shared is not None and not _NORMAL_ATTACK_NAME_RE.match(shared)
        else None
    )
    return SkillDamageRow(
        category=SkillCategory.NORMAL,
        name=f"普攻 · {named}" if named else "普攻（各段合并）",
        cast_count=casts,
        total_damage=total,
        avg_damage=total / casts if casts else 0.0,
        max_damage=max(stat.max_damage for stat in segments),
        merged_count=len(segments),
    )


def element_label(element: str | None) -> str | None:
    if not element:
        return None
    return _ELEMENT_LABELS.get(element.lower(), element)


def stat_label(name: str) -> str:
    return _STAT_LABELS.get(name.strip().lower(), name.strip())


def suit_catalog_id(item_id: str | None) -> str | None:
    """The game data catalog id of this piece's suit, if it has one.

    Only ``_suit_`` pieces belong to a suit; a ``_parts_`` piece is
    standalone and has nothing to look up.
    """

    if not item_id:
        return None
    match = _SUIT_TOKEN_RE.match(item_id)
    if match is None or match.group("kind").lower() != "suit":
        return None
    return f"suit_{match.group('token').lower()}"


def is_raw_item_name(piece_name: str, item_id: str | None) -> bool:
    """Whether upstream fell back to the item id instead of a real name."""

    stripped = piece_name.strip()
    return not stripped or stripped == item_id or stripped.startswith("item_")


def skill_level_summary(entry: BattleRosterEntry) -> tuple[SkillLevel, ...]:
    """普攻 / 战技 / 连携 / 终结 levels, in that order, when recorded."""

    levels: list[SkillLevel] = []
    for label, suffix in _SKILL_LEVEL_SLOTS:
        for skill in entry.skills:
            if skill.skill_key.lower().endswith(suffix):
                levels.append(SkillLevel(label=label, level=skill.level))
                break
    return tuple(levels)


def weapon_skill_levels(weapon: BattleWeapon) -> tuple[int | None, tuple[int, ...]]:
    """The weapon's own skill level and the levels of its affixes."""

    own: int | None = None
    affixes: list[int] = []
    for skill in weapon.skills:
        if skill.level is None:
            continue
        if skill.skill_key.lower().startswith(_WEAPON_OWN_SKILL_PREFIX):
            if own is None:
                own = skill.level
            continue
        affixes.append(skill.level)
    return own, tuple(affixes)


def _looks_like_raw_key(name: str) -> bool:
    return "_" in name and not _CJK_RE.search(name) and " " not in name
