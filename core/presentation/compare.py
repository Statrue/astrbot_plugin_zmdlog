"""对比: two battles on one boss, side by side, on the 960 wide page.

The page reads down in one order: the VS strip (who, rank, team, time),
the other headline figures with the gap between them, each character's
DPS as a pair of bars growing out from the middle, both teams' DPS 曲线
on one time axis, and a build card per character — what differs between
the two, what they share, and each side's heaviest damage sources.

**Each side is called by its uploader's name, never A / B** — on the
strip, in every gap ("shiki 快 1.05 秒"), over the bars, on the cards and
on the buttons under the picture. Two uploads by one person get a
qualifier (``side_names``): the rank each was picked by, else the time it
was fought, else which side of the page it is on. ``a`` and ``b`` survive
only as the two sides' places in the view model.

**The first battle keeps the order its own pages show; the second is
matched to it**, never both sorted (a sorted page matches neither battle's
own pages, which a reader cross-checks against). The DPS rows follow the
first battle's DPS order, the build cards its 养成 order (the roster's
slots), and the second battle's characters the first did not field come
after; its two 配件 are paired to the first's by item id.

The build cards draw from 养成's own model (``build``): the level and the
潜能, the weapon with its 精炼 and its 词条 / 武器技能 levels, the four
skill levels. Two weapons that read the same but differ in a level say
which levels; two pieces of gear that read the same say their full name
and their 强化.
"""

from dataclasses import dataclass

from ..loadout import group_skill_damage
from ..metrics import METRIC_DPS, is_rdps, metric_label
from ..models import BattleDetailSummary, BattleRosterEntry
from ..telemetry import build_dps_curve
from .battle import EquipView, SkillRowView, _equip_view, _skill_rows
from .build import BuildCharacterView, _character
from .charts import (
    _CURVE_MAX_POINTS,
    RailTickView,
    _horizontal_ticks,
    polyline_points,
)
from .common import (
    PageHeader,
    _bar_width,
    _displayed,
    _format_axis_value,
    _format_datetime,
    _initial,
    _nice_ceiling,
    _safe_asset_url,
    _share,
    format_duration,
    format_number,
)

# A side's heaviest damage sources on its build card.
_CARD_SOURCES = 3
_GEAR_PARTS = ("护手", "护甲")
_ACCESSORY_ROWS = ("配件 1", "配件 2")
# The lines a build card compares, in the order it lists them.
LINE_LABELS = ("等级", "武器", *_GEAR_PARTS, *_ACCESSORY_ROWS, "技能等级")
_SKILL_SLOTS = ("普攻", "战技", "连携", "终结")
# The qualifier of last resort for two uploads by one person: where each
# side is on the page.
_PLACE_NAMES = ("左", "右")


@dataclass(frozen=True, slots=True)
class CompareFaceView:
    character_name: str
    character_initial: str
    avatar_url: str | None


@dataclass(frozen=True, slots=True)
class CompareSideView:
    # The side's name everywhere on the page: the uploader's, qualified
    # when both sides were uploaded by one person.
    name: str
    # What the VS strip prints beside the rank: the bare uploader when the
    # ranks already tell two uploads by one person apart, else ``name``.
    headline: str
    rank_label: str | None
    battle_id: str
    battle_date: str
    duration: str
    # The team, highest DPS first: the first is the main C.
    team: tuple[CompareFaceView, ...]


@dataclass(frozen=True, slots=True)
class CompareFactView:
    label: str
    a: str
    b: str
    # "a" / "b" for the side that is better on this fact, "" when equal or
    # when the fact is not a quantity.
    better: str
    # The gap in words, the better side by its name ("shiki 高 2.7%").
    delta_label: str


@dataclass(frozen=True, slots=True)
class CompareDpsView:
    """One side of a character's DPS row."""

    dps: str
    damage_share: str
    # The bar, a percent of the highest DPS on either side.
    bar_percent: float


