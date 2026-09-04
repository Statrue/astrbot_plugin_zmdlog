"""DPS curve and BUFF coverage band, shared by the battle pages."""

import re
from dataclasses import dataclass

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
    # 1-6, matching the participant colour keys used everywhere on the card.
    colour_index: int
    polyline: str
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
    colours = {
        participant.character_name: index
        for index, participant in enumerate(participants, start=1)
    }

    def polyline(series) -> str:
        return " ".join(
            f"{point.at_ms / curve.duration_ms * 100:.2f},"
            f"{100 - min(point.dps, axis_max) / axis_max * 100:.2f}"
            for point in series.points
        )

    total_damage = max(battle.total_damage, 1)
    return DpsCurveView(
        series=tuple(
            CurveSeriesView(
                character_name=series.character_name,
                colour_index=colours.get(series.character_name, 6),
                polyline=polyline(series),
                final_dps=format_number(round(series.final_dps, 2)),
                damage_share=_share(series.total_damage, total_damage),
            )
            for series in curve.series
        ),
        team_polyline=polyline(curve.team),
        team_dps=format_number(round(curve.team.final_dps, 2)),
        axis_labels=tuple(
            _format_axis_value(axis_max * (4 - index) / 4) for index in range(5)
        ),
        ticks=_horizontal_ticks(curve.duration_ms),
        peak_label=format_number(round(curve.peak_dps)),
        bucket_label=f"{curve.bucket_ms // 1000} 秒",
    )


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
