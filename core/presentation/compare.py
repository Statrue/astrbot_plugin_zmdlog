"""Two battles side by side."""

from dataclasses import dataclass

from ..models import (
    BattleDetailSummary,
)
from ..telemetry import build_dps_curve
from .battle import (
    BattlePage,
    BattleParticipantView,
    EquipView,
    LoadoutView,
    SkillRowView,
    build_battle_page,
)
from .charts import (
    _CURVE_MAX_POINTS,
    RailTickView,
    _horizontal_ticks,
)
from .common import (
    PageHeader,
    _format_axis_value,
    _nice_ceiling,
    format_number,
)


@dataclass(frozen=True, slots=True)
class CompareFactView:
    label: str
    a: str
    b: str
    # "a" / "b" for the side that is better on this fact, "" when equal or
    # when the fact is not a quantity.
    better: str
    delta_label: str


@dataclass(frozen=True, slots=True)
class CompareRosterRowView:
    character_name: str
    character_initial: str
    character_avatar_url: str | None
    a: BattleParticipantView | None
    b: BattleParticipantView | None


@dataclass(frozen=True, slots=True)
class CompareCurveView:
    polyline_a: str
    polyline_b: str
    axis_labels: tuple[str, ...]
    ticks: tuple[RailTickView, ...]
    # Where each fight ends on the shared axis, as a percent of it.
    end_a: float
    end_b: float
    dps_a: str
    dps_b: str


@dataclass(frozen=True, slots=True)
class CompareGearLineView:
    label: str
    a: str
    b: str
    differs: bool


@dataclass(frozen=True, slots=True)
class CompareLoadoutRowView:
    character_name: str
    character_initial: str
    character_avatar_url: str | None
    # "" when the character fought on both sides, else the side they were on.
    only: str
    lines: tuple[CompareGearLineView, ...]
    skills_a: tuple[SkillRowView, ...]
    skills_b: tuple[SkillRowView, ...]


@dataclass(frozen=True, slots=True)
class CompareSideView:
    label: str
    rank_label: str | None
    battle_id: str
    report_url: str
    uploader_display_name: str
    battle_date: str
    duration: str
    total_dps: str
    total_damage: str


@dataclass(frozen=True, slots=True)
class ComparePage:
    header: PageHeader
    sides: tuple[CompareSideView, CompareSideView]
    facts: tuple[CompareFactView, ...]
    roster: tuple[CompareRosterRowView, ...]
    curve: CompareCurveView | None
    loadouts: tuple[CompareLoadoutRowView, ...]


def build_compare_page(
    a: BattleDetailSummary,
    b: BattleDetailSummary,
    *,
    query: str,
    web_base_url: str,
    rank_a: int | None = None,
    rank_b: int | None = None,
    suits: dict[str, str] | None = None,
) -> ComparePage:
    """Two battles side by side: summary facts, rosters, curves, gear.

    Built from the same per-battle views the card uses, so every number here
    is the number the 战报 card would print for that fight on its own.
    """

    page_a = build_battle_page(a, query=query, web_base_url=web_base_url)
    page_b = build_battle_page(b, query=query, web_base_url=web_base_url)
    # Both fights are on the same boss: the handler refuses anything else,
    # because every boss has its own rotation and a cross-boss page would
    # compare two different games.
    sides = (
        _compare_side("A", a, page_a, rank_a),
        _compare_side("B", b, page_b, rank_b),
    )
    return ComparePage(
        header=PageHeader(
            title=a.boss_name,
            subtitle=a.dungeon_name,
            query=query,
            matched_name=f"{a.battle_id} vs {b.battle_id}",
            target_type="战报对比",
            footer_note="公开战报 · DPS 口径 · A 与 B 并排",
        ),
        sides=sides,
        facts=_compare_facts(a, b, page_a, page_b),
        roster=_compare_roster(page_a, page_b),
        curve=_compare_curve(a, b),
        loadouts=_compare_loadouts(page_a, page_b),
    )


def _compare_side(
    label: str,
    battle: BattleDetailSummary,
    page: BattlePage,
    rank: int | None,
) -> CompareSideView:
    return CompareSideView(
        label=label,
        rank_label=f"第 {rank} 名" if rank is not None else None,
        battle_id=battle.battle_id,
        report_url=page.report_url,
        uploader_display_name=page.uploader_display_name,
        battle_date=page.battle_date,
        duration=page.duration,
        total_dps=page.total_dps,
        total_damage=page.total_damage,
    )


