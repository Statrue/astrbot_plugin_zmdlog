"""Board pages: one board's ranking, every board's leaders, one dungeon's top three."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import messages
from ..characters import (
    CharacterFilterScope,
    CharacterResolutionStatus,
    pick_character_filter_scope,
    ranking_character_names,
    resolve_character_name,
    row_fields,
)
from ..matcher import MatchChoice
from ..messages import shorten
from ..models import BossRanking, HotBossCard
from ..professions import normalize_profession
from ..routing import DEFAULT_RANKING_TOP

if TYPE_CHECKING:
    from ..datasource import ZmdLogsDataSource
    from ..render import LongImageRenderer


@dataclass(frozen=True, slots=True)
class RankingRecipe:
    """One board's public ranking, cut by main C / roster, element and profession."""

    ranking: BossRanking
    query: str
    web_base_url: str | None
    ranking_limit: int
    # The resolved ``--角色`` names, in the board's spelling, and how they
    # are matched: one name as main C when the board has such rows, in the
    # roster otherwise; several names always in the roster (a team has one
    # main C).
    character_filter: tuple[str, ...] | None
    character_filter_scope: CharacterFilterScope
    element_filter: str | None
    profession_filter: str | None
    elements: Mapping[str, str]

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_ranking(
            self.ranking,
            query=self.query,
            ranking_limit=self.ranking_limit,
            web_base_url=self.web_base_url,
            character_filter=self.character_filter,
            character_filter_scope=self.character_filter_scope,
            element_filter=self.element_filter,
            elements=self.elements,
            profession_filter=self.profession_filter,
        )


async def prepare_ranking(
    data: "ZmdLogsDataSource",
    ranking: BossRanking,
    *,
    query: str,
    web_base_url: str | None,
    ranking_limit: int = DEFAULT_RANKING_TOP,
    character_filter: str | None = None,
    element_filter: str | None = None,
    profession_filter: str | None = None,
) -> RankingRecipe | str:
    """The ranking page, or the one-line reason its filters leave nothing.

    ``character_filter`` is the typed names, space-separated, resolved here
    against the board's own rosters. Every filter is checked alone and then
    all together, because each alone may keep rows while the combination
    draws an empty page — which reads as a broken render, not as an answer.
    """

    board = ranking.boss_name
    names: tuple[str, ...] | None = None
    scope = CharacterFilterScope.MAIN
    if character_filter is not None and character_filter.strip():
        resolved = _resolve_filter_names(ranking, character_filter)
        if isinstance(resolved, str):
            return resolved
        names = resolved
        if len(names) == 1:
            # No main-C records is the normal case for supports, so widen
            # the filter to the whole roster instead of answering "nothing".
            scope = pick_character_filter_scope(ranking, names[0])
            if scope is CharacterFilterScope.NONE:
                return f"「{board}」的公开排名里没有带「{names[0]}」的记录。"
        else:
            # Several names ask for teams fielding all of them; a team has
            # one main C, so this is a roster question by definition.
            scope = CharacterFilterScope.ROSTER
            if not any(row_fields(row, names, scope) for row in ranking.rows):
                return (
                    f"「{board}」的公开排名里没有同时带上"
                    f"「{'、'.join(names)}」的记录。"
                )
    elements = await data.character_elements(names=ranking_character_names(ranking))
    if element_filter is not None:
        if not elements:
            return "角色属性目录暂时读不到，无法按属性筛选，请稍后再试。"
        if not any(
            elements.get(row.character_name) == element_filter for row in ranking.rows
        ):
            return f"「{board}」的公开排名里没有主 C 为{element_filter}属性的记录。"
    if profession_filter is not None and not any(
        normalize_profession(row.character_profession or "") == profession_filter
        for row in ranking.rows
    ):
        return f"「{board}」的公开排名里没有主 C 为{profession_filter}的记录。"
    labels = []
    if element_filter is not None:
        labels.append(f"主 C 为{element_filter}属性")
    if profession_filter is not None:
        labels.append(f"主 C 为{profession_filter}")
    if names is not None:
        labels.append(f"带「{'、'.join(names)}」")
    if len(labels) > 1 and not any(
        (element_filter is None or elements.get(row.character_name) == element_filter)
        and (
            profession_filter is None
            or normalize_profession(row.character_profession or "")
            == profession_filter
        )
        and (names is None or row_fields(row, names, scope))
        for row in ranking.rows
    ):
        return f"「{board}」的公开排名里没有{'且'.join(labels)}的记录。"
    return RankingRecipe(
        ranking=ranking,
        query=query,
        web_base_url=web_base_url,
        ranking_limit=ranking_limit,
        character_filter=names,
        character_filter_scope=scope,
        element_filter=element_filter,
        profession_filter=profession_filter,
        elements=elements,
    )


def _resolve_filter_names(
    ranking: BossRanking, character_filter: str
) -> tuple[str, ...] | str:
    """Every ``--角色`` name in the board's spelling, or why one of them is not."""

    names: list[str] = []
    for wanted in character_filter.split():
        resolution = resolve_character_name(wanted, ranking_character_names(ranking))
        if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
            return messages.ambiguous_character(resolution.query, resolution.candidates)
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            return (
                f"「{ranking.boss_name}」的公开排名里没有"
                f"「{shorten(resolution.query)}」这个角色，可能是名字不对。"
            )
        if resolution.name not in names:
            names.append(resolution.name)
    return tuple(names)


@dataclass(frozen=True, slots=True)
class BoardsOverviewRecipe:
    """榜单: every public board with its top three, from the board list itself."""

    cards: tuple[HotBossCard, ...]
    query: str
    web_base_url: str | None

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_all_top3(
            self.cards, query=self.query, web_base_url=self.web_base_url
        )


async def prepare_boards_overview(
    data: "ZmdLogsDataSource", *, web_base_url: str | None, query: str = "榜单"
) -> BoardsOverviewRecipe:
    return BoardsOverviewRecipe(
        cards=await data.list_hot_bosses(), query=query, web_base_url=web_base_url
    )


@dataclass(frozen=True, slots=True)
class DungeonOverviewRecipe:
    """The top three of every board of one dungeon or phase."""

    choice: MatchChoice
    # The dungeon's own boards, in the board list's order.
    cards: tuple[HotBossCard, ...]
    query: str
    web_base_url: str | None

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_dungeon_top3(
            self.choice, self.cards, query=self.query, web_base_url=self.web_base_url
        )


def prepare_dungeon_overview(
    choice: MatchChoice,
    cards: tuple[HotBossCard, ...],
    *,
    query: str,
    web_base_url: str | None,
) -> DungeonOverviewRecipe:
    """``choice`` is a dungeon or scope hit; ``cards`` the whole board list."""

    slugs = set(choice.target.boss_slugs)
    return DungeonOverviewRecipe(
        choice=choice,
        cards=tuple(card for card in cards if card.boss_slug in slugs),
        query=query,
        web_base_url=web_base_url,
    )
