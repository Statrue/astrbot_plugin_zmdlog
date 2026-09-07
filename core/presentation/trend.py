"""Rank trend of one watched account."""

from dataclasses import dataclass
from datetime import UTC, datetime

from ..history import (
    AccountHistory,
    BoardHistory,
    RankPoint,
    trend_points,
    window_start,
)
from ..timestamps import parse_timestamp
from .common import (
    _RANGE_LABELS,
    PageHeader,
    _format_date,
    _format_datetime,
    public_url,
)


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


@dataclass(frozen=True, slots=True)
class TrendPage:
    header: PageHeader
    account_id: str
    account_url: str
    range_label: str
    tracked_since: str
    tracked_days: str
    last_checked: str
    board_count: int
    best_rank: str
    improved_count: int
    declined_count: int
    rows: tuple[TrendRowView, ...]


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
    )
