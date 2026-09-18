"""The battle card and its loadout / skill pages."""

from dataclasses import dataclass

from ..contract import group_contract_tags, tag_display_name, tag_short_name
from ..loadout import (
    CharacterSkillDamage,
    element_label,
    group_skill_damage,
    is_raw_item_name,
    skill_level_summary,
    stat_label,
    suit_catalog_id,
    weapon_skill_levels,
)
from ..models import (
    BattleDetailSummary,
    BattleEquip,
    BattleExport,
    BattleWeapon,
)
from .charts import (
    BuffBandView,
    DpsCurveView,
    build_buff_band_view,
    build_dps_curve_view,
)
from .common import (
    _EQUIP_ICON_PATH,
    _WEAPON_ICON_PATH,
    PageHeader,
    _bar_width,
    _clean_text,
    _derived_asset_url,
    _format_datetime,
    _format_stat_value,
    _initial,
    _safe_asset_url,
    _share,
    format_duration,
    format_number,
    public_url,
)
from .rail import (
    _RAIL_CARD_MAX_PPS,
    _RAIL_CARD_MIN_PPS,
    _RAIL_CARD_TARGET_PX,
    TimelineView,
    build_timeline_view,
)


@dataclass(frozen=True, slots=True)
class BattleParticipantView:
    character_name: str
    character_profession: str
    character_initial: str
    character_avatar_url: str | None
    dps: str
    rdps: str
    total_damage: str
    damage_share: str
    rdps_share: str
    dps_share_percent: float
    rdps_share_percent: float
    max_hit: str
    crit_rate: str


@dataclass(frozen=True, slots=True)
class EquipStatView:
    name: str
    value: str
    is_main: bool


@dataclass(frozen=True, slots=True)
class EquipView:
    part_name: str
    # The real piece name, or a placeholder when upstream only had the item id.
    piece_label: str
    # Upstream's own id for the piece. The labels are display text and
    # upstream contradicts itself about them; this is what two pages compare.
    item_id: str | None
    suit_label: str | None
    icon_url: str | None
    enhance_label: str | None
    stats: tuple[EquipStatView, ...]
    # One-line form for the compact card, e.g. "动火用 · 护甲".
    compact_label: str


@dataclass(frozen=True, slots=True)
class WeaponView:
    name: str
    icon_url: str | None
    refine_label: str | None
    level_label: str | None
    skill_label: str | None


@dataclass(frozen=True, slots=True)
class SkillLevelView:
    label: str
    level: int


@dataclass(frozen=True, slots=True)
class SkillRowView:
    category: str
    name: str
    cast_count: int
    total_damage: str
    avg_damage: str
    max_damage: str
    # Share of this character's own skill-stat total.
    share: str
    share_width: float
    merged: bool


@dataclass(frozen=True, slots=True)
class LoadoutView:
    slot: int
    character_name: str
    character_initial: str
    character_avatar_url: str | None
    profession: str
    element: str | None
    level_label: str | None
    potential_label: str | None
    weapon: WeaponView | None
    equips: tuple[EquipView, ...]
    skill_levels: tuple[SkillLevelView, ...]
    # Heaviest damage sources of this character, for the compact card.
    top_skills: tuple[SkillRowView, ...]
    damage_share: str | None


@dataclass(frozen=True, slots=True)
class LoadoutPage:
    header: PageHeader
    battle_id: str
    report_url: str
    uploader_display_name: str
    duration: str
    total_dps: str
    total_damage: str
    battle_date: str
    loadouts: tuple[LoadoutView, ...]
    stat_lines_available: bool


@dataclass(frozen=True, slots=True)
class SkillGroupView:
    character_name: str
    character_initial: str
    character_avatar_url: str | None
    profession: str
    total_damage: str
    team_share: str
    team_share_width: float
    rows: tuple[SkillRowView, ...]
    hidden_count: int


@dataclass(frozen=True, slots=True)
class SkillPage:
    header: PageHeader
    battle_id: str
    report_url: str
    uploader_display_name: str
    duration: str
    total_dps: str
    total_damage: str
    battle_date: str
    groups: tuple[SkillGroupView, ...]
    has_merged_rows: bool


@dataclass(frozen=True, slots=True)
class ContractTagView:
    name: str
    score: int
    icon_url: str | None
    # Shown on the tile when there is no sprite (upstream's iconUrl is null
    # for some tags) or it fails to load — the avatar's initial, for a tag.
    initial: str


@dataclass(frozen=True, slots=True)
class ContractGroupView:
    family: str
    tags: tuple[ContractTagView, ...]
    count: int
    score: int


