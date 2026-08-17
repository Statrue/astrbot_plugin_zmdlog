"""AstrBot entry point for ZmdLogBot."""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # StarTools.get_data_dir is missing on older AstrBot releases.
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover - depends on host AstrBot version
    StarTools = None

from .core.cache import AsyncTTLCache, CacheState
from .core.client import (
    DEFAULT_API_BASE_URL,
    DEFAULT_REQUEST_TIMEOUT_MS,
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsClientError,
    is_valid_boss_slug,
)
from .core.identifiers import (
    PublicReferenceError,
    extract_battle_references,
    parse_account_reference,
    parse_battle_reference,
)
from .core.matcher import (
    AliasConfig,
    AliasConfigError,
    MatchChoice,
    MatchStatus,
    RankingMatcher,
    TargetType,
)
from .core.models import (
    BattleDetailSummary,
    BossRanking,
    HotBossCard,
    PublicUserRankings,
)
from .core.render import (
    LongImageRenderer,
    RenderError,
    TemplateConfigurationError,
)
from .core.routing import (
    RouteKind,
    RouteParseError,
    RouteRequest,
    parse_zmdlog_payload,
)

_BOARD_QUERY_TARGETS = frozenset(
    {TargetType.BOARD, TargetType.DUNGEON, TargetType.DUNGEON_SCOPE}
)
_RENDER_FAILURE_MESSAGE = "图片生成失败，请稍后重试。"
_UNEXPECTED_FAILURE_MESSAGE = "ZmdLogBot 暂时无法完成查询，请稍后重试。"
_BATTLE_LINK_FILTER = (
    r"https?://[^\s<>\"']+/(?:battle|share|axis)/btl_[A-Za-z0-9_-]+"
)


@dataclass(frozen=True, slots=True)
class _DispatchOutcome:
    image_path: str | None = None
    message: str | None = None


