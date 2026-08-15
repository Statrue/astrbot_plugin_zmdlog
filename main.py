"""AstrBot entry point for ZmdLogBot."""

from dataclasses import dataclass
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.cache import AsyncTTLCache, CacheState
from .core.client import (
    DEFAULT_API_BASE_URL,
    DEFAULT_REQUEST_TIMEOUT_MS,
    ZmdLogsClient,
    ZmdLogsClientError,
    is_valid_boss_slug,
)
from .core.matcher import (
    AliasConfig,
    AliasConfigError,
    MatchChoice,
    MatchStatus,
    RankingMatcher,
    TargetType,
)
from .core.models import BossRanking, HotBossCard
from .core.routing import (
    RouteKind,
    RouteParseError,
    RouteRequest,
    parse_zmdlog_payload,
)
from .core.render import (
    LongImageRenderer,
    RenderError,
    TemplateConfigurationError,
)

_BOARD_QUERY_TARGETS = frozenset(
    {TargetType.BOARD, TargetType.DUNGEON, TargetType.DUNGEON_SCOPE}
)
_RENDER_FAILURE_MESSAGE = "图片生成失败，请稍后重试。"
_UNEXPECTED_FAILURE_MESSAGE = "ZmdLogBot 暂时无法完成查询，请稍后重试。"


@dataclass(frozen=True, slots=True)
class _DispatchOutcome:
    image_path: str | None = None
    message: str | None = None


