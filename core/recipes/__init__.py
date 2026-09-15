"""What a page needs, worked out once for the command and the tool that draw it.

A recipe is the data behind one page plus the call that draws it. The
command path turns it straight into an image; the tool path draws the same
image and hands the same fields to ``facts`` for its text. Before this
layer each path fetched and filtered on its own, and the two drifted: the
bare 角色排名 page carried a 从未上榜 section when a tool drew it and none
when a command did, a board's ``--角色`` filter was checked against the
element cut on one path only, and the battle card told a command's reader
why the cast sequence was missing while a tool's picture said nothing.

Resolution stays with the caller — a tool refuses a weak board match a
command would render, a command posts a pick list a tool cannot wait for —
so a recipe starts where the target is already known. A ``prepare_*``
returns the recipe, or the one-line refusal when the page must not be
drawn (a team no record fields, a filter that leaves nothing): both
callers hand that text on as it is. An upstream error raises out of it,
because the two paths word a missing battle or account differently.
Every page drawn from the ranking index starts from one
:class:`IndexSnapshot`, read once, so the rows it counts and the age it
prints describe the same moment.
"""

from .accounts import AccountRecipe, prepare_account
from .battles import (
    EXPORT_UNSUPPORTED,
    BattleRecipe,
    CompareRecipe,
    export_refusal,
    prepare_battle,
    prepare_compare,
)
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
    "EXPORT_UNSUPPORTED",
    "AccountRecipe",
    "BattleRecipe",
    "BoardsOverviewRecipe",
    "ChampionsRecipe",
    "CharacterBossRecipe",
    "CharacterStatsRecipe",
    "CompareRecipe",
    "DungeonOverviewRecipe",
    "IndexSnapshot",
    "PlayerChampionsRecipe",
    "RankingRecipe",
    "RecordsRecipe",
    "StandingsRecipe",
    "export_refusal",
    "index_snapshot",
    "prepare_account",
    "prepare_battle",
    "prepare_boards_overview",
    "prepare_champions",
    "prepare_character_boss",
    "prepare_character_stats",
    "prepare_compare",
    "prepare_dungeon_overview",
    "prepare_player_champions",
    "prepare_ranking",
    "prepare_records",
    "prepare_standings",
]