@dataclass(frozen=True, slots=True)
class BattlePage:
    header: PageHeader
    battle_id: str
    report_url: str
    account_id: str
    uploader_display_name: str
    duration: str
    total_dps: str
    total_damage: str
    battle_date: str
    timer_label: str
    integrity_label: str
    contract_score: str | None
    participants: tuple[BattleParticipantView, ...]
    # "rDPS 榜记录" when this upload made the board's rDPS ranking; empty
    # otherwise, including when the payload does not say.
    rdps_ranking_label: str = ""
    loadouts: tuple[LoadoutView, ...] = ()
    skill_stats_available: bool = False
    # The cast rail, when the public export was available; otherwise a short
    # reason (old upload, rate limit) or nothing at all.
    timeline: TimelineView | None = None
    timeline_note: str | None = None
    # Both read from the battle's own per-hit telemetry; None when the upload
    # carried none (older parsers) or nothing survived parsing.
    dps_curve: DpsCurveView | None = None
    buff_band: BuffBandView | None = None
    # 危机合约 only: the record's own tags by family. Name and score, nothing
    # more — no tier (the id's last digit is not reliably the score) and no
    # description (an unexpandable template, UPSTREAM.md). Empty off the
    # contract board.
    contract_groups: tuple[ContractGroupView, ...] = ()


def build_battle_page(
    battle: BattleDetailSummary,
    *,
    query: str,
    web_base_url: str,
    export: BattleExport | None = None,
    export_note: str | None = None,
    suits: dict[str, str] | None = None,
) -> BattlePage:
    """Build the battle card; ``export`` adds the cast rail when available."""

    timer_is_official = (
        battle.time_source == "game_timer"
        and battle.official_timer_start_seen is True
        and battle.official_timer_end_seen is True
    )
    total_damage = battle.total_damage
    # Highest DPS first, mirroring the site's contribution breakdown.
    participants = sorted(
        battle.participants,
        key=lambda participant: participant.dps,
        reverse=True,
    )
    total_rdps = sum(max(participant.rdps, 0.0) for participant in participants)
    return BattlePage(
        header=PageHeader(
            title=battle.boss_name,
            subtitle=battle.dungeon_name,
            query=query,
            matched_name=battle.battle_id,
            target_type="公开战报",
            footer_note="公开战报 · DPS / rDPS",
        ),
        battle_id=battle.battle_id,
        report_url=public_url(
            web_base_url,
            "battle",
            battle.battle_id,
        ),
        account_id=battle.uploader_user_id,
        uploader_display_name=battle.uploader_display_name,
        duration=format_duration(battle.duration_ms),
        total_dps=format_number(battle.total_dps),
        total_damage=format_number(total_damage),
        battle_date=_format_datetime(battle.battle_end_at),
        timer_label="官方计时" if timer_is_official else "计时待核验",
        integrity_label=(
            "结构校验通过" if battle.integrity_verified else "结构校验未通过"
        ),
        rdps_ranking_label="rDPS 榜记录" if battle.rdps_ranking_eligible else "",
        contract_score=(
            format_number(battle.contract_tag_score)
            if battle.contract_tag_score is not None
            else None
        ),
        loadouts=_build_loadouts(
            battle,
            web_base_url=web_base_url,
            top_skills=_MAX_CARD_SKILLS,
            suits=suits,
        ),
        skill_stats_available=bool(battle.skill_stats),
        timeline=(
            build_timeline_view(
                export,
                web_base_url=web_base_url,
                target_height=_RAIL_CARD_TARGET_PX,
                min_pps=_RAIL_CARD_MIN_PPS,
                max_pps=_RAIL_CARD_MAX_PPS,
            )
            if export is not None
            else None
        ),
        timeline_note=export_note if export is None else None,
        dps_curve=build_dps_curve_view(battle, tuple(participants)),
        buff_band=build_buff_band_view(battle),
        contract_groups=_build_contract_groups(battle, web_base_url=web_base_url),
        participants=tuple(
            BattleParticipantView(
                character_name=participant.character_name,
                character_profession=participant.character_profession or "",
                character_initial=_initial(participant.character_name),
                character_avatar_url=_safe_asset_url(
                    participant.character_avatar_url,
                    base_url=web_base_url,
                ),
                dps=format_number(participant.dps),
                rdps=format_number(participant.rdps),
                total_damage=format_number(participant.total_damage),
                damage_share=(
                    _share(participant.total_damage, total_damage)
                    if total_damage > 0
                    else "—"
                ),
                rdps_share=(
                    _share(participant.rdps, total_rdps)
                    if total_rdps > 0
                    else "—"
                ),
                dps_share_percent=(
                    round(participant.total_damage / total_damage * 100, 2)
                    if total_damage > 0
                    else 0.0
                ),
                rdps_share_percent=(
                    round(max(participant.rdps, 0.0) / total_rdps * 100, 2)
                    if total_rdps > 0
                    else 0.0
                ),
                max_hit=(
                    format_number(participant.max_hit)
                    if participant.max_hit is not None
                    else "—"
                ),
                crit_rate=(
                    f"{format_number(round(participant.crit_rate * 100, 1))}%"
                    if participant.crit_rate is not None
                    else "—"
                ),
            )
            for participant in participants
        ),
    )


