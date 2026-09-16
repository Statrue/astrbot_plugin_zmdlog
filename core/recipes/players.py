"""玩家排名: which public accounts uploaded the most first places."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..history import window_label, window_start
from ..standings import AccountTally, account_tallies
from .index import IndexSnapshot

if TYPE_CHECKING:
    from ..render import LongImageRenderer


@dataclass(frozen=True, slots=True)
class PlayerChampionsRecipe:
    tallies: tuple[AccountTally, ...]
    board_count: int
    age_seconds: float | None
    missing_count: int
    window_label: str
    query: str = "玩家排名"
    metric: str = "dps"

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_player_champions(
            self.tallies,
            board_count=self.board_count,
            query=self.query,
            age_seconds=self.age_seconds,
            window_label=self.window_label,
            metric=self.metric,
        )


def prepare_player_champions(
    snapshot: IndexSnapshot, *, time_range: str = "all"
) -> PlayerChampionsRecipe:
    since = window_start(time_range, now=datetime.now(UTC))
    return PlayerChampionsRecipe(
        tallies=account_tallies(snapshot.rankings, since=since),
        board_count=snapshot.board_count,
        age_seconds=snapshot.age_seconds,
        missing_count=snapshot.missing_count,
        window_label=window_label(time_range),
        metric=snapshot.metric,
    )
