"""战报, 数据 and 对比: a battle's 摘要 and its numbers, two battles side by side."""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import messages
from ..battle_views import view_strip
from ..candidates import CandidateView
from ..client import ZmdLogsAPIError
from ..crit import CritExpectation, CritHit, build_crit_expectation
from ..messages import shorten
from ..metrics import METRIC_DPS
from ..models import BattleDetailSummary, BattleExport

if TYPE_CHECKING:
    from ..datasource import ZmdLogsDataSource
    from ..render import LongImageRenderer, RenderedImage

EXPORT_UNSUPPORTED = "battle_export_unsupported"


def casts_unsupported(error: ZmdLogsAPIError) -> bool:
    """Whether the export endpoint refused an upload older than parser v33.

    Such an upload carries no casts, now or ever; every other refusal may
    pass on a retry.
    """

    return error.status_code == 422 and error.code == EXPORT_UNSUPPORTED


def export_refusal(error: ZmdLogsAPIError) -> str | None:
    """The one-line reason the export endpoint gave, for the two known refusals.

    An upload older than parser v33 carries no casts (422), and the endpoint
    is rate-limited per IP (429); anything else is not the reader's concern.
    """

    if casts_unsupported(error):
        return messages.NO_TIMELINE
    if error.status_code == 429:
        return messages.TIMELINE_RATE_LIMITED
    return None


@dataclass(frozen=True, slots=True)
class BattleRecipe:
    """战报: the detail, and the cast sequence when there is one.

    The 摘要 draws neither the casts nor the suits; the tool's text reads
    both, and whether the export refused the upload decides which of the
    battle's pages it offers (``core/battle_views``).
    """

    battle: BattleDetailSummary
    export: BattleExport | None
    suits: dict[str, str]
    query: str
    web_base_url: str
    # The upload has no casts at all, so it has no 排轴 either.
    casts_unsupported: bool = False
    # 暴击期望; None unless the upload recorded the crit roll of some hit.
    crit: CritExpectation | None = None

    async def draw(self, renderer: "LongImageRenderer") -> "RenderedImage":
        return await renderer.render_battle(
            self.battle,
            query=self.query,
            web_base_url=self.web_base_url,
            crit=self.crit,
            views=view_strip(
                CandidateView.BATTLE,
                self.battle,
                casts_refused=self.casts_unsupported,
            ),
        )


async def prepare_battle(
    data: "ZmdLogsDataSource", battle_id: str, *, query: str, web_base_url: str
) -> BattleRecipe:
    """战报's reads; a missing battle raises, the rest is best effort.

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
        suits=await data.equip_suits_for(battle),
        query=query,
        web_base_url=web_base_url,
        casts_unsupported=error is not None and casts_unsupported(error),
        crit=battle_crit(battle),
    )


def battle_crit(battle: BattleDetailSummary) -> CritExpectation | None:
    """暴击期望 from the battle's own hits; None when none recorded a roll."""

    return build_crit_expectation(
        (
            CritHit(point.value, point.crit_roll, point.character_name)
            for point in battle.damage_points
        ),
        duration_ms=battle.duration_ms,
    )


@dataclass(frozen=True, slots=True)
class BattleDataRecipe:
    """数据: the detail alone, and the 暴击期望 its hits carry.

    It reads neither the casts nor the suits, so its strip judges 排轴 by
    the detail only (``core/battle_views``).
    """

    battle: BattleDetailSummary
    query: str
    web_base_url: str
    crit: CritExpectation | None = None

    async def draw(self, renderer: "LongImageRenderer") -> "RenderedImage":
        return await renderer.render_battle_data(
            self.battle,
            query=self.query,
            web_base_url=self.web_base_url,
            crit=self.crit,
            views=view_strip(CandidateView.DATA, self.battle),
        )


async def prepare_battle_data(
    data: "ZmdLogsDataSource", battle_id: str, *, query: str, web_base_url: str
) -> BattleDataRecipe | str:
    """数据's one read; an upload with no skill statistics has no 数据."""

    battle = await data.get_battle_detail(battle_id)
    if not battle.skill_stats:
        return messages.NO_SKILL_STATS
    return BattleDataRecipe(
        battle=battle,
        query=query,
        web_base_url=web_base_url,
        crit=battle_crit(battle),
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
    # Which of the board's two rankings those ranks are on.
    metric: str = METRIC_DPS

    async def draw(self, renderer: "LongImageRenderer") -> "RenderedImage":
        return await renderer.render_compare(
            self.first,
            self.second,
            query=self.query,
            web_base_url=self.web_base_url,
            rank_a=self.rank_a,
            rank_b=self.rank_b,
            suits=self.suits,
            metric=self.metric,
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
    metric: str = METRIC_DPS,
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
        metric=metric,
    )