def _compare_facts(
    a: BattleDetailSummary,
    b: BattleDetailSummary,
    page_a: BattlePage,
    page_b: BattlePage,
) -> tuple[CompareFactView, ...]:
    facts = [
        CompareFactView(
            label="通关时间",
            a=page_a.duration,
            b=page_b.duration,
            better=_better(a.duration_ms, b.duration_ms, lower_is_better=True),
            delta_label=_seconds_delta(a.duration_ms, b.duration_ms),
        ),
        CompareFactView(
            label="总 DPS",
            a=page_a.total_dps,
            b=page_b.total_dps,
            better=_better(a.total_dps, b.total_dps),
            delta_label=_percent_delta(a.total_dps, b.total_dps),
        ),
        CompareFactView(
            label="总伤害",
            a=page_a.total_damage,
            b=page_b.total_damage,
            better=_better(a.total_damage, b.total_damage),
            delta_label=_percent_delta(a.total_damage, b.total_damage),
        ),
    ]
    lead_a = page_a.participants[0] if page_a.participants else None
    lead_b = page_b.participants[0] if page_b.participants else None
    if lead_a is not None and lead_b is not None:
        facts.append(
            CompareFactView(
                label="主 C",
                a=f"{lead_a.character_name} · {lead_a.damage_share}",
                b=f"{lead_b.character_name} · {lead_b.damage_share}",
                better="",
                delta_label=(
                    "同一主 C"
                    if lead_a.character_name == lead_b.character_name
                    else "主 C 不同"
                ),
            )
        )
        dps_a = a.participants[0].dps
        dps_b = b.participants[0].dps
        facts.append(
            CompareFactView(
                label="主 C DPS",
                a=lead_a.dps,
                b=lead_b.dps,
                better=_better(dps_a, dps_b),
                delta_label=_percent_delta(dps_a, dps_b),
            )
        )
    return tuple(facts)


def _better(a: float, b: float, *, lower_is_better: bool = False) -> str:
    if a == b:
        return ""
    a_wins = a < b if lower_is_better else a > b
    return "a" if a_wins else "b"


def _seconds_delta(a_ms: int, b_ms: int) -> str:
    if a_ms == b_ms:
        return "相同"
    faster = "A" if a_ms < b_ms else "B"
    return f"{faster} 快 {abs(a_ms - b_ms) / 1000:.2f} 秒"


def _percent_delta(a: float, b: float) -> str:
    if a == b:
        return "相同"
    higher, base = ("A", b) if a > b else ("B", a)
    if base <= 0:
        return f"{higher} 更高"
    return f"{higher} 高 {abs(a - b) / base * 100:.1f}%"


def _compare_roster(
    page_a: BattlePage,
    page_b: BattlePage,
) -> tuple[CompareRosterRowView, ...]:
    by_a = {view.character_name: view for view in page_a.participants}
    by_b = {view.character_name: view for view in page_b.participants}
    names = list(dict.fromkeys((*by_a, *by_b)))
    rows = []
    for name in names:
        source = by_a.get(name) or by_b[name]
        rows.append(
            CompareRosterRowView(
                character_name=name,
                character_initial=source.character_initial,
                character_avatar_url=source.character_avatar_url,
                a=by_a.get(name),
                b=by_b.get(name),
            )
        )
    return tuple(rows)


def _compare_curve(
    a: BattleDetailSummary,
    b: BattleDetailSummary,
) -> CompareCurveView | None:
    """Both team lines on one axis; the shorter fight's line stops early."""

    curve_a = build_dps_curve(
        a.damage_points,
        duration_ms=a.duration_ms,
        character_names=tuple(p.character_name for p in a.participants),
        max_points=_CURVE_MAX_POINTS,
    )
    curve_b = build_dps_curve(
        b.damage_points,
        duration_ms=b.duration_ms,
        character_names=tuple(p.character_name for p in b.participants),
        max_points=_CURVE_MAX_POINTS,
    )
    if curve_a is None or curve_b is None:
        return None
    if len(curve_a.team.points) < 2 or len(curve_b.team.points) < 2:
        return None
    shared_ms = max(curve_a.duration_ms, curve_b.duration_ms, 1)
    axis_max = _nice_ceiling(max(curve_a.team.peak_dps, curve_b.team.peak_dps))
    if axis_max <= 0:
        return None

    def polyline(series) -> str:
        return " ".join(
            f"{point.at_ms / shared_ms * 100:.2f},"
            f"{100 - min(point.dps, axis_max) / axis_max * 100:.2f}"
            for point in series.points
        )

    return CompareCurveView(
        polyline_a=polyline(curve_a.team),
        polyline_b=polyline(curve_b.team),
        axis_labels=tuple(
            _format_axis_value(axis_max * (4 - index) / 4) for index in range(5)
        ),
        ticks=_horizontal_ticks(shared_ms),
        end_a=round(curve_a.duration_ms / shared_ms * 100, 2),
        end_b=round(curve_b.duration_ms / shared_ms * 100, 2),
        dps_a=format_number(round(curve_a.team.final_dps, 2)),
        dps_b=format_number(round(curve_b.team.final_dps, 2)),
    )


_COMPARE_PARTS = ("护手", "护甲", "配件", "配件")