_MAX_CARD_SKILLS = 3


_MAX_SKILL_ROWS = 12


def build_loadout_page(
    battle: BattleDetailSummary,
    *,
    query: str,
    web_base_url: str,
    suits: dict[str, str] | None = None,
) -> LoadoutPage:
    """Every deployed character's weapon, gear lines and skill levels."""

    loadouts = _build_loadouts(
        battle,
        web_base_url=web_base_url,
        top_skills=_MAX_CARD_SKILLS,
        suits=suits,
    )
    return LoadoutPage(
        header=PageHeader(
            title=battle.boss_name,
            subtitle=battle.dungeon_name,
            query=query,
            matched_name=battle.battle_id,
            target_type="战报配装",
            footer_note="公开战报 · 上传时记录的阵容配装",
        ),
        **_report_facts(battle, web_base_url),
        loadouts=loadouts,
        stat_lines_available=any(
            equip.stats for load in loadouts for equip in load.equips
        ),
    )


def build_skill_page(
    battle: BattleDetailSummary,
    *,
    query: str,
    web_base_url: str,
) -> SkillPage:
    """Per-character damage by skill, heaviest first."""

    groups = group_skill_damage(battle.skill_stats)
    grand_total = sum(group.total_damage for group in groups)
    identities = _roster_identities(battle)
    views: list[SkillGroupView] = []
    for group in groups:
        avatar_url, profession = identities.get(group.character_name, (None, ""))
        rows = _skill_rows(group, limit=_MAX_SKILL_ROWS)
        views.append(
            SkillGroupView(
                character_name=group.character_name,
                character_initial=_initial(group.character_name),
                character_avatar_url=_safe_asset_url(
                    avatar_url, base_url=web_base_url
                ),
                profession=profession,
                total_damage=format_number(group.total_damage),
                team_share=_share(group.total_damage, grand_total),
                team_share_width=_bar_width(group.total_damage, grand_total),
                rows=rows,
                hidden_count=max(0, len(group.rows) - len(rows)),
            )
        )
    return SkillPage(
        header=PageHeader(
            title=battle.boss_name,
            subtitle=battle.dungeon_name,
            query=query,
            matched_name=battle.battle_id,
            target_type="技能统计",
            footer_note="公开战报 · 各角色技能伤害统计",
        ),
        **_report_facts(battle, web_base_url),
        groups=tuple(views),
        has_merged_rows=any(
            row.merged_count > 1 for group in groups for row in group.rows
        ),
    )


def _build_contract_groups(
    battle: BattleDetailSummary,
    *,
    web_base_url: str,
) -> tuple[ContractGroupView, ...]:
    return tuple(
        ContractGroupView(
            family=group.family,
            tags=tuple(
                ContractTagView(
                    name=tag_display_name(tag),
                    score=tag.score,
                    icon_url=_safe_asset_url(tag.icon_url, base_url=web_base_url),
                    # The character after the family prefix: 热 for 改写：热量汲取.
                    initial=_initial(tag_short_name(tag)),
                )
                for tag in group.tags
            ),
            count=len(group.tags),
            score=group.score,
        )
        for group in group_contract_tags(battle.contract_tags)
    )


def _build_loadouts(
    battle: BattleDetailSummary,
    *,
    web_base_url: str | None,
    top_skills: int,
    suits: dict[str, str] | None,
) -> tuple[LoadoutView, ...]:
    suits = suits or {}
    groups = {
        group.character_name: group
        for group in group_skill_damage(battle.skill_stats)
    }
    damage = {
        participant.character_name: participant.total_damage
        for participant in battle.participants
    }
    views: list[LoadoutView] = []
    for entry in sorted(battle.roster, key=lambda item: item.slot):
        group = groups.get(entry.character_name)
        dealt = damage.get(entry.character_name)
        views.append(
            LoadoutView(
                slot=entry.slot,
                character_name=entry.character_name,
                character_initial=_initial(entry.character_name),
                character_avatar_url=_safe_asset_url(
                    entry.character_avatar_url, base_url=web_base_url
                ),
                profession=_clean_text(entry.character_profession),
                element=element_label(entry.character_element),
                level_label=(
                    f"Lv.{entry.character_level}"
                    if entry.character_level is not None
                    else None
                ),
                potential_label=(
                    f"潜能 {entry.character_potential}"
                    if entry.character_potential is not None
                    else None
                ),
                weapon=(
                    _weapon_view(entry.weapon, web_base_url=web_base_url)
                    if entry.weapon is not None
                    else None
                ),
                equips=tuple(
                    _equip_view(equip, suits, web_base_url=web_base_url)
                    for equip in sorted(entry.equips, key=lambda item: item.slot)
                ),
                skill_levels=tuple(
                    SkillLevelView(label=level.label, level=level.level)
                    for level in skill_level_summary(entry)
                ),
                top_skills=(
                    _skill_rows(group, limit=top_skills) if group else ()
                ),
                damage_share=(
                    _share(dealt, battle.total_damage)
                    if dealt is not None and battle.total_damage > 0
                    else None
                ),
            )
        )
    return tuple(views)


