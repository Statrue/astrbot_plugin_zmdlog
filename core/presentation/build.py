"""养成: how far each fielded character of a battle is built.

The 详细视图 that replaced 配装 (ADR 0003). One card per character in roster
order: the level, the 潜能 as the game's potential star, the four skill
levels, the weapon — its 精炼 on the same star, then 词条 1, 词条 2 and the
weapon skill each with its level pips — and the three kinds of gear, 护甲
then 护手 then the 配件.

The game's marks are decided here, not in the template, so they are tested
as data:

- **The star** has five blades that light in a fixed order, from the
  upper-left one counter-clockwise (A → B → E → D → C). At rank k the first
  k blades are white and the next — the one to gain next — yellow; at the
  top (潜能 5, 精炼 6) all five are white and the star glows. 精炼 counts
  from 1, the weapon as it comes, so 精炼 r lights r − 1.
- **The pips**: nine a line, one a level. Reached is a yellow capsule, not
  reached a pale outline; past what the weapon's 精炼 allows a grey outline
  crossed out, 9 − cap of them. A 词条 is capped at 9; the weapon skill at
  精炼 + 3 (4 at 精炼 1, 9 at 精炼 6). MAX is level 9, and only level 9.
"""

from dataclasses import dataclass
from enum import Enum

from ..loadout import (
    element_label,
    is_raw_item_name,
    skill_level_summary,
    weapon_skill_lines,
)
from ..models import BattleDetailSummary, BattleEquip, BattleRosterEntry, BattleWeapon
from .battle import EquipStatView, ViewTabView, _equip_view
from .common import (
    _WEAPON_ICON_PATH,
    PageHeader,
    _clean_text,
    _derived_asset_url,
    _format_datetime,
    _initial,
    _safe_asset_url,
    _share,
    format_duration,
)

# A level line's length: every weapon skill and 词条 tops out at 9.
LEVEL_PIPS = 9
POTENTIAL_MAX = 5
REFINE_MAX = 6
# 精炼 1 caps the weapon skill at 4, each 精炼 after it adds one.
_REFINE_CAP_OFFSET = 3
# The order the star's blades light in, from the upper-left one
# counter-clockwise; the template draws each by its letter.
STAR_BLADES = ("A", "B", "E", "D", "C")
_SKILL_SLOTS = ("普攻", "战技", "连携", "终结")
# The gear in the game's order: the armour, the gloves, then the kits.
_GEAR_ORDER = ("护甲", "护手")
_GEAR_PLACES = 4
_UNNAMED = "名称未收录"


class BladeState(str, Enum):
    """One blade of the potential star; the value is its class on the page."""

    LIT = "lit"
    # The blade gained next: yellow.
    NEXT = "lead"
    DARK = "base"


class PipState(str, Enum):
    """One level of a weapon line; the value names its drawing."""

    ON = "on"
    OFF = "off"
    # Past the cap this 精炼 allows: crossed out.
    LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class StarView:
    """The potential star: each blade's state in STAR_BLADES order."""

    blades: tuple[BladeState, ...]
    # At the top rank: every blade white, the star aglow.
    full: bool


@dataclass(frozen=True, slots=True)
class WeaponLineView:
    """词条 1, 词条 2 or the weapon skill: its name and its level of nine."""

    name: str
    level: int
    cap: int
    pips: tuple[PipState, ...]
    level_label: str
    # MAX is printed at level 9 only.
    maxed: bool
    at_cap: bool
    is_weapon_skill: bool


@dataclass(frozen=True, slots=True)
class WeaponBuildView:
    name: str
    icon_url: str | None
    # None when the upload did not record it; the page leaves the star out.
    refine: int | None
    refine_star: StarView | None
    refine_label: str | None
    lines: tuple[WeaponLineView, ...]


@dataclass(frozen=True, slots=True)
class GearTileView:
    part_name: str
    # The piece's name; never a raw id (名称未收录, or its suit and part
    # when the catalog knows the suit).
    label: str
    enhance: str | None
    icon_url: str | None
    stats: tuple[EquipStatView, ...]


@dataclass(frozen=True, slots=True)
class SkillSlotView:
    label: str
    level: int | None


@dataclass(frozen=True, slots=True)
class BuildCharacterView:
    slot_label: str
    character_name: str
    character_initial: str
    avatar_url: str | None
    profession: str
    element: str | None
    damage_share: str | None
    # The main C: the highest DPS of the battle.
    lead: bool
    level: int | None
    potential: int | None
    potential_star: StarView
    skill_levels: tuple[SkillSlotView, ...]
    weapon: WeaponBuildView | None
    # 护甲, 护手, 配件, 配件; None for a place the upload left empty.
    gear: tuple[GearTileView | None, ...]


@dataclass(frozen=True, slots=True)
class BattleBuildPage:
    header: PageHeader
    uploader_display_name: str
    duration: str
    battle_date: str
    characters: tuple[BuildCharacterView, ...]
    views: tuple[ViewTabView, ...]


def build_battle_build_page(
    battle: BattleDetailSummary,
    *,
    query: str,
    web_base_url: str,
    suits: dict[str, str] | None = None,
    views: tuple[tuple[str, bool], ...] = (),
) -> BattleBuildPage:
    """养成; ``views`` is the strip of the battle's pages, ``(label, current)``."""

    damage = {
        participant.character_name: participant.total_damage
        for participant in battle.participants
    }
    lead = max(battle.participants, key=lambda item: item.dps, default=None)
    return BattleBuildPage(
        header=PageHeader(
            title=battle.boss_name,
            subtitle=battle.dungeon_name,
            query=query,
            matched_name=battle.battle_id,
            target_type="战报养成",
            footer_note="公开战报 · 上传时记录的养成",
        ),
        uploader_display_name=battle.uploader_display_name,
        duration=format_duration(battle.duration_ms),
        battle_date=_format_datetime(battle.battle_end_at),
        characters=tuple(
            _character(
                entry,
                dealt=damage.get(entry.character_name),
                total_damage=battle.total_damage,
                lead=lead is not None and entry.character_name == lead.character_name,
                suits=suits or {},
                web_base_url=web_base_url,
            )
            for entry in sorted(battle.roster, key=lambda item: item.slot)
        ),
        views=tuple(ViewTabView(label, current) for label, current in views),
    )


