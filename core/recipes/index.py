"""The ranking index as one page sees it: every board held, read once."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..models import BossRanking
from ..standings import roster_character_names

if TYPE_CHECKING:
    from ..datasource import ZmdLogsDataSource


@dataclass(frozen=True, slots=True)
class IndexSnapshot:
    """What the ranking index held when a page asked for it.

    A page drawn from memory reads the index exactly once, so the rows it
    counts, the age it prints and the boards it admits to missing all
    describe the same moment.
    """

    rankings: tuple[BossRanking, ...]
    # Every character any roster fields, four-stars included: what a typed
    # name is resolved against, and the names the catalogs are asked about.
    fielded: tuple[str, ...]
    # How far behind the oldest board held is; the page's 数据截至 label.
    age_seconds: float | None
    # Boards the board list names that the index has not read yet.
    missing_count: int

    @property
    def board_count(self) -> int:
        return len(self.rankings)


async def index_snapshot(data: "ZmdLogsDataSource") -> IndexSnapshot | None:
    """The index once filled; None when the bounded wait for a fill ran out."""

    index = data.ranking_index
    if not await index.wait_filled():
        return None
    rankings = tuple(entry.ranking for entry in index.entries())
    return IndexSnapshot(
        rankings=rankings,
        fielded=roster_character_names(rankings),
        age_seconds=index.oldest_age_seconds(),
        missing_count=index.missing_count,
    )
