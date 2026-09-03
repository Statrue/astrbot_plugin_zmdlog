"""Image-template view models for public ZMDLogs ranking data."""

import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urljoin, urlsplit

from .characters import CharacterFilterScope
from .history import AccountHistory, BoardHistory, RankPoint, trend_points, window_start
from .loadout import (
    CharacterSkillDamage,
    SkillCategory,
    element_label,
    group_skill_damage,
    infer_suit_names,
    is_raw_item_name,
    skill_level_summary,
    stat_label,
    suit_token,
    weapon_skill_levels,
)
from .matcher import MatchChoice, TargetType
from .models import (
    BattleDetailSummary,
    BattleEquip,
    BattleExport,
    BattleWeapon,
    BossRanking,
    BossRankingRosterEntry,
    BossRankingRow,
    CharacterBossStatistics,
    CharacterStatistics,
    HotBossCard,
    PublicUserRankings,
)
from .routing import (
    DEFAULT_RANKING_TOP,
    MAX_RANKING_TOP,
    MIN_RANKING_TOP,
)
from .timeline import TimelineLane, build_timeline
from .timestamps import parse_timestamp

# Temporary presentation compatibility: upstream currently exposes the
# contract board as bossName="破潮之像", while the public site labels the
# activity and ranking page as "危机合约". Remove this override after the
# upstream API provides the same public-facing name.
_CRISIS_CONTRACT_BOSS_SLUG = "indie_group_ccdg"


class PresentationError(ValueError):
    """Raised when data cannot be represented safely in a public template."""


@dataclass(frozen=True, slots=True)
class PageHeader:
    title: str
    subtitle: str
    query: str
    matched_name: str
    target_type: str
    footer_note: str = "公开榜单 · DPS 口径"


@dataclass(frozen=True, slots=True)
class TopRunView:
    rank: int
    character_name: str
    character_initial: str
    character_avatar_url: str | None
    uploader_nickname: str
    duration: str
    contract_score: str | None


@dataclass(frozen=True, slots=True)
class TopCardView:
    dungeon_name: str
    boss_name: str
    runs: tuple[TopRunView, ...]


@dataclass(frozen=True, slots=True)
class TopCardGroupView:
    dungeon_name: str
    cards: tuple[TopCardView, ...]


@dataclass(frozen=True, slots=True)
class Top3Page:
    header: PageHeader
    dungeon_names: tuple[str, ...]
    cards: tuple[TopCardView, ...]
    card_groups: tuple[TopCardGroupView, ...]
    group_by_dungeon: bool


@dataclass(frozen=True, slots=True)
class RosterEntryView:
    character_name: str
    profession: str
    character_initial: str
    avatar_url: str | None


@dataclass(frozen=True, slots=True)
class RankingRowView:
    rank: int
    percentile: str
    account_display_name: str
    character_name: str
    character_profession: str
    character_initial: str
    character_avatar_url: str | None
    roster: tuple[RosterEntryView, ...]
    dps: str
    duration: str
    contract_score: str | None
    # Real DPS share of the board leader, for the in-row bar. Upstream
    # ``scorePercent`` is a rank percentile and would only restate the rank.
    score_bar_width: float = 100.0


@dataclass(frozen=True, slots=True)
class RankingPage:
    header: PageHeader
    boss_slug: str
    row_count: int
    show_contract_score: bool
    rows: tuple[RankingRowView, ...]
    # Formatted DPS of the board leader; the 100% mark of the in-row bars.
    top_dps: str = ""
    character_filter: str | None = None
    # MAIN filters on the row's main C; ROSTER is the automatic fallback for
    # characters that never carry, matching anywhere in the four-man team.
    character_filter_scope: CharacterFilterScope = CharacterFilterScope.MAIN
    filtered_count: int | None = None


@dataclass(frozen=True, slots=True)
class CharacterStatRowView:
    rank: int
    character_name: str
    character_profession: str
    character_initial: str
    character_avatar_url: str | None
    sample_count: int
    outlier_count: int
    median: str
    maximum: str
    # Horizontal box-plot geometry, already expressed as percentages of the
    # shared axis so the template performs no arithmetic.
    whisker_left: float
    whisker_width: float
    box_left: float
    box_width: float
    median_left: float
    p10_left: float
    p90_left: float
    maximum_left: float | None


@dataclass(frozen=True, slots=True)
class CharacterStatChipView:
    character_name: str
    sample_count: int


@dataclass(frozen=True, slots=True)
class CharacterStatsPage:
    header: PageHeader
    scope_label: str
    range_label: str
    potential_label: str
    eligible_battle_count: int
    total_sample_count: int
    total_outlier_count: int
    included_boss_count: int
    minimum_sample_count: int
    axis_labels: tuple[str, ...]
    rows: tuple[CharacterStatRowView, ...]
    insufficient: tuple[CharacterStatChipView, ...]


@dataclass(frozen=True, slots=True)
class CharacterBossRowView:
    boss_name: str
    dungeon_name: str
    rank: int
    ranked_character_count: int
    sample_count: int
    outlier_count: int
    median: str
    maximum: str
    whisker_left: float
    whisker_width: float
    box_left: float
    box_width: float
    median_left: float
    p10_left: float
    p90_left: float
    maximum_left: float | None


@dataclass(frozen=True, slots=True)
class CharacterBossChipView:
    boss_name: str
    sample_count: int


@dataclass(frozen=True, slots=True)
class CharacterBossPage:
    header: PageHeader
    character_name: str
    character_profession: str
    character_initial: str
    character_avatar_url: str | None
    range_label: str
    potential_label: str
    total_sample_count: int
    total_outlier_count: int
    included_boss_count: int
    minimum_sample_count: int
    axis_labels: tuple[str, ...]
    rows: tuple[CharacterBossRowView, ...]
    insufficient: tuple[CharacterBossChipView, ...]


@dataclass(frozen=True, slots=True)
class UsageEntryView:
    character_name: str
    character_initial: str
    avatar_url: str | None
    percent: str
    bar_width: float


@dataclass(frozen=True, slots=True)
class ProfessionUsageView:
    profession: str
    entries: tuple[UsageEntryView, ...]
    hidden_count: int


@dataclass(frozen=True, slots=True)
class RosterComboView:
    members: tuple[RosterEntryView, ...]
    count: int
    percent: str
    best_rank: int
    best_dps: str


@dataclass(frozen=True, slots=True)
class MainCharacterView:
    character_name: str
    character_initial: str
    avatar_url: str | None
    count: int
    percent: str
    bar_width: float


@dataclass(frozen=True, slots=True)
class RosterPage:
    header: PageHeader
    row_count: int
    sample_size: int
    profession_usage: tuple[ProfessionUsageView, ...]
    combos: tuple[RosterComboView, ...]
    main_characters: tuple[MainCharacterView, ...]


