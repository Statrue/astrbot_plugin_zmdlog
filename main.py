"""AstrBot entry point for ZmdBot."""

from pathlib import Path

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
from .core.matcher import (
    AliasConfig,
    AliasConfigError,
    MatchChoice,
    MatchStatus,
    RankingMatcher,
    TargetType,
)
from .core.routing import RouteKind, RouteRequest, parse_zmdlog_payload

_BOARD_QUERY_TARGETS = frozenset(
    {TargetType.BOARD, TargetType.DUNGEON, TargetType.DUNGEON_SCOPE}
)


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
        self.aliases = self._load_aliases()
        self.fuzzy_match_threshold = self.config.get(
            "fuzzy_match_threshold",
            0.65,
        )
        self.ambiguity_score_gap = self.config.get(
            "ambiguity_score_gap",
            0.08,
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

        try:
            cards = await self.client.list_hot_bosses()
        except ZmdLogsClientError:
            if self._looks_like_direct_slug(route.query):
                ranking = await self.client.get_boss_rankings(route.query)
                return self._boss_ranking_preview(ranking)
            raise

        matcher = RankingMatcher(
            cards,
            self.aliases,
            fuzzy_threshold=self.fuzzy_match_threshold,
            ambiguity_score_gap=self.ambiguity_score_gap,
        )
        for issue in matcher.issues:
            logger.warning("ZmdBot alias index: %s", issue)
        # The account version can extend the smart route's set without changing
        # the matcher or the explicit board route.
        match = matcher.match(route.query, allowed_types=_BOARD_QUERY_TARGETS)

        if match.status is MatchStatus.NOT_FOUND:
            if self._looks_like_direct_slug(route.query):
                ranking = await self.client.get_boss_rankings(route.query)
                return self._boss_ranking_preview(ranking)
            return f"没有找到与「{route.query}」匹配的榜单或副本。"
        if match.status is MatchStatus.AMBIGUOUS:
            return self._candidate_preview(match.candidates)

        choice = match.selected
        if choice is None:
            return f"没有找到与「{route.query}」匹配的榜单或副本。"
        if choice.target.target_type is TargetType.BOARD:
            ranking = await self.client.get_boss_rankings(choice.target.key)
            return self._boss_ranking_preview(ranking)

        selected_slugs = set(choice.target.boss_slugs)
        selected_cards = tuple(
            card for card in cards if card.boss_slug in selected_slugs
        )
        return self._dungeon_preview(choice, selected_cards)

    def _load_aliases(self) -> AliasConfig:
        configured_path = Path(self.config.get("alias_file_path", "aliases.json"))
        alias_path = (
            configured_path
            if configured_path.is_absolute()
            else Path(__file__).parent / configured_path
        )
        try:
            return AliasConfig.load(alias_path)
        except AliasConfigError as exc:
            logger.error("ZmdBot alias configuration failed: %s", exc)
            return AliasConfig.empty()

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

    @staticmethod
    def _dungeon_preview(
        choice: MatchChoice,
        cards: tuple[HotBossCard, ...],
    ) -> str:
        run_count = sum(len(card.top_speed_runs) for card in cards)
        if choice.target.target_type is TargetType.DUNGEON_SCOPE:
            return (
                f"已命中副本范围「{choice.target.name}」，包含 "
                f"{len(choice.target.dungeon_names)} 个标准副本、"
                f"{len(cards)} 个榜单和 {run_count} 条前三名记录；"
                "图片模板将在后续步骤接入。"
            )
        return (
            f"已命中副本「{choice.target.name}」，包含 {len(cards)} 个榜单和 "
            f"{run_count} 条前三名记录；图片模板将在后续步骤接入。"
        )

    @staticmethod
    def _candidate_preview(candidates: tuple[MatchChoice, ...]) -> str:
        labels = {
            TargetType.BOARD: "榜单",
            TargetType.DUNGEON: "副本",
            TargetType.DUNGEON_SCOPE: "副本范围",
        }
        lines = ["匹配到多个可能的目标，请提供更完整的关键词："]
        for index, candidate in enumerate(candidates, start=1):
            dungeon = "、".join(candidate.target.dungeon_names)
            lines.append(
                f"{index}. {candidate.target.name}"
                f"（{labels[candidate.target.target_type]}；{dungeon}；"
                f"查询关键词：{candidate.target.query_text}）"
            )
        return "\n".join(lines)

    @staticmethod
    def _looks_like_direct_slug(query: str) -> bool:
        return is_valid_boss_slug(query) and ("_" in query or "-" in query)

    async def terminate(self) -> None:
        """Release plugin resources added by later implementation steps."""

        await self.client.close()
        logger.info("ZmdBot plugin terminated.")
