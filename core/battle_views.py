"""A battle's pages on the V2 shell: 摘要 and the 详细视图 beside it.

A battle draws its 摘要 by default and opens its 详细视图 — 数据, 排轴 and
养成 (ADR 0003) — each as its own command. Every one of those pages lists
the others in its foot, its own lit, and on the QQ official bot offers them
as buttons under the picture; a view the battle lacks the data for is left
out of both, so neither ever sends a reader to a page that would answer
"这份战报没有…".

This table is the one place that decides which views exist and when a
battle can draw one: the strip on the page (``view_strip``), the buttons
(``core/buttons``) and the views a result names unavailable (``queries``)
all read it, in the order the strip prints it.

What a battle "can draw" is judged only on what the page that is drawing
already read — the detail, and whether the cast export refused the upload
as too old — never on a read made for the purpose.
"""

from collections.abc import Callable
from dataclasses import dataclass

from .candidates import CandidateView
from .models import BattleDetailSummary


@dataclass(frozen=True, slots=True)
class BattleView:
    """One page of a battle: its name on the strip and the command that draws it.

    ``drawable`` answers from the detail (None when the page did not read
    it, so the view is assumed there) and whether the cast export refused
    the upload as one with no casts.
    """

    view: CandidateView
    label: str
    word: str
    drawable: Callable[[BattleDetailSummary | None, bool], bool]


def _always(battle: BattleDetailSummary | None, casts_refused: bool) -> bool:
    return True


def _has_skill_stats(
    battle: BattleDetailSummary | None, casts_refused: bool
) -> bool:
    """数据: its 技能伤害 is the one section nothing else on a battle draws.

    The table, 战斗贡献 and the curve would still draw from an old upload's
    participants, but so does the 摘要; without skill statistics 数据 has
    nothing of its own to add, and answers in text instead.
    """

    return battle is None or bool(battle.skill_stats)


def _has_casts(battle: BattleDetailSummary | None, casts_refused: bool) -> bool:
    """An upload the export refused as too old has no casts, so no 排轴."""

    return not casts_refused


def _has_roster(battle: BattleDetailSummary | None, casts_refused: bool) -> bool:
    """养成 draws the roster; an upload without one has nothing to show."""

    return battle is None or bool(battle.roster)


# The strip's order.
BATTLE_VIEWS: tuple[BattleView, ...] = (
    BattleView(CandidateView.BATTLE, "摘要", "战报", _always),
    BattleView(CandidateView.DATA, "数据", "数据", _has_skill_stats),
    BattleView(CandidateView.CAST, "排轴", "排轴", _has_casts),
    BattleView(CandidateView.BUILD, "养成", "养成", _has_roster),
)


def battle_view(view: CandidateView) -> BattleView | None:
    """The V2 page ``view`` names; None for a view not (yet) on the strip."""

    return next((entry for entry in BATTLE_VIEWS if entry.view is view), None)


def unavailable_views(
    battle: BattleDetailSummary | None, *, casts_refused: bool = False
) -> frozenset[CandidateView]:
    """The views on the strip this battle cannot draw."""

    return frozenset(
        entry.view
        for entry in BATTLE_VIEWS
        if not entry.drawable(battle, casts_refused)
    )


def view_strip(
    current: CandidateView,
    battle: BattleDetailSummary | None,
    *,
    casts_refused: bool = False,
) -> tuple[tuple[str, bool], ...]:
    """``(label, is_current)`` of every view this battle can draw, in order.

    The page being drawn is always on it: it is drawn, so it is drawable.
    """

    missing = unavailable_views(battle, casts_refused=casts_refused)
    return tuple(
        (entry.label, entry.view is current)
        for entry in BATTLE_VIEWS
        if entry.view is current or entry.view not in missing
    )
