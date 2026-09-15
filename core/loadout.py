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
# ``A5 派生`` / ``A4-2 派生（格挡）``: a sub-hit of the basic chain, in the
# A-notation upstream uses for it. Keys spell the segment both ways
# (``attack5_projhit`` and ``attack_5_projhit``), so the name is the
# reliable signal for the category; folding into 普攻（各段合并） stays
# limited to the bare segments.
_NORMAL_ATTACK_FAMILY_RE = re.compile(r"^A[1-9](?:-\d+)?\b", re.IGNORECASE)
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
    # 庄方宜 战技·惊霆诀: the 青霆剑 it leaves behind strike the target one
    # by one, one row per strike. Named by the user on 2026-09-05.
    "buff_chr_0030_zhuangfy_sword_triggerd": "青霆剑",
    # The 燃烧 status tick itself (狼卫's 灼热獠牙 talent only boosts it).
    "buff_common_burning_status": "燃烧",
    # 卡缪 战技·驱火焚影 summons 衔火血翼, which hovers, strikes and bursts.
    "chr_0033_camille_skill_213": "战技 · 衔火血翼",
    # 诀 连携技·应龙四式 sets four 战术分身 on the target; the three numbered
    # skills are that follow-up in her different forms (阵诀·智 / 阵诀·意),
    # so they fold into one row on purpose.
    "chr_0032_lizhiyan_skill_3782": "连携技 · 战术分身",
    "chr_0032_lizhiyan_skill_3423": "连携技 · 战术分身",
    "chr_0032_lizhiyan_skill_3090": "连携技 · 战术分身",
}
_SKILL_NAME_OVERRIDES = {
    "cryst triggered physical break": "寒冷击破触发",
}
# Upstream element keys as the game names them; unknown keys are shown raw.
_ELEMENT_LABELS = {
    "physical": "物理",
    "fire": "灼热",
    "cryst": "寒冷",
    "crystal": "寒冷",
    "natural": "自然",
    "pulse": "电磁",
    "spell": "法术",
}
# The anomaly an element applies when it is triggered by another one
# (parser_core cross_element_trigger_match).
_ANOMALY_BY_ELEMENT = {
    "fire": "燃烧",
    "pulse": "导电",
    "cryst": "冻结",
    "natural": "腐蚀",
}
# A damage row whose upstream name still carries a word like this was named
# from its key, not from the game's text table.
_ASCII_WORD_RE = re.compile(r"[A-Za-z]{3,}")
# ``buff_chr_0030_zhuangfy_sword_triggerd`` / ``buff_common_burning_status``.
_BUFF_KEY_RE = re.compile(r"^buff_(?:common_|chr_\d+_[a-z0-9]+_)", re.IGNORECASE)
_KEY_DIGITS_RE = re.compile(r"^\d+$")
_ATTACK_TOKEN_RE = re.compile(r"^attack(\d+)$", re.IGNORECASE)
# The skill family a raw key starts with, longest phrase first. Upstream's
# own prettifier translates ``normal`` on its own and leaves ``skill``, which
# is how a 战技 sub-hit came to be printed as ``普攻 / skill / 派生``.
_SKILL_FAMILIES = (
    (("normal", "skill"), "战技"),
    (("ultimate", "skill"), "终结技"),
    (("ultimate",), "终结技"),
    (("combo", "skill"), "连携技"),
    (("combo",), "连携技"),
    (("power", "attack"), "重击"),
    (("plunging", "attack"), "下落攻击"),
    (("dash", "attack"), "闪避攻击"),
)
# What the remaining segments of a key mean. The first block is upstream's
# own table (parser_core ``_humanize_skill_suffix_text``); the second is the
# tokens it leaves untranslated that the public boards actually show.
_KEY_TOKEN_LABELS = {
    "normal": "普攻",
    "combo": "连携",
    "absorb": "吸收",
    "air": "空中",
    "airborne": "浮空",
    "attack": "攻击",
    "blocked": "格挡",
    "bleed": "流血",
    "break": "击破",
    "damage": "伤害",
    "drone": "无人机",
    "effect": "效果",
    "entity": "实体",
    "extra": "额外",
    "fx": "特效",
    "hit": "命中",
    "loop": "循环",
    "move": "移动",
    "projhit": "派生",
    "range": "范围",
    "remain": "持续",
    "self": "自身",
    "sheep": "绵羊",
    "shockwave": "冲击波",
    "spawn": "召唤",
    "start": "起手",
    "talent": "天赋",
    "delay": "延迟",
    "persistentdamage": "持续伤害",
    "soundwave": "声波",
    "floating": "浮空",
    "triggered": "触发",
    "triggerd": "触发",
    "status": "状态",
    "burning": "燃烧",
    "weakness": "脆弱",
    "phantom": "幻影",
    **_ELEMENT_LABELS,
}
_SIDE_LABELS = {"l": "左", "r": "右"}
_CHINESE_ORDINALS = "零一二三四五六七八九"
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
        return _humanise_key(skill_key or "") or skill_key or "未命名技能"
    if (
        _looks_like_raw_key(name)
        or _ASCII_WORD_RE.search(name)
        or _looks_like_token_join(name)
    ):
        # Named from a key rather than from the game's text table. When the
        # name is upstream's segment-by-segment reading of a key whose shape
        # we understand, the key says what the row is; a name that carries
        # text of its own (``塞什卡的秘传 / phantom``, ``召唤 / pet``) keeps
        # it and only has its English words read.
        if _named_from_key(name, skill_key):
            humanised = _humanise_key(skill_key or name)
            if humanised:
                return humanised
        return _translate_name_segments(name)
    return name


