"""DPS curve, BUFF coverage band and 暴击期望's bell, shared by the battle pages."""

import math
import re
from dataclasses import dataclass

from ..crit import CritTotals
from ..models import (
    BattleDetailSummary,
    BattleParticipant,
)
from ..telemetry import build_buff_coverage, build_dps_curve
from .common import (
    _format_axis_value,
    _nice_ceiling,
    _share,
    format_duration,
    format_number,
)


@dataclass(frozen=True, slots=True)
class RailTickView:
    top: int
    label: str
    major: bool


@dataclass(frozen=True, slots=True)
class CurveSeriesView:
    character_name: str
    # 1-6, matching the colour the character wears on every battle page.
    colour_index: int
    polyline: str
    # Whole units, as the V2 pages print every DPS.
    final_dps: str
    damage_share: str


@dataclass(frozen=True, slots=True)
class DpsCurveView:
    series: tuple[CurveSeriesView, ...]
    team_polyline: str
    team_dps: str
    axis_labels: tuple[str, ...]
    # The same second-based ticks the buff band uses, so the two charts share
    # one time axis when stacked.
    ticks: tuple[RailTickView, ...]
    peak_label: str
    bucket_label: str


@dataclass(frozen=True, slots=True)
class BuffSpanView:
    left: float
    width: float
    target_label: str


@dataclass(frozen=True, slots=True)
class BuffRowView:
    name: str
    effect_label: str
    zone: str
    source_name: str
    coverage_label: str
    target_label: str
    team_wide: bool
    # A debuff on the boss; drawn in the danger colour, not a team colour.
    on_enemy: bool
    spans: tuple[BuffSpanView, ...]


@dataclass(frozen=True, slots=True)
class BuffBandView:
    rows: tuple[BuffRowView, ...]
    ticks: tuple[RailTickView, ...]
    duration_label: str
    hidden_rows: int


# The DPS curve is drawn as an SVG polyline in a 0-100 box, scaled by the
# template; the buff band is HTML positioned by percentages.
_CURVE_MAX_POINTS = 150


_BUFF_TICK_MIN_PERCENT = 6.0


_ENEMY_KEY_RE = re.compile(r"^eny_[A-Za-z0-9_]*$")


_BUFF_SPAN_MIN_PERCENT = 0.6


def build_dps_curve_view(
    battle: BattleDetailSummary,
    participants: tuple[BattleParticipant, ...],
) -> DpsCurveView | None:
    """Average-DPS-to-date lines, one per participant, or None without data.

    Each line ends at that character's DPS in the participant table above,
    because both are the same quantity read at the same instant.
    ``participants`` must already be in the card's display order so a line
    keeps the colour its character has in every other section.
    """

    curve = build_dps_curve(
        battle.damage_points,
        duration_ms=battle.duration_ms,
        character_names=tuple(
            participant.character_name for participant in participants
        ),
        max_points=_CURVE_MAX_POINTS,
    )
    if curve is None or len(curve.team.points) < 2:
        return None
    axis_max = _nice_ceiling(curve.peak_dps)
    if axis_max <= 0:
        return None
    colours = colour_keys(participants)

    def polyline(series) -> str:
        return polyline_points(
            series.points, duration_ms=curve.duration_ms, axis_max=axis_max
        )

    total_damage = max(battle.total_damage, 1)
    return DpsCurveView(
        series=tuple(
            CurveSeriesView(
                character_name=series.character_name,
                colour_index=colours.get(series.character_name, 6),
                polyline=polyline(series),
                final_dps=format_number(round(series.final_dps)),
                damage_share=_share(series.total_damage, total_damage),
            )
            for series in curve.series
        ),
        team_polyline=polyline(curve.team),
        team_dps=format_number(round(curve.team.final_dps)),
        axis_labels=tuple(
            _format_axis_value(axis_max * (4 - index) / 4) for index in range(5)
        ),
        ticks=_horizontal_ticks(curve.duration_ms),
        peak_label=format_number(round(curve.peak_dps)),
        bucket_label=f"{curve.bucket_ms // 1000} 秒",
    )


