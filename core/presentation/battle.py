"""A battle's pages: its 摘要 and 排轴, and the parts its other pages share.

The 摘要 (``build_battle_summary_page``) is what 战报 draws: the record
band and three charts, each left out when the upload lacks its data.
``build_battle_cast_page`` is the 排轴 view, drawn from the cast export.
数据 is ``battle_data``'s, 养成 ``build``'s and 对比 ``compare``'s; the
gear, skill and crit views here are the ones they read.
"""

from dataclasses import dataclass, replace

from ..contract import group_contract_tags, tag_display_name, tag_short_name
from ..crit import CritExpectation, CritTotals, coverage_percent
from ..loadout import (
    CharacterSkillDamage,
    is_raw_item_name,
    stat_label,
    suit_catalog_id,
)
from ..models import (
    BattleDetailSummary,
    BattleEquip,
    BattleExport,
    BattleParticipant,
)
from .charts import (
    BuffBandView,
    BuffRowView,
    CritBellView,
    DpsCurveView,
    build_buff_band_view,
    build_crit_bell,
    build_dps_curve_view,
    colour_keys,
)
from .common import (
    _EQUIP_ICON_PATH,
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
from .rail import TimelineView, build_timeline_view


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
    # The short form 对比 reads a piece by, e.g. "动火用 · 护甲".
    compact_label: str


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
class CritRowView:
    label: str
    # The character's colour key on the card; None on the team row.
    colour_index: int | None
    actual_damage: str
    expected_damage: str
    expected_dps: str
    deviation: str
    # Only when part of this row's skill damage carried no crit roll.
    coverage_note: str | None
    is_team: bool = False


@dataclass(frozen=True, slots=True)
class CritView:
    # The characters in the card's order, then the team.
    rows: tuple[CritRowView, ...]


@dataclass(frozen=True, slots=True)
class ViewTabView:
    """One entry of the strip of a battle's pages in the foot."""

    label: str
    current: bool


@dataclass(frozen=True, slots=True)
class SummaryCharacterView:
    """One character of the 摘要: a face in the record band, a 伤害构成 row."""

    character_name: str
    character_initial: str
    avatar_url: str | None
    # 1-6 by DPS, the colour the character wears in every chart.
    colour_index: int
    dps: str
    damage_share: str
    # The track's fill, a percent of the team's damage.
    share_percent: float


@dataclass(frozen=True, slots=True)
class CurveKeyView:
    """One line of the DPS 曲线's key and the DPS it ends at."""

    label: str
    # None for the team's line, drawn in ink.
    colour_index: int | None
    dps: str


@dataclass(frozen=True, slots=True)
class CritSummaryView:
    """暴击期望 at a glance: how far off the run landed, and the bell."""

    # Above (or at) the expectation: the figure goes on yellow.
    above: bool
    # Unsigned; ``above`` says which way.
    deviation: str
    actual_damage: str
    expected_damage: str
    expected_dps: str
    # Only when part of the team's skill damage carried no crit roll.
    coverage_note: str | None
    bell: CritBellView


@dataclass(frozen=True, slots=True)
class BattleSummaryPage:
    """战报's default page: the record band, 伤害构成, 暴击期望, DPS 曲线.

    No per-character table — the numbers live on 数据. A chart whose data
    the upload lacks is None, and the page leaves its section out.
    """

    header: PageHeader
    battle_id: str
    report_url: str
    uploader_display_name: str
    duration: str
    total_dps: str
    total_damage: str
    battle_date: str
    timer_label: str
    integrity_label: str
    # "rDPS 榜记录" when this upload made the board's rDPS ranking.
    rdps_ranking_label: str
    contract_score: str | None
    # Highest DPS first; the first is the main C.
    characters: tuple[SummaryCharacterView, ...]
    crit: CritSummaryView | None
    dps_curve: DpsCurveView | None
    curve_keys: tuple[CurveKeyView, ...]
    views: tuple[ViewTabView, ...]


def build_battle_summary_page(
    battle: BattleDetailSummary,
    *,
    query: str,
    web_base_url: str,
    crit: CritExpectation | None = None,
    views: tuple[tuple[str, bool], ...] = (),
) -> BattleSummaryPage:
    """The 摘要; ``views`` is the strip, ``(label, is_current)`` in order."""

    participants = tuple(
        sorted(battle.participants, key=lambda item: item.dps, reverse=True)
    )
    total_damage = battle.total_damage
    curve = build_dps_curve_view(battle, participants)
    return BattleSummaryPage(
        header=PageHeader(
            title=battle.boss_name,
            subtitle=battle.dungeon_name,
            query=query,
            matched_name=battle.battle_id,
            target_type="公开战报",
            footer_note="公开战报 · DPS / rDPS",
        ),
        battle_id=battle.battle_id,
        report_url=public_url(web_base_url, "battle", battle.battle_id),
        uploader_display_name=battle.uploader_display_name,
        duration=format_duration(battle.duration_ms),
        total_dps=format_number(round(battle.total_dps)),
        total_damage=format_number(total_damage),
        battle_date=_format_datetime(battle.battle_end_at),
        timer_label=(
            "官方计时" if _official_timer(battle) else "计时待核验"
        ),
        integrity_label=(
            "结构校验通过" if battle.integrity_verified else "结构校验未通过"
        ),
        rdps_ranking_label="rDPS 榜记录" if battle.rdps_ranking_eligible else "",
        contract_score=(
            format_number(battle.contract_tag_score)
            if battle.contract_tag_score is not None
            else None
        ),
        characters=tuple(
            SummaryCharacterView(
                character_name=participant.character_name,
                character_initial=_initial(participant.character_name),
                avatar_url=_safe_asset_url(
                    participant.character_avatar_url, base_url=web_base_url
                ),
                colour_index=index,
                dps=format_number(round(participant.dps)),
                damage_share=(
                    _share(participant.total_damage, total_damage)
                    if total_damage > 0
                    else "—"
                ),
                share_percent=_bar_width(participant.total_damage, total_damage),
            )
            for index, participant in enumerate(participants, start=1)
        ),
        crit=_crit_summary(crit),
        dps_curve=curve,
        curve_keys=curve_keys(curve),
        views=tuple(ViewTabView(label, current) for label, current in views),
    )


def curve_keys(curve: DpsCurveView | None) -> tuple[CurveKeyView, ...]:
    """The DPS 曲线's key: the team's line, then each character's."""

    if curve is None:
        return ()
    return (
        CurveKeyView(label="全队", colour_index=None, dps=curve.team_dps),
        *(
            CurveKeyView(
                label=series.character_name,
                colour_index=series.colour_index,
                dps=series.final_dps,
            )
            for series in curve.series
        ),
    )


def _official_timer(battle: BattleDetailSummary) -> bool:
    return (
        battle.time_source == "game_timer"
        and battle.official_timer_start_seen is True
        and battle.official_timer_end_seen is True
    )


def _crit_summary(crit: CritExpectation | None) -> CritSummaryView | None:
    """The team's row of 暴击期望, or None with no rolls or no spread to draw."""

    if crit is None:
        return None
    bell = build_crit_bell(crit.team)
    if bell is None:
        return None
    team = _crit_row(crit.team, label="全队", colour_index=None, is_team=True)
    return CritSummaryView(
        above=crit.team.relative_difference >= 0,
        deviation=f"{abs(crit.team.relative_difference):.2%}",
        actual_damage=team.actual_damage,
        expected_damage=team.expected_damage,
        expected_dps=team.expected_dps,
        coverage_note=team.coverage_note,
        bell=bell,
    )


@dataclass(frozen=True, slots=True)
class BattleCastPage:
    """排轴: BUFF 覆盖, then the cast rail.

    The band's time runs left to right and the rail's top to bottom, so the
    page draws the band first and the switch falls on the rail's heading.
    The rail is the export's; the band and the uploader come from the
    detail, and without it the page is the rail alone.
    """

    header: PageHeader
    battle_id: str
    report_url: str
    # None without the detail, which alone names who uploaded and when.
    uploader_display_name: str | None
    battle_date: str | None
    duration: str
    # Rows alike but for their element merged into one (``_merge_buff_rows``);
    # None without the detail or without buffs to draw.
    buff_band: BuffBandView | None
    rail: TimelineView
    views: tuple[ViewTabView, ...]


def build_battle_cast_page(
    export: BattleExport,
    *,
    query: str,
    web_base_url: str,
    battle: BattleDetailSummary | None = None,
    views: tuple[tuple[str, bool], ...] = (),
) -> BattleCastPage:
    """排轴; ``views`` is the strip, ``(label, is_current)`` in order."""

    band = build_buff_band_view(battle) if battle is not None else None
    return BattleCastPage(
        header=PageHeader(
            title=export.boss_name,
            subtitle=export.dungeon_name,
            query=query,
            matched_name=export.battle_id,
            target_type="排轴",
            footer_note="公开战报 · 上传时记录的施法序列",
        ),
        battle_id=export.battle_id,
        report_url=public_url(web_base_url, "battle", export.battle_id),
        uploader_display_name=(
            battle.uploader_display_name if battle is not None else None
        ),
        battle_date=(
            _format_datetime(battle.battle_end_at) if battle is not None else None
        ),
        duration=format_duration(
            battle.duration_ms if battle is not None else export.duration_ms
        ),
        buff_band=(
            replace(band, rows=_merge_buff_rows(band.rows))
            if band is not None
            else None
        ),
        rail=build_timeline_view(export, web_base_url=web_base_url),
        views=tuple(ViewTabView(label, current) for label, current in views),
    )


def _merge_buff_rows(rows: tuple[BuffRowView, ...]) -> tuple[BuffRowView, ...]:
    """BUFF 覆盖 rows that are one buff wearing several elements, as one row.

    Rows with one source, one target, one value and the very same spans
    differ only in what they boost: 寒冷增幅 +47% and 自然增幅 +47% become
    寒冷/自然增幅 +47%, the way upstream already names some buffs. Names
    that share their zone keep it once; others are joined whole. A row with
    two effects (``攻击 +16% · 增伤 +20%``) is never merged.
    """

    merged: dict[tuple, list[BuffRowView]] = {}
    for index, row in enumerate(rows):
        name, _, value = row.effect_label.rpartition(" ")
        if not name or " · " in row.effect_label:
            merged[(index,)] = [row]
            continue
        key = (
            row.on_enemy,
            row.source_name,
            row.target_label,
            value,
            tuple((span.left, span.width) for span in row.spans),
        )
        merged.setdefault(key, []).append(row)
    return tuple(_merged_row(group) for group in merged.values())


def _merged_row(group: list[BuffRowView]) -> BuffRowView:
    first = group[0]
    if len(group) == 1:
        return first
    names = [row.effect_label.rpartition(" ")[0] for row in group]
    value = first.effect_label.rpartition(" ")[2]
    # Every zone (攻击, 增幅, 脆弱, …) is two characters long.
    zone = names[0][-2:]
    if all(len(name) > 2 and name.endswith(zone) for name in names):
        label = "/".join(name[:-2] for name in names) + zone
    else:
        label = "/".join(names)
    return replace(first, effect_label=f"{label} {value}")


def _build_crit_view(
    crit: CritExpectation | None,
    participants: tuple[BattleParticipant, ...],
) -> CritView | None:
    """``participants`` in the card's order, so each row keeps its colour."""

    if crit is None:
        return None
    colours = colour_keys(participants)
    # Stable: a dealer the participant list lacks keeps the heaviest-first
    # order it came in, after the rest, in the curve's fallback colour.
    characters = sorted(
        crit.characters,
        key=lambda row: colours.get(row.character, len(colours) + 1),
    )
    return CritView(
        rows=(
            *(
                _crit_row(
                    row,
                    label=row.character or "",
                    colour_index=colours.get(row.character, 6),
                )
                for row in characters
            ),
            _crit_row(crit.team, label="全队", colour_index=None, is_team=True),
        )
    )


def _crit_row(
    totals: CritTotals,
    *,
    label: str,
    colour_index: int | None,
    is_team: bool = False,
) -> CritRowView:
    return CritRowView(
        label=label,
        colour_index=colour_index,
        actual_damage=format_number(totals.actual_damage),
        expected_damage=format_number(round(totals.expected_damage)),
        expected_dps=format_number(round(totals.expected_dps)),
        deviation=f"{totals.relative_difference:+.2%}",
        coverage_note=(
            f"已覆盖 {format_number(coverage_percent(totals.coverage))}% 伤害"
            if totals.coverage < 1
            else None
        ),
        is_team=is_team,
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