def _weapon_view(weapon: BattleWeapon, *, web_base_url: str | None) -> WeaponView:
    own_level, affix_levels = weapon_skill_levels(weapon)
    parts: list[str] = []
    if own_level is not None:
        parts.append(f"武器技能 {own_level}")
    if affix_levels:
        parts.append("词条 " + " / ".join(str(level) for level in affix_levels))
    return WeaponView(
        name=_clean_text(weapon.name) or "武器未记录",
        icon_url=_derived_asset_url(
            weapon.icon_url,
            _WEAPON_ICON_PATH,
            weapon.template,
            web_base_url=web_base_url,
        ),
        refine_label=f"精炼 {weapon.refine}" if weapon.refine is not None else None,
        level_label=f"Lv.{weapon.level}" if weapon.level else None,
        skill_label=" · ".join(parts) if parts else None,
    )


def _equip_view(
    equip: BattleEquip,
    suits: dict[str, str],
    *,
    web_base_url: str | None,
) -> EquipView:
    part_name = _clean_text(equip.part_name) or "装备"
    raw_name = is_raw_item_name(equip.piece_name, equip.item_id)
    # The catalog is keyed by the id the piece carries and says the same
    # thing in every battle. ``suitName`` is filled per upload, contradicts
    # itself across characters of one fight and has been seen naming a
    # different suit outright, so it is only the fallback.
    catalog_id = suit_catalog_id(equip.item_id)
    suit_name = suits.get(catalog_id, "") if catalog_id else ""
    if not suit_name:
        suit_name = _clean_text(equip.suit_name)
    piece_label = "名称未收录" if raw_name else _clean_text(equip.piece_name)
    if suit_name:
        compact_label = f"{suit_name} · {part_name}"
    elif raw_name:
        compact_label = f"{part_name}（未收录）"
    else:
        compact_label = piece_label
    levels = tuple(level for _, level in equip.enhance_levels)
    return EquipView(
        part_name=part_name,
        piece_label=piece_label,
        item_id=equip.item_id,
        suit_label=suit_name or None,
        icon_url=_derived_asset_url(
            equip.icon_url,
            _EQUIP_ICON_PATH,
            equip.item_id,
            web_base_url=web_base_url,
        ),
        enhance_label=(
            "强化 " + " / ".join(f"+{level}" for level in levels)
            if levels
            else None
        ),
        stats=tuple(
            EquipStatView(
                name=stat_label(stat.name),
                value=_format_stat_value(stat.value),
                is_main=stat.slot == "main",
            )
            for stat in equip.stats
        ),
        compact_label=compact_label,
    )


def _skill_rows(
    group: CharacterSkillDamage,
    *,
    limit: int,
) -> tuple[SkillRowView, ...]:
    return tuple(
        SkillRowView(
            category=row.category.value,
            name=row.name,
            cast_count=row.cast_count,
            total_damage=format_number(row.total_damage),
            avg_damage=format_number(round(row.avg_damage)),
            max_damage=format_number(row.max_damage),
            share=_share(row.total_damage, group.total_damage),
            share_width=_bar_width(row.total_damage, group.total_damage),
            merged=row.merged_count > 1,
        )
        for row in group.rows[:limit]
    )


def _roster_identities(
    battle: BattleDetailSummary,
) -> dict[str, tuple[str | None, str]]:
    """Avatar and profession by character name, roster first, participants next."""

    identities: dict[str, tuple[str | None, str]] = {}
    for entry in battle.roster:
        identities.setdefault(
            entry.character_name,
            (entry.character_avatar_url, _clean_text(entry.character_profession)),
        )
    for participant in battle.participants:
        identities.setdefault(
            participant.character_name,
            (
                participant.character_avatar_url,
                _clean_text(participant.character_profession),
            ),
        )
    return identities


def _report_facts(battle: BattleDetailSummary, web_base_url: str) -> dict[str, str]:
    """The identity line every battle page repeats: id, link, uploader, totals."""

    return {
        "battle_id": battle.battle_id,
        "report_url": public_url(web_base_url, "battle", battle.battle_id),
        "uploader_display_name": battle.uploader_display_name,
        "duration": format_duration(battle.duration_ms),
        "total_dps": format_number(battle.total_dps),
        "total_damage": format_number(battle.total_damage),
        "battle_date": _format_datetime(battle.battle_end_at),
    }
