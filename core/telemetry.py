"""Time series derived from one battle's per-hit timeline.

The battle detail response carries a `timelineEvents` list (every damage tick
and every buff application) and a `characterStates` list (the same buffs
already grouped per character). The plugin ignored both until 0.8.0; this
module turns them into the two things a reader can act on:

* the DPS curve — cumulative damage divided by elapsed time, bucketed per
  second, which is the same reading the boards use, so the end of each line
  equals that character's DPS in the participant table;
* the buff coverage rows — one row per buff, bars where it was actually up.

Everything here is pure. Nothing validates the upstream contract either: the
parse layer already dropped whatever it could not read, because these two
sections are additions to a card that must keep rendering without them.
"""

import math
import re
from dataclasses import dataclass

from .loadout import element_label
from .models import BattleBuff, BattleDamagePoint

CURVE_BUCKET_MS = 1000
# The same buff landing on several characters within this window is one
# application (upstream uses the same 80 ms for its own buff axis).
TEAM_MERGE_GAP_MS = 80
MAX_BUFF_ROWS = 14
# Zones that describe damage output; the rest (speed, shields) would only add
# rows nobody reads on a damage report. A debuff on the boss amplifies damage
# through its own set of zones, so the two directions allow different ones.
# Surveyed over the top-3 battles of every board (135 fights, 2026-09-04):
# a team buff carries atk / dmg_inc / amp / combo, plus res for one talent
# that ignores resistance; a debuff on the boss carries 脆弱 / 易伤 / 减抗,
# plus dmg_inc for the elemental reactions that make the boss take more
# damage. amp never appears on a boss, and a self-inflicted 易伤 on a player
# is a drawback rather than damage output, so both stay out. speedup / slow
# are not damage either.
_PLAYER_ZONES = ("atk", "dmg_inc", "amp", "combo", "res")
_ENEMY_ZONES = ("fragile", "vuln_taken", "res", "dmg_inc")
# On the boss, "more damage" means more damage *taken*; the label must say so.
_ENEMY_ZONE_LABELS = {"dmg_inc": "承伤"}
_MAX_EFFECTS_PER_LABEL = 2
_ZONE_LABELS = {
    "atk": "攻击",
    "dmg_inc": "增伤",
    "amp": "增幅",
    "combo": "连击",
    "fragile": "脆弱",
    "vuln_taken": "易伤",
    "res": "减抗",
}
# An effect that applies to everything carries no element in its label.
_ANY_ELEMENTS = frozenset({"all", "any", "unknown", ""})
_EXTRA_ELEMENTS = {"spell": "法术", "crystal": "寒冷"}
_RAW_BUFF_NAME_RE = re.compile(r"^(?:buff|chr|wpn|sk|item|skill)_", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CurvePoint:
    at_ms: int
    # Average DPS from the start of the fight up to ``at_ms``.
    dps: float


@dataclass(frozen=True, slots=True)
class CurveSeries:
    character_name: str
    points: tuple[CurvePoint, ...]
    total_damage: int
    # Average DPS over the whole fight; the last point of the line.
    final_dps: float
    peak_dps: float


@dataclass(frozen=True, slots=True)
class DpsCurve:
    duration_ms: int
    bucket_ms: int
    series: tuple[CurveSeries, ...]
    team: CurveSeries
    peak_dps: float


@dataclass(frozen=True, slots=True)
class BuffSpan:
    start_ms: int
    end_ms: int
    # Characters this application covered.
    targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BuffRow:
    name: str
    effect_label: str
    zone: str
    source_name: str
    spans: tuple[BuffSpan, ...]
    # Milliseconds any target had it, capped at the fight length.
    covered_ms: int
    # True when one application covered the whole roster at once.
    team_wide: bool
    max_targets: int
    # A debuff put on the boss rather than a buff the team received.
    on_enemy: bool = False


@dataclass(frozen=True, slots=True)
class BuffCoverage:
    duration_ms: int
    rows: tuple[BuffRow, ...]
    hidden_rows: int


def build_dps_curve(
    points: tuple[BattleDamagePoint, ...],
    *,
    duration_ms: int,
    character_names: tuple[str, ...],
    bucket_ms: int = CURVE_BUCKET_MS,
    max_points: int = 150,
) -> DpsCurve | None:
    """Average-DPS-to-date per character, or None when there is nothing to draw.

    ``character_names`` fixes the series order (the participant order), so a
    line keeps the colour its character has everywhere else on the card.
    """

    duration = max(duration_ms, 1)
    bucket = max(bucket_ms, 1)
    usable = tuple(
        point
        for point in points
        if point.value > 0 and 0 <= point.at_ms <= duration
    )
    if not usable:
        return None
    bucket_count = max(1, math.ceil(duration / bucket))
    # Cumulative damage per character at the end of every bucket.
    totals: dict[str, list[int]] = {
        name: [0] * bucket_count for name in character_names
    }
    team_totals = [0] * bucket_count
    for point in usable:
        index = min(bucket_count - 1, int(point.at_ms // bucket))
        team_totals[index] += point.value
        row = totals.get(point.character_name)
        if row is not None:
            row[index] += point.value
    # Long fights would emit a point per second per character; thinning keeps
    # the page small without moving the line (kept points are exact).
    step = max(1, math.ceil(bucket_count / max(1, max_points)))
    keep = sorted({*range(0, bucket_count, step), bucket_count - 1})

    def series(name: str, buckets: list[int]) -> CurveSeries:
        running = 0
        points_out: list[CurvePoint] = []
        peak = 0.0
        total = 0
        for index, value in enumerate(buckets):
            running += value
            at_ms = min(duration, (index + 1) * bucket)
            dps = running / max(at_ms / 1000, 1.0)
            peak = max(peak, dps)
            if index in keep:
                points_out.append(CurvePoint(at_ms=at_ms, dps=dps))
            total = running
        return CurveSeries(
            character_name=name,
            points=tuple(points_out),
            total_damage=total,
            final_dps=total / max(duration / 1000, 1.0),
            peak_dps=peak,
        )

    built = tuple(
        series(name, totals[name])
        for name in character_names
        if any(totals[name])
    )
    if not built:
        return None
    team = series("全队", team_totals)
    return DpsCurve(
        duration_ms=duration,
        bucket_ms=bucket,
        series=built,
        team=team,
        # The team line is the sum, so it is always the highest; leaving it out
        # of the peak would let the axis clip it flat at the top.
        peak_dps=max(team.peak_dps, *(entry.peak_dps for entry in built)),
    )


def build_buff_coverage(
    buffs: tuple[BattleBuff, ...],
    *,
    duration_ms: int,
    roster_names: tuple[str, ...],
    max_rows: int = MAX_BUFF_ROWS,
) -> BuffCoverage | None:
    """Group buff applications into one row per buff, or None when there are none.

    Rows are picked by how much of the fight they covered (the buffs worth
    reading) and then ordered by when they first landed, so the band reads
    chronologically.
    """

    duration = max(duration_ms, 1)
    roster = set(roster_names)
    # Rows are keyed by what a reader can tell apart — the name, the effect
    # and who applied it. Upstream splits the same buff across several event
    # keys (a caster variant, an owner variant, a weapon tag), which would
    # otherwise draw three identical rows instead of one uptime bar.
    grouped: dict[tuple[bool, str, str, str], list[BattleBuff]] = {}
    for buff in buffs:
        # A team buff has to land on someone in the roster; a debuff lands on
        # whatever the boss happens to be called this fight.
        if not buff.on_enemy and buff.target_name not in roster:
            continue
        if buff.duration_ms is None or buff.duration_ms <= 0:
            continue
        if buff.start_ms > duration:
            continue
        effect = _effect_label(buff)
        if effect is None:
            continue
        name = _buff_name(buff, effect)
        key = (buff.on_enemy, name, effect, buff.source_name or "")
        grouped.setdefault(key, []).append(buff)
    rows: list[BuffRow] = []
    for (on_enemy, name, effect, _), entries in grouped.items():
        spans = _merge_spans(entries, duration=duration)
        if not spans:
            continue
        first = min(entries, key=lambda item: item.start_ms)
        zone = _leading_zone(first)
        rows.append(
            BuffRow(
                name=name,
                effect_label=effect,
                zone=zone or "",
                source_name=first.source_name or "未记录",
                spans=spans,
                covered_ms=sum(span.end_ms - span.start_ms for span in spans),
                team_wide=not on_enemy
                and any(len(span.targets) >= len(roster) for span in spans),
                max_targets=max(len(span.targets) for span in spans),
                on_enemy=on_enemy,
            )
        )
    if not rows:
        return None
    kept = sorted(rows, key=lambda row: (-row.covered_ms, row.effect_label))[
        :max_rows
    ]
    # Team buffs first, boss debuffs after, each in the order they landed:
    # "what we gained" then "what the boss took" reads better than one
    # interleaved list.
    kept.sort(key=lambda row: (row.on_enemy, row.spans[0].start_ms, row.effect_label))
    return BuffCoverage(
        duration_ms=duration,
        rows=tuple(kept),
        hidden_rows=len(rows) - len(kept),
    )


def _merge_spans(
    entries: list[BattleBuff],
    *,
    duration: int,
) -> tuple[BuffSpan, ...]:
    """One span per application; simultaneous targets fold into one span."""

    applications: list[list[BattleBuff]] = []
    for buff in sorted(entries, key=lambda item: item.start_ms):
        if (
            applications
            and buff.start_ms - applications[-1][0].start_ms <= TEAM_MERGE_GAP_MS
        ):
            applications[-1].append(buff)
        else:
            applications.append([buff])
    spans: list[BuffSpan] = []
    for group in applications:
        start = max(0, min(item.start_ms for item in group))
        end = min(
            duration,
            max(item.start_ms + (item.duration_ms or 0) for item in group),
        )
        if end <= start:
            continue
        targets = tuple(
            dict.fromkeys(item.target_name for item in group if item.target_name)
        )
        # A later application of the same buff often overlaps the previous one
        # (a refresh); merge so the row shows uptime rather than stacked bars.
        if spans and start <= spans[-1].end_ms:
            previous = spans[-1]
            spans[-1] = BuffSpan(
                start_ms=previous.start_ms,
                end_ms=max(previous.end_ms, end),
                targets=tuple(dict.fromkeys((*previous.targets, *targets))),
            )
            continue
        spans.append(BuffSpan(start_ms=start, end_ms=end, targets=targets))
    return tuple(spans)


def _effect_label(buff: BattleBuff) -> str | None:
    """``攻击 +16%``, or ``攻击 +16% · 增伤 +20%`` for a buff with two damage
    effects; None when nothing on it changes damage."""

    allowed = _ENEMY_ZONES if buff.on_enemy else _PLAYER_ZONES
    parts: list[str] = []
    for effect in buff.effects:
        zone = (effect.zone or "").lower()
        if zone not in allowed or effect.rate is None:
            continue
        raw = (effect.element or "").lower()
        if raw in _ANY_ELEMENTS:
            element = ""
        else:
            element = _EXTRA_ELEMENTS.get(raw) or element_label(effect.element) or ""
        label = (
            _ENEMY_ZONE_LABELS.get(zone, _ZONE_LABELS[zone])
            if buff.on_enemy
            else _ZONE_LABELS[zone]
        )
        parts.append(
            f"{label}{element} "
            f"{'+' if effect.rate >= 0 else ''}{_format_rate(effect.rate)}"
        )
        if len(parts) == _MAX_EFFECTS_PER_LABEL:
            break
    return " · ".join(parts) if parts else None


def _leading_zone(buff: BattleBuff) -> str | None:
    allowed = _ENEMY_ZONES if buff.on_enemy else _PLAYER_ZONES
    for effect in buff.effects:
        zone = (effect.zone or "").lower()
        if zone in allowed and effect.rate is not None:
            return zone
    return None


def _format_rate(rate: float) -> str:
    percent = rate * 100
    digits = 1 if abs(percent) >= 10 else 2
    return f"{percent:.{digits}f}".rstrip("0").rstrip(".") + "%"


def _buff_name(buff: BattleBuff, effect: str) -> str:
    """The buff's own name, or empty when upstream only had a raw key.

    Empty rather than the effect text: the effect is already printed beside
    the name, and repeating it reads as a stutter.
    """

    name = " ".join((buff.name or "").split())
    if not name or name == buff.event_key or _RAW_BUFF_NAME_RE.match(name):
        return ""
    if buff.on_enemy and name == "增伤":
        # Upstream names the reaction debuff from the attacker's point of
        # view; beside the boss it has to read as damage taken.
        return _ENEMY_ZONE_LABELS["dmg_inc"]
    return name
