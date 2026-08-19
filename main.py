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
from .core.candidates import (
    CandidateStore,
    CandidateView,
    PendingCandidates,
    extract_code,
    format_candidates,
    parse_selection,
)
from .core.characters import (
    CharacterResolutionStatus,
    ranking_character_names,
    resolve_character_name,
)
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
    MatchLevel,
    MatchStatus,
    RankingMatcher,
    TargetType,
)
from .core.models import (
    BattleDetailSummary,
    BossRanking,
    CharacterStatistics,
    HotBossCard,
    PublicUserRankings,
    parse_hot_bosses,
)
from .core.persistence import load_json, save_json
from .core.render import (
    LongImageRenderer,
    RenderError,
    TemplateConfigurationError,
)
from .core.routing import (
    DEFAULT_RANKING_TOP,
    RouteKind,
    RouteParseError,
    RouteRequest,
    parse_zmdlog_payload,
)

_BOARD_QUERY_TARGETS = frozenset(
    {TargetType.BOARD, TargetType.DUNGEON, TargetType.DUNGEON_SCOPE}
)
_ALIAS_ROUTES = frozenset(
    {RouteKind.ALIAS_LIST, RouteKind.ALIAS_ADD, RouteKind.ALIAS_REMOVE}
)
_PLUGIN_DATA_NAME = "astrbot_plugin_zmdlog"
_HOT_BOSSES_SNAPSHOT = "hot-bosses.json"
_RENDER_FAILURE_MESSAGE = "图片生成失败，请稍后重试。"
_UNEXPECTED_FAILURE_MESSAGE = "ZmdLogBot 暂时无法完成查询，请稍后重试。"
_BATTLE_LINK_FILTER = (
    r"https?://[^\s<>\"']+/(?:battle|share|axis)/btl_[A-Za-z0-9_-]+"
)
_BOARD_ONLY_VIEWS = frozenset(
    {CandidateView.CHARACTER_STATS, CandidateView.ROSTER}
)
_CHARACTER_STATS_UNAVAILABLE = "character_statistics_not_available"


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
        self.data_dir = self._plugin_data_dir()
        self.alias_path = self._resolve_alias_path()
        self.aliases = self._load_aliases()
        self.candidates = CandidateStore()
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
        character_stats_cache_ttl_seconds = self.config.get(
            "character_stats_cache_ttl_seconds",
            120,
        )
        self.character_stats_cache = AsyncTTLCache[
            tuple[str, str, str],
            CharacterStatistics,
        ](
            character_stats_cache_ttl_seconds,
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

        payload = self._extract_payload(event.get_message_str())
        quoted_code = self._quoted_candidate_code(event)
        if quoted_code is not None and parse_selection(payload) is not None:
            async for result in self._reply_with_candidate(
                event,
                quoted_code,
                payload,
            ):
                yield result
            return

        try:
            route = parse_zmdlog_payload(payload)
        except RouteParseError as exc:
            yield event.plain_result(str(exc))
            return
        if route.kind in _ALIAS_ROUTES:
            yield event.plain_result(
                await self._handle_alias_route(route, event)
            )
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

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def pick_candidate(self, event: AstrMessageEvent):
        """Let a bare ``2`` that quotes a candidate list select that entry."""

        if self._is_command_message(event):
            return
        text = event.get_message_str() or ""
        if parse_selection(text) is None:
            return
        code = self._quoted_candidate_code(event)
        if code is None:
            return
        async for result in self._reply_with_candidate(event, code, text):
            yield result

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

        if route.kind is RouteKind.CHARACTER_STATS and not route.query.strip():
            stats = await self._get_character_statistics(
                None,
                time_range=route.stats_range,
                potential=route.stats_potential,
            )
            image_path = await renderer.render_character_stats(
                stats,
                query="角色",
                web_base_url=self.web_base_url,
            )
            return _DispatchOutcome(image_path=image_path)

        view = _route_view(route)
        pending = _pending_from_route(route)

        try:
            cards = await self._list_hot_bosses()
        except ZmdLogsClientError:
            if self._looks_like_direct_slug(route.query):
                return await self._render_board(
                    route.query, query=route.query, pending=pending
                )
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

        not_found_message = (
            f"没有找到与「{route.query}」匹配的榜单。"
            if view in _BOARD_ONLY_VIEWS
            else f"没有找到与「{route.query}」匹配的榜单或副本。"
        )
        if match.status is MatchStatus.NOT_FOUND:
            if self._looks_like_direct_slug(route.query):
                return await self._render_board(
                    route.query, query=route.query, pending=pending
                )
            hint = await self._character_name_hint(route.query, view)
            return _DispatchOutcome(message=hint or not_found_message)

        candidates: tuple[MatchChoice, ...] = ()
        choice: MatchChoice | None = None
        if match.status is MatchStatus.AMBIGUOUS:
            candidates = match.candidates
        else:
            choice = match.selected
        if view in _BOARD_ONLY_VIEWS:
            # Character statistics and roster pages exist per board only, so a
            # dungeon / scope hit becomes a pick list of its boards.
            if choice is not None and choice.target.target_type is not TargetType.BOARD:
                candidates = (choice,)
                choice = None
            if candidates:
                candidates = matcher.expand_to_boards(candidates)
                if len(candidates) == 1:
                    choice = candidates[0]
                    candidates = ()
        if candidates:
            entry = self.candidates.remember(
                route.query,
                candidates,
                ranking_top=pending.ranking_top,
                view=pending.view,
                character_filter=pending.character_filter,
                stats_range=pending.stats_range,
                stats_potential=pending.stats_potential,
            )
            return _DispatchOutcome(
                message=format_candidates(
                    entry,
                    ttl_seconds=self.candidates.ttl_seconds,
                )
            )
        if choice is None:
            return _DispatchOutcome(message=not_found_message)
        return await self._render_choice(
            choice,
            cards,
            query=route.query,
            pending=pending,
        )

    async def _render_choice(
        self,
        choice: MatchChoice,
        cards: tuple[HotBossCard, ...],
        *,
        query: str,
        pending: PendingCandidates,
    ) -> _DispatchOutcome:
        renderer = self._require_renderer()
        if choice.target.target_type is TargetType.BOARD:
            return await self._render_board(
                choice.target.key, query=query, pending=pending
            )

        if pending.ranking_top is not None:
            return _DispatchOutcome(
                message="--top 仅适用于具体榜单查询，请补充具体榜单关键词。"
            )
        if pending.character_filter is not None:
            return _DispatchOutcome(
                message="--角色 仅适用于具体榜单查询，请补充具体榜单关键词。"
            )

        selected_slugs = set(choice.target.boss_slugs)
        selected_cards = tuple(
            card for card in cards if card.boss_slug in selected_slugs
        )
        image_path = await renderer.render_dungeon_top3(
            choice,
            selected_cards,
            query=query,
            web_base_url=self.web_base_url,
        )
        return _DispatchOutcome(image_path=image_path)

    async def _character_name_hint(
        self,
        query: str,
        view: CandidateView,
    ) -> str | None:
        """Explain the right command when a board query is really a character.

        ``/zmdlog 角色 庄方宜`` is a natural misreading of the board-only
        ``角色`` route. The global statistics response doubles as a six-star
        character catalog, so use it (cached) to recognise the name.
        """

        try:
            stats = await self._get_character_statistics(
                None, time_range="all", potential="all"
            )
        except ZmdLogsClientError:
            return None
        names = tuple(row.character_name for row in stats.rows)
        resolution = resolve_character_name(query, names)
        if resolution.status is not CharacterResolutionStatus.MATCHED:
            return None
        name = resolution.name
        if view is CandidateView.CHARACTER_STATS:
            return (
                f"「{name}」是角色名。`角色` 后面接榜单关键词，"
                f"例如 `角色 罗丹` 看该榜的角色分布；"
                f"要看 {name} 的排名请用 `罗丹 --角色 {name}`。"
            )
        if view is CandidateView.ROSTER:
            return (
                f"「{name}」是角色名。`阵容` 后面接榜单关键词，"
                f"例如 `阵容 罗丹`。"
            )
        return (
            f"「{name}」是角色名，不是榜单。"
            f"要看 {name} 的排名请在榜单后加 `--角色 {name}`，"
            f"例如 `罗丹 --角色 {name}`。"
        )

    async def _render_board(
        self,
        boss_slug: str,
        *,
        query: str,
        pending: PendingCandidates,
    ) -> _DispatchOutcome:
        """Render one concrete board in whichever view the request asked for."""

        renderer = self._require_renderer()
        ranking_limit = (
            pending.ranking_top
            if pending.ranking_top is not None
            else DEFAULT_RANKING_TOP
        )
        if pending.view is CandidateView.CHARACTER_STATS:
            stats = await self._get_character_statistics(
                boss_slug,
                time_range=pending.stats_range,
                potential=pending.stats_potential,
            )
            image_path = await renderer.render_character_stats(
                stats,
                query=query,
                web_base_url=self.web_base_url,
            )
            return _DispatchOutcome(image_path=image_path)

        ranking = await self._get_boss_ranking(boss_slug)
        if pending.view is CandidateView.ROSTER:
            image_path = await renderer.render_roster(
                ranking,
                query=query,
                ranking_limit=ranking_limit,
                web_base_url=self.web_base_url,
            )
            return _DispatchOutcome(image_path=image_path)

        character_filter = None
        if pending.character_filter is not None:
            resolution = resolve_character_name(
                pending.character_filter,
                ranking_character_names(ranking),
            )
            if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
                options = " / ".join(resolution.candidates)
                return _DispatchOutcome(
                    message=f"「{resolution.query}」可能是：{options}，请写全名。"
                )
            if resolution.status is CharacterResolutionStatus.NOT_FOUND:
                return _DispatchOutcome(
                    message=(
                        f"「{ranking.boss_name}」的公开排名里没有"
                        f"「{resolution.query}」。"
                    )
                )
            character_filter = resolution.name
            if not any(
                row.character_name == character_filter for row in ranking.rows
            ):
                return _DispatchOutcome(
                    message=(
                        f"「{ranking.boss_name}」的公开排名里没有以"
                        f"「{character_filter}」为主C的记录。"
                    )
                )
        image_path = await renderer.render_ranking(
            ranking,
            query=query,
            ranking_limit=ranking_limit,
            web_base_url=self.web_base_url,
            character_filter=character_filter,
        )
        return _DispatchOutcome(image_path=image_path)

    async def _reply_with_candidate(
        self,
        event: AstrMessageEvent,
        code: str,
        selection: str,
    ):
        resolved = self.candidates.resolve(code, selection)
        if resolved is None:
            yield event.plain_result("这份候选列表已过期或序号无效，请重新查询。")
            return
        entry, choice = resolved
        try:
            cards = await self._list_hot_bosses()
            outcome = await self._render_choice(
                choice,
                cards,
                query=entry.query,
                pending=entry,
            )
        except ZmdLogsAPIError as exc:
            logger.warning("ZmdLogBot API request failed: %s", exc.code)
            yield event.plain_result(self._board_api_error_message(exc))
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
            logger.exception("ZmdLogBot unexpected candidate failure")
            yield event.plain_result(_UNEXPECTED_FAILURE_MESSAGE)
            return
        if outcome.image_path is not None:
            yield event.image_result(outcome.image_path)
            return
        yield event.plain_result(outcome.message or "本次查询未产生结果。")

    @staticmethod
    def _quoted_candidate_code(event: AstrMessageEvent) -> str | None:
        """Return the ``#QXXXX`` marker of a quoted candidate list, if any."""

        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", None) or ()
        for component in chain:
            if type(component).__name__ != "Reply":
                continue
            for attribute in ("message_str", "text"):
                code = extract_code(getattr(component, attribute, None))
                if code is not None:
                    return code
            quoted_chain = getattr(component, "chain", None) or ()
            for quoted in quoted_chain:
                code = extract_code(getattr(quoted, "text", None))
                if code is not None:
                    return code
        return None

    async def _handle_alias_route(
        self,
        route: RouteRequest,
        event: AstrMessageEvent,
    ) -> str:
        if route.kind is RouteKind.ALIAS_LIST:
            names: dict[str, str] = {}
            try:
                cards = await self._list_hot_bosses()
            except ZmdLogsClientError:
                cards = ()
            for card in cards:
                names[card.boss_slug] = card.boss_name
            return self._format_alias_list(names)
        if not self._event_is_admin(event):
            return "只有机器人管理员可以修改别名。"
        if route.kind is RouteKind.ALIAS_REMOVE:
            updated, removed = self.aliases.without_alias(route.query)
            if removed == 0:
                return f"没有找到自定义别名「{route.query}」。"
            if not self._save_aliases(updated):
                return "别名文件写入失败，请检查数据目录权限。"
            return f"已删除别名「{route.query}」。"

        target_text, _, alias_text = route.query.partition(" ")
        aliases = tuple(dict.fromkeys(alias_text.split()))
        try:
            cards = await self._list_hot_bosses()
        except ZmdLogsClientError:
            return "ZMDLogs 暂时不可用，无法核对目标，请稍后重试。"
        matcher = RankingMatcher(
            cards,
            self.aliases,
            fuzzy_threshold=self.fuzzy_match_threshold,
            ambiguity_score_gap=self.ambiguity_score_gap,
        )
        match = matcher.match(
            target_text,
            allowed_types=frozenset({TargetType.BOARD, TargetType.DUNGEON}),
        )
        choice = match.selected
        if match.status is not MatchStatus.MATCHED or choice is None:
            return f"没有唯一匹配到「{target_text}」，请用更完整的榜单或副本名。"
        exact_types = frozenset({TargetType.BOARD, TargetType.DUNGEON})
        taken = set()
        for alias in aliases:
            probe = matcher.match(alias, allowed_types=exact_types)
            selected = probe.selected
            if (
                probe.status is MatchStatus.MATCHED
                and selected is not None
                and selected.level <= MatchLevel.NORMALIZED_EXACT
                and selected.target.key != choice.target.key
            ):
                taken.add(alias)
        if taken:
            return "以下别名已被其它榜单或副本占用：" + "、".join(sorted(taken))
        updated = self.aliases.with_aliases(
            choice.target.target_type,
            choice.target.key,
            aliases,
        )
        if not self._save_aliases(updated):
            return "别名文件写入失败，请检查数据目录权限。"
        label = "榜单" if choice.target.target_type is TargetType.BOARD else "副本"
        return (
            f"已为{label}「{choice.target.name}」添加别名："
            + "、".join(aliases)
        )

    def _format_alias_list(self, board_names: dict[str, str]) -> str:
        lines: list[str] = []
        for target_type, key, values in self.aliases.iter_entries():
            if target_type is TargetType.BOARD:
                label, shown = "榜单", board_names.get(key, key)
            else:
                label, shown = "副本", key
            lines.append(f"{label} {shown}：{'、'.join(values)}")
        if not lines:
            return (
                "当前没有自定义别名。"
                "榜单名去掉难度/副本前缀、拼音首字母都已内置支持。"
            )
        return "自定义别名：\n" + "\n".join(lines)

    def _save_aliases(self, updated: AliasConfig) -> bool:
        if self.alias_path is None or not save_json(
            self.alias_path,
            updated.to_payload(),
        ):
            return False
        self.aliases = updated
        return True

    @staticmethod
    def _event_is_admin(event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_admin", None)
        try:
            return bool(checker()) if callable(checker) else False
        except Exception:
            return False

    @staticmethod
    def _plugin_data_dir() -> Path | None:
        if StarTools is None:
            return None
        try:
            return Path(StarTools.get_data_dir(_PLUGIN_DATA_NAME))
        except Exception as exc:
            logger.warning(
                "ZmdLogBot cannot resolve the plugin data dir: %s",
                type(exc).__name__,
            )
            return None

    def _resolve_alias_path(self) -> Path | None:
        """Editable alias file lives in the data dir; plugin ships defaults."""

        configured = Path(self.config.get("alias_file_path", "aliases.json"))
        if configured.is_absolute():
            return configured
        bundled = Path(__file__).parent / configured
        if self.data_dir is None:
            return bundled
        target = self.data_dir / configured
        if not target.exists():
            payload = load_json(bundled) if bundled.is_file() else None
            if payload is None:
                payload = AliasConfig.empty().to_payload()
            if not save_json(target, payload):
                logger.warning("ZmdLogBot cannot seed the alias file in data dir")
                return bundled
        return target

    def _load_aliases(self) -> AliasConfig:
        if self.alias_path is None:
            return AliasConfig.empty()
        try:
            return AliasConfig.load(self.alias_path)
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

    def _render_output_dir(self) -> Path | None:
        """Keep generated images under AstrBot's data dir, never the plugin dir."""

        return None if self.data_dir is None else self.data_dir / "render"

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
            self._fetch_hot_bosses,
            allow_stale_on_error=True,
        )
        if result.state is CacheState.STALE:
            logger.warning(
                "ZmdLogBot is using stale hot-bosses data after refresh failure."
            )
        return result.value

    async def _fetch_hot_bosses(self) -> tuple[HotBossCard, ...]:
        """Fetch the board index; fall back to the on-disk snapshot if needed."""

        snapshot_path = (
            None if self.data_dir is None else self.data_dir / _HOT_BOSSES_SNAPSHOT
        )
        try:
            cards, payload = await self.client.list_hot_bosses_with_payload()
        except ZmdLogsClientError as upstream_error:
            payload = None if snapshot_path is None else load_json(snapshot_path)
            if payload is None:
                raise
            try:
                cards = parse_hot_bosses(payload)
            except Exception:
                # A corrupt snapshot must not mask the real upstream failure.
                raise upstream_error from None
            logger.warning(
                "ZmdLogBot is serving the board index from the local snapshot."
            )
            return cards
        if snapshot_path is not None:
            save_json(snapshot_path, payload)
        return cards

    async def _get_boss_ranking(self, boss_slug: str) -> BossRanking:
        result = await self.boss_ranking_cache.get_or_load(
            boss_slug,
            lambda: self.client.get_boss_rankings(boss_slug),
        )
        return result.value

    async def _get_character_statistics(
        self,
        boss_slug: str | None,
        *,
        time_range: str,
        potential: str,
    ) -> CharacterStatistics:
        key = (boss_slug or "all", time_range, potential)
        result = await self.character_stats_cache.get_or_load(
            key,
            lambda: self.client.get_character_statistics(
                boss_slug,
                time_range=time_range,
                potential=potential,
            ),
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

    @classmethod
    def _api_error_message(
        cls,
        route: RouteRequest,
        error: ZmdLogsAPIError,
    ) -> str:
        if error.status_code == 404:
            if route.kind is RouteKind.ACCOUNT_QUERY:
                return "没有找到这个公开账号，或该账号暂无公开榜单记录。"
            if route.kind is RouteKind.BATTLE_QUERY:
                return "战报不存在、未公开或已删除。"
            if route.kind in {
                RouteKind.RANKING_QUERY,
                RouteKind.SMART_QUERY,
                RouteKind.CHARACTER_STATS,
                RouteKind.ROSTER_QUERY,
            }:
                return cls._board_api_error_message(error)
        return "ZMDLogs 暂时不可用，请稍后重试。"

    @staticmethod
    def _board_api_error_message(error: ZmdLogsAPIError) -> str:
        if error.status_code == 404:
            if error.code == _CHARACTER_STATS_UNAVAILABLE:
                return "危机合约不提供角色统计。"
            return "没有找到这个榜单，可能已下线或暂未公开。"
        return "ZMDLogs 暂时不可用，请稍后重试。"

    @staticmethod
    def _looks_like_direct_slug(query: str) -> bool:
        return is_valid_boss_slug(query) and ("_" in query or "-" in query)

    async def terminate(self) -> None:
        """Release HTTP, browser, and generated-image resources."""

        await self.hot_boss_cache.close()
        await self.boss_ranking_cache.close()
        await self.account_cache.close()
        await self.battle_cache.close()
        await self.character_stats_cache.close()
        try:
            await self.client.close()
        finally:
            if self.renderer is not None:
                await self.renderer.close()
        logger.info("ZmdLogBot plugin terminated.")


def _route_view(route: RouteRequest) -> CandidateView:
    if route.kind is RouteKind.CHARACTER_STATS:
        return CandidateView.CHARACTER_STATS
    if route.kind is RouteKind.ROSTER_QUERY:
        return CandidateView.ROSTER
    return CandidateView.RANKING


def _pending_from_route(route: RouteRequest) -> PendingCandidates:
    """Carry the route's view and options in the same shape candidates use."""

    return PendingCandidates(
        code="",
        query=route.query,
        choices=(),
        ranking_top=route.ranking_top,
        created_at=0.0,
        view=_route_view(route),
        character_filter=route.character_filter,
        stats_range=route.stats_range,
        stats_potential=route.stats_potential,
    )
