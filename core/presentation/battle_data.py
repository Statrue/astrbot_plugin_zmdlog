"""战报's 数据: who did what, in full.

The 摘要 shows the run at a glance; 数据 is the 详细视图 that answers with
the numbers (ADR 0003): 战斗贡献 (each character's share of the damage
beside its share of the team's rDPS, so a support's buffs show beside its
hits), the full character table, 暴击期望 with a row a character, the DPS
曲线, 技能伤害 by character and, on a 危机合约 record, its tags. Every DPS
is whole, as on the 摘要, and every character wears the colour of its DPS
order in every section.

A section whose data the upload lacks is None or empty and the page leaves
it out, heading and all; a battle with no crit rolls has no 暴击期望. One
without skill statistics has no 数据 at all (``core/battle_views``), so
the page never draws an empty 技能伤害.
"""

from dataclasses import dataclass

from ..crit import CritExpectation
from ..loadout import group_skill_damage
from ..models import BattleDetailSummary, BattleParticipant
from .battle import (
    ContractGroupView,
    CritRowView,
    CurveKeyView,
    SkillRowView,
    ViewTabView,
    _build_contract_groups,
    _build_crit_view,
    _roster_identities,
    _skill_rows,
    curve_keys,
)
from .charts import (
    CritBellView,
    DpsCurveView,
    build_crit_bell,
    build_dps_curve_view,
    colour_keys,
)
from .common import (
    PageHeader,
    _bar_width,
    _clean_text,
    _format_datetime,
    _initial,
    _safe_asset_url,
    _share,
    format_duration,
    format_number,
    public_url,
)

# A character's heaviest skills; the rest are counted, not drawn.
_MAX_SKILL_ROWS = 12
# Where a skill's dealer is not among the participants: the last colour.
_FALLBACK_COLOUR = 6


@dataclass(frozen=True, slots=True)
class DataCharacterView:
    """One character: a 战斗贡献 row and a row of the table."""

    character_name: str
    character_initial: str
    avatar_url: str | None
    profession: str
    # 1-6 by DPS, the colour the character wears in every section.
    colour_index: int
    dps: str
    rdps: str
    total_damage: str
    # Of the team's damage, and of the team's summed rDPS.
    damage_share: str
    rdps_share: str
    # The two bars' fills, in percent.
    damage_percent: float
    rdps_percent: float
    max_hit: str
    crit_rate: str


@dataclass(frozen=True, slots=True)
class DataCritView:
    """暴击期望: the team's figures, its bell, then a row a character."""

    actual_damage: str
    expected_damage: str
    expected_dps: str
    # Signed, as the site prints it: +13.91%.
    deviation: str
    # Only when part of the team's skill damage carried no crit roll.
    coverage_note: str | None
    # None when no hit could have gone either way: nothing to draw.
    bell: CritBellView | None
    rows: tuple[CritRowView, ...]


@dataclass(frozen=True, slots=True)
class SkillGroupView:
    """One character's damage by skill, heaviest first."""

    character_name: str
    character_initial: str
    avatar_url: str | None
    colour_index: int
    total_damage: str
    # Of the team's skill-stat total.
    team_share: str
    rows: tuple[SkillRowView, ...]
    hidden_count: int


@dataclass(frozen=True, slots=True)
class BattleDataPage:
    header: PageHeader
    battle_id: str
    report_url: str
    uploader_display_name: str
    duration: str
    battle_date: str
    # Highest DPS first; the first is the main C.
    characters: tuple[DataCharacterView, ...]
    crit: DataCritView | None
    dps_curve: DpsCurveView | None
    curve_keys: tuple[CurveKeyView, ...]
    skill_groups: tuple[SkillGroupView, ...]
    # 危机合约 only: the record's score and its tags by family.
    contract_score: str | None
    contract_groups: tuple[ContractGroupView, ...]
    views: tuple[ViewTabView, ...]


