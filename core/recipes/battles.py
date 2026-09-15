"""战报 and 对比: one battle's card, and two battles of one boss side by side."""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import messages
from ..client import ZmdLogsAPIError
from ..messages import shorten
from ..models import BattleDetailSummary, BattleExport

if TYPE_CHECKING:
    from ..datasource import ZmdLogsDataSource
    from ..render import LongImageRenderer

EXPORT_UNSUPPORTED = "battle_export_unsupported"


def export_refusal(error: ZmdLogsAPIError) -> str | None:
    """The one-line reason the export endpoint gave, for the two known refusals.

    An upload older than parser v33 carries no casts (422), and the endpoint
    is rate-limited per IP (429); anything else is not the reader's concern.
    """

    if error.status_code == 422 and error.code == EXPORT_UNSUPPORTED:
        return messages.NO_TIMELINE
    if error.status_code == 429:
        return messages.TIMELINE_RATE_LIMITED
    return None


@dataclass(frozen=True, slots=True)
class BattleRecipe:
    """The battle card: the detail, and the cast sequence when there is one."""

    battle: BattleDetailSummary
    export: BattleExport | None
    # Why the 施法节奏 section is missing, when the export endpoint said so.
    export_note: str | None
    suits: dict[str, str]
    query: str
    web_base_url: str

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_battle(
            self.battle,
            query=self.query,
            web_base_url=self.web_base_url,
            export=self.export,
            export_note=self.export_note,
            suits=self.suits,
        )


async def prepare_battle(
    data: "ZmdLogsDataSource", battle_id: str, *, query: str, web_base_url: str
) -> BattleRecipe:
    """The card's reads; a missing battle raises, the rest is best effort.

    Three upstream reads, each with its own latency, go out together:
    awaiting them in turn put the optional two in front of the payload.
    The catalog is read once more with the battle's own suit ids, which is
    a cache hit unless the battle wore a suit the catalog has never named.
    """

    battle, (export, error), _warm = await asyncio.gather(
        data.get_battle_detail(battle_id),
        data.battle_export_for_card(battle_id),
        data.equip_suits_for(),
    )
    return BattleRecipe(
        battle=battle,
        export=export,
        export_note=export_refusal(error) if error is not None else None,
        suits=await data.equip_suits_for(battle),
        query=query,
        web_base_url=web_base_url,
    )


@dataclass(frozen=True, slots=True)
class CompareRecipe:
    """Two battles side by side.

    Unlike the other recipes this one is built even when the page must not
    be drawn: two bosses have two rotations, so the page would compare
    nothing, but the differences are still worth naming in text. ``refusal``
    carries that reason; the command answers with it, the tool keeps its
    text and drops the picture.
    """

    first: BattleDetailSummary
    second: BattleDetailSummary
    suits: dict[str, str]
    refusal: str | None
    query: str
    web_base_url: str
    # The board ranks the two were picked by, when they were.
    rank_a: int | None = None
    rank_b: int | None = None

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_compare(
            self.first,
            self.second,
            query=self.query,
            web_base_url=self.web_base_url,
            rank_a=self.rank_a,
            rank_b=self.rank_b,
            suits=self.suits,
        )


async def prepare_compare(
    data: "ZmdLogsDataSource",
    battle_id_a: str,
    battle_id_b: str,
    *,
    query: str,
    web_base_url: str,
    rank_a: int | None = None,
    rank_b: int | None = None,
) -> CompareRecipe:
    """Both details, fetched together; the caller has ruled out one id twice."""

    first, second, _warm = await asyncio.gather(
        data.get_battle_detail(battle_id_a),
        data.get_battle_detail(battle_id_b),
        data.equip_suits_for(),
    )
    refusal = None
    if first.boss_name != second.boss_name:
        refusal = messages.COMPARE_CROSS_BOSS.format(
            first=shorten(first.boss_name), second=shorten(second.boss_name)
        )
    return CompareRecipe(
        first=first,
        second=second,
        suits=await data.equip_suits_for(first, second),
        refusal=refusal,
        query=query,
        web_base_url=web_base_url,
        rank_a=rank_a,
        rank_b=rank_b,
    )