def colour_keys(participants: tuple[BattleParticipant, ...]) -> dict[str, int]:
    """Each character's colour key (the class ``cN``), by DPS order.

    Every chart of a battle's pages draws a character in this one colour;
    ``participants`` must already be in the display order, highest DPS first.
    """

    return {
        participant.character_name: index
        for index, participant in enumerate(participants, start=1)
    }


def build_buff_band_view(battle: BattleDetailSummary) -> BuffBandView | None:
    """Buff uptime rows on a left-to-right time axis, or None without data."""

    coverage = build_buff_coverage(
        battle.buffs,
        duration_ms=battle.duration_ms,
        roster_names=tuple(entry.character_name for entry in battle.roster),
    )
    if coverage is None:
        return None
    duration = coverage.duration_ms
    roster_size = len(battle.roster)
    ticks = _horizontal_ticks(duration)
    rows: list[BuffRowView] = []
    for row in coverage.rows:
        spans = []
        for span in row.spans:
            left = round(span.start_ms / duration * 100, 3)
            width = max(
                _BUFF_SPAN_MIN_PERCENT,
                round((span.end_ms - span.start_ms) / duration * 100, 3),
            )
            spans.append(
                BuffSpanView(
                    left=left,
                    width=min(width, round(100.0 - left, 3)),
                    target_label=_buff_target_label(
                        span.targets,
                        roster_size=roster_size,
                        on_enemy=row.on_enemy,
                    ),
                )
            )
        rows.append(
            BuffRowView(
                name=row.name,
                effect_label=row.effect_label,
                zone=row.zone or "other",
                source_name=row.source_name,
                coverage_label=f"{row.covered_ms / duration * 100:.0f}%",
                target_label=_buff_target_label(
                    tuple(
                        dict.fromkeys(
                            target for span in row.spans for target in span.targets
                        )
                    ),
                    roster_size=roster_size,
                    on_enemy=row.on_enemy,
                ),
                team_wide=row.team_wide,
                on_enemy=row.on_enemy,
                spans=tuple(spans),
            )
        )
    return BuffBandView(
        rows=tuple(rows),
        ticks=ticks,
        duration_label=format_duration(duration),
        hidden_rows=coverage.hidden_rows,
    )


def _horizontal_ticks(duration_ms: int) -> tuple[RailTickView, ...]:
    """Second-based ticks across a left-to-right axis, as percents of it.

    Shared by the DPS curve and the buff band, which sit one above the other
    with the same column widths, so their tick lines fall on the same x.
    """

    duration = max(duration_ms, 1)
    step = next(
        (
            candidate
            for candidate in _RAIL_TICK_STEPS_MS
            if candidate / duration * 100 >= _BUFF_TICK_MIN_PERCENT
        ),
        _RAIL_TICK_STEPS_MS[-1],
    )
    return tuple(
        RailTickView(
            top=round(mark / duration * 100, 3),
            label=_clock_label(mark),
            major=mark % (step * 5) == 0,
        )
        for mark in range(0, duration + 1, step)
    )


def _buff_target_label(
    targets: tuple[str, ...],
    *,
    roster_size: int,
    on_enemy: bool = False,
) -> str:
    if on_enemy:
        # Upstream names the boss inconsistently: sometimes its display name,
        # sometimes the raw ``eny_*`` key, and a wave fight has several.
        named = tuple(
            target for target in targets if not _ENEMY_KEY_RE.match(target)
        )
        if len(named) == 1 and len(targets) == 1:
            return named[0]
        return "敌方"
    if roster_size and len(targets) >= roster_size:
        return "全队"
    if not targets:
        return "—"
    if len(targets) == 1:
        return targets[0]
    return f"{len(targets)} 人"


_RAIL_TICK_STEPS_MS = (1_000, 2_000, 5_000, 10_000, 30_000, 60_000)