@dataclass(frozen=True, slots=True)
class AccountRankingView:
    boss_name: str
    dungeon_name: str | None
    rank: int
    percentile: str
    duration: str
    total_dps: str
    battle_date: str
    roster: tuple[str, ...]
    contract_score: str | None


@dataclass(frozen=True, slots=True)
class AccountPage:
    header: PageHeader
    account_id: str
    account_url: str
    record_count: int
    best_rank: str
    best_percentile: str
    average_percentile: str
    show_contract_score: bool
    rows: tuple[AccountRankingView, ...]


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
    suit_label: str | None
    # The suit was read off a named sibling piece, not from this piece itself.
    inferred_suit: bool
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
    has_inferred_suit: bool


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
class TrendRowView:
    boss_name: str
    dungeon_name: str
    current_rank: int
    start_rank: int
    delta_label: str
    # "up" (rank improved), "down" (rank worsened) or "flat".
    delta_kind: str
    best_rank: int
    worst_rank: int
    # Stepped polyline in a 100×44 viewBox; time left to right, best rank up.
    polyline: str
    # Change points as (left %, top %) for HTML-positioned dots.
    dots: tuple[tuple[float, float], ...]
    axis_top: str
    axis_bottom: str
    last_change: str
    point_count: int


@dataclass(frozen=True, slots=True)
class TrendPage:
    header: PageHeader
    account_id: str
    account_url: str
    display_name: str
    range_label: str
    tracked_since: str
    tracked_days: str
    last_checked: str
    board_count: int
    best_rank: str
    improved_count: int
    declined_count: int
    rows: tuple[TrendRowView, ...]


@dataclass(frozen=True, slots=True)
class RailEventView:
    """One move on a character's rail, placed in pixels from the chart top."""

    top: int
    # Bar length along the rail; 0 for an instant, which is drawn as a dot.
    height: int
    # Horizontal offset of the column this move sits in (overlapping moves
    # step right) and the bar width its importance earns.
    left: int
    width: int
    # bar (a move with a known end) or dot (an instant).
    shape: str
    # A 终结技 also gets a diamond landmark.
    landmark: bool
    # CSS modifier: ultimate / skill / combo / heavy / normal / other.
    category: str
    name: str
    count: int
    time_label: str
    label_visible: bool
    # Labels slide down when neighbours are too close; the bar never moves.
    label_top: int
    # A thin leader from a narrow bar to its label; width 0 draws none.
    lead_top: int
    lead_left: int
    lead_width: int
    summon: bool
    energy: bool


@dataclass(frozen=True, slots=True)
class RailLaneView:
    character_name: str
    character_initial: str
    character_avatar_url: str | None
    cast_count: int
    # Where labels start; further right when the lane needed extra columns.
    label_left: int
    events: tuple[RailEventView, ...]


@dataclass(frozen=True, slots=True)
class RailTickView:
    top: int
    label: str
    major: bool


@dataclass(frozen=True, slots=True)
class RailLegendView:
    css: str
    label: str
    count: int


@dataclass(frozen=True, slots=True)
class TimelineView:
    """The rail chart itself; embedded in the battle card and the 技能轴 page."""

    chart_height: int
    scale_label: str
    duration_label: str
    ticks: tuple[RailTickView, ...]
    lanes: tuple[RailLaneView, ...]
    legend: tuple[RailLegendView, ...]
    cast_count: int
    ultimate_count: int
    hidden_count: int
    clipped_count: int
    has_summon: bool
    has_energy: bool
    has_instant: bool


@dataclass(frozen=True, slots=True)
class TimelinePage:
    header: PageHeader
    battle_id: str
    report_url: str
    timeline: TimelineView


@dataclass(frozen=True, slots=True)
class BattlePage:
    header: PageHeader
    battle_id: str
    report_url: str
    account_id: str
    account_url: str
    uploader_display_name: str
    duration: str
    total_dps: str
    total_damage: str
    battle_date: str
    timer_label: str
    integrity_label: str
    contract_score: str | None
    participants: tuple[BattleParticipantView, ...]
    loadouts: tuple[LoadoutView, ...] = ()
    skill_stats_available: bool = False
    # The cast rail, when the public export was available; otherwise a short
    # reason (old upload, rate limit) or nothing at all.
    timeline: TimelineView | None = None
    timeline_note: str | None = None


def build_all_top3_page(
    cards: tuple[HotBossCard, ...],
    *,
    query: str,
    web_base_url: str | None = None,
) -> Top3Page:
    """Build the all-board page without exposing ranking-only fields."""

    card_views = tuple(
        _build_top_card(card, web_base_url=web_base_url) for card in cards
    )
    return Top3Page(
        header=PageHeader(
            title="全部榜单前三名",
            subtitle="当前公开榜单与各榜最快记录",
            query=query,
            matched_name="全部公开榜单",
            target_type="全部榜单",
        ),
        dungeon_names=_unique_dungeon_names(cards),
        cards=card_views,
        card_groups=_group_top_cards(card_views),
        group_by_dungeon=False,
    )


def build_dungeon_top3_page(
    choice: MatchChoice,
    cards: tuple[HotBossCard, ...],
    *,
    query: str,
    web_base_url: str | None = None,
) -> Top3Page:
    """Build a dungeon or dungeon-scope page using the shared top-three card."""

    target_type = choice.target.target_type
    if target_type not in {TargetType.DUNGEON, TargetType.DUNGEON_SCOPE}:
        raise PresentationError("dungeon page requires a dungeon target")

    label = "副本范围" if target_type is TargetType.DUNGEON_SCOPE else "副本"
    subtitle = (
        "范围内全部标准副本与榜单前三名"
        if target_type is TargetType.DUNGEON_SCOPE
        else "该标准副本下全部榜单前三名"
    )
    card_views = tuple(
        _build_top_card(card, web_base_url=web_base_url) for card in cards
    )
    return Top3Page(
        header=PageHeader(
            title=f"{label}榜单前三名",
            subtitle=subtitle,
            query=query,
            matched_name=choice.target.name,
            target_type=label,
        ),
        dungeon_names=choice.target.dungeon_names,
        cards=card_views,
        card_groups=_group_top_cards(card_views),
        group_by_dungeon=target_type is TargetType.DUNGEON_SCOPE,
    )