def _compare_loadouts(
    page_a: BattlePage,
    page_b: BattlePage,
) -> tuple[CompareLoadoutRowView, ...]:
    by_a = {view.character_name: view for view in page_a.loadouts}
    by_b = {view.character_name: view for view in page_b.loadouts}
    rows = []
    for name in dict.fromkeys((*by_a, *by_b)):
        left, right = by_a.get(name), by_b.get(name)
        source = left or right
        pairs = _pair_accessories(_accessories(left), _accessories(right))
        lines_a = _gear_lines(left, [pair[0] for pair in pairs])
        lines_b = _gear_lines(right, [pair[1] for pair in pairs])
        rows.append(
            CompareLoadoutRowView(
                character_name=name,
                character_initial=source.character_initial,
                character_avatar_url=source.character_avatar_url,
                only="" if left and right else ("a" if left else "b"),
                lines=tuple(
                    _compare_gear_line(
                        label,
                        lines_a.get(label),
                        lines_b.get(label),
                        both_present=bool(left and right),
                    )
                    for label in _GEAR_LINE_LABELS
                ),
                skills_a=left.top_skills if left else (),
                skills_b=right.top_skills if right else (),
            )
        )
    return tuple(rows)


_GEAR_LINE_LABELS = (
    "等级 · 潜能",
    "武器",
    "护手",
    "护甲",
    "配件 1",
    "配件 2",
    "技能等级",
)


@dataclass(frozen=True, slots=True)
class _GearLine:
    """What one gear line shows, and what decides whether it differs.

    The two are not the same for a piece of gear: ``text`` carries the suit
    name, which upstream reports inconsistently for one and the same item,
    while ``key`` is the item id, which is stable. Comparing the text would
    call identical gear different.
    """

    text: str
    key: str


def _compare_gear_line(
    label: str,
    a: "_GearLine | None",
    b: "_GearLine | None",
    *,
    both_present: bool,
) -> CompareGearLineView:
    return CompareGearLineView(
        label=label,
        a=a.text if a is not None else "—",
        b=b.text if b is not None else "—",
        differs=both_present
        and (a.key if a is not None else None) != (b.key if b is not None else None),
    )


_FIXED_PARTS = ("护手", "护甲")


def _accessories(view: LoadoutView | None) -> list[EquipView]:
    """The 配件 pieces in the order the loadout page shows them."""

    if view is None:
        return []
    return [equip for equip in view.equips if equip.part_name not in _FIXED_PARTS]


def _pair_accessories(
    a: list[EquipView],
    b: list[EquipView],
) -> list[tuple[EquipView | None, EquipView | None]]:
    """Line the two 配件 rows up, leaving A in the order its own page shows.

    Both accessory slots hold the same kind of piece and upstream keeps
    whatever order the client sent, so one player's pair sits in slot 2/3
    and another's in 3/2. Sorting both sides would align them but leave
    neither matching the 配装 page a reader cross-checks against, so B is
    matched to A by item id instead and A is left alone.
    """

    remaining = list(b)
    pairs: list[tuple[EquipView | None, EquipView | None]] = []
    for equip in a:
        match = next(
            (other for other in remaining if other.item_id == equip.item_id), None
        )
        if match is not None:
            remaining.remove(match)
        pairs.append((equip, match))
    # Whatever B still holds fills the rows A could not match, then extends.
    for index, (left, right) in enumerate(pairs):
        if right is None and remaining:
            pairs[index] = (left, remaining.pop(0))
    pairs.extend((None, equip) for equip in remaining)
    return pairs


def _gear_line(equip: EquipView) -> "_GearLine":
    return _GearLine(equip.compact_label, equip.item_id or equip.compact_label)


def _gear_lines(
    view: LoadoutView | None,
    accessories: list[EquipView | None],
) -> dict[str, _GearLine]:
    """One comparable line per gear slot; missing slots stay absent.

    ``accessories`` is this side of the paired 配件 rows, so the two sides
    agree on which row is which; a ``None`` leaves that row empty.
    """

    if view is None:
        return {}
    lines: dict[str, _GearLine] = {}
    level = " · ".join(
        part for part in (view.level_label, view.potential_label) if part
    )
    if level:
        lines["等级 · 潜能"] = _GearLine(level, level)
    if view.weapon is not None:
        weapon = view.weapon.name
        if view.weapon.refine_label:
            weapon += f" · {view.weapon.refine_label}"
        lines["武器"] = _GearLine(weapon, weapon)
    for equip in view.equips:
        if equip.part_name in _FIXED_PARTS:
            lines[equip.part_name] = _gear_line(equip)
    for index, equip in enumerate(accessories, start=1):
        if equip is not None:
            lines[f"配件 {index}"] = _gear_line(equip)
    if view.skill_levels:
        skills = " ".join(
            f"{level.label}{level.level}" for level in view.skill_levels
        )
        lines["技能等级"] = _GearLine(skills, skills)
    return lines