def _clock_label(ms: int) -> str:
    """``8s`` under a minute, ``1:05`` past it; whole seconds only."""

    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}:{rest:02d}"


def polyline_points(points, *, duration_ms: int, axis_max: float) -> str:
    """SVG polyline coordinates in a 100×100 box: time across, DPS up."""

    return " ".join(
        f"{point.at_ms / duration_ms * 100:.2f},"
        f"{100 - min(point.dps, axis_max) / axis_max * 100:.2f}"
        for point in points
    )


@dataclass(frozen=True, slots=True)
class CritBellView:
    """暴击期望's distribution, drawn as the bell its mean and spread describe.

    The curve is SVG in a ``width`` × ``height`` box the template stretches;
    everything placed over it is a percent of the plot's width.
    """

    width: int
    height: int
    line: str
    area: str
    expected_left: float
    actual_left: float
    # ±1σ around the expectation.
    spread_left: float
    spread_width: float
    # ``(left, label)``, six of them from edge to edge.
    ticks: tuple[tuple[float, str], ...]
    # The two marks stand so close that their labels, centred, would
    # overlap; each label then leans away from the other.
    marks_close: bool
    actual_below: bool


_BELL_WIDTH = 1000
_BELL_HEIGHT = 260
_BELL_STEPS = 160
# The peak sits this far under the top, so the curve's stroke is not cut.
_BELL_HEADROOM = 16
# Labels about 40 px wide on a plot about 600 px wide: closer than this,
# centred, they touch.
_BELL_CLOSE_PERCENT = 8.0


def build_crit_bell(totals: CritTotals) -> CritBellView | None:
    """The team's damage as a normal curve, and where the run landed on it.

    A normal approximation of the site's exact histogram: the expected
    total and its standard deviation are all :mod:`core.crit` computes,
    and over the thousands of hits of a run the two hardly differ. The
    axis runs four deviations either side, widened to keep the actual
    total half a deviation inside it. None when there is no spread to
    draw (every analysed hit certain to crit or not).
    """

    mean = totals.expected_damage
    spread = totals.standard_deviation
    actual = totals.actual_damage
    if spread <= 0 or mean <= 0:
        return None
    low = min(mean - 4 * spread, actual - 0.5 * spread)
    high = max(mean + 4 * spread, actual + 0.5 * spread)
    span = high - low

    def x(value: float) -> float:
        return (value - low) / span * _BELL_WIDTH

    def percent(value: float) -> float:
        return round((value - low) / span * 100, 2)

    points = []
    for step in range(_BELL_STEPS + 1):
        value = low + span * step / _BELL_STEPS
        density = math.exp(-0.5 * ((value - mean) / spread) ** 2)
        y = _BELL_HEIGHT - density * (_BELL_HEIGHT - _BELL_HEADROOM)
        points.append(f"{x(value):.1f},{y:.1f}")
    expected_left = percent(mean)
    actual_left = percent(actual)
    return CritBellView(
        width=_BELL_WIDTH,
        height=_BELL_HEIGHT,
        line="M" + " L".join(points),
        area=(
            f"M0,{_BELL_HEIGHT} L" + " L".join(points)
            + f" L{_BELL_WIDTH},{_BELL_HEIGHT} Z"
        ),
        expected_left=expected_left,
        actual_left=actual_left,
        spread_left=percent(mean - spread),
        spread_width=round(percent(mean + spread) - percent(mean - spread), 2),
        ticks=tuple(
            (round(index * 20.0, 2), _wan(low + span * index / 5))
            for index in range(6)
        ),
        marks_close=abs(actual_left - expected_left) < _BELL_CLOSE_PERCENT,
        actual_below=actual < mean,
    )


def _wan(value: float) -> str:
    """A damage total on the bell's ruler, in 万 to one decimal: ``638.6万``."""

    return f"{format_number(round(value / 10_000, 1))}万"
