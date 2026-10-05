"""Rank trend of one account, from the trace every board read keeps.

趋势 is one wide table, never paged: a row a board, its change over the
window and the stepped line it took, with the window's dates over the
lines; a week unless ``--范围`` asks for more (``history.DEFAULT_TREND_RANGE``).
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from ..history import (
    ALL_TREND_LABEL,
    DEFAULT_TREND_RANGE,
    AccountHistory,
    BoardHistory,
    RankPoint,
    trend_points,
    trend_window_start,
)
from ..timestamps import parse_timestamp
from .common import _RANGE_LABELS, PageHeader, _format_month_day


@dataclass(frozen=True, slots=True)
class TrendRowView:
    boss_name: str
    dungeon_name: str
    current_rank: int
    start_rank: int
    # Places gained or lost over the window, never negative; 0 when flat.
    change: int
    # "up" (rank improved), "down" (rank worsened) or "flat".
    delta_kind: str
    best_rank: int
    worst_rank: int
    # Stepped polyline in a 100×44 viewBox; time left to right, best rank up.
    polyline: str
    # The ground under the line: a clip-path polygon in the chart's own %.
    area: str
    # Change points as (left %, top %); the one at the left edge is the
    # rank carried into the window, not a change.
    dots: tuple[tuple[float, float], ...]
    axis_top: str
    axis_bottom: str
    # 月-日 of the newest move, or 未变 when the rank held all window.
    last_change: str


@dataclass(frozen=True, slots=True)
class TrendPage:
    header: PageHeader
    account_id: str
    range_label: str
    # 月-日 at the left and right edge of the lines.
    window_start: str
    window_end: str
    # How long the trend has recorded this account, at most the 90 days kept.
    tracked_days: str
    board_count: int
    improved_count: int
    declined_count: int
    flat_count: int
    rows: tuple[TrendRowView, ...]


_TREND_CHART_TOP = 6.0


_TREND_CHART_BOTTOM = 38.0


_TREND_CHART_HEIGHT = 44.0


def build_trend_page(
    history: AccountHistory,
    *,
    query: str,
    web_base_url: str,
    time_range: str = DEFAULT_TREND_RANGE,
    now: datetime | None = None,
) -> TrendPage:
    """One stepped line per board from the ranks the board reads recorded.

    Every board the window holds is a row; the page is drawn whole.
    """

    current_time = now if now is not None else datetime.now(UTC)
    start = trend_window_start(time_range, now=current_time)
    # Each board keeps its newest point however old; nothing before the 90
    # days kept is drawn, so the trace counts as starting there at most.
    kept_from = trend_window_start("all", now=current_time)
    stamps = [
        stamp
        for board in history.boards
        for point in board.points
        if (stamp := parse_timestamp(point.checked_at)) is not None
    ]
    first_seen = max(min(stamps), kept_from) if stamps else None
    # ``all`` starts the axis where the trace does, a window at its edge.
    axis_start = (first_seen or current_time) if time_range == "all" else start
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
            subtitle="各榜名次的变化",
            query=query,
            matched_name=history.account_id,
            target_type="名次趋势",
            footer_note="公开账号 · 机器人每次读榜时记录的名次变化",
        ),
        account_id=history.account_id,
        range_label=(
            ALL_TREND_LABEL
            if time_range == "all"
            else _RANGE_LABELS.get(time_range, time_range)
        ),
        window_start=_format_month_day(axis_start.isoformat()),
        window_end=_format_month_day(current_time.isoformat()),
        tracked_days=(
            "—"
            if tracked_days is None
            else (f"{tracked_days} 天" if tracked_days >= 1 else "不足 1 天")
        ),
        board_count=len(rows),
        improved_count=sum(1 for row in rows if row.delta_kind == "up"),
        declined_count=sum(1 for row in rows if row.delta_kind == "down"),
        flat_count=sum(1 for row in rows if row.delta_kind == "flat"),
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
    kind = "up" if delta > 0 else "down" if delta < 0 else "flat"
    area = [f"{x}% {round(y / _TREND_CHART_HEIGHT * 100, 2)}%" for x, y in coords]
    area += ["100% 100%", f"{coords[0][0]}% 100%"]
    return TrendRowView(
        boss_name=board.boss_name,
        dungeon_name=board.dungeon_name,
        current_rank=current_rank,
        start_rank=start_rank,
        change=abs(delta),
        delta_kind=kind,
        best_rank=best,
        worst_rank=worst,
        polyline=" ".join(f"{x},{y}" for x, y in coords),
        area=", ".join(area),
        dots=tuple(
            (x_of(point), round(y_of(point.rank) / _TREND_CHART_HEIGHT * 100, 2))
            for point in points
        ),
        axis_top=f"#{best}",
        axis_bottom=f"#{worst}",
        last_change=(
            "未变" if best == worst else _format_month_day(points[-1].checked_at)
        ),
    )
