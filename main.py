"""AstrBot entry point for ZmdBot."""

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.routing import RouteKind, RouteRequest, parse_zmdlog_payload


class ZmdBotPlugin(Star):
    """Query public ZMDLogs rankings from a single ``zmdlog`` command."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context)
        self.config = config or {}

    @filter.command("zmdlog")
    async def zmdlog(self, event: AstrMessageEvent):
        """查询 ZMDLogs 公开榜单。"""

        route = parse_zmdlog_payload(self._extract_payload(event.get_message_str()))
        yield event.plain_result(self._placeholder_result(route))

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
    def _placeholder_result(route: RouteRequest) -> str:
        """Describe a routed request until its feature handler is implemented."""

        if route.kind is RouteKind.HELP:
            return "ZmdBot 已加载。帮助图片将在后续步骤接入。"
        if route.kind is RouteKind.ALL_RANKINGS:
            return "已识别全部榜单查询；ZMDLogs API 将在下一步接入。"
        if route.kind is RouteKind.RANKING_QUERY:
            return f"已识别榜单查询：{route.query}"
        return f"已识别快捷查询：{route.query}"

    async def terminate(self) -> None:
        """Release plugin resources added by later implementation steps."""

        logger.info("ZmdBot plugin terminated.")
