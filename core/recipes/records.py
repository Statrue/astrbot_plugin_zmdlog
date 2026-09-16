"""新纪录: first places changing hands, new uploads and board activity in a window."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..events import BoardActivity, RecordEvent, board_activity
from ..history import window_label, window_start
from .index import IndexSnapshot

if TYPE_CHECKING:
    from ..datasource import ZmdLogsDataSource
    from ..render import LongImageRenderer


@dataclass(frozen=True, slots=True)
class RecordsRecipe:
    events: tuple[RecordEvent, ...]
    activity: tuple[BoardActivity, ...]
    window_label: str
    age_seconds: float | None
    missing_count: int
    # When the event log started; None while it has seen nothing yet.
    log_since: str | None
    query: str = "新纪录"
    metric: str = "dps"

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_records(
            self.events,
            self.activity,
            query=self.query,
            window_label=self.window_label,
            age_seconds=self.age_seconds,
            log_since=self.log_since,
            metric=self.metric,
        )


def prepare_records(
    data: "ZmdLogsDataSource", snapshot: IndexSnapshot, *, time_range: str = "7d"
) -> RecordsRecipe:
    # The log is pruned past 30 days, so "all time" means the default week.
    span = time_range if time_range != "all" else "7d"
    since = window_start(span, now=datetime.now(UTC))
    log = data.event_log
    return RecordsRecipe(
        events=log.recent(since=since, metric=snapshot.metric),
        activity=board_activity(snapshot.rankings, since=since),
        window_label=window_label(span),
        age_seconds=snapshot.age_seconds,
        missing_count=snapshot.missing_count,
        log_since=log.oldest_seen_at(),
        metric=snapshot.metric,
    )
