"""角色统计: the six-star DPS distributions, per board or per character."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..metrics import METRIC_DPS
from ..models import CharacterBossStatistics, CharacterStatistics

if TYPE_CHECKING:
    from ..datasource import ZmdLogsDataSource
    from ..render import LongImageRenderer


@dataclass(frozen=True, slots=True)
class CharacterStatsRecipe:
    """Every six-star's distribution on one board, or over all of them."""

    stats: CharacterStatistics
    query: str
    web_base_url: str | None

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_character_stats(
            self.stats, query=self.query, web_base_url=self.web_base_url
        )


async def prepare_character_stats(
    data: "ZmdLogsDataSource",
    boss_slug: str | None,
    *,
    time_range: str,
    potential: str,
    query: str,
    web_base_url: str | None,
    metric: str = METRIC_DPS,
) -> CharacterStatsRecipe:
    """``boss_slug`` None means the global statistics, over every board."""

    stats = await data.get_character_statistics(
        boss_slug, time_range=time_range, potential=potential, metric=metric
    )
    return CharacterStatsRecipe(stats=stats, query=query, web_base_url=web_base_url)


@dataclass(frozen=True, slots=True)
class CharacterBossRecipe:
    """One six-star's distribution on every board that has statistics."""

    stats: CharacterBossStatistics
    query: str
    web_base_url: str | None

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_character_boss(
            self.stats, query=self.query, web_base_url=self.web_base_url
        )


async def prepare_character_boss(
    data: "ZmdLogsDataSource",
    character_key: str,
    *,
    time_range: str,
    potential: str,
    query: str,
    web_base_url: str | None,
    metric: str = METRIC_DPS,
) -> CharacterBossRecipe:
    """``character_key`` is the catalog's key; the caller resolved the name."""

    stats = await data.get_character_boss_statistics(
        character_key, time_range=time_range, potential=potential, metric=metric
    )
    return CharacterBossRecipe(stats=stats, query=query, web_base_url=web_base_url)
