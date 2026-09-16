"""The ranking index as one page sees it: every board held, read once."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..metrics import METRIC_DPS
from ..models import BossRanking
from ..standings import roster_character_names

if TYPE_CHECKING:
    from ..datasource import ZmdLogsDataSource


@dataclass(frozen=True, slots=True)
class IndexSnapshot:
    """What the ranking index held when a page asked for it.

    A page drawn from memory reads the index exactly once, so the rows it
    counts, the age it prints and the boards it admits to missing all
    describe the same moment — and the same ranking: ``metric`` says which
    of every board's two rankings the snapshot holds.
    """

    rankings: tuple[BossRanking, ...]
    # Every character any roster fields, four-stars included: what a typed
    # name is resolved against, and the names the catalogs are asked about.
    fielded: tuple[str, ...]
    # How far behind the oldest board held is; the page's 数据截至 label.
    age_seconds: float | None
    # Boards the board list names that the index has not read yet.
    missing_count: int
    metric: str = METRIC_DPS

    @property
    def board_count(self) -> int:
        return len(self.rankings)


async def index_snapshot(
    data: "ZmdLogsDataSource", *, metric: str = METRIC_DPS
) -> IndexSnapshot | None:
    """The index once filled for ``metric``; None when the bounded wait ran out.

    The rDPS boards are filled after the DPS ones, so a cold boot answers a
    DPS page sooner than an rDPS one.
    """

    index = data.ranking_index
    if not await index.wait_filled(metric=metric):
        return None
    rankings = tuple(entry.ranking for entry in index.entries(metric))
    return IndexSnapshot(
        rankings=rankings,
        fielded=roster_character_names(rankings),
        age_seconds=index.oldest_age_seconds(metric),
        missing_count=index.missing(metric),
        metric=metric,
    )