def build_ranking_page(
    ranking: BossRanking,
    *,
    query: str,
    display_limit: int = DEFAULT_RANKING_TOP,
    web_base_url: str | None = None,
    character_filter: str | None = None,
    character_filter_scope: CharacterFilterScope = CharacterFilterScope.MAIN,
) -> RankingPage:
    """Build the first public DPS rows in their upstream order.

    ``character_filter`` is an already-resolved character name matched against
    the row's main C, or against the whole roster when the scope says so; rows
    keep their upstream global rank after filtering.
    """

    if isinstance(display_limit, bool) or not isinstance(display_limit, int):
        raise PresentationError("ranking display limit must be an integer")
    if not MIN_RANKING_TOP <= display_limit <= MAX_RANKING_TOP:
        raise PresentationError("ranking display limit must be between 1 and 30")

    source_rows = ranking.rows
    filtered_count: int | None = None
    if character_filter is not None:
        if character_filter_scope is CharacterFilterScope.ROSTER:
            source_rows = tuple(
                row
                for row in ranking.rows
                if any(
                    entry.character_name == character_filter
                    for entry in row.roster_entries
                )
            )
        else:
            source_rows = tuple(
                row
                for row in ranking.rows
                if row.character_name == character_filter
            )
        filtered_count = len(source_rows)
    displayed_rows = source_rows[:display_limit]
    top_dps = ranking.rows[0].dps if ranking.rows else 0.0
    is_crisis_contract = ranking.boss_slug == _CRISIS_CONTRACT_BOSS_SLUG
    title = "危机合约" if is_crisis_contract else ranking.boss_name
    subtitle = "活动竞速" if is_crisis_contract else ranking.dungeon_name
    matched_name = (
        "危机合约"
        if is_crisis_contract
        else f"{ranking.dungeon_name} · {ranking.boss_name}"
    )
    return RankingPage(
        header=PageHeader(
            title=title,
            subtitle=subtitle,
            query=query,
            matched_name=matched_name,
            target_type="具体榜单",
        ),
        boss_slug=ranking.boss_slug,
        row_count=len(ranking.rows),
        show_contract_score=any(
            row.contract_tag_score is not None for row in displayed_rows
        ),
        rows=tuple(
            RankingRowView(
                rank=row.rank,
                percentile=f"{format_number(row.score_percent)}%",
                account_display_name=row.account_display_name,
                character_name=row.character_name,
                character_profession=row.character_profession,
                character_initial=_initial(row.character_name),
                character_avatar_url=_safe_asset_url(
                    row.character_avatar_url,
                    base_url=web_base_url,
                ),
                roster=_build_roster(
                    row.roster_entries,
                    row.roster_summary,
                    web_base_url=web_base_url,
                ),
                dps=format_number(row.dps),
                duration=format_duration(row.duration_ms),
                contract_score=(
                    format_number(row.contract_tag_score)
                    if row.contract_tag_score is not None
                    else None
                ),
                score_bar_width=_dps_share(row.dps, top_dps),
            )
            for row in displayed_rows
        ),
        top_dps=format_number(top_dps) if ranking.rows else "",
        character_filter=character_filter,
        character_filter_scope=character_filter_scope,
        filtered_count=filtered_count,
    )


def _dps_share(dps: float, top_dps: float) -> float:
    """Percent of the board leader's DPS, clamped to a visible minimum."""

    if top_dps <= 0:
        return 2.0
    return round(max(2.0, min(100.0, dps / top_dps * 100.0)), 2)


_RANGE_LABELS = {
    "7d": "近 7 天",
    "14d": "近 14 天",
    "30d": "近 30 天",
    "all": "全部时间",
}
_POTENTIAL_LABELS = {"0": "0 潜能", "1-5": "1–5 潜能", "all": "全部潜能"}
_MAX_USAGE_ENTRIES = 6
_MAX_COMBOS = 5
_MAX_MAIN_CHARACTERS = 8
_PROFESSION_ORDER = ("近卫", "重装", "辅助", "突击", "术士", "先锋")


def build_character_stats_page(
    stats: CharacterStatistics,
    *,
    query: str,
    web_base_url: str | None = None,
) -> CharacterStatsPage:
    """Turn upstream percentile rows into box-plot geometry for the template."""

    is_global = stats.scope == "all"
    title = "全部副本" if is_global else stats.boss_name
    subtitle = "角色统计总榜" if is_global else stats.dungeon_name
    matched_name = (
        "角色统计总榜" if is_global else f"{stats.dungeon_name} · {stats.boss_name}"
    )
    ranked = tuple(
        row
        for row in stats.rows
        if row.rank is not None and not row.insufficient_samples
    )
    axis_max = _stats_axis_max(ranked)
    step = axis_max / 4 if axis_max else 0
    axis_labels = tuple(_format_axis_value(step * index) for index in range(5))

    def percent(value: float | None) -> float:
        if value is None or axis_max <= 0:
            return 0.0
        return round(max(0.0, min(100.0, value / axis_max * 100)), 2)

    rows: list[CharacterStatRowView] = []
    for row in ranked:
        low = row.lower_whisker if row.lower_whisker is not None else row.p25
        high = row.upper_whisker if row.upper_whisker is not None else row.p75
        p25 = row.p25 if row.p25 is not None else row.median
        p75 = row.p75 if row.p75 is not None else row.median
        whisker_left = percent(low)
        whisker_right = percent(high)
        box_left = percent(p25)
        box_right = percent(p75)
        maximum_left = None
        if row.maximum is not None and high is not None and row.maximum > high:
            # Outlier maxima can dwarf the whisker scale; pin them to the edge.
            maximum_left = min(percent(row.maximum), 100.0)
        rows.append(
            CharacterStatRowView(
                rank=row.rank or 0,
                character_name=row.character_name,
                character_profession=row.character_profession,
                character_initial=_initial(row.character_name),
                character_avatar_url=_safe_asset_url(
                    row.character_avatar_url, base_url=web_base_url
                ),
                sample_count=row.normal_sample_count,
                outlier_count=row.outlier_count,
                median=format_number(row.median) if row.median is not None else "—",
                maximum=(
                    format_number(row.maximum) if row.maximum is not None else "—"
                ),
                whisker_left=whisker_left,
                whisker_width=round(max(0.0, whisker_right - whisker_left), 2),
                box_left=box_left,
                box_width=round(max(0.0, box_right - box_left), 2),
                median_left=percent(row.median),
                p10_left=percent(row.p10 if row.p10 is not None else p25),
                p90_left=percent(row.p90 if row.p90 is not None else p75),
                maximum_left=maximum_left,
            )
        )

    insufficient = tuple(
        CharacterStatChipView(
            character_name=row.character_name,
            sample_count=row.sample_count,
        )
        for row in stats.rows
        if row.insufficient_samples and row.sample_count > 0
    )
    return CharacterStatsPage(
        header=PageHeader(
            title=title,
            subtitle=subtitle,
            query=query,
            matched_name=matched_name,
            target_type="角色统计",
            footer_note="公开战斗 · 六星角色 DPS 分布",
        ),
        scope_label="全部副本" if is_global else "单个榜单",
        range_label=_RANGE_LABELS.get(stats.range, stats.range),
        potential_label=_POTENTIAL_LABELS.get(stats.potential, stats.potential),
        eligible_battle_count=stats.eligible_battle_count,
        total_sample_count=stats.total_sample_count,
        total_outlier_count=stats.total_outlier_count,
        included_boss_count=stats.included_boss_count,
        minimum_sample_count=stats.minimum_sample_count,
        axis_labels=axis_labels,
        rows=tuple(rows),
        insufficient=insufficient,
    )


