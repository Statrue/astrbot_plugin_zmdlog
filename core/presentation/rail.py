"""The vertical cast rail (施法节奏 / 技能轴)."""

import math
from dataclasses import dataclass

from ..loadout import (
    SkillCategory,
)
from ..models import (
    BattleDetailSummary,
    BattleExport,
)
from ..timeline import TimelineLane, build_timeline
from .charts import (
    _RAIL_TICK_STEPS_MS,
    BuffBandView,
    RailTickView,
    _clock_label,
    build_buff_band_view,
)
from .common import (
    _CHARACTER_AVATAR_PATH,
    PageHeader,
    _initial,
    _safe_asset_url,
    format_duration,
    public_url,
)


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
    # From the battle detail, when the page could also fetch it.
    buff_band: BuffBandView | None = None


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


def build_timeline_page(
    export: BattleExport,
    *,
    query: str,
    web_base_url: str,
    battle: BattleDetailSummary | None = None,
) -> TimelinePage:
    """The 技能轴 page: the rail chart at full height.

    ``battle`` is the detail response when the caller could get it; it only
    adds the BUFF 覆盖 band above the rail, so the page stands without it.
    """

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
        buff_band=build_buff_band_view(battle) if battle is not None else None,
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


def _cast_time_label(ms: int) -> str:
    """Start of a move to a tenth of a second: ``8.5s`` or ``1:05.2``."""

    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}"