@dataclass(frozen=True, slots=True)
class CompareRosterRowView:
    character_name: str
    character_initial: str
    avatar_url: str | None
    # The first battle's main C: its row comes first and its face is ringed.
    lead: bool
    # None on the side the character did not fight on.
    a: CompareDpsView | None
    b: CompareDpsView | None


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
class CompareLevelView:
    """One of the four skill levels; ``differs`` from the other side's."""

    label: str
    level: str
    differs: bool


@dataclass(frozen=True, slots=True)
class CompareCellView:
    """One side of a build line.

    ``text`` is the short form the two sides are read by and a shared line
    prints. A line that differs prints ``name`` (the full piece name when
    the two short forms read the same) over ``sub`` (what then tells them
    apart: the 强化, or a weapon's levels), with the item's ``icon_url``;
    the skill levels print ``levels``, the ones that differ marked.
    """

    text: str
    name: str
    sub: str | None
    icon_url: str | None
    levels: tuple[CompareLevelView, ...] = ()


@dataclass(frozen=True, slots=True)
class CompareLineView:
    label: str
    a: CompareCellView | None
    b: CompareCellView | None
    differs: bool


@dataclass(frozen=True, slots=True)
class CompareBuildView:
    character_name: str
    character_initial: str
    avatar_url: str | None
    # "" when the character fought on both sides, else the side it was on.
    only: str
    # What the card's bar says on the right: 仅 X 上场, N 项不同, 养成一致.
    note: str
    # Every line, in LINE_LABELS order, for the lines both sides recorded
    # or either did.
    lines: tuple[CompareLineView, ...]
    # The heaviest damage sources; None on a side the character sat out.
    sources_a: tuple[SkillRowView, ...] | None
    sources_b: tuple[SkillRowView, ...] | None

    @property
    def differences(self) -> tuple[CompareLineView, ...]:
        return tuple(line for line in self.lines if line.differs)

    @property
    def shared(self) -> tuple[tuple[str, str], ...]:
        """``(label, text)`` of every line the card lists as one."""

        rows = []
        for line in self.lines:
            if line.differs:
                continue
            cell = line.a or line.b
            if cell is not None:
                rows.append((line.label, cell.text))
        return tuple(rows)


@dataclass(frozen=True, slots=True)
class ComparePage:
    header: PageHeader
    sides: tuple[CompareSideView, CompareSideView]
    # 通关时间, which the VS strip carries.
    time: CompareFactView
    # The rest of the headline figures, for 对比摘要.
    facts: tuple[CompareFactView, ...]
    roster: tuple[CompareRosterRowView, ...]
    curve: CompareCurveView | None
    builds: tuple[CompareBuildView, ...]


def side_names(
    a: BattleDetailSummary,
    b: BattleDetailSummary,
    *,
    rank_a: int | None = None,
    rank_b: int | None = None,
    metric: str = METRIC_DPS,
) -> tuple[str, str]:
    """What the page and its buttons call each side: the uploader's name.

    Two uploads by one person are told apart by the rank each was picked
    by — "shiki（第 1 名）" — when both were, else by when each was fought
    (to the minute), else by where each stands on the page.
    """

    name_a, name_b = a.uploader_display_name, b.uploader_display_name
    if name_a != name_b:
        return name_a, name_b
    first, second = _PLACE_NAMES
    for qualifiers in (
        (_rank_label(rank_a, metric), _rank_label(rank_b, metric)),
        (_short_time(a.battle_end_at), _short_time(b.battle_end_at)),
    ):
        if all(qualifiers) and qualifiers[0] != qualifiers[1]:
            first, second = qualifiers
            break
    return f"{name_a}（{first}）", f"{name_b}（{second}）"