class ZmdLogBotPlugin(Star):
    """Query public ZMDLogs rankings, accounts, and battle reports."""

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
        account_cache_ttl_seconds = self.config.get(
            "account_cache_ttl_seconds",
            60,
        )
        battle_cache_ttl_seconds = self.config.get(
            "battle_cache_ttl_seconds",
            300,
        )
        self.account_cache = AsyncTTLCache[str, PublicUserRankings](
            account_cache_ttl_seconds,
        )
        self.battle_cache = AsyncTTLCache[str, BattleDetailSummary](
            battle_cache_ttl_seconds,
        )
        self.web_base_url = self.config.get(
            "web_base_url",
            DEFAULT_API_BASE_URL,
        )
        self.auto_expand_battle_links = bool(
            self.config.get("auto_expand_battle_links", False)
        )
        configured_dedupe_seconds = self.config.get(
            "battle_link_dedupe_seconds",
            300,
        )
        self.battle_link_dedupe_seconds = (
            float(configured_dedupe_seconds)
            if isinstance(configured_dedupe_seconds, int | float)
            and not isinstance(configured_dedupe_seconds, bool)
            and configured_dedupe_seconds > 0
            else 300.0
        )
        self._auto_expand_lock = asyncio.Lock()
        self._auto_expanded_until: dict[tuple[str, str], float] = {}
        self.fallback_to_astrbot_renderer = bool(
            self.config.get("fallback_to_astrbot_renderer", True)
        )
        try:
            self.renderer: LongImageRenderer | None = LongImageRenderer(
                Path(__file__).parent,
                render_timeout_ms=self.config.get(
                    "render_timeout_ms",
                    30_000,
                ),
                output_dir=self._render_output_dir(),
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

    @filter.on_astrbot_loaded()
    async def warm_up_renderer(self) -> None:
        """Start Chromium ahead of the first query so it does not time out."""

        if self.renderer is None:
            return
        try:
            await self.renderer.warm_up()
        except RenderError as exc:
            logger.warning(
                "ZmdLogBot renderer warm-up failed: %s",
                type(exc).__name__,
            )

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
        except ZmdLogsAPIError as exc:
            logger.warning("ZmdLogBot API request failed: %s", exc.code)
            yield event.plain_result(self._api_error_message(route, exc))
            return
        except ZmdLogsClientError as exc:
            logger.warning("ZmdLogBot request failed: %s", type(exc).__name__)
            yield event.plain_result("ZMDLogs 暂时不可用，请稍后重试。")
            return
        except RenderError as exc:
            logger.error("ZmdLogBot image rendering failed: %s", type(exc).__name__)
            fallback_path = await self._render_with_astrbot(exc)
            if fallback_path is not None:
                yield event.image_result(fallback_path)
                return
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

    @filter.regex(_BATTLE_LINK_FILTER)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def expand_battle_link(self, event: AstrMessageEvent):
        """Expand the first trusted ZMDLogs battle link in a group message."""

        if not self.auto_expand_battle_links or self._is_command_message(event):
            return
        battle_ids = extract_battle_references(
            event.get_message_str(),
            web_base_url=self.web_base_url,
            limit=1,
        )
        if not battle_ids:
            return
        battle_id = battle_ids[0]
        origin = getattr(event, "unified_msg_origin", "") or "unknown"
        if not await self._claim_auto_expand(origin, battle_id):
            return
        try:
            battle = await self._get_battle_detail(battle_id)
            renderer = self._require_renderer()
            image_path = await renderer.render_battle(
                battle,
                query=battle_id,
                web_base_url=self.web_base_url,
            )
        except ZmdLogsAPIError as exc:
            await self._release_auto_expand(origin, battle_id)
            logger.warning("ZmdLogBot auto-expand API request failed: %s", exc.code)
            yield event.plain_result(
                "链接对应的公开战报不存在、未公开或已删除。"
                if exc.status_code == 404
                else "ZMDLogs 暂时不可用，请稍后重试。"
            )
            return
        except ZmdLogsClientError as exc:
            await self._release_auto_expand(origin, battle_id)
            logger.warning(
                "ZmdLogBot auto-expand request failed: %s",
                type(exc).__name__,
            )
            yield event.plain_result("ZMDLogs 暂时不可用，请稍后重试。")
            return
        except RenderError as exc:
            await self._release_auto_expand(origin, battle_id)
            logger.error(
                "ZmdLogBot auto-expand rendering failed: %s",
                type(exc).__name__,
            )
            fallback_path = await self._render_with_astrbot(exc)
            if fallback_path is not None:
                yield event.image_result(fallback_path)
                return
            yield event.plain_result(_RENDER_FAILURE_MESSAGE)
            return
        except Exception:
            await self._release_auto_expand(origin, battle_id)
            logger.exception("ZmdLogBot unexpected auto-expand failure")
            yield event.plain_result(_UNEXPECTED_FAILURE_MESSAGE)
            return
        yield event.image_result(image_path)

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
                web_base_url=self.web_base_url,
            )
            return _DispatchOutcome(image_path=image_path)

        if route.kind is RouteKind.ACCOUNT_QUERY:
            try:
                account_id = parse_account_reference(
                    route.query,
                    web_base_url=self.web_base_url,
                )
            except PublicReferenceError as exc:
                return _DispatchOutcome(message=str(exc))
            account = await self._get_public_user_rankings(account_id)
            image_path = await renderer.render_account(
                account,
                query=route.query,
                web_base_url=self.web_base_url,
            )
            return _DispatchOutcome(image_path=image_path)

        if route.kind is RouteKind.BATTLE_QUERY:
            try:
                battle_id = parse_battle_reference(
                    route.query,
                    web_base_url=self.web_base_url,
                )
            except PublicReferenceError as exc:
                return _DispatchOutcome(message=str(exc))
            battle = await self._get_battle_detail(battle_id)
            image_path = await renderer.render_battle(
                battle,
                query=route.query,
                web_base_url=self.web_base_url,
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
                    web_base_url=self.web_base_url,
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
                    web_base_url=self.web_base_url,
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
                web_base_url=self.web_base_url,
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
            web_base_url=self.web_base_url,
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

    @staticmethod
    def _render_output_dir() -> Path | None:
        """Keep generated images under AstrBot's data dir, never the plugin dir."""

        if StarTools is None:
            return None
        try:
            return StarTools.get_data_dir("astrbot_plugin_zmdlog") / "render"
        except Exception as exc:
            logger.warning(
                "ZmdLogBot cannot resolve the plugin data dir: %s",
                type(exc).__name__,
            )
            return None

    async def _render_with_astrbot(self, error: RenderError) -> str | None:
        """Render the already-built page through AstrBot's own text-to-image
        service when the bundled Chromium capture is unavailable."""

        html = error.html
        if not self.fallback_to_astrbot_renderer or html is None:
            return None
        # AstrBot treats the argument as a Jinja template; neutralise delimiters
        # so user-supplied text (nicknames) can never become template code.
        html = (
            html.replace("{{", "&#123;&#123;")
            .replace("{%", "&#123;%")
            .replace("{#", "&#123;#")
        )
        try:
            try:
                return await self.html_render(
                    html,
                    {},
                    options={"full_page": True},
                )
            except TypeError:
                # Older AstrBot builds have no ``options`` parameter.
                return await self.html_render(html, {})
        except Exception as exc:
            logger.warning(
                "ZmdLogBot AstrBot renderer fallback failed: %s",
                type(exc).__name__,
            )
            return None

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

    async def _get_public_user_rankings(
        self,
        account_id: str,
    ) -> PublicUserRankings:
        result = await self.account_cache.get_or_load(
            account_id,
            lambda: self.client.get_public_user_rankings(account_id),
        )
        return result.value

    async def _get_battle_detail(self, battle_id: str) -> BattleDetailSummary:
        result = await self.battle_cache.get_or_load(
            battle_id,
            lambda: self.client.get_battle_detail(battle_id),
        )
        return result.value

    async def _claim_auto_expand(self, origin: str, battle_id: str) -> bool:
        now = time.monotonic()
        key = (origin, battle_id)
        async with self._auto_expand_lock:
            self._auto_expanded_until = {
                entry_key: expires_at
                for entry_key, expires_at in self._auto_expanded_until.items()
                if expires_at > now
            }
            if key in self._auto_expanded_until:
                return False
            self._auto_expanded_until[key] = (
                now + self.battle_link_dedupe_seconds
            )
            return True

    async def _release_auto_expand(self, origin: str, battle_id: str) -> None:
        async with self._auto_expand_lock:
            self._auto_expanded_until.pop((origin, battle_id), None)

    def _is_command_message(self, event: AstrMessageEvent) -> bool:
        raw_message = getattr(event.message_obj, "message_str", "")
        if not isinstance(raw_message, str):
            return False
        normalized = raw_message.lstrip()
        prefix = self._command_prefix(event)
        return normalized.startswith(f"{prefix}zmdlog") or normalized.startswith(
            "zmdlog"
        )

    @staticmethod
    def _api_error_message(
        route: RouteRequest,
        error: ZmdLogsAPIError,
    ) -> str:
        if error.status_code == 404:
            if route.kind is RouteKind.ACCOUNT_QUERY:
                return "没有找到这个公开账号，或该账号暂无公开榜单记录。"
            if route.kind is RouteKind.BATTLE_QUERY:
                return "战报不存在、未公开或已删除。"
            if route.kind in {RouteKind.RANKING_QUERY, RouteKind.SMART_QUERY}:
                return "没有找到这个榜单，可能已下线或暂未公开。"
        return "ZMDLogs 暂时不可用，请稍后重试。"

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
        await self.account_cache.close()
        await self.battle_cache.close()
        try:
            await self.client.close()
        finally:
            if self.renderer is not None:
                await self.renderer.close()
        logger.info("ZmdLogBot plugin terminated.")