def build_character_boss_page(
    stats: CharacterBossStatistics,
    *,
    query: str,
    web_base_url: str | None = None,
) -> CharacterBossPage:
    """One character across every board: rank badge first, box plot second."""

    ranked = tuple(
        row
        for row in stats.rows
        if row.rank is not None and not row.insufficient_samples
    )
    ranked = tuple(
        sorted(
            ranked,
            key=lambda row: (
                -(row.median if row.median is not None else -1.0),
                row.boss_name,
            ),
        )
    )
    axis_max = _stats_axis_max(ranked)
    step = axis_max / 4 if axis_max else 0
    axis_labels = tuple(_format_axis_value(step * index) for index in range(5))

    def percent(value: float | None) -> float:
        if value is None or axis_max <= 0:
            return 0.0
        return round(max(0.0, min(100.0, value / axis_max * 100)), 2)

    rows: list[CharacterBossRowView] = []
    for row in ranked:
        low = row.lower_whisker if row.lower_whisker is not None else row.p25
        high = row.upper_whisker if row.upper_whisker is not None else row.p75
        p25 = row.p25 if row.p25 is not None else row.median
        p75 = row.p75 if row.p75 is not None else row.median
        whisker_left = percent(low)
        whisker_right = percent(high)
        box_left = percent(p25)
        box_right = percent(p75)
        maximum_left = None
        if row.maximum is not None and high is not None and row.maximum > high:
            maximum_left = min(percent(row.maximum), 100.0)
        rows.append(
            CharacterBossRowView(
                boss_name=row.boss_name,
                dungeon_name=row.dungeon_name,
                rank=row.rank or 0,
                ranked_character_count=row.ranked_character_count,
                sample_count=row.normal_sample_count,
                outlier_count=row.outlier_count,
                median=format_number(row.median) if row.median is not None else "—",
                maximum=(
                    format_number(row.maximum) if row.maximum is not None else "—"
                ),
                whisker_left=whisker_left,
                whisker_width=round(max(0.0, whisker_right - whisker_left), 2),
                box_left=box_left,
                box_width=round(max(0.0, box_right - box_left), 2),
                median_left=percent(row.median),
                p10_left=percent(row.p10 if row.p10 is not None else p25),
                p90_left=percent(row.p90 if row.p90 is not None else p75),
                maximum_left=maximum_left,
            )
        )

    insufficient = tuple(
        CharacterBossChipView(
            boss_name=row.boss_name,
            sample_count=row.sample_count,
        )
        for row in stats.rows
        if row.insufficient_samples and row.sample_count > 0
    )
    return CharacterBossPage(
        header=PageHeader(
            title=stats.character_name,
            subtitle=stats.character_profession,
            query=query,
            matched_name=f"{stats.character_name} · 全榜单角色统计",
            target_type="角色统计",
            footer_note="公开战斗 · 单角色全榜单 DPS 分布",
        ),
        character_name=stats.character_name,
        character_profession=stats.character_profession,
        character_initial=_initial(stats.character_name),
        character_avatar_url=_safe_asset_url(
            stats.character_avatar_url, base_url=web_base_url
        ),
        range_label=_RANGE_LABELS.get(stats.range, stats.range),
        potential_label=_POTENTIAL_LABELS.get(stats.potential, stats.potential),
        total_sample_count=stats.total_sample_count,
        total_outlier_count=stats.total_outlier_count,
        included_boss_count=stats.included_boss_count,
        minimum_sample_count=stats.minimum_sample_count,
        axis_labels=axis_labels,
        rows=tuple(rows),
        insufficient=insufficient,
    )


def build_roster_page(
    ranking: BossRanking,
    *,
    query: str,
    display_limit: int = DEFAULT_RANKING_TOP,
    web_base_url: str | None = None,
) -> RosterPage:
    """Profession usage (whole board) plus top-N roster / main-character stats."""

    if isinstance(display_limit, bool) or not isinstance(display_limit, int):
        raise PresentationError("ranking display limit must be an integer")
    if not MIN_RANKING_TOP <= display_limit <= MAX_RANKING_TOP:
        raise PresentationError("ranking display limit must be between 1 and 30")

    is_crisis_contract = ranking.boss_slug == _CRISIS_CONTRACT_BOSS_SLUG
    title = "危机合约" if is_crisis_contract else ranking.boss_name
    subtitle = "活动竞速" if is_crisis_contract else ranking.dungeon_name
    matched_name = (
        "危机合约"
        if is_crisis_contract
        else f"{ranking.dungeon_name} · {ranking.boss_name}"
    )
    sample_rows = ranking.rows[:display_limit]
    sample_size = len(sample_rows)

    profession_usage = []
    for group in ranking.profession_groups:
        entries = group.entries[:_MAX_USAGE_ENTRIES]
        peak = max((entry.usage_percent for entry in entries), default=0.0)
        profession_usage.append(
            ProfessionUsageView(
                profession=group.profession,
                entries=tuple(
                    UsageEntryView(
                        character_name=entry.character_name,
                        character_initial=_initial(entry.character_name),
                        avatar_url=_safe_asset_url(
                            entry.avatar_url, base_url=web_base_url
                        ),
                        percent=f"{format_number(entry.usage_percent)}%",
                        bar_width=_bar_width(entry.usage_percent, peak),
                    )
                    for entry in entries
                ),
                hidden_count=max(0, len(group.entries) - len(entries)),
            )
        )

    combos = _build_roster_combos(sample_rows, web_base_url=web_base_url)

    main_counter: Counter[str] = Counter(row.character_name for row in sample_rows)
    main_avatars: dict[str, str | None] = {}
    for row in sample_rows:
        main_avatars.setdefault(row.character_name, row.character_avatar_url)
    main_peak = max(main_counter.values(), default=0)
    main_characters = tuple(
        MainCharacterView(
            character_name=name,
            character_initial=_initial(name),
            avatar_url=_safe_asset_url(
                main_avatars.get(name), base_url=web_base_url
            ),
            count=count,
            percent=_share(count, sample_size),
            bar_width=_bar_width(count, main_peak),
        )
        for name, count in sorted(
            main_counter.items(), key=lambda item: (-item[1], item[0])
        )[:_MAX_MAIN_CHARACTERS]
    )

    return RosterPage(
        header=PageHeader(
            title=title,
            subtitle=subtitle,
            query=query,
            matched_name=matched_name,
            target_type="榜单阵容",
        ),
        row_count=len(ranking.rows),
        sample_size=sample_size,
        profession_usage=tuple(profession_usage),
        combos=combos,
        main_characters=main_characters,
    )