class ZmdLogBotPlugin(Star):
    """Query public ZMDLogs rankings from a single ``zmdlog`` command."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}
        self.api_base_url = self.config.get(
            "api_base_url",
            DEFAULT_API_BASE_URL,
        )
        self.client = ZmdLogsClient(
            api_base_url=self.api_base_url,
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
        cache_ttl_seconds = self.config.get(
            "ranking_cache_ttl_seconds",
            60,
        )
        self.hot_boss_cache = AsyncTTLCache[
            str,
            tuple[HotBossCard, ...],
        ](
            cache_ttl_seconds,
            stale_ttl_seconds=cache_ttl_seconds,
        )
        self.boss_ranking_cache = AsyncTTLCache[str, BossRanking](
            cache_ttl_seconds,
        )
        self.web_base_url = self.config.get(
            "web_base_url",
            DEFAULT_API_BASE_URL,
        )
        try:
            self.renderer: LongImageRenderer | None = LongImageRenderer(
                Path(__file__).parent,
                render_timeout_ms=self.config.get(
                    "render_timeout_ms",
                    30_000,
                ),
                allowed_image_origins=(
                    self.api_base_url,
                    self.web_base_url,
                ),
            )
        except TemplateConfigurationError as exc:
            logger.error(
                "ZmdLogBot renderer configuration failed: %s",
                type(exc).__name__,
            )
            self.renderer = None

    @filter.command("zmdlog")
    async def zmdlog(self, event: AstrMessageEvent):
        """查询 ZMDLogs 公开榜单。"""

        try:
            route = parse_zmdlog_payload(
                self._extract_payload(event.get_message_str())
            )
        except RouteParseError as exc:
            yield event.plain_result(str(exc))
            return
        try:
            outcome = await self._dispatch(
                route,
                command_prefix=self._command_prefix(event),
            )
        except ZmdLogsClientError as exc:
            logger.warning("ZmdLogBot ranking request failed: %s", type(exc).__name__)
            yield event.plain_result("ZMDLogs 暂时不可用，请稍后重试。")
            return
        except RenderError as exc:
            logger.error("ZmdLogBot image rendering failed: %s", type(exc).__name__)
            yield event.plain_result(_RENDER_FAILURE_MESSAGE)
            return
        except Exception:
            logger.exception("ZmdLogBot unexpected command failure")
            yield event.plain_result(_UNEXPECTED_FAILURE_MESSAGE)
            return

        if outcome.image_path is not None:
            yield event.image_result(outcome.image_path)
            return
        yield event.plain_result(outcome.message or "本次查询未产生结果。")

    async def _dispatch(
        self,
        route: RouteRequest,
        *,
        command_prefix: str,
    ) -> _DispatchOutcome:
        renderer = self._require_renderer()
        if route.kind is RouteKind.HELP:
            image_path = await renderer.render_help(
                command_prefix=command_prefix,
            )
            return _DispatchOutcome(image_path=image_path)

        if route.kind is RouteKind.ALL_RANKINGS:
            cards = await self._list_hot_bosses()
            image_path = await renderer.render_all_top3(
                cards,
                query="榜单",
            )
            return _DispatchOutcome(image_path=image_path)

        try:
            cards = await self._list_hot_bosses()
        except ZmdLogsClientError:
            if self._looks_like_direct_slug(route.query):
                ranking = await self._get_boss_ranking(route.query)
                image_path = await renderer.render_ranking(
                    ranking,
                    query=route.query,
                    ranking_limit=route.ranking_limit,
                )
                return _DispatchOutcome(image_path=image_path)
            raise

        matcher = RankingMatcher(
            cards,
            self.aliases,
            fuzzy_threshold=self.fuzzy_match_threshold,
            ambiguity_score_gap=self.ambiguity_score_gap,
        )
        for issue in matcher.issues:
            logger.warning("ZmdLogBot alias index: %s", issue)
        # The account version can extend the smart route's set without changing
        # the matcher or the explicit board route.
        match = matcher.match(route.query, allowed_types=_BOARD_QUERY_TARGETS)

        if match.status is MatchStatus.NOT_FOUND:
            if self._looks_like_direct_slug(route.query):
                ranking = await self._get_boss_ranking(route.query)
                image_path = await renderer.render_ranking(
                    ranking,
                    query=route.query,
                    ranking_limit=route.ranking_limit,
                )
                return _DispatchOutcome(image_path=image_path)
            return _DispatchOutcome(
                message=f"没有找到与「{route.query}」匹配的榜单或副本。"
            )
        if match.status is MatchStatus.AMBIGUOUS:
            return _DispatchOutcome(
                message=self._candidate_preview(match.candidates)
            )

        choice = match.selected
        if choice is None:
            return _DispatchOutcome(
                message=f"没有找到与「{route.query}」匹配的榜单或副本。"
            )
        if choice.target.target_type is TargetType.BOARD:
            ranking = await self._get_boss_ranking(choice.target.key)
            image_path = await renderer.render_ranking(
                ranking,
                query=route.query,
                ranking_limit=route.ranking_limit,
            )
            return _DispatchOutcome(image_path=image_path)

        if route.ranking_top is not None:
            return _DispatchOutcome(
                message="--top 仅适用于具体榜单查询，请补充具体榜单关键词。"
            )

        selected_slugs = set(choice.target.boss_slugs)
        selected_cards = tuple(
            card for card in cards if card.boss_slug in selected_slugs
        )
        image_path = await renderer.render_dungeon_top3(
            choice,
            selected_cards,
            query=route.query,
        )
        return _DispatchOutcome(image_path=image_path)

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
            logger.error("ZmdLogBot alias configuration failed: %s", exc)
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

    def _command_prefix(self, event: AstrMessageEvent) -> str:
        try:
            config = self.context.get_config(event.unified_msg_origin)
            prefixes = config.get("wake_prefix", [])
        except Exception as exc:
            logger.debug(
                "ZmdLogBot cannot read the active command prefix: %s",
                type(exc).__name__,
            )
            return ""
        if isinstance(prefixes, str):
            configured = (prefixes,)
        elif isinstance(prefixes, (list, tuple)):
            configured = tuple(
                prefix for prefix in prefixes if isinstance(prefix, str)
            )
        else:
            configured = ()
        raw_message = getattr(event.message_obj, "message_str", "")
        if isinstance(raw_message, str):
            for prefix in configured:
                if raw_message.startswith(f"{prefix}zmdlog"):
                    return prefix
            if raw_message.startswith("zmdlog"):
                return ""
        return configured[0] if configured else ""

    def _require_renderer(self) -> LongImageRenderer:
        if self.renderer is None:
            raise RenderError("renderer is unavailable")
        return self.renderer

    async def _list_hot_bosses(self) -> tuple[HotBossCard, ...]:
        result = await self.hot_boss_cache.get_or_load(
            "all_board_top3",
            self.client.list_hot_bosses,
            allow_stale_on_error=True,
        )
        if result.state is CacheState.STALE:
            logger.warning(
                "ZmdLogBot is using stale hot-bosses data after refresh failure."
            )
        return result.value

    async def _get_boss_ranking(self, boss_slug: str) -> BossRanking:
        result = await self.boss_ranking_cache.get_or_load(
            boss_slug,
            lambda: self.client.get_boss_rankings(boss_slug),
        )
        return result.value

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
        """Release HTTP, browser, and generated-image resources."""

        await self.hot_boss_cache.close()
        await self.boss_ranking_cache.close()
        try:
            await self.client.close()
        finally:
            if self.renderer is not None:
                await self.renderer.close()
        logger.info("ZmdLogBot plugin terminated.")