def build_compare_page(
    a: BattleDetailSummary,
    b: BattleDetailSummary,
    *,
    query: str,
    web_base_url: str,
    rank_a: int | None = None,
    rank_b: int | None = None,
    suits: dict[str, str] | None = None,
    metric: str = METRIC_DPS,
) -> ComparePage:
    """Two battles on one boss, side by side; the first is ``a``.

    Every figure is the one that battle's own pages print. ``metric`` names
    the ranking the ranks were read off: an rDPS rank says so, and never
    reads as the DPS board's.
    """

    # Both fights are on the same boss: the handler refuses anything else,
    # because every boss has its own rotation and a cross-boss page would
    # compare two different games.
    names = side_names(a, b, rank_a=rank_a, rank_b=rank_b, metric=metric)
    rank_labels = (_rank_label(rank_a, metric), _rank_label(rank_b, metric))
    # The ranks tell the two apart on the strip, so it keeps the bare names.
    ranked = all(rank_labels) and rank_labels[0] != rank_labels[1]
    sides = tuple(
        CompareSideView(
            name=name,
            headline=battle.uploader_display_name if ranked else name,
            rank_label=rank_label,
            battle_id=battle.battle_id,
            battle_date=_format_datetime(battle.battle_end_at),
            duration=format_duration(battle.duration_ms),
            team=tuple(
                CompareFaceView(
                    character_name=participant.character_name,
                    character_initial=_initial(participant.character_name),
                    avatar_url=_safe_asset_url(
                        participant.character_avatar_url, base_url=web_base_url
                    ),
                )
                for participant in _by_dps(battle)
            ),
        )
        for battle, name, rank_label in zip((a, b), names, rank_labels)
    )
    return ComparePage(
        header=PageHeader(
            title=a.boss_name,
            subtitle=a.dungeon_name,
            query=query,
            matched_name=f"{a.battle_id} vs {b.battle_id}",
            target_type="战报对比",
            footer_note="公开战报 · DPS 口径 · 两场并排",
        ),
        sides=sides,
        time=CompareFactView(
            label="通关时间",
            a=sides[0].duration,
            b=sides[1].duration,
            better=_better(a.duration_ms, b.duration_ms, lower_is_better=True),
            delta_label=_seconds_delta(a.duration_ms, b.duration_ms, names),
        ),
        facts=_compare_facts(a, b, names),
        roster=_compare_roster(a, b, web_base_url=web_base_url),
        curve=_compare_curve(a, b),
        builds=_compare_builds(
            a, b, names, web_base_url=web_base_url, suits=suits or {}
        ),
    )


def _rank_label(rank: int | None, metric: str) -> str | None:
    if rank is None:
        return None
    if is_rdps(metric):
        return f"{metric_label(metric)} 第 {rank} 名"
    return f"第 {rank} 名"


def _short_time(value: str) -> str:
    try:
        return _displayed(value).strftime("%m-%d %H:%M")
    except ValueError:
        return value


def _by_dps(battle: BattleDetailSummary):
    return sorted(battle.participants, key=lambda item: item.dps, reverse=True)


def _whole(value: float) -> str:
    return format_number(round(value))


def _compare_facts(
    a: BattleDetailSummary,
    b: BattleDetailSummary,
    names: tuple[str, str],
) -> tuple[CompareFactView, ...]:
    facts = [
        CompareFactView(
            label="总 DPS",
            a=_whole(a.total_dps),
            b=_whole(b.total_dps),
            better=_better(a.total_dps, b.total_dps),
            delta_label=_percent_delta(a.total_dps, b.total_dps, names),
        ),
        CompareFactView(
            label="总伤害",
            a=format_number(a.total_damage),
            b=format_number(b.total_damage),
            better=_better(a.total_damage, b.total_damage),
            delta_label=_percent_delta(a.total_damage, b.total_damage, names),
        ),
    ]
    team_a, team_b = _by_dps(a), _by_dps(b)
    if team_a and team_b:
        lead_a, lead_b = team_a[0], team_b[0]
        facts.append(
            CompareFactView(
                label="主 C",
                a=f"{lead_a.character_name} · {_damage_share(lead_a, a)}",
                b=f"{lead_b.character_name} · {_damage_share(lead_b, b)}",
                better="",
                delta_label=(
                    "同一主 C"
                    if lead_a.character_name == lead_b.character_name
                    else "主 C 不同"
                ),
            )
        )
        facts.append(
            CompareFactView(
                label="主 C DPS",
                a=_whole(lead_a.dps),
                b=_whole(lead_b.dps),
                better=_better(lead_a.dps, lead_b.dps),
                delta_label=_percent_delta(lead_a.dps, lead_b.dps, names),
            )
        )
    return tuple(facts)