def _build_roster_combos(
    rows: tuple[BossRankingRow, ...],
    *,
    web_base_url: str | None,
) -> tuple[RosterComboView, ...]:
    groups: dict[tuple[str, ...], list[BossRankingRow]] = {}
    for row in rows:
        groups.setdefault(_roster_key(row), []).append(row)
    ordered = sorted(
        groups.items(),
        key=lambda item: (-len(item[1]), min(row.rank for row in item[1])),
    )[:_MAX_COMBOS]
    combos: list[RosterComboView] = []
    for _, members in ordered:
        best = min(members, key=lambda row: row.rank)
        sorted_entries = _sorted_roster_entries(best)
        combos.append(
            RosterComboView(
                members=_build_roster(
                    sorted_entries,
                    best.roster_summary,
                    web_base_url=web_base_url,
                ),
                count=len(members),
                percent=_share(len(members), len(rows)),
                best_rank=best.rank,
                best_dps=format_number(best.dps),
            )
        )
    return tuple(combos)


def _sorted_roster_entries(
    row: BossRankingRow,
) -> tuple[BossRankingRosterEntry, ...]:
    def order(entry: BossRankingRosterEntry) -> tuple[int, str]:
        try:
            index = _PROFESSION_ORDER.index(entry.profession)
        except ValueError:
            index = len(_PROFESSION_ORDER)
        return index, entry.character_name

    return tuple(sorted(row.roster_entries, key=order))


def _roster_key(row: BossRankingRow) -> tuple[str, ...]:
    names = (
        tuple(entry.character_name for entry in row.roster_entries)
        or row.roster_summary
    )
    return tuple(sorted(names))


def _share(count: int, total: int) -> str:
    if total <= 0:
        return "0%"
    return f"{format_number(round(count / total * 100, 1))}%"


def _bar_width(value: float, peak: float) -> float:
    if peak <= 0 or value <= 0:
        return 0.0
    return round(min(100.0, value / peak * 100), 2)


def _stats_axis_max(rows) -> float:
    """Axis spans the normal-sample whiskers, not the (often extreme) maxima."""

    peak = 0.0
    for row in rows:
        for value in (row.upper_whisker, row.p90, row.p75, row.median):
            if value is not None and value > peak:
                peak = value
    if peak <= 0:
        return 0.0
    # Round up to a "nice" step so the axis labels read cleanly.
    magnitude = 10 ** (len(str(int(peak))) - 1)
    for unit in (1, 2, 2.5, 5, 10):
        candidate = magnitude * unit
        if peak <= candidate:
            return float(candidate)
    return float(math.ceil(peak / magnitude) * magnitude)


def _format_axis_value(value: float) -> str:
    if value >= 10_000:
        return f"{format_number(round(value / 10_000, 1))}万"
    return format_number(round(value))


def build_account_page(
    account: PublicUserRankings,
    *,
    query: str,
    web_base_url: str,
) -> AccountPage:
    """Build one exact public account's best-record overview."""

    rows = account.rankings
    record_count = len(rows)
    best_rank = f"#{min(row.rank for row in rows)}" if rows else "—"
    best_percentile = (
        f"{max(row.score_percent for row in rows)}%" if rows else "—"
    )
    average_percentile = (
        f"{round(sum(row.score_percent for row in rows) / record_count)}%"
        if rows
        else "—"
    )
    return AccountPage(
        header=PageHeader(
            title=account.account_display_name,
            subtitle="公开账号各首领最佳记录",
            query=query,
            matched_name=account.account_id,
            target_type="公开账号",
            footer_note="公开账号 · 当前最佳记录",
        ),
        account_id=account.account_id,
        account_url=public_url(
            web_base_url,
            "records",
            account.account_id,
        ),
        record_count=record_count,
        best_rank=best_rank,
        best_percentile=best_percentile,
        average_percentile=average_percentile,
        show_contract_score=any(
            row.contract_tag_score is not None for row in rows
        ),
        rows=tuple(
            AccountRankingView(
                boss_name=row.boss_name,
                dungeon_name=(
                    row.dungeon_name
                    if row.dungeon_name != row.boss_name
                    else None
                ),
                rank=row.rank,
                percentile=f"{format_number(row.score_percent)}%",
                duration=format_duration(row.duration_ms),
                total_dps=format_number(row.total_dps),
                battle_date=_format_date(row.battle_end_at),
                roster=row.roster_summary,
                contract_score=(
                    format_number(row.contract_tag_score)
                    if row.contract_tag_score is not None
                    else None
                ),
            )
            for row in rows
        ),
    )


def build_battle_page(
    battle: BattleDetailSummary,
    *,
    query: str,
    web_base_url: str,
    export: BattleExport | None = None,
    export_note: str | None = None,
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
        account_url=public_url(
            web_base_url,
            "records",
            battle.uploader_user_id,
        ),
        uploader_display_name=battle.uploader_display_name,
        duration=format_duration(battle.duration_ms),
        total_dps=format_number(battle.total_dps),
        total_damage=format_number(total_damage),
        battle_date=_format_datetime(battle.battle_end_at),
        timer_label="官方计时" if timer_is_official else "计时待核验",
        integrity_label=(
            "结构校验通过" if battle.integrity_verified else "结构校验未通过"
        ),
        contract_score=(
            format_number(battle.contract_tag_score)
            if battle.contract_tag_score is not None
            else None
        ),
        loadouts=_build_loadouts(
            battle,
            web_base_url=web_base_url,
            top_skills=_MAX_CARD_SKILLS,
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
                    f"{participant.total_damage / total_damage * 100:.1f}%"
                    if total_damage > 0
                    else "—"
                ),
                rdps_share=(
                    f"{participant.rdps / total_rdps * 100:.1f}%"
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
                    f"{participant.crit_rate * 100:.1f}%"
                    if participant.crit_rate is not None
                    else "—"
                ),
            )
            for participant in participants
        ),
    )