def star_view(lit: int | None, *, top: int) -> StarView:
    """The star at ``lit`` blades of ``top``; all dark when not recorded."""

    if lit is None:
        return StarView(blades=(BladeState.DARK,) * len(STAR_BLADES), full=False)
    lit = max(0, min(lit, len(STAR_BLADES)))
    return StarView(
        blades=tuple(
            BladeState.LIT
            if index < lit
            else BladeState.NEXT
            if index == lit
            else BladeState.DARK
            for index in range(len(STAR_BLADES))
        ),
        full=lit >= top,
    )


def _character(
    entry: BattleRosterEntry,
    *,
    dealt: int | None,
    total_damage: int,
    lead: bool,
    suits: dict[str, str],
    web_base_url: str,
) -> BuildCharacterView:
    levels = {level.label: level.level for level in skill_level_summary(entry)}
    potential = entry.character_potential
    return BuildCharacterView(
        slot_label=f"{entry.slot:02d}",
        character_name=entry.character_name,
        character_initial=_initial(entry.character_name),
        avatar_url=_safe_asset_url(entry.character_avatar_url, base_url=web_base_url),
        profession=_clean_text(entry.character_profession),
        element=element_label(entry.character_element),
        damage_share=(
            _share(dealt, total_damage)
            if dealt is not None and total_damage > 0
            else None
        ),
        lead=lead,
        level=entry.character_level or None,
        potential=potential,
        potential_star=star_view(potential, top=POTENTIAL_MAX),
        skill_levels=tuple(
            SkillSlotView(label=label, level=levels.get(label))
            for label in _SKILL_SLOTS
        ),
        weapon=(
            _weapon(entry.weapon, web_base_url=web_base_url)
            if entry.weapon is not None
            else None
        ),
        gear=_gear(entry.equips, suits, web_base_url=web_base_url),
    )


def _weapon(weapon: BattleWeapon, *, web_base_url: str) -> WeaponBuildView:
    refine = weapon.refine
    skill_cap = (
        min(refine + _REFINE_CAP_OFFSET, LEVEL_PIPS) if refine else LEVEL_PIPS
    )
    return WeaponBuildView(
        name=_clean_text(weapon.name) or "武器未记录",
        icon_url=_derived_asset_url(
            weapon.icon_url,
            _WEAPON_ICON_PATH,
            weapon.template,
            web_base_url=web_base_url,
        ),
        refine=refine,
        refine_star=(
            star_view(refine - 1, top=REFINE_MAX - 1) if refine else None
        ),
        refine_label=(
            ("MAX" if refine >= REFINE_MAX else str(refine)) if refine else None
        ),
        lines=tuple(
            _line(
                line.name,
                line.level,
                cap=skill_cap if line.is_weapon_skill else LEVEL_PIPS,
                is_weapon_skill=line.is_weapon_skill,
            )
            for line in weapon_skill_lines(weapon)
        ),
    )


def _line(name: str, level: int, *, cap: int, is_weapon_skill: bool) -> WeaponLineView:
    # A level past the cap is the upload contradicting its own 精炼 (seen on
    # the boards): what it recorded is drawn reached, never crossed out.
    level = max(0, min(level, LEVEL_PIPS))
    cap = max(cap, level)
    return WeaponLineView(
        name=name,
        level=level,
        cap=cap,
        pips=tuple(
            PipState.ON
            if index < level
            else PipState.OFF
            if index < cap
            else PipState.LOCKED
            for index in range(LEVEL_PIPS)
        ),
        level_label=f"{level}/{cap}",
        maxed=level >= LEVEL_PIPS,
        at_cap=level >= cap,
        is_weapon_skill=is_weapon_skill,
    )


def _gear(
    equips: tuple[BattleEquip, ...],
    suits: dict[str, str],
    *,
    web_base_url: str,
) -> tuple[GearTileView | None, ...]:
    """护甲, 护手, then the rest in upload order, padded to four places."""

    def place(equip: BattleEquip) -> int:
        part = _clean_text(equip.part_name)
        return _GEAR_ORDER.index(part) if part in _GEAR_ORDER else len(_GEAR_ORDER)

    # Stable on the slot order, so the kits keep the order they came in.
    ordered = sorted(sorted(equips, key=lambda item: item.slot), key=place)
    tiles: list[GearTileView | None] = [
        _gear_tile(equip, suits, web_base_url=web_base_url) for equip in ordered
    ]
    tiles += [None] * (_GEAR_PLACES - len(tiles))
    return tuple(tiles)


def _gear_tile(
    equip: BattleEquip, suits: dict[str, str], *, web_base_url: str
) -> GearTileView:
    view = _equip_view(equip, suits, web_base_url=web_base_url)
    label = view.piece_label
    if is_raw_item_name(equip.piece_name, equip.item_id):
        label = view.compact_label if view.suit_label else _UNNAMED
    levels = [level for _, level in equip.enhance_levels]
    return GearTileView(
        part_name=view.part_name,
        label=label,
        enhance=" / ".join(f"+{level}" for level in levels) if levels else None,
        icon_url=view.icon_url,
        stats=view.stats,
    )