def _looks_like_token_join(name: str) -> str | None:
    """``连携 / 02 / 派生``: every segment a token label or a number.

    Such a name carries no English word to trip the other check, but it is
    still upstream's segment join and reads better from the key.
    """

    if " / " not in name:
        return None
    labels = set(_KEY_TOKEN_LABELS.values())
    segments = [segment.strip() for segment in name.split("/")]
    return all(
        segment in labels or _KEY_DIGITS_RE.match(segment) for segment in segments
    ) or None


def _named_from_key(name: str, skill_key: str | None) -> bool:
    """Whether upstream built ``name`` by translating ``skill_key`` token by token.

    Upstream joins one word per key segment, so the counts match and every
    Chinese word is one of its token labels. A name with a different shape
    was resolved from somewhere else and must not be replaced by the key.
    """

    if _looks_like_raw_key(name):
        return True
    body = _key_body(skill_key or "")
    if body is None:
        return False
    # One word per raw segment: ``combo_skillfloating`` is two words to
    # upstream even though it is read here as three tokens.
    segments = [segment for segment in body.split("_") if segment]
    words = [word for word in re.split(r"[\s/]+", name) if word]
    if len(words) != len(segments):
        return False
    labels = set(_KEY_TOKEN_LABELS.values())
    return all(not _CJK_RE.search(word) or word in labels for word in words)


def _key_body(key: str) -> str | None:
    """The segments of a character or buff key after its owner prefix."""

    lowered = key.strip().lower()
    if lowered.startswith("buff_"):
        if not _BUFF_KEY_RE.match(lowered):
            return None
        return _BUFF_KEY_RE.sub("", lowered)
    if _CHARACTER_KEY_PREFIX_RE.match(lowered):
        return _CHARACTER_KEY_PREFIX_RE.sub("", lowered)
    return None


def _translate_name_segments(name: str) -> str:
    """Read the English words of an upstream-prettified name, keep the rest."""

    segments = [segment.strip() for segment in name.split("/")]
    translated = []
    for segment in segments:
        words = segment.split()
        if words and all(word.lower() in _KEY_TOKEN_LABELS for word in words):
            translated.append("".join(_KEY_TOKEN_LABELS[w.lower()] for w in words))
        else:
            translated.append(segment)
    if len(segments) == 1 and not _CJK_RE.search(name):
        # A bare raw key with a shape we did not recognise: the old tidy.
        stripped = _CHARACTER_KEY_PREFIX_RE.sub("", name).replace("_", " ").strip()
        return translated[0] if translated[0] != name else (stripped or name)
    return " / ".join(translated)


def _humanise_key(key: str) -> str | None:
    """Read a raw skill or buff key the way its segments are meant.

    Mirrors parser_core's families and token table, then goes one step
    further for the shapes the boards actually show: ``normal_skill`` is one
    phrase (战技), a trailing character token on a same-element burst is
    dropped, and ``attackN`` inside a family reads as the N-th hit. Returns
    None when the key is not a character or buff key at all.
    """

    lowered = key.strip().lower()
    body = _key_body(lowered)
    if body is None:
        return None
    if lowered.startswith("buff_common_"):
        reaction = _reaction_name(body)
        if reaction:
            return reaction
    tokens = _split_key_tokens(body)
    if not tokens:
        return None
    family = None
    for phrase, label in _SKILL_FAMILIES:
        if tuple(tokens[: len(phrase)]) == phrase:
            family, tokens = label, tokens[len(phrase) :]
            break
    if family is None and len(tokens) == 2 and tokens[0] == "skill":
        if _KEY_DIGITS_RE.match(tokens[1]):
            return f"技能 {int(tokens[1])}"
    rest = _render_key_tokens(tokens, in_family=family is not None)
    if family and rest:
        return f"{family} · {rest}"
    return family or rest or None