_MAX_CARD_SKILLS = 3
_MAX_SKILL_ROWS = 12
# The rail runs down the page. The scale is chosen per fight so the chart
# lands near a target height: a short section inside the battle card, a full
# page for 技能轴. Labels slide down when two moves are closer than one text
# line and are dropped when they would drift too far from their node.
_RAIL_CARD_TARGET_PX = 560
_RAIL_CARD_MIN_PPS = 6.0
_RAIL_CARD_MAX_PPS = 24.0
_RAIL_PAGE_TARGET_PX = 1600
_RAIL_PAGE_MIN_PPS = 10.0
_RAIL_PAGE_MAX_PPS = 40.0
_RAIL_MIN_HEIGHT_PX = 200
_RAIL_LINE_PX = 15
_RAIL_TICK_MIN_PX = 34.0
_RAIL_TICK_STEPS_MS = (1_000, 2_000, 5_000, 10_000, 30_000, 60_000)
# Geometry of one lane in CSS pixels: a 2 px rail line at _RAIL_X_PX, bars
# growing rightwards from it by an importance-graded width (the rail line
# carries continuity, the bar length the duration, the bar width and colour
# the kind of move, a dot an instant), labels starting at _RAIL_LABEL_LEFT_PX
# and further right when overlapping moves needed extra columns.
_RAIL_X_PX = 15
_RAIL_BAR_PX = {
    SkillCategory.NORMAL: 8,
    SkillCategory.OTHER: 12,
    SkillCategory.SKILL: 16,
    SkillCategory.COMBO: 16,
    SkillCategory.HEAVY: 16,
    SkillCategory.ULTIMATE: 20,
}
_RAIL_DOT_PX = 7
_RAIL_COLUMN_STEP_PX = 22
_RAIL_LABEL_LEFT_PX = 44
_RAIL_LEAD_MIN_PX = 6
_RAIL_MAX_COLUMNS = 3
_RAIL_BLOCK_MIN_PX = 5
# How far a label may be pushed below its node before it is dropped instead.
_RAIL_LABEL_SLACK_PX = {
    SkillCategory.ULTIMATE: 10_000,
    SkillCategory.COMBO: 24,
    SkillCategory.SKILL: 24,
    SkillCategory.HEAVY: 24,
    SkillCategory.OTHER: 16,
    SkillCategory.NORMAL: 8,
}
_RAIL_SUMMON_LABEL_SLACK_PX = 8
_RAIL_CATEGORY_CSS = {
    SkillCategory.ULTIMATE: "ultimate",
    SkillCategory.SKILL: "skill",
    SkillCategory.COMBO: "combo",
    SkillCategory.HEAVY: "heavy",
    SkillCategory.NORMAL: "normal",
}
_RAIL_LEGEND = (
    ("ultimate", "终结技"),
    ("skill", "战技"),
    ("combo", "连携技"),
    ("heavy", "重击"),
    ("normal", "普攻连段"),
    ("other", "其他"),
)
# The export carries no portrait URLs; this is the path every upstream
# response uses for character portraits. The template drops the image when it
# does not load, so a wrong guess only costs the picture.
_CHARACTER_AVATAR_PATH = "/images/character/charremoteicon/icon_{key}.png"
# Gear icons follow the same convention, keyed by item id / weapon template.
# Upstream omits ``iconUrl`` for pieces its catalog does not know yet, while
# the files themselves exist, so the path is derived when it is missing.
_EQUIP_ICON_PATH = "/images/equip/iconbig/{item_id}.png"
_WEAPON_ICON_PATH = "/images/weapon/icon/{template}.png"
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


def _derived_asset_url(
    explicit: str | None,
    pattern: str,
    key: str | None,
    *,
    web_base_url: str | None,
) -> str | None:
    """The upstream URL when given, else the conventional path for ``key``."""

    if explicit:
        return _safe_asset_url(explicit, base_url=web_base_url)
    if not key or not _ASSET_ID_RE.match(key):
        return None
    path = pattern.format_map({"item_id": key, "template": key})
    return _safe_asset_url(path, base_url=web_base_url)


def build_timeline_page(
    export: BattleExport,
    *,
    query: str,
    web_base_url: str,
) -> TimelinePage:
    """The 技能轴 page: the rail chart at full height."""

    return TimelinePage(
        header=PageHeader(
            title=export.boss_name,
            subtitle=export.dungeon_name,
            query=query,
            matched_name=export.battle_id,
            target_type="技能轴",
            footer_note="公开战报 · 上传时记录的施法序列",
        ),
        battle_id=export.battle_id,
        report_url=public_url(web_base_url, "battle", export.battle_id),
        timeline=build_timeline_view(
            export,
            web_base_url=web_base_url,
            target_height=_RAIL_PAGE_TARGET_PX,
            min_pps=_RAIL_PAGE_MIN_PPS,
            max_pps=_RAIL_PAGE_MAX_PPS,
        ),
    )


def build_timeline_view(
    export: BattleExport,
    *,
    web_base_url: str,
    target_height: int = _RAIL_PAGE_TARGET_PX,
    min_pps: float = _RAIL_PAGE_MIN_PPS,
    max_pps: float = _RAIL_PAGE_MAX_PPS,
) -> TimelineView:
    """One rail per character, time running down, moves as nodes."""

    timeline = build_timeline(export)
    seconds = timeline.duration_ms / 1000
    pps = max(min_pps, min(max_pps, target_height / seconds))
    chart_height = max(_RAIL_MIN_HEIGHT_PX, math.ceil(seconds * pps))

    def y(ms: int) -> int:
        return int(round(min(ms, timeline.duration_ms) / 1000 * pps))

    step = next(
        (
            candidate
            for candidate in _RAIL_TICK_STEPS_MS
            if candidate / 1000 * pps >= _RAIL_TICK_MIN_PX
        ),
        _RAIL_TICK_STEPS_MS[-1],
    )
    ticks = tuple(
        RailTickView(
            top=y(mark), label=_clock_label(mark), major=mark % (step * 5) == 0
        )
        for mark in range(0, timeline.duration_ms + 1, step)
    )
    counts: dict[str, int] = {css: 0 for css, _ in _RAIL_LEGEND}
    for block in timeline.blocks:
        if not block.summon:
            counts[_RAIL_CATEGORY_CSS.get(block.category, "other")] += 1
    return TimelineView(
        chart_height=chart_height,
        scale_label=f"每格 {step // 1000} 秒",
        duration_label=format_duration(export.duration_ms),
        ticks=ticks,
        lanes=tuple(
            _rail_lane_view(
                lane, y=y, chart_height=chart_height, web_base_url=web_base_url
            )
            for lane in timeline.lanes
        ),
        legend=tuple(
            RailLegendView(css=css, label=label, count=counts[css])
            for css, label in _RAIL_LEGEND
            if counts[css]
        ),
        cast_count=len(timeline.blocks),
        ultimate_count=counts["ultimate"],
        hidden_count=timeline.hidden_count,
        clipped_count=timeline.clipped_count,
        has_summon=any(block.summon for block in timeline.blocks),
        has_energy=any(block.recovers_energy for block in timeline.blocks),
        has_instant=any(
            block.instant and not block.summon for block in timeline.blocks
        ),
    )


