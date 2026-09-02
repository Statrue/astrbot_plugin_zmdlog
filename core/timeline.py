"""Skill timeline (技能轴) of one public battle, built from the export casts.

The public export lists every cast as an interval per character. This module
turns that list into one rail per roster character: a time-ordered list of
events where a *named move* (战技 / 连携技 / 终结技 / 重击 / a character's own
named attack) is one node and a burst of normal-attack segments is folded
into a single run. Summoned entities' casts form a second, quieter stream on
the same rail. Nothing here formats or fetches; the presentation layer turns
milliseconds into pixels.

Cast keys are classified the way upstream's own axis tool does it: only keys
that *end* in a known player-action suffix count as that action, because
mechanism sub-entities (a vortex that lives 40 seconds) carry loose variants
of the same keys and would otherwise be drawn as the player's 连携技.
"""

import re
from dataclasses import dataclass

from .loadout import SkillCategory, skill_display_name
from .models import BattleExport

# Two casts closer than this are the same burst: A1→A2→A3, or the same skill
# fired again straight away (a support spamming her ability).
MERGE_GAP_MS = 800
SUMMON_MERGE_GAP_MS = 1_500
_SUMMON_SOURCE = "summon"

_NOISE_RE = re.compile(
    r"(?:^|_)(?:dodge|dash|sprint|idle|born|die|hitstop|switch)(?:$|_)",
    re.IGNORECASE,
)
_PLUNGING_END_RE = re.compile(r"_plunging_attack_end$", re.IGNORECASE)
_EXECUTION_RE = re.compile(r"execut", re.IGNORECASE)
_ULTIMATE_END_RE = re.compile(r"_ultimate_skill\d*$", re.IGNORECASE)
_COMBO_END_RE = re.compile(r"_combo(?:_\d+)?_skill$", re.IGNORECASE)
_NORMAL_SKILL_END_RE = re.compile(r"_normal_skill$", re.IGNORECASE)
_ULT_ATTACK_END_RE = re.compile(r"_ult(?:imate)?_attack_?\d*$", re.IGNORECASE)
_HEAVY_END_RE = re.compile(r"(?:power|heavy)_attack$", re.IGNORECASE)
_PLUNGING_START_RE = re.compile(r"_plunging_attack(?:_start)?$", re.IGNORECASE)
_ATTACK_SEGMENT_END_RE = re.compile(r"_attack_?\d+$", re.IGNORECASE)
_NORMAL_ATTACK_NAME_RE = re.compile(r"^A[1-9]$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CastBlock:
    """One drawable cast, clipped to the battle and classified."""

    character_key: str
    name: str
    category: SkillCategory
    start_ms: int
    end_ms: int
    # The parser never saw the end (or it equals the start): drawn as a mark.
    instant: bool
    summon: bool
    recovers_energy: bool


@dataclass(frozen=True, slots=True)
class RailEvent:
    """One node on a character's rail: a move, or a folded burst of them."""

    name: str
    category: SkillCategory
    start_ms: int
    end_ms: int
    # Casts folded into this event; 1 for a single move.
    count: int
    instant: bool
    summon: bool
    recovers_energy: bool


@dataclass(frozen=True, slots=True)
class TimelineLane:
    character_key: str
    character_name: str
    slot: int
    # The character's own moves in time order.
    events: tuple[RailEvent, ...]
    # Casts of entities the character summoned, in time order.
    summon_events: tuple[RailEvent, ...]
    cast_count: int


@dataclass(frozen=True, slots=True)
class Timeline:
    duration_ms: int
    lanes: tuple[TimelineLane, ...]
    blocks: tuple[CastBlock, ...]
    # Casts skipped as movement noise (dodge, dash, ...).
    hidden_count: int
    # Casts stamped outside the recorded fight, dropped or clipped to it.
    clipped_count: int


def classify_cast(skill_name: str, skill_key: str) -> SkillCategory | None:
    """Category of a cast, or None when it is movement noise to hide.

    Name-based rules first (upstream names real actions 战技 / 连携技 / 终结技
    / A1..A5), then end-anchored key rules; anything else is a real cast of
    unknown kind and stays visible as 其他.
    """

    key = skill_key.lower()
    if _NOISE_RE.search(key) or _PLUNGING_END_RE.search(key):
        return None
    display = cast_display_name(skill_name, skill_key)
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
    if _EXECUTION_RE.search(key):
        return SkillCategory.HEAVY
    if _ULTIMATE_END_RE.search(key):
        return SkillCategory.ULTIMATE
    if _COMBO_END_RE.search(key):
        return SkillCategory.COMBO
    if _NORMAL_SKILL_END_RE.search(key):
        return SkillCategory.SKILL
    if _ULT_ATTACK_END_RE.search(key) or _PLUNGING_START_RE.search(key):
        return SkillCategory.NORMAL
    if _HEAVY_END_RE.search(key):
        return SkillCategory.HEAVY
    if _ATTACK_SEGMENT_END_RE.search(key):
        return SkillCategory.NORMAL
    return SkillCategory.OTHER


def cast_display_name(skill_name: str, skill_key: str) -> str:
    """Like :func:`skill_display_name` without the loose combo-by-key rule."""

    if _COMBO_END_RE.search(skill_key):
        return "连携技"
    return skill_display_name(skill_name, skill_key, combo_from_key=False)


def build_timeline(
    export: BattleExport,
    *,
    merge_gap_ms: int = MERGE_GAP_MS,
    summon_merge_gap_ms: int = SUMMON_MERGE_GAP_MS,
) -> Timeline:
    duration = max(export.duration_ms, 1)
    blocks, hidden, clipped = _prepare_blocks(export, duration=duration)
    lanes: list[TimelineLane] = []
    for character_key, character_name, slot in _lane_order(export, blocks):
        own = [block for block in blocks if block.character_key == character_key]
        lanes.append(
            TimelineLane(
                character_key=character_key,
                character_name=character_name,
                slot=slot,
                events=_fold(
                    [block for block in own if not block.summon],
                    gap_ms=merge_gap_ms,
                ),
                summon_events=_fold(
                    [block for block in own if block.summon],
                    gap_ms=summon_merge_gap_ms,
                ),
                cast_count=len(own),
            )
        )
    return Timeline(
        duration_ms=duration,
        lanes=tuple(lanes),
        blocks=tuple(blocks),
        hidden_count=hidden,
        clipped_count=clipped,
    )


def _prepare_blocks(
    export: BattleExport,
    *,
    duration: int,
) -> tuple[list[CastBlock], int, int]:
    blocks: list[CastBlock] = []
    hidden = 0
    clipped = 0
    for cast in export.casts:
        category = classify_cast(cast.skill_name, cast.skill_key)
        if category is None:
            hidden += 1
            continue
        # Casts stamped before the timer started (or after it stopped) are
        # outside the recorded fight; whatever reaches into it is clipped.
        if cast.start_ms < 0 and (cast.end_ms is None or cast.end_ms <= 0):
            clipped += 1
            continue
        if cast.start_ms > duration:
            clipped += 1
            continue
        start = max(0, cast.start_ms)
        end = cast.end_ms if cast.end_ms is not None else start
        instant = end <= start
        if cast.start_ms < 0 or end > duration:
            clipped += 1
        end = min(end, duration)
        blocks.append(
            CastBlock(
                character_key=cast.character_key,
                name=cast_display_name(cast.skill_name, cast.skill_key),
                category=category,
                start_ms=start,
                end_ms=max(end, start),
                instant=instant,
                summon=bool(cast.source) and _SUMMON_SOURCE in cast.source.lower(),
                recovers_energy=cast.recovers_energy,
            )
        )
    blocks.sort(key=lambda block: (block.start_ms, -(block.end_ms - block.start_ms)))
    return blocks, hidden, clipped


def _lane_order(
    export: BattleExport,
    blocks: list[CastBlock],
) -> list[tuple[str, str, int]]:
    """Roster order first; a caster missing from the roster gets a lane too."""

    lanes: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for entry in sorted(export.roster, key=lambda item: item.slot):
        key = entry.character_key or entry.character_name
        if key in seen:
            continue
        seen.add(key)
        lanes.append((key, entry.character_name, entry.slot))
    extra_slot = max((slot for _, _, slot in lanes), default=0)
    for block in blocks:
        if block.character_key in seen:
            continue
        seen.add(block.character_key)
        extra_slot += 1
        lanes.append((block.character_key, block.character_key, extra_slot))
    return lanes


def _fold(blocks: list[CastBlock], *, gap_ms: int) -> tuple[RailEvent, ...]:
    """Fold bursts into single events, in time order.

    Consecutive normal-attack segments become one run (named after the attack
    when every segment carries the same real name, ``普攻`` otherwise), and the
    same move fired again within the gap becomes one node with a count.
    """

    groups: list[list[CastBlock]] = []
    for block in sorted(
        blocks, key=lambda item: (item.start_ms, -(item.end_ms - item.start_ms))
    ):
        if groups and _joins(groups[-1], block, gap_ms=gap_ms):
            groups[-1].append(block)
        else:
            groups.append([block])
    events: list[RailEvent] = []
    for group in groups:
        first = group[0]
        names = {block.name for block in group}
        name = first.name
        if first.category is SkillCategory.NORMAL and (
            len(names) > 1 or _NORMAL_ATTACK_NAME_RE.match(first.name)
        ):
            name = "普攻"
        end = max(block.end_ms for block in group)
        events.append(
            RailEvent(
                name=name,
                category=first.category,
                start_ms=first.start_ms,
                end_ms=end,
                count=len(group),
                instant=all(block.instant for block in group),
                summon=first.summon,
                recovers_energy=any(block.recovers_energy for block in group),
            )
        )
    return tuple(events)


def _joins(group: list[CastBlock], block: CastBlock, *, gap_ms: int) -> bool:
    last = group[-1]
    if block.start_ms - max(item.end_ms for item in group) > gap_ms:
        return False
    if block.category is not last.category:
        return False
    if block.category is SkillCategory.NORMAL:
        return True
    return block.name == last.name
