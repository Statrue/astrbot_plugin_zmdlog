"""Character statistics per board and one character across boards."""

import math
from dataclasses import dataclass

from ..metrics import metric_label
from ..models import (
    CharacterBossStatistics,
    CharacterStatistics,
)
from .common import (
    _RANGE_LABELS,
    PageHeader,
    _format_axis_value,
    _initial,
    _safe_asset_url,
    format_number,
)


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
    metric_label: str = "DPS"


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
    metric_label: str = "DPS"


_POTENTIAL_LABELS = {"0": "0 潜能", "1-5": "1–5 潜能", "all": "全部潜能"}


def _box_plot(row, axis_max: float) -> dict[str, object]:
    """One row's box-plot geometry as percentages of the shared axis.

    The same twelve fields on both statistics pages: sample counts, the
    formatted median and maximum, and whisker / box / median / p10 / p90
    positions, with an outlier maximum pinned to the right edge.
    """

    def percent(value: float | None) -> float:
        if value is None or axis_max <= 0:
            return 0.0
        return round(max(0.0, min(100.0, value / axis_max * 100)), 2)

    low = row.lower_whisker if row.lower_whisker is not None else row.p25
    high = row.upper_whisker if row.upper_whisker is not None else row.p75
    p25 = row.p25 if row.p25 is not None else row.median
    p75 = row.p75 if row.p75 is not None else row.median
    whisker_left = percent(low)
    box_left = percent(p25)
    maximum_left = None
    if row.maximum is not None and high is not None and row.maximum > high:
        maximum_left = min(percent(row.maximum), 100.0)
    return {
        "sample_count": row.normal_sample_count,
        "outlier_count": row.outlier_count,
        "median": format_number(row.median) if row.median is not None else "—",
        "maximum": format_number(row.maximum) if row.maximum is not None else "—",
        "whisker_left": whisker_left,
        "whisker_width": round(max(0.0, percent(high) - whisker_left), 2),
        "box_left": box_left,
        "box_width": round(max(0.0, percent(p75) - box_left), 2),
        "median_left": percent(row.median),
        "p10_left": percent(row.p10 if row.p10 is not None else p25),
        "p90_left": percent(row.p90 if row.p90 is not None else p75),
        "maximum_left": maximum_left,
    }


def build_character_stats_page(
    stats: CharacterStatistics,
    *,
    query: str,
    web_base_url: str | None = None,
) -> CharacterStatsPage:
    """Turn upstream percentile rows into box-plot geometry for the template."""

    is_global = stats.scope == "all"
    title = "全部榜单" if is_global else stats.boss_name
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

    rows = tuple(
        CharacterStatRowView(
            rank=row.rank or 0,
            character_name=row.character_name,
            character_profession=row.character_profession,
            character_initial=_initial(row.character_name),
            character_avatar_url=_safe_asset_url(
                row.character_avatar_url, base_url=web_base_url
            ),
            **_box_plot(row, axis_max),
        )
        for row in ranked
    )

    insufficient = tuple(
        CharacterStatChipView(
            character_name=row.character_name,
            sample_count=row.sample_count,
        )
        for row in stats.rows
        if row.insufficient_samples and row.sample_count > 0
    )
    label = metric_label(stats.metric)
    return CharacterStatsPage(
        header=PageHeader(
            title=title,
            subtitle=subtitle,
            query=query,
            matched_name=matched_name,
            target_type="角色统计",
            footer_note=f"公开战斗 · 六星角色 {label} 分布",
            metric=stats.metric,
        ),
        metric_label=label,
        scope_label="全部榜单" if is_global else "单个榜单",
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

    rows = tuple(
        CharacterBossRowView(
            boss_name=row.boss_name,
            dungeon_name=row.dungeon_name,
            rank=row.rank or 0,
            ranked_character_count=row.ranked_character_count,
            **_box_plot(row, axis_max),
        )
        for row in ranked
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
            footer_note=f"公开战斗 · 单角色全榜单 {metric_label(stats.metric)} 分布",
            metric=stats.metric,
        ),
        metric_label=metric_label(stats.metric),
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