def _rail_lane_view(
    lane: TimelineLane,
    *,
    y,
    chart_height: int,
    web_base_url: str,
) -> RailLaneView:
    # Own moves that overlap in time (a long-lived entity beside the
    # character's own casts) step right into further columns. Summon casts
    # keep to their own strip beside the rail. Both share one label column,
    # so they are laid out together in time order, and a summoned entity is
    # named once per lane.
    lowest = chart_height - _RAIL_LINE_PX
    columns, column_count = _rail_columns(lane.events)
    label_left = _RAIL_LABEL_LEFT_PX + (column_count - 1) * _RAIL_COLUMN_STEP_PX
    stream = sorted(
        (
            *(
                (event, False, index * _RAIL_COLUMN_STEP_PX)
                for event, index in zip(lane.events, columns, strict=True)
            ),
            *((event, True, 0) for event in lane.summon_events),
        ),
        key=lambda item: (item[0].start_ms, item[1]),
    )
    named_summons: set[str] = set()
    views: list[RailEventView] = []
    next_free = -_RAIL_LINE_PX
    for event, is_summon, left in stream:
        top = y(event.start_ms)
        instant = event.instant and not is_summon
        if instant:
            height = 0
        else:
            height = max(_RAIL_BLOCK_MIN_PX, y(event.end_ms) - top)
            height = max(1, min(height, chart_height - top))
        width = (
            0
            if is_summon
            else _RAIL_BAR_PX.get(event.category, _RAIL_BAR_PX[SkillCategory.OTHER])
        )
        wants_label = True
        slack = _RAIL_LABEL_SLACK_PX.get(event.category, 16)
        if is_summon:
            wants_label = event.name not in named_summons
            named_summons.add(event.name)
            slack = _RAIL_SUMMON_LABEL_SLACK_PX
        # Labels stay inside the lane: clamped at the top edge, and at the
        # bottom the last few lines stack upwards instead of falling off.
        anchor = max(0, min(top - 7, lowest))
        label_top = max(anchor, next_free)
        label_visible = wants_label and label_top - anchor <= slack
        if label_visible and label_top > lowest:
            label_top = lowest
            label_visible = label_top >= next_free
        if label_visible:
            next_free = label_top + _RAIL_LINE_PX
        # A narrow bar sits well left of the label column; a hairline leader
        # ties the two together, but only while the label is still level
        # with the move it names.
        lead_top = lead_left = lead_width = 0
        if label_visible and not is_summon:
            right = _RAIL_X_PX + left + (_RAIL_DOT_PX - 2 if instant else width)
            lead_left = right + 2
            lead_width = label_left - 2 - lead_left
            lead_top = label_top + _RAIL_LINE_PX // 2
            level = (
                abs(lead_top - top) <= 8 if instant else top <= lead_top <= top + height
            )
            if lead_width < _RAIL_LEAD_MIN_PX or not level:
                lead_top = lead_left = lead_width = 0
        views.append(
            RailEventView(
                top=top,
                height=height,
                left=left,
                width=width,
                shape="dot" if instant else "bar",
                landmark=event.category is SkillCategory.ULTIMATE and not is_summon,
                category=_RAIL_CATEGORY_CSS.get(event.category, "other"),
                name=event.name,
                count=event.count,
                time_label=_cast_time_label(event.start_ms),
                label_visible=label_visible,
                label_top=label_top if label_visible else top,
                lead_top=lead_top,
                lead_left=lead_left,
                lead_width=lead_width,
                summon=is_summon,
                energy=event.recovers_energy,
            )
        )
    return RailLaneView(
        character_name=lane.character_name,
        character_initial=_initial(lane.character_name),
        character_avatar_url=(
            _safe_asset_url(
                _CHARACTER_AVATAR_PATH.format(key=lane.character_key),
                base_url=web_base_url,
            )
            if lane.character_key.startswith("chr_")
            else None
        ),
        cast_count=lane.cast_count,
        label_left=label_left,
        events=tuple(views),
    )


def _rail_columns(events) -> tuple[list[int], int]:
    """First-fit column of every own move inside the track, and how many."""

    ends: list[int] = []
    columns: list[int] = []
    for event in events:
        for index, end in enumerate(ends):
            if end <= event.start_ms:
                ends[index] = event.end_ms
                columns.append(index)
                break
        else:
            if len(ends) < _RAIL_MAX_COLUMNS:
                ends.append(event.end_ms)
                columns.append(len(ends) - 1)
            else:
                index = min(range(len(ends)), key=lambda item: ends[item])
                ends[index] = event.end_ms
                columns.append(index)
    return columns, max(1, len(ends))


def _clock_label(ms: int) -> str:
    """``8s`` under a minute, ``1:05`` past it; whole seconds only."""

    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}:{rest:02d}"


def _cast_time_label(ms: int) -> str:
    """Start of a move to a tenth of a second: ``8.5s`` or ``1:05.2``."""

    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}"


_TREND_CHART_TOP = 6.0
_TREND_CHART_BOTTOM = 38.0
_TREND_CHART_HEIGHT = 44.0


def build_trend_page(
    history: AccountHistory,
    *,
    query: str,
    web_base_url: str,
    time_range: str = "30d",
    now: datetime | None = None,
    last_checked: str | None = None,
) -> TrendPage:
    """One stepped line per board from the ranks the watch cycle recorded."""

    current_time = now if now is not None else datetime.now(UTC)
    start = window_start(time_range, now=current_time)
    stamps = [
        stamp
        for board in history.boards
        for point in board.points
        if (stamp := parse_timestamp(point.checked_at)) is not None
    ]
    first_seen = min(stamps) if stamps else None
    axis_start = start if start is not None else (first_seen or current_time)
    rows: list[TrendRowView] = []
    for board in history.boards:
        points = trend_points(board, start=start)
        if not points:
            continue
        rows.append(_trend_row(board, points, axis_start=axis_start, now=current_time))
    rows.sort(key=lambda row: (row.current_rank, row.boss_name))
    tracked_days = (
        (current_time - first_seen).days if first_seen is not None else None
    )
    return TrendPage(
        header=PageHeader(
            title=history.display_name,
            subtitle="关注期间的名次变化",
            query=query,
            matched_name=history.account_id,
            target_type="名次趋势",
            footer_note="公开账号 · 名次通报记录的名次变化",
        ),
        account_id=history.account_id,
        account_url=public_url(web_base_url, "records", history.account_id),
        display_name=history.display_name,
        range_label=_RANGE_LABELS.get(time_range, time_range),
        tracked_since=(
            _format_date(first_seen.isoformat()) if first_seen is not None else "—"
        ),
        tracked_days=(
            "—"
            if tracked_days is None
            else (f"{tracked_days} 天" if tracked_days >= 1 else "不足 1 天")
        ),
        last_checked=_format_datetime(last_checked or current_time.isoformat()),
        board_count=len(rows),
        best_rank=f"#{min(row.current_rank for row in rows)}" if rows else "—",
        improved_count=sum(1 for row in rows if row.delta_kind == "up"),
        declined_count=sum(1 for row in rows if row.delta_kind == "down"),
        rows=tuple(rows),
    )


