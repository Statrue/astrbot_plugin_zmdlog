"""角色档案: how one character is built and how fast it clears, from its profile.

The site keeps a profile for every character, four-stars included, so a
name is resolved against the game-data catalog, which lists them all with
their keys; the statistics catalog behind 角色统计 lists six-stars only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import messages
from ..characters import CharacterResolutionStatus, resolve_character_name
from ..client import ZmdLogsAPIError
from ..history import window_label
from ..messages import shorten
from ..models import CharacterProfile, CharacterType

if TYPE_CHECKING:
    from ..datasource import ZmdLogsDataSource
    from ..render import LongImageRenderer, RenderedImage


@dataclass(frozen=True, slots=True)
class CharacterProfileRecipe:
    """One character's shares and 通关名次, and the catalog its faces come from."""

    profile: CharacterProfile
    character: CharacterType
    query: str
    web_base_url: str | None
    icons: Mapping[str, str]

    async def draw(self, renderer: "LongImageRenderer") -> "RenderedImage":
        return await renderer.render_character_profile(
            self.profile,
            character=self.character,
            query=self.query,
            web_base_url=self.web_base_url,
            icons=self.icons,
        )


async def find_profile_character(
    data: "ZmdLogsDataSource", query: str
) -> CharacterType | str:
    """The catalog entry ``query`` names, or the reply saying why it names none.

    Resolved as every character command resolves a name — pinyin initials
    included. A miss asks the catalog for its bounded re-read once, because
    a character released since the last read looks exactly like one.
    """

    catalog = await data.get_character_types()
    resolution = _resolve(query, catalog)
    if resolution.status is CharacterResolutionStatus.NOT_FOUND:
        catalog = await data.get_character_types(names=(resolution.query,))
        resolution = _resolve(query, catalog)
    if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
        return messages.ambiguous_character(resolution.query, resolution.candidates)
    if resolution.status is CharacterResolutionStatus.NOT_FOUND:
        return messages.CHARACTER_UNKNOWN.format(name=shorten(resolution.query))
    return catalog[resolution.name]


def _resolve(query: str, catalog: Mapping[str, CharacterType]):
    # A catalog entry without a key names no profile.
    return resolve_character_name(
        query, tuple(name for name, entry in catalog.items() if entry.key)
    )


async def prepare_character_profile(
    data: "ZmdLogsDataSource",
    character: CharacterType,
    *,
    time_range: str,
    query: str,
    web_base_url: str | None,
    boss_slug: str | None = None,
) -> CharacterProfileRecipe | str:
    """``character`` is the catalog's entry; ``boss_slug`` cuts it to one board.

    Three refusals, worded the same on every path: a character the site
    keeps no profile of, a board it keeps none for (the crisis contract —
    asked for a board the index lists, ``boss_not_found`` can mean nothing
    else), and a window with no record of the character in it, which would
    draw a page of empty sections. Cut to a board, that last one says the
    board: the character may well have records elsewhere.
    """

    try:
        profile = await data.get_character_profile(
            character.key, time_range=time_range, boss_slug=boss_slug
        )
    except ZmdLogsAPIError as exc:
        if exc.status_code != 404:
            raise
        if exc.code == "character_not_found":
            return messages.CHARACTER_PROFILE_MISSING
        if exc.code == "boss_not_found" and boss_slug is not None:
            return messages.BOARD_HAS_NO_PROFILE
        raise
    if profile.sample_count == 0:
        refusal = (
            messages.NO_PROFILE_RECORDS
            if boss_slug is None
            else messages.NO_BOARD_PROFILE_RECORDS
        )
        return refusal.format(window=window_label(time_range), name=character.name)
    names = (character.name, *(entry.name for entry in profile.teammates))
    return CharacterProfileRecipe(
        profile=profile,
        character=character,
        query=query,
        web_base_url=web_base_url,
        icons=await data.character_icons(names=names),
    )