def _reaction_name(body: str) -> str | None:
    """parser_core's names for ``buff_common_<element>…_triggered`` rows.

    The same-element burst tolerates a trailing character token, which is
    how 提弗洛斯's own 自然爆发 arrives (``…_natural_natural_triggered_typhoea``).
    """

    tokens = body.split("_")
    if len(tokens) >= 3 and tokens[2] == "triggered" and tokens[0] in _ELEMENT_LABELS:
        applied, source = tokens[0], tokens[1]
        if applied == source:
            return f"{_ELEMENT_LABELS[applied]}爆发"
        if source in _ELEMENT_LABELS and len(tokens) == 3:
            return _ANOMALY_BY_ELEMENT.get(applied)
    if len(tokens) >= 2 and tokens[1] == "triggered" and tokens[0] in _ELEMENT_LABELS:
        # ``natural_triggered``: the element being put on the target (自然附着),
        # confirmed in play for 诀 and 洁尔佩塔; ``_fx`` / ``_start`` tails
        # read on as usual.
        tail = _render_key_tokens(tokens[2:], in_family=False)
        return f"{_ELEMENT_LABELS[tokens[0]]}附着{tail}"
    return None


def _split_key_tokens(body: str) -> list[str]:
    tokens: list[str] = []
    for token in body.split("_"):
        if not token:
            continue
        # ``skillfloating``: two words the key glued together.
        if token.startswith("skill") and token[5:] in _KEY_TOKEN_LABELS:
            tokens.extend(("skill", token[5:]))
        else:
            tokens.append(token)
    return tokens


def _render_key_tokens(tokens: list[str], *, in_family: bool) -> str:
    """Join translated segments the way a Chinese label reads.

    Inside a family ``attack2`` is the second hit (二段); on its own it is
    written the way upstream writes the basic chain (``A1-01``). A variant
    number goes last, after the words that describe the hit.
    """

    pieces: list[str] = []
    numbers: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        attack = _ATTACK_TOKEN_RE.match(token)
        if attack and in_family:
            hit = int(attack.group(1))
            ordinal = (
                _CHINESE_ORDINALS[hit] if hit < len(_CHINESE_ORDINALS) else str(hit)
            )
            pieces.append(f"{ordinal}段")
        elif attack:
            label = f"A{int(attack.group(1))}"
            if index + 1 < len(tokens) and _KEY_DIGITS_RE.match(tokens[index + 1]):
                label += f"-{tokens[index + 1]}"
                index += 1
            pieces.append(f" {label} ")
        elif _KEY_DIGITS_RE.match(token):
            numbers.append(str(int(token)))
        elif token in _SIDE_LABELS:
            pieces.append(f"（{_SIDE_LABELS[token]}）")
        elif token in _KEY_TOKEN_LABELS:
            pieces.append(_KEY_TOKEN_LABELS[token])
        else:
            pieces.append(f" {token} ")
        index += 1
    text = " ".join("".join(pieces).split())
    if numbers:
        text = f"{text} {' '.join(numbers)}".strip()
    return text


def skill_category(skill_name: str, skill_key: str | None) -> SkillCategory:
    """Bucket one skill-stat row, mirroring the site's category rules."""

    display = skill_display_name(skill_name, skill_key)
    # ``战技 · 派生`` is still the 战技; the family is what buckets a row.
    family = display.split(" · ", 1)[0]
    if _NORMAL_ATTACK_NAME_RE.match(display) or _NORMAL_ATTACK_FAMILY_RE.match(display):
        return SkillCategory.NORMAL
    if family == "战技":
        return SkillCategory.SKILL
    if family == "连携技":
        return SkillCategory.COMBO
    if family in {"重击", "处决"}:
        return SkillCategory.HEAVY
    if family == "终结技":
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


def battle_suit_ids(*battles) -> tuple[str, ...]:
    """Every suit the pieces of these battles belong to, for the catalog read.

    A suit the catalog does not name yet is what a new suit looks like; the
    data source re-reads the catalog for it, once in a while.
    """

    ids: dict[str, None] = {}
    for battle in battles:
        for entry in battle.roster:
            for equip in entry.equips:
                suit_id = suit_catalog_id(equip.item_id)
                if suit_id:
                    ids.setdefault(suit_id, None)
    return tuple(ids)


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