def _trend_row(
    board: BoardHistory,
    points: tuple[RankPoint, ...],
    *,
    axis_start: datetime,
    now: datetime,
) -> TrendRowView:
    ranks = [point.rank for point in points]
    best, worst = min(ranks), max(ranks)
    span = (now - axis_start).total_seconds()

    def x_of(point: RankPoint) -> float:
        stamp = parse_timestamp(point.checked_at) or axis_start
        if span <= 0:
            return 0.0
        ratio = (stamp - axis_start).total_seconds() / span
        return round(max(0.0, min(100.0, ratio * 100)), 2)

    def y_of(rank: int) -> float:
        if best == worst:
            return round(_TREND_CHART_HEIGHT / 2, 2)
        scale = (_TREND_CHART_BOTTOM - _TREND_CHART_TOP) / (worst - best)
        return round(_TREND_CHART_TOP + (rank - best) * scale, 2)

    coords: list[tuple[float, float]] = []
    for index, point in enumerate(points):
        x, y = x_of(point), y_of(point.rank)
        if index > 0:
            # Hold the previous rank until the moment it changed.
            coords.append((x, coords[-1][1]))
        coords.append((x, y))
    coords.append((100.0, coords[-1][1]))
    start_rank, current_rank = points[0].rank, points[-1].rank
    delta = start_rank - current_rank
    if delta > 0:
        kind, label = "up", f"上升 {delta}"
    elif delta < 0:
        kind, label = "down", f"下降 {-delta}"
    else:
        kind, label = "flat", "持平"
    return TrendRowView(
        boss_name=board.boss_name,
        dungeon_name=board.dungeon_name,
        current_rank=current_rank,
        start_rank=start_rank,
        delta_label=label,
        delta_kind=kind,
        best_rank=best,
        worst_rank=worst,
        polyline=" ".join(f"{x},{y}" for x, y in coords),
        dots=tuple(
            (x_of(point), round(y_of(point.rank) / _TREND_CHART_HEIGHT * 100, 2))
            for point in points
        ),
        axis_top=f"#{best}",
        axis_bottom=f"#{worst}",
        last_change=_format_date(points[-1].checked_at),
        point_count=len(points),
    )


def build_loadout_page(
    battle: BattleDetailSummary,
    *,
    query: str,
    web_base_url: str,
) -> LoadoutPage:
    """Every deployed character's weapon, gear lines and skill levels."""

    loadouts = _build_loadouts(
        battle,
        web_base_url=web_base_url,
        top_skills=_MAX_CARD_SKILLS,
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
        battle_id=battle.battle_id,
        report_url=public_url(web_base_url, "battle", battle.battle_id),
        uploader_display_name=battle.uploader_display_name,
        duration=format_duration(battle.duration_ms),
        total_dps=format_number(battle.total_dps),
        total_damage=format_number(battle.total_damage),
        battle_date=_format_datetime(battle.battle_end_at),
        loadouts=loadouts,
        stat_lines_available=any(
            equip.stats for load in loadouts for equip in load.equips
        ),
        has_inferred_suit=any(
            equip.inferred_suit for load in loadouts for equip in load.equips
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
        battle_id=battle.battle_id,
        report_url=public_url(web_base_url, "battle", battle.battle_id),
        uploader_display_name=battle.uploader_display_name,
        duration=format_duration(battle.duration_ms),
        total_dps=format_number(battle.total_dps),
        total_damage=format_number(battle.total_damage),
        battle_date=_format_datetime(battle.battle_end_at),
        groups=tuple(views),
        has_merged_rows=any(
            row.merged_count > 1 for group in groups for row in group.rows
        ),
    )


def _build_loadouts(
    battle: BattleDetailSummary,
    *,
    web_base_url: str | None,
    top_skills: int,
) -> tuple[LoadoutView, ...]:
    suits = infer_suit_names(battle.roster)
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
        name=_clean_text(weapon.name) or "未知武器",
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
    suit_name = _clean_text(equip.suit_name)
    inferred = False
    if not suit_name:
        token = suit_token(equip.item_id)
        guess = suits.get(token) if token else None
        if guess:
            suit_name, inferred = guess, True
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
        suit_label=suit_name or None,
        inferred_suit=inferred,
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


def _format_stat_value(value: float) -> str:
    """Ratios arrive as fractions (0.1495), flat stats as plain numbers."""

    if 0 < abs(value) < 1:
        return f"{format_number(round(value * 100, 1))}%"
    return format_number(round(value, 1))


def _clean_text(value: str | None) -> str:
    """Collapse whitespace; upstream piece names can carry a stray newline."""

    return " ".join(value.split()) if value else ""


def format_duration(duration_ms: int) -> str:
    """Format milliseconds as ``minutes:seconds.milliseconds``."""

    if duration_ms < 0:
        raise PresentationError("duration cannot be negative")
    minutes, remainder = divmod(duration_ms, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{minutes}:{seconds:02d}.{milliseconds:03d}"


def format_number(value: int | float) -> str:
    """Use grouping separators without inventing insignificant decimals."""

    number = float(value)
    if number.is_integer():
        return f"{number:,.0f}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _build_top_card(
    card: HotBossCard,
    *,
    web_base_url: str | None,
) -> TopCardView:
    return TopCardView(
        dungeon_name=card.dungeon_name,
        boss_name=card.boss_name,
        runs=tuple(
            TopRunView(
                rank=index,
                character_name=run.character_name,
                character_initial=_initial(run.character_name),
                character_avatar_url=_safe_asset_url(
                    run.character_avatar_url,
                    base_url=web_base_url,
                ),
                uploader_nickname=run.uploader_nickname,
                duration=format_duration(run.duration_ms),
                contract_score=(
                    format_number(run.contract_tag_score)
                    if run.contract_tag_score is not None
                    else None
                ),
            )
            for index, run in enumerate(card.top_speed_runs, start=1)
        ),
    )


def _build_roster(
    entries: tuple[BossRankingRosterEntry, ...],
    summary: tuple[str, ...],
    *,
    web_base_url: str | None,
) -> tuple[RosterEntryView, ...]:
    if entries:
        return tuple(
            RosterEntryView(
                character_name=entry.character_name,
                profession=entry.profession,
                character_initial=_initial(entry.character_name),
                avatar_url=_safe_asset_url(
                    entry.avatar_url,
                    base_url=web_base_url,
                ),
            )
            for entry in entries
        )
    return tuple(
        RosterEntryView(
            character_name=name,
            profession="",
            character_initial=_initial(name),
            avatar_url=None,
        )
        for name in summary
    )


def _unique_dungeon_names(cards: tuple[HotBossCard, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(card.dungeon_name for card in cards))


def _group_top_cards(
    cards: tuple[TopCardView, ...],
) -> tuple[TopCardGroupView, ...]:
    """Group cards by dungeon while preserving the upstream result order."""

    grouped: dict[str, list[TopCardView]] = {}
    for card in cards:
        grouped.setdefault(card.dungeon_name, []).append(card)
    return tuple(
        TopCardGroupView(dungeon_name=name, cards=tuple(group_cards))
        for name, group_cards in grouped.items()
    )


def _initial(value: str) -> str:
    return value[:1] or "?"


def _safe_http_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _safe_asset_url(
    value: str | None,
    *,
    base_url: str | None,
) -> str | None:
    """Resolve upstream asset paths (usually site-relative) to safe URLs."""

    if value is None:
        return None
    if base_url is None:
        return _safe_http_url(value)
    resolved = urljoin(f"{base_url.rstrip('/')}/", value)
    return _safe_http_url(resolved)


def public_url(base_url: str, resource: str, identifier: str) -> str:
    path = f"{resource}/{quote(identifier, safe='')}"
    return urljoin(f"{base_url.rstrip('/')}/", path)


def _format_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d"
        )
    except ValueError:
        return value[:10]


def _format_datetime(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return value