def _damage_share(participant, battle: BattleDetailSummary) -> str:
    if battle.total_damage <= 0:
        return "—"
    return _share(participant.total_damage, battle.total_damage)


def _better(a: float, b: float, *, lower_is_better: bool = False) -> str:
    if a == b:
        return ""
    a_wins = a < b if lower_is_better else a > b
    return "a" if a_wins else "b"


def _seconds_delta(a_ms: int, b_ms: int, names: tuple[str, str]) -> str:
    if a_ms == b_ms:
        return "相同"
    faster = names[0] if a_ms < b_ms else names[1]
    return f"{faster} 快 {abs(a_ms - b_ms) / 1000:.2f} 秒"


def _percent_delta(a: float, b: float, names: tuple[str, str]) -> str:
    if a == b:
        return "相同"
    higher, base = (names[0], b) if a > b else (names[1], a)
    if base <= 0:
        return f"{higher} 更高"
    return f"{higher} 高 {abs(a - b) / base * 100:.1f}%"


def _compare_roster(
    a: BattleDetailSummary,
    b: BattleDetailSummary,
    *,
    web_base_url: str,
) -> tuple[CompareRosterRowView, ...]:
    """Every character of either team, in the first's DPS order, then the second's."""

    by_a = {item.character_name: item for item in _by_dps(a)}
    by_b = {item.character_name: item for item in _by_dps(b)}
    peak = max((item.dps for item in (*by_a.values(), *by_b.values())), default=0)

    def cell(participant, battle) -> CompareDpsView | None:
        if participant is None:
            return None
        return CompareDpsView(
            dps=_whole(participant.dps),
            damage_share=_damage_share(participant, battle),
            bar_percent=_bar_width(participant.dps, peak),
        )

    rows = []
    for index, name in enumerate(dict.fromkeys((*by_a, *by_b))):
        source = by_a.get(name) or by_b[name]
        rows.append(
            CompareRosterRowView(
                character_name=name,
                character_initial=_initial(name),
                avatar_url=_safe_asset_url(
                    source.character_avatar_url, base_url=web_base_url
                ),
                lead=index == 0 and name in by_a,
                a=cell(by_a.get(name), a),
                b=cell(by_b.get(name), b),
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
        return polyline_points(series.points, duration_ms=shared_ms, axis_max=axis_max)

    return CompareCurveView(
        polyline_a=polyline(curve_a.team),
        polyline_b=polyline(curve_b.team),
        axis_labels=tuple(
            _format_axis_value(axis_max * (4 - index) / 4) for index in range(5)
        ),
        ticks=_horizontal_ticks(shared_ms),
        end_a=round(curve_a.duration_ms / shared_ms * 100, 2),
        end_b=round(curve_b.duration_ms / shared_ms * 100, 2),
        dps_a=_whole(curve_a.team.final_dps),
        dps_b=_whole(curve_b.team.final_dps),
    )


@dataclass(frozen=True, slots=True)
class _Fielded:
    """One side's character as its build card reads it."""

    build: BuildCharacterView
    equips: tuple[EquipView, ...]
    sources: tuple[SkillRowView, ...]


@dataclass(frozen=True, slots=True)
class _Cell:
    """One side of a line before it is compared.

    ``key`` decides whether two sides differ, never the text: upstream
    names one and the same piece differently from one upload to the next
    (its suit name is filled per upload), while the item id is stable.
    ``full_name`` and ``detail`` are what the line prints instead of the
    text when the two sides read the same but are not.
    """

    text: str
    key: object
    full_name: str
    detail: str | None
    icon_url: str | None
    levels: tuple[tuple[str, int | None], ...] = ()


def _compare_builds(
    a: BattleDetailSummary,
    b: BattleDetailSummary,
    names: tuple[str, str],
    *,
    web_base_url: str,
    suits: dict[str, str],
) -> tuple[CompareBuildView, ...]:
    """A card per character, in the first battle's 养成 order, then the second's."""

    fielded_a = _fielded(a, web_base_url=web_base_url, suits=suits)
    fielded_b = _fielded(b, web_base_url=web_base_url, suits=suits)
    cards = []
    for name in dict.fromkeys((*fielded_a, *fielded_b)):
        left, right = fielded_a.get(name), fielded_b.get(name)
        source = (left or right).build
        lines = _lines(left, right)
        only = "" if left and right else ("a" if left else "b")
        differing = sum(line.differs for line in lines)
        if only:
            note = f"仅 {names[0] if only == 'a' else names[1]} 上场"
        elif differing:
            note = f"{differing} 项不同"
        else:
            note = "养成一致"
        cards.append(
            CompareBuildView(
                character_name=name,
                character_initial=source.character_initial,
                avatar_url=source.avatar_url,
                only=only,
                note=note,
                lines=lines,
                sources_a=left.sources if left else None,
                sources_b=right.sources if right else None,
            )
        )
    return tuple(cards)


def _fielded(
    battle: BattleDetailSummary,
    *,
    web_base_url: str,
    suits: dict[str, str],
) -> dict[str, _Fielded]:
    """Each fielded character by name, in the order 养成 shows them."""

    groups = {
        group.character_name: group
        for group in group_skill_damage(battle.skill_stats)
    }
    fielded: dict[str, _Fielded] = {}
    for entry in sorted(battle.roster, key=lambda item: item.slot):
        group = groups.get(entry.character_name)
        fielded[entry.character_name] = _Fielded(
            build=_character(
                entry,
                dealt=None,
                total_damage=battle.total_damage,
                lead=False,
                suits=suits,
                web_base_url=web_base_url,
            ),
            equips=_equips(entry, suits, web_base_url=web_base_url),
            sources=_skill_rows(group, limit=_CARD_SOURCES) if group else (),
        )
    return fielded


def _equips(
    entry: BattleRosterEntry, suits: dict[str, str], *, web_base_url: str
) -> tuple[EquipView, ...]:
    return tuple(
        _equip_view(equip, suits, web_base_url=web_base_url)
        for equip in sorted(entry.equips, key=lambda item: item.slot)
    )


def _lines(
    left: _Fielded | None, right: _Fielded | None
) -> tuple[CompareLineView, ...]:
    pairs = _pair_accessories(_accessories(left), _accessories(right))
    cells_a = _cells(left, [pair[0] for pair in pairs])
    cells_b = _cells(right, [pair[1] for pair in pairs])
    both = left is not None and right is not None
    lines = []
    for label in LINE_LABELS:
        first, second = cells_a.get(label), cells_b.get(label)
        if first is None and second is None:
            continue
        differs = both and (
            (first.key if first else None) != (second.key if second else None)
        )
        lines.append(
            CompareLineView(
                label=label,
                a=_cell_view(first, second, differs=differs),
                b=_cell_view(second, first, differs=differs),
                differs=differs,
            )
        )
    return tuple(lines)


def _cell_view(
    cell: _Cell | None, other: _Cell | None, *, differs: bool
) -> CompareCellView | None:
    if cell is None:
        return None
    # Two that differ but read the same say what tells them apart.
    alike = differs and other is not None and other.text == cell.text
    other_levels = dict(other.levels) if other is not None else {}
    return CompareCellView(
        text=cell.text,
        name=cell.full_name if alike else cell.text,
        sub=cell.detail if alike else None,
        icon_url=cell.icon_url,
        levels=tuple(
            CompareLevelView(
                label=label,
                level=str(level) if level is not None else "—",
                differs=differs
                and label in other_levels
                and other_levels[label] != level,
            )
            for label, level in cell.levels
        ),
    )


def _cells(
    fielded: _Fielded | None, accessories: list[EquipView | None]
) -> dict[str, _Cell]:
    """One comparable cell per line; a line not recorded stays absent.

    ``accessories`` is this side of the paired 配件 rows, so the two sides
    agree on which row is which; a ``None`` leaves that row empty.
    """

    if fielded is None:
        return {}
    build = fielded.build
    cells: dict[str, _Cell] = {}
    level = " · ".join(
        part
        for part in (
            f"Lv.{build.level}" if build.level is not None else "",
            f"潜能 {build.potential}" if build.potential is not None else "",
        )
        if part
    )
    if level:
        cells["等级"] = _Cell(level, level, level, None, None)
    weapon = build.weapon
    if weapon is not None:
        text = weapon.name
        if weapon.refine is not None:
            text += f" · 精炼 {weapon.refine}"
        levels = tuple(line.level for line in weapon.lines)
        # Two that read the same share their 精炼 as well; the name alone
        # then keeps the line short enough for its half of the card.
        cells["武器"] = _Cell(
            text=text,
            key=(text, levels),
            full_name=weapon.name,
            detail=_weapon_levels(weapon.lines),
            icon_url=weapon.icon_url,
        )
    for equip in fielded.equips:
        if equip.part_name in _GEAR_PARTS:
            cells[equip.part_name] = _gear_cell(equip)
    for label, equip in zip(_ACCESSORY_ROWS, accessories):
        if equip is not None:
            cells[label] = _gear_cell(equip)
    if any(slot.level is not None for slot in build.skill_levels):
        levels = tuple((slot.label, slot.level) for slot in build.skill_levels)
        text = " / ".join(
            str(level) if level is not None else "—" for _, level in levels
        )
        cells["技能等级"] = _Cell(text, levels, text, None, None, levels)
    return cells


def _weapon_levels(lines) -> str | None:
    """A weapon's 词条 and 武器技能 levels in one line: 词条 9/9 · 技能 6."""

    affixes = [str(line.level) for line in lines if not line.is_weapon_skill]
    skills = [str(line.level) for line in lines if line.is_weapon_skill]
    parts = []
    if affixes:
        parts.append("词条 " + "/".join(affixes))
    if skills:
        parts.append("技能 " + "/".join(skills))
    return " · ".join(parts) or None


def _gear_cell(equip: EquipView) -> _Cell:
    enhance = equip.enhance_label
    return _Cell(
        text=equip.compact_label,
        key=equip.item_id or equip.compact_label,
        full_name=equip.piece_label,
        detail=enhance.removeprefix("强化 ") if enhance else None,
        icon_url=equip.icon_url,
    )


def _accessories(fielded: _Fielded | None) -> list[EquipView]:
    """The 配件 pieces in the order the 养成 page shows them."""

    if fielded is None:
        return []
    return [equip for equip in fielded.equips if equip.part_name not in _GEAR_PARTS]


def _pair_accessories(
    a: list[EquipView],
    b: list[EquipView],
) -> list[tuple[EquipView | None, EquipView | None]]:
    """Line the two 配件 rows up, leaving the first in the order its page shows.

    Both accessory slots hold the same kind of piece and upstream keeps
    whatever order the client sent, so one player's pair sits in slot 2/3
    and another's in 3/2. Sorting both sides would align them but leave
    neither matching the 养成 page a reader cross-checks against, so the
    second is matched to the first by item id instead.
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
    # Whatever the second still holds fills the rows the first could not
    # match, then extends.
    for index, (left, right) in enumerate(pairs):
        if right is None and remaining:
            pairs[index] = (left, remaining.pop(0))
    pairs.extend((None, equip) for equip in remaining)
    return pairs
