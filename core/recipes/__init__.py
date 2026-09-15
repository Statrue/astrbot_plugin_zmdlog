"""What a page needs, worked out once for the command and the tool that draw it.

A recipe is the data behind one page plus the call that draws it. The
command path turns it straight into an image; the tool path draws the same
image and hands the same fields to ``facts`` for its text. Before this
layer each path fetched and filtered on its own, and the two drifted: the
bare 角色排名 page carried a 从未上榜 section when a tool drew it and none
when a command did, and a board's ``--角色`` filter was checked against
the element cut on one path only.

Resolution stays with the caller — a tool refuses a weak board match a
command would render, a command posts a pick list a tool cannot wait for —
so a recipe starts where the target is already known. A ``prepare_*``
returns the recipe, or the one-line refusal when the page must not be
drawn (a team no record fields, a filter that leaves nothing): both
callers hand that text on as it is. Every page drawn from the ranking
index starts from one :class:`IndexSnapshot`, read once, so the rows it
counts and the age it prints describe the same moment.
"""

from .boards import (
    BoardsOverviewRecipe,
    DungeonOverviewRecipe,
    RankingRecipe,
    prepare_boards_overview,
    prepare_dungeon_overview,
    prepare_ranking,
)
from .index import IndexSnapshot, index_snapshot
from .players import PlayerChampionsRecipe, prepare_player_champions
from .records import RecordsRecipe, prepare_records
from .standings import (
    ChampionsRecipe,
    StandingsRecipe,
    prepare_champions,
    prepare_standings,
)
from .statistics import (
    CharacterBossRecipe,
    CharacterStatsRecipe,
    prepare_character_boss,
    prepare_character_stats,
)

__all__ = [
    "BoardsOverviewRecipe",
    "ChampionsRecipe",
    "CharacterBossRecipe",
    "CharacterStatsRecipe",
    "DungeonOverviewRecipe",
    "IndexSnapshot",
    "PlayerChampionsRecipe",
    "RankingRecipe",
    "RecordsRecipe",
    "StandingsRecipe",
    "index_snapshot",
    "prepare_boards_overview",
    "prepare_champions",
    "prepare_character_boss",
    "prepare_character_stats",
    "prepare_dungeon_overview",
    "prepare_player_champions",
    "prepare_ranking",
    "prepare_records",
    "prepare_standings",
]
