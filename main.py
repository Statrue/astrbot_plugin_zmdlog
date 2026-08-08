"""AstrBot entry point for ZmdBot."""

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.client import (
    DEFAULT_API_BASE_URL,
    DEFAULT_REQUEST_TIMEOUT_MS,
    ZmdLogsClient,
    ZmdLogsClientError,
    is_valid_boss_slug,
)
from .core.models import BossRanking, HotBossCard
from .core.routing import RouteKind, RouteRequest, parse_zmdlog_payload


class ZmdBotPlugin(Star):
    """Query public ZMDLogs rankings from a single ``zmdlog`` command."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}
        self.client = ZmdLogsClient(
            api_base_url=self.config.get("api_base_url", DEFAULT_API_BASE_URL),
            request_timeout_ms=self.config.get(
                "request_timeout_ms",
                DEFAULT_REQUEST_TIMEOUT_MS,
            ),
        )

    @filter.command("zmdlog")
    async def zmdlog(self, event: AstrMessageEvent):
        """查询 ZMDLogs 公开榜单。"""

        route = parse_zmdlog_payload(self._extract_payload(event.get_message_str()))
        try:
            result = await self._dispatch(route)
        except ZmdLogsClientError as exc:
            logger.warning("ZmdBot ranking request failed: %s", type(exc).__name__)
            result = "ZMDLogs 暂时不可用，请稍后重试。"
        yield event.plain_result(result)

    async def _dispatch(self, route: RouteRequest) -> str:
        if route.kind is RouteKind.HELP:
            return "ZmdBot 已加载。帮助图片将在后续步骤接入。"

        if route.kind is RouteKind.ALL_RANKINGS:
            cards = await self.client.list_hot_bosses()
            return self._hot_bosses_preview(cards)

        if is_valid_boss_slug(route.query):
            ranking = await self.client.get_boss_rankings(route.query)
            return self._boss_ranking_preview(ranking)

        return f"已识别查询：{route.query}；名称匹配将在下一步接入。"

    @staticmethod
    def _extract_payload(message: str) -> str:
        """Return all text after the command token.

        AstrBot has already removed the configured wake prefix at this point.
        Splitting on the first whitespace also keeps this compatible with a
        command renamed by an administrator.
        """

        normalized = " ".join(message.split())
        _, separator, payload = normalized.partition(" ")
        return payload if separator else ""

    @staticmethod
    def _hot_bosses_preview(cards: tuple[HotBossCard, ...]) -> str:
        run_count = sum(len(card.top_speed_runs) for card in cards)
        return (
            f"已获取 {len(cards)} 个榜单、{run_count} 条前三名记录；"
            "图片模板将在后续步骤接入。"
        )

    @staticmethod
    def _boss_ranking_preview(ranking: BossRanking) -> str:
        return (
            f"已获取「{ranking.dungeon_name} · {ranking.boss_name}」DPS 榜单，"
            f"共 {len(ranking.rows)} 条公开排名；图片模板将在后续步骤接入。"
        )

    async def terminate(self) -> None:
        """Release plugin resources added by later implementation steps."""

        await self.client.close()
        logger.info("ZmdBot plugin terminated.")