def build_battle_data_page(
    battle: BattleDetailSummary,
    *,
    query: str,
    web_base_url: str,
    crit: CritExpectation | None = None,
    views: tuple[tuple[str, bool], ...] = (),
) -> BattleDataPage:
    """数据; ``views`` is the strip, ``(label, is_current)`` in order."""

    participants = tuple(
        sorted(battle.participants, key=lambda item: item.dps, reverse=True)
    )
    curve = build_dps_curve_view(battle, participants)
    return BattleDataPage(
        header=PageHeader(
            title=battle.boss_name,
            subtitle=battle.dungeon_name,
            query=query,
            matched_name=battle.battle_id,
            target_type="战报数据",
            footer_note="公开战报 · DPS / rDPS",
        ),
        battle_id=battle.battle_id,
        report_url=public_url(web_base_url, "battle", battle.battle_id),
        uploader_display_name=battle.uploader_display_name,
        duration=format_duration(battle.duration_ms),
        battle_date=_format_datetime(battle.battle_end_at),
        characters=_characters(battle, participants, web_base_url=web_base_url),
        crit=_crit(crit, participants),
        dps_curve=curve,
        curve_keys=curve_keys(curve),
        skill_groups=_skill_groups(battle, participants, web_base_url=web_base_url),
        contract_score=(
            format_number(battle.contract_tag_score)
            if battle.contract_tag_score is not None
            else None
        ),
        contract_groups=_build_contract_groups(battle, web_base_url=web_base_url),
        views=tuple(ViewTabView(label, current) for label, current in views),
    )


def _characters(
    battle: BattleDetailSummary,
    participants: tuple[BattleParticipant, ...],
    *,
    web_base_url: str,
) -> tuple[DataCharacterView, ...]:
    total_damage = battle.total_damage
    # A negative rDPS (a run that hurt its team) counts as none toward
    # the team's, or every other share would read over 100%.
    total_rdps = sum(max(participant.rdps, 0.0) for participant in participants)
    return tuple(
        DataCharacterView(
            character_name=participant.character_name,
            character_initial=_initial(participant.character_name),
            avatar_url=_safe_asset_url(
                participant.character_avatar_url, base_url=web_base_url
            ),
            profession=_clean_text(participant.character_profession),
            colour_index=index,
            dps=format_number(round(participant.dps)),
            rdps=format_number(round(participant.rdps)),
            total_damage=format_number(participant.total_damage),
            damage_share=(
                _share(participant.total_damage, total_damage)
                if total_damage > 0
                else "—"
            ),
            rdps_share=(
                _share(max(participant.rdps, 0.0), total_rdps)
                if total_rdps > 0
                else "—"
            ),
            damage_percent=_bar_width(participant.total_damage, total_damage),
            rdps_percent=_bar_width(participant.rdps, total_rdps),
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
        for index, participant in enumerate(participants, start=1)
    )


def _crit(
    crit: CritExpectation | None,
    participants: tuple[BattleParticipant, ...],
) -> DataCritView | None:
    view = _build_crit_view(crit, participants)
    if crit is None or view is None:
        return None
    *characters, team = view.rows
    return DataCritView(
        actual_damage=team.actual_damage,
        expected_damage=team.expected_damage,
        expected_dps=team.expected_dps,
        deviation=team.deviation,
        coverage_note=team.coverage_note,
        bell=build_crit_bell(crit.team),
        rows=tuple(characters),
    )


def _skill_groups(
    battle: BattleDetailSummary,
    participants: tuple[BattleParticipant, ...],
    *,
    web_base_url: str,
) -> tuple[SkillGroupView, ...]:
    groups = group_skill_damage(battle.skill_stats)
    grand_total = sum(group.total_damage for group in groups)
    colours = colour_keys(participants)
    identities = _roster_identities(battle)
    views: list[SkillGroupView] = []
    for group in groups:
        avatar_url, _ = identities.get(group.character_name, (None, ""))
        rows = _skill_rows(group, limit=_MAX_SKILL_ROWS)
        views.append(
            SkillGroupView(
                character_name=group.character_name,
                character_initial=_initial(group.character_name),
                avatar_url=_safe_asset_url(avatar_url, base_url=web_base_url),
                colour_index=colours.get(group.character_name, _FALLBACK_COLOUR),
                total_damage=format_number(group.total_damage),
                team_share=_share(group.total_damage, grand_total),
                rows=rows,
                hidden_count=max(0, len(group.rows) - len(rows)),
            )
        )
    return tuple(views)
