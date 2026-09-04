"""AstrBot entry point for ZmdLogBot."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:  # StarTools.get_data_dir is missing on older AstrBot releases.
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover - depends on host AstrBot version
    StarTools = None

try:  # Plain lives under different api surfaces across AstrBot releases.
    from astrbot.api.message_components import Plain
except ImportError:  # pragma: no cover - depends on host AstrBot version
    try:
        from astrbot.core.message.components import Plain
    except ImportError:
        # Only rank notices need it; every query must keep working without it.
        Plain = None

from .core.cache import AsyncTTLCache, CacheState
from .core.candidates import (
    MAX_CANDIDATES,
    CandidateStore,
    CandidateView,
    PendingCandidates,
    extract_code,
    format_candidates,
    parse_selection,
)
from .core.characters import (
    CharacterFilterScope,
    CharacterResolutionStatus,
    pick_character_filter_scope,
    ranking_character_names,
    resolve_character_name,
)
from .core.client import (
    MIN_ACCOUNT_SEARCH_LENGTH,
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsClientError,
    is_valid_boss_slug,
)
from .core.history import (
    AccountHistory,
    history_payload,
    parse_history_payload,
    record_rankings,
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
    MatcherCache,
    MatchLevel,
    MatchStatus,
    MatchTarget,
    TargetType,
    fold_text,
)
from .core.models import (
    BattleDetailSummary,
    BattleExport,
    BossRanking,
    CharacterBossStatistics,
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
from .core.settings import load_settings
from .core.timestamps import utc_now_text
from .core.watch import (
    MAX_DROPS_PER_NOTICE,
    AccountSnapshot,
    BoardSnapshot,
    RankDrop,
    board_snapshot_is_usable,
    board_snapshot_payload,
    build_board_snapshot,
    build_snapshot,
    find_new_record_above,
    find_rank_drops,
    find_top_run_changes,
    format_board_notice,
    format_rank_drop_notice,
    join_board_notices,
    join_rank_drop_notices,
    parse_board_snapshot_payload,
    parse_snapshot_payload,
    snapshot_is_usable,
    snapshot_payload,
)
from .core.watchlist import (
    WatchedAccount,
    WatchedBoard,
    WatchList,
    board_label,
    format_watchlist,
    parse_watchlist,
)

_BOARD_QUERY_TARGETS = frozenset(
    {TargetType.BOARD, TargetType.DUNGEON, TargetType.DUNGEON_SCOPE}
)
_ALIAS_ROUTES = frozenset(
    {RouteKind.ALIAS_LIST, RouteKind.ALIAS_ADD, RouteKind.ALIAS_REMOVE}
)
_WATCH_ROUTES = frozenset(
    {
        RouteKind.WATCH_LIST,
        RouteKind.WATCH_ADD,
        RouteKind.WATCH_REMOVE,
        RouteKind.WATCH_BOARD_ADD,
        RouteKind.WATCH_BOARD_REMOVE,
    }
)
_WATCHLIST_FILE = "watchlist.json"
_RANK_SNAPSHOT_FILE = "rank-snapshot.json"
_BOARD_SNAPSHOT_FILE = "board-snapshot.json"
_RANK_HISTORY_FILE = "rank-history.json"
_TREND_NO_DATA_MESSAGE = (
    "这个账号不在任何关注列表里，还没有名次记录；"
    "用 关注 <昵称或accountId> 关注后会从下一轮检查开始记录。"
)
_RANK_WATCH_CONCURRENCY = 2
_NOTICE_SEND_TIMEOUT_SECONDS = 30.0
_WATCH_DISABLED_MESSAGE = "本机器人未开启名次通报功能。"
_NO_ORIGIN_MESSAGE = "无法确定当前会话，关注功能在这里不可用。"
_PLUGIN_DATA_NAME = "astrbot_plugin_zmdlog"
_HOT_BOSSES_SNAPSHOT = "hot-bosses.json"
_RENDER_FAILURE_MESSAGE = "图片生成失败，请稍后重试。"
_UNEXPECTED_FAILURE_MESSAGE = "ZmdLogBot 暂时无法完成查询，请稍后重试。"
_UPSTREAM_UNAVAILABLE_MESSAGE = "ZMDLogs 暂时不可用，请稍后重试。"
_ACCOUNT_NOT_FOUND_MESSAGE = "没有找到这个公开账号，或该账号暂无公开榜单记录。"
# User text echoed back into a reply is cut here; the matcher already ignores
# anything longer, and a reply must never repeat a multi-kilobyte message.
_ECHO_LIMIT = 40
_BATTLE_LINK_FILTER = (
    r"https?://[^\s<>\"']+/(?:battle|share|axis)/btl_[A-Za-z0-9_-]+"
)
# 战报 / 配装 / 技能 share one argument shape and one lookup; only the page
# drawn from the battle differs.
_BATTLE_STYLE_ROUTES = frozenset(
    {
        RouteKind.BATTLE_QUERY,
        RouteKind.LOADOUT_QUERY,
        RouteKind.SKILL_QUERY,
        RouteKind.TIMELINE_QUERY,
    }
)
_BATTLE_VIEWS = frozenset(
    {
        CandidateView.BATTLE,
        CandidateView.LOADOUT,
        CandidateView.SKILLS,
        CandidateView.TIMELINE,
    }
)
_BOARD_ONLY_VIEWS = (
    frozenset(
        {CandidateView.CHARACTER_STATS, CandidateView.ROSTER, CandidateView.COMPARE}
    )
    | _BATTLE_VIEWS
)
_COMPARE_REFERENCE_MESSAGE = (
    "对比两场战报时，两个参数都要是 battleId 或战报链接。"
)
_COMPARE_SAME_MESSAGE = "两边是同一场战报，没有可比的。"
# Every boss has its own rotation, so a comparison across bosses says nothing;
# the user asked for it to be refused rather than drawn.
_COMPARE_CROSS_MESSAGE = (
    "两场不是同一个首领（{first} / {second}），每个首领的排轴都不同，不做跨榜单对比。"
)
_CHARACTER_STATS_UNAVAILABLE = "character_statistics_not_available"
_NO_LOADOUT_MESSAGE = "这份战报没有记录阵容配装。"
_NO_SKILL_STATS_MESSAGE = "这份战报没有技能统计数据。"
_NO_TIMELINE_MESSAGE = "这条战斗由旧版客户端上传，没有完整施法序列，画不了技能轴。"
_TIMELINE_RATE_LIMITED_MESSAGE = "技能轴接口请求过于频繁，请稍后再试。"
_EXPORT_UNSUPPORTED = "battle_export_unsupported"


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
        self.settings = load_settings(config, warn=logger.warning)
        settings = self.settings
        self.web_base_url = settings.web_base_url
        self.client = ZmdLogsClient(
            api_base_url=settings.api_base_url,
            request_timeout_ms=settings.request_timeout_ms,
        )
        self.data_dir = self._plugin_data_dir()
        self.alias_path = self._resolve_alias_path()
        self.aliases = self._load_aliases()
        self.candidates = CandidateStore()
        self._matchers = MatcherCache(
            fuzzy_threshold=settings.fuzzy_match_threshold,
            ambiguity_score_gap=settings.ambiguity_score_gap,
            warn=lambda issue: logger.warning("ZmdLogBot alias index: %s", issue),
        )
        self.watchlist_path = (
            None if self.data_dir is None else self.data_dir / _WATCHLIST_FILE
        )
        self.watchlist = (
            WatchList.empty()
            if self.watchlist_path is None
            else parse_watchlist(load_json(self.watchlist_path))
        )
        self.rank_snapshot_path = (
            None
            if self.data_dir is None
            else self.data_dir / _RANK_SNAPSHOT_FILE
        )
        self.rank_snapshots = (
            {}
            if self.rank_snapshot_path is None
            else parse_snapshot_payload(load_json(self.rank_snapshot_path))
        )
        self.board_snapshot_path = (
            None
            if self.data_dir is None
            else self.data_dir / _BOARD_SNAPSHOT_FILE
        )
        self.board_snapshots = (
            {}
            if self.board_snapshot_path is None
            else parse_board_snapshot_payload(load_json(self.board_snapshot_path))
        )
        self._board_snapshot_write_failed = False
        self.rank_history_path = (
            None if self.data_dir is None else self.data_dir / _RANK_HISTORY_FILE
        )
        self.rank_history: dict[str, AccountHistory] = (
            {}
            if self.rank_history_path is None
            else parse_history_payload(load_json(self.rank_history_path))
        )
        self._rank_history_write_failed = False
        self._rank_watch_task: asyncio.Task | None = None
        self._rank_snapshot_write_failed = False
        self.hot_boss_cache = AsyncTTLCache[
            str,
            tuple[HotBossCard, ...],
        ](
            settings.ranking_cache_ttl_seconds,
            stale_ttl_seconds=settings.ranking_cache_ttl_seconds,
        )
        self.boss_ranking_cache = AsyncTTLCache[str, BossRanking](
            settings.ranking_cache_ttl_seconds,
        )
        self.account_cache = AsyncTTLCache[str, PublicUserRankings](
            settings.account_cache_ttl_seconds,
        )
        self.character_stats_cache = AsyncTTLCache[
            tuple[str, str, str],
            CharacterStatistics,
        ](
            settings.character_stats_cache_ttl_seconds,
        )
        self.character_boss_cache = AsyncTTLCache[
            tuple[str, str, str],
            CharacterBossStatistics,
        ](
            settings.character_stats_cache_ttl_seconds,
        )
        self.battle_cache = AsyncTTLCache[str, BattleDetailSummary](
            settings.battle_cache_ttl_seconds,
        )
        self.battle_export_cache = AsyncTTLCache[str, BattleExport](
            settings.battle_cache_ttl_seconds,
        )
        self._auto_expand_lock = asyncio.Lock()
        self._auto_expanded_until: dict[tuple[str, str], float] = {}
        try:
            self.renderer: LongImageRenderer | None = LongImageRenderer(
                Path(__file__).parent,
                render_timeout_ms=settings.render_timeout_ms,
                output_dir=self._render_output_dir(),
                allowed_image_origins=(
                    settings.api_base_url,
                    settings.web_base_url,
                ),
            )
        except TemplateConfigurationError as exc:
            logger.error(
                "ZmdLogBot renderer configuration failed: %s",
                type(exc).__name__,
            )
            self.renderer = None

    async def initialize(self) -> None:
        """Start the rank watcher on every plugin load.

        ``on_astrbot_loaded`` fires once per process, so a plugin installed from
        the market or reloaded after a config change would never poll if that
        were the only start path. ``_start_rank_watch`` is idempotent, and the
        loop sleeps a full interval before its first cycle, so starting here
        cannot race platform startup.
        """

        self._start_rank_watch()

    @filter.on_astrbot_loaded()
    async def on_astrbot_ready(self) -> None:
        """Start Chromium and, on a cold boot, the rank watcher."""

        self._start_rank_watch()
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
        except ValueError:
            # Defence in depth: a parse-layer ValueError that is not a
            # RouteParseError must still answer briefly, never as a traceback.
            yield event.plain_result("指令参数无法解析，请检查后重试。")
            return
        if route.kind in _ALIAS_ROUTES or route.kind in _WATCH_ROUTES:
            # These reply in text and never reach _dispatch, so they need their
            # own guard: an unexpected error must not surface as a traceback.
            try:
                if route.kind in _ALIAS_ROUTES:
                    message = await self._handle_alias_route(route, event)
                else:
                    message = await self._handle_watch_route(route, event)
            except ZmdLogsClientError as exc:
                logger.warning(
                    "ZmdLogBot request failed: %s", type(exc).__name__
                )
                message = _UPSTREAM_UNAVAILABLE_MESSAGE
            except Exception:
                logger.exception("ZmdLogBot unexpected command failure")
                message = _UNEXPECTED_FAILURE_MESSAGE
            yield event.plain_result(message)
            return
        outcome, _ = await self._run_guarded(
            lambda: self._dispatch(
                route,
                command_prefix=self._command_prefix(event),
                origin=self._event_origin(event),
            ),
            api_error_message=lambda exc: self._api_error_message(route, exc),
            failure_label="command",
        )
        yield self._outcome_result(event, outcome)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def pick_candidate(self, event: AstrMessageEvent):
        """Let a bare ``2`` that quotes a candidate list select that entry."""

        # This handler sees every message in every chat, so the regex runs
        # first and the prefix lookup only for a message that is a bare number.
        text = event.get_message_str() or ""
        if parse_selection(text) is None or self._is_command_message(event):
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

        if not self.settings.auto_expand_battle_links or self._is_command_message(
            event
        ):
            return
        battle_ids = extract_battle_references(
            event.get_message_str(),
            web_base_url=self.web_base_url,
            limit=1,
        )
        if not battle_ids:
            return
        battle_id = battle_ids[0]
        origin = self._event_origin(event) or "unknown"
        if not await self._claim_auto_expand(origin, battle_id):
            return

        async def expand() -> _DispatchOutcome:
            battle = await self._get_battle_detail(battle_id)
            export, note = await self._battle_export_for_card(battle_id)
            renderer = self._require_renderer()
            image_path = await renderer.render_battle(
                battle,
                query=battle_id,
                web_base_url=self.web_base_url,
                export=export,
                export_note=note,
            )
            return _DispatchOutcome(image_path=image_path)

        outcome, transient = await self._run_guarded(
            expand,
            api_error_message=_battle_link_error_message,
            failure_label="auto-expand",
        )
        # The cooldown exists to stop the same link being answered again and
        # again, so it is released only when nothing useful was sent AND a
        # repost could plausibly do better. A dead link (404) or an image
        # delivered through the fallback renderer keeps the claim.
        if transient:
            await self._release_auto_expand(origin, battle_id)
        yield self._outcome_result(event, outcome)

    async def _run_guarded(
        self,
        action: Callable[[], Awaitable[_DispatchOutcome]],
        *,
        api_error_message: Callable[[ZmdLogsAPIError], str],
        failure_label: str,
    ) -> tuple[_DispatchOutcome, bool]:
        """Run one query action and turn every failure into a short reply.

        This is the single error ladder behind the command, the quoted
        candidate pick and the auto-expand handler, so the three can never
        drift apart again. The flag says whether the failure was transient,
        i.e. a retry could plausibly succeed; only the auto-expand cooldown
        reads it.
        """

        try:
            return await action(), False
        except ZmdLogsAPIError as exc:
            logger.warning(
                "ZmdLogBot %s API request failed: %s", failure_label, exc.code
            )
            return (
                _DispatchOutcome(message=api_error_message(exc)),
                exc.status_code != 404,
            )
        except ZmdLogsClientError as exc:
            logger.warning(
                "ZmdLogBot %s request failed: %s",
                failure_label,
                type(exc).__name__,
            )
            return _DispatchOutcome(message=_UPSTREAM_UNAVAILABLE_MESSAGE), True
        except RenderError as exc:
            logger.error(
                "ZmdLogBot %s rendering failed: %s",
                failure_label,
                type(exc).__name__,
            )
            fallback_path = await self._render_with_astrbot(exc)
            if fallback_path is not None:
                return _DispatchOutcome(image_path=fallback_path), False
            return _DispatchOutcome(message=_RENDER_FAILURE_MESSAGE), True
        except Exception:
            logger.exception("ZmdLogBot unexpected %s failure", failure_label)
            return _DispatchOutcome(message=_UNEXPECTED_FAILURE_MESSAGE), False

    @staticmethod
    def _outcome_result(event: AstrMessageEvent, outcome: _DispatchOutcome):
        if outcome.image_path is not None:
            return event.image_result(outcome.image_path)
        return event.plain_result(outcome.message or "本次查询未产生结果。")

    async def _dispatch(
        self,
        route: RouteRequest,
        *,
        command_prefix: str,
        origin: str = "",
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
            except PublicReferenceError:
                # Not an ID or trusted URL: treat the text as a nickname.
                outcome = await self._account_search_outcome(
                    route.query, origin=origin
                )
                if outcome is None:
                    return _DispatchOutcome(
                        message=(
                            "请提供公开昵称（至少 2 个字符）、accountId "
                            "或 ZMDLogs 账号主页链接。"
                        )
                    )
                return outcome
            return await self._render_account_outcome(
                account_id, query=route.query
            )

        if route.kind is RouteKind.COMPARE_QUERY and route.compare_target:
            # Two explicit references: no board lookup at all.
            try:
                first = parse_battle_reference(
                    route.query, web_base_url=self.web_base_url
                )
                second = parse_battle_reference(
                    route.compare_target, web_base_url=self.web_base_url
                )
            except PublicReferenceError:
                return _DispatchOutcome(message=_COMPARE_REFERENCE_MESSAGE)
            return await self._render_compare(
                first, second, query=f"{route.query} vs {route.compare_target}"
            )

        if route.kind in _BATTLE_STYLE_ROUTES:
            try:
                battle_id = parse_battle_reference(
                    route.query,
                    web_base_url=self.web_base_url,
                )
            except PublicReferenceError:
                # Not an exact reference: fall through and treat the text as
                # a board keyword whose rank-N battle should be shown.
                battle_id = None
            if battle_id is not None:
                return await self._render_battle_view(
                    battle_id, _route_view(route), query=route.query
                )

        if route.kind is RouteKind.TREND_QUERY:
            try:
                account_id = parse_account_reference(
                    route.query,
                    web_base_url=self.web_base_url,
                )
            except PublicReferenceError:
                account_id = None
            if account_id is not None:
                return await self._render_trend_outcome(
                    account_id, query=route.query, time_range=route.stats_range
                )
            # A watched account is addressable by the nickname its history
            # already holds, with no upstream search at all.
            local = self._find_history_by_name(route.query)
            if len(local) == 1:
                return await self._render_trend_outcome(
                    local[0].account_id,
                    query=route.query,
                    time_range=route.stats_range,
                )
            if len(local) > 1:
                entry = self.candidates.remember(
                    route.query,
                    tuple(
                        _account_choice_from(
                            item.account_id, item.display_name, route.query
                        )
                        for item in local[:MAX_CANDIDATES]
                    ),
                    origin=origin,
                    view=CandidateView.TREND,
                    stats_range=route.stats_range,
                )
                return _DispatchOutcome(
                    message=format_candidates(
                        entry, ttl_seconds=self.candidates.ttl_seconds
                    )
                )
            outcome = await self._account_search_outcome(
                route.query,
                origin=origin,
                view=CandidateView.TREND,
                stats_range=route.stats_range,
            )
            if outcome is None:
                return _DispatchOutcome(
                    message=(
                        "请提供公开昵称（至少 2 个字符）、accountId "
                        "或 ZMDLogs 账号主页链接。"
                    )
                )
            return outcome

        if route.kind is RouteKind.CHARACTER_STATS and not route.query.strip():
            stats = await self._get_character_statistics(
                None,
                time_range=route.stats_range,
                potential=route.stats_potential,
            )
            image_path = await renderer.render_character_stats(
                stats,
                query="角色统计",
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

        matcher = self._matchers.matcher_for(cards, self.aliases)
        # The account version can extend the smart route's set without changing
        # the matcher or the explicit board route.
        match = matcher.match(route.query, allowed_types=_BOARD_QUERY_TARGETS)

        shown_query = _shorten(route.query)
        not_found_message = (
            f"没有找到与「{shown_query}」匹配的榜单。"
            if view in _BOARD_ONLY_VIEWS
            else f"没有找到与「{shown_query}」匹配的榜单或副本。"
        )
        if match.status is MatchStatus.NOT_FOUND:
            if self._looks_like_direct_slug(route.query):
                try:
                    return await self._render_board(
                        route.query, query=route.query, pending=pending
                    )
                except ZmdLogsAPIError as exc:
                    if exc.status_code != 404:
                        raise
                    # Not a slug after all: nicknames such as Re-Zero or
                    # xiao_ming pass the slug shape too, so keep looking.
            if view is CandidateView.CHARACTER_STATS:
                outcome = await self._character_boss_outcome(route.query, pending)
                if outcome is not None:
                    return outcome
            if (
                view is CandidateView.RANKING
                and pending.ranking_top is None
                and pending.character_filter is None
            ):
                outcome = await self._account_search_outcome(
                    route.query, quiet=True, origin=origin
                )
                if outcome is not None:
                    return outcome
            hint = await self._character_name_hint(route.query, view)
            return _DispatchOutcome(message=hint or not_found_message)

        candidates: tuple[MatchChoice, ...] = ()
        choice: MatchChoice | None = None
        if match.status is MatchStatus.AMBIGUOUS:
            candidates = match.candidates
        else:
            choice = match.selected

        # An exact public nickname must beat fuzzy board hits (the smart route
        # searches boards AND accounts). Exact board tiers stay untouched and
        # skip the extra request entirely.
        best_level = (
            choice.level
            if choice is not None
            else min((entry.level for entry in candidates), default=None)
        )
        if (
            view is CandidateView.CHARACTER_STATS
            and best_level is not None
            and best_level > MatchLevel.PINYIN_EXACT
        ):
            # 角色统计 <角色名>: a recognised character beats fuzzy board hits.
            outcome = await self._character_boss_outcome(route.query, pending)
            if outcome is not None:
                return outcome
        if (
            view is CandidateView.RANKING
            and pending.ranking_top is None
            and pending.character_filter is None
            and best_level is not None
            and best_level > MatchLevel.PINYIN_EXACT
        ):
            account_choices = await self._search_account_choices(route.query)
            if account_choices:
                folded_query = fold_text(route.query)
                exact = tuple(
                    entry
                    for entry in account_choices
                    if fold_text(entry.target.name) == folded_query
                )
                if len(exact) == 1:
                    return await self._render_choice(
                        exact[0], cards, query=route.query, pending=pending
                    )
                if len(exact) > 1:
                    choice = None
                    candidates = exact
                elif candidates:
                    # Fuzzy on both sides: one typed pick list.
                    candidates = (candidates + account_choices)[:MAX_CANDIDATES]

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
                origin=origin,
                ranking_top=pending.ranking_top,
                view=pending.view,
                character_filter=pending.character_filter,
                stats_range=pending.stats_range,
                stats_potential=pending.stats_potential,
                battle_rank=pending.battle_rank,
                compare_rank=pending.compare_rank,
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
        if choice.target.target_type is TargetType.ACCOUNT:
            if pending.view is CandidateView.TREND:
                return await self._render_trend_outcome(
                    choice.target.key, query=query, time_range=pending.stats_range
                )
            return await self._render_account_outcome(
                choice.target.key, query=query
            )
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

    async def _account_search_outcome(
        self,
        query: str,
        *,
        quiet: bool = False,
        origin: str = "",
        view: CandidateView = CandidateView.RANKING,
        stats_range: str = "all",
    ) -> _DispatchOutcome | None:
        """Resolve a nickname via upstream search; None means "not applicable".

        ``quiet`` marks the smart-query fallback where an empty result should
        fall through to other hints instead of producing a message. ``view``
        says which page a hit should draw: the account page by default, the
        rank trend for 趋势.
        """

        stripped = query.strip()
        if len(stripped) < MIN_ACCOUNT_SEARCH_LENGTH or len(stripped) > 64:
            return None
        try:
            search = await self.client.search_public_accounts(
                stripped, limit=MAX_CANDIDATES
            )
        except ZmdLogsClientError:
            if quiet:
                return None
            raise
        if not search.accounts:
            if quiet:
                return None
            return _DispatchOutcome(
                message=f"没有找到昵称包含「{_shorten(stripped)}」的公开账号。"
            )
        if len(search.accounts) == 1 and not search.has_more:
            if view is CandidateView.TREND:
                return await self._render_trend_outcome(
                    search.accounts[0].account_id,
                    query=query,
                    time_range=stats_range,
                )
            return await self._render_account_outcome(
                search.accounts[0].account_id, query=query
            )
        choices = tuple(
            _account_choice(hit, stripped) for hit in search.accounts
        )
        entry = self.candidates.remember(
            stripped, choices, origin=origin, view=view, stats_range=stats_range
        )
        return _DispatchOutcome(
            message=format_candidates(
                entry,
                ttl_seconds=self.candidates.ttl_seconds,
                note=(
                    "还有更多同名结果未列出，可输入更完整的昵称。"
                    if search.has_more
                    else None
                ),
            )
        )

    async def _search_account_choices(
        self,
        query: str,
    ) -> tuple[MatchChoice, ...]:
        """Quiet nickname lookup for the smart route; empty on any failure."""

        stripped = query.strip()
        if len(stripped) < MIN_ACCOUNT_SEARCH_LENGTH or len(stripped) > 64:
            return ()
        try:
            search = await self.client.search_public_accounts(
                stripped, limit=MAX_CANDIDATES
            )
        except ZmdLogsClientError:
            return ()
        return tuple(_account_choice(hit, stripped) for hit in search.accounts)

    async def _render_account_outcome(
        self,
        account_id: str,
        *,
        query: str,
    ) -> _DispatchOutcome:
        """Render one public account; a 404 answers in account terms.

        The same lookup is reached from the 账号 route, from a nickname pick
        list and from the smart route, and only the first of those knows from
        its route kind that an account is meant — so the wording is decided
        here rather than by whoever catches the error.
        """

        renderer = self._require_renderer()
        try:
            account = await self._get_public_user_rankings(account_id)
        except ZmdLogsAPIError as exc:
            if exc.status_code != 404:
                raise
            logger.warning("ZmdLogBot API request failed: %s", exc.code)
            return _DispatchOutcome(message=_ACCOUNT_NOT_FOUND_MESSAGE)
        image_path = await renderer.render_account(
            account,
            query=query,
            web_base_url=self.web_base_url,
        )
        return _DispatchOutcome(image_path=image_path)

    async def _render_trend_outcome(
        self,
        account_id: str,
        *,
        query: str,
        time_range: str,
    ) -> _DispatchOutcome:
        """Draw the rank trace of one account; text when nothing was recorded.

        The trace only exists for accounts the rank watch polls, so this never
        talks to upstream: an unknown account is answered with how to start.
        """

        history = self.rank_history.get(account_id)
        if history is None or not history.boards:
            return _DispatchOutcome(message=_TREND_NO_DATA_MESSAGE)
        renderer = self._require_renderer()
        snapshot = self.rank_snapshots.get(account_id)
        image_path = await renderer.render_trend(
            history,
            query=query,
            web_base_url=self.web_base_url,
            time_range=time_range,
            last_checked=snapshot.checked_at if snapshot is not None else None,
        )
        return _DispatchOutcome(image_path=image_path)

    def _find_history_by_name(self, query: str) -> tuple[AccountHistory, ...]:
        """Watched accounts whose recorded nickname matches ``query``."""

        stripped = query.strip()
        if len(stripped) < MIN_ACCOUNT_SEARCH_LENGTH:
            return ()
        folded = fold_text(stripped)
        entries = tuple(self.rank_history.values())
        exact = tuple(
            entry for entry in entries if fold_text(entry.display_name) == folded
        )
        if exact:
            return exact
        return tuple(
            entry
            for entry in entries
            if folded and folded in fold_text(entry.display_name)
        )

    def _save_rank_history(self, history: dict[str, AccountHistory]) -> None:
        self.rank_history = history
        if self.rank_history_path is None:
            return
        if save_json(self.rank_history_path, history_payload(history)):
            self._rank_history_write_failed = False
            return
        if not self._rank_history_write_failed:
            self._rank_history_write_failed = True
            logger.warning(
                "ZmdLogBot could not persist the rank history; "
                "check the plugin data directory."
            )

    async def _character_boss_outcome(
        self,
        query: str,
        pending: PendingCandidates,
    ) -> _DispatchOutcome | None:
        """Render one character's all-boards page when ``query`` names one.

        Returns None when the query is not a recognised character (or the
        catalog is unavailable) so board handling can continue.
        """

        try:
            catalog = await self._get_character_statistics(
                None, time_range="all", potential="all"
            )
        except ZmdLogsClientError:
            return None
        names = tuple(row.character_name for row in catalog.rows)
        resolution = resolve_character_name(query, names)
        if resolution.status is CharacterResolutionStatus.NOT_FOUND:
            return None
        if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
            options = " / ".join(resolution.candidates)
            return _DispatchOutcome(
                message=f"「{resolution.query}」可能是：{options}，请写全名。"
            )
        character_key = next(
            row.character_key
            for row in catalog.rows
            if row.character_name == resolution.name
        )
        stats = await self._get_character_boss_statistics(
            character_key,
            time_range=pending.stats_range,
            potential=pending.stats_potential,
        )
        renderer = self._require_renderer()
        image_path = await renderer.render_character_boss(
            stats,
            query=query,
            web_base_url=self.web_base_url,
        )
        return _DispatchOutcome(image_path=image_path)

    async def _get_character_boss_statistics(
        self,
        character_key: str,
        *,
        time_range: str,
        potential: str,
    ) -> CharacterBossStatistics:
        key = (character_key, time_range, potential)
        result = await self.character_boss_cache.get_or_load(
            key,
            lambda: self.client.get_character_boss_statistics(
                character_key,
                time_range=time_range,
                potential=potential,
            ),
        )
        return result.value

    async def _character_name_hint(
        self,
        query: str,
        view: CandidateView,
    ) -> str | None:
        """Explain the right command when a board query is really a character.

        ``/zmdlog 角色统计 庄方宜`` is a natural misreading of the board-only
        ``角色统计`` route. The global statistics response doubles as a six-star
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
        if view is CandidateView.ROSTER:
            return (
                f"「{name}」是角色名。`阵容` 后面接榜单关键词，"
                f"例如 `阵容 罗丹`。"
            )
        return (
            f"「{name}」是角色名，不是榜单。"
            f"用 `角色统计 {name}` 看其全部榜单的分布，"
            f"或在榜单后加 `--角色 {name}` 只看该榜排名。"
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
        if pending.view is CandidateView.COMPARE:
            wanted = (pending.battle_rank, pending.compare_rank)
            rows = {
                entry.rank: entry for entry in ranking.rows if entry.rank in wanted
            }
            missing = [rank for rank in wanted if rank not in rows]
            if missing:
                return _DispatchOutcome(
                    message=(
                        f"「{ranking.boss_name}」公开排名共 {len(ranking.rows)} 条，"
                        f"没有第 {missing[0]} 名。"
                    )
                )
            return await self._render_compare(
                rows[wanted[0]].battle_id,
                rows[wanted[1]].battle_id,
                query=query,
                rank_a=wanted[0],
                rank_b=wanted[1],
            )
        if pending.view in _BATTLE_VIEWS:
            row = next(
                (
                    entry
                    for entry in ranking.rows
                    if entry.rank == pending.battle_rank
                ),
                None,
            )
            if row is None:
                return _DispatchOutcome(
                    message=(
                        f"「{ranking.boss_name}」公开排名共 {len(ranking.rows)} 条，"
                        f"没有第 {pending.battle_rank} 名。"
                    )
                )
            return await self._render_battle_view(
                row.battle_id, pending.view, query=query
            )
        if pending.view is CandidateView.ROSTER:
            image_path = await renderer.render_roster(
                ranking,
                query=query,
                ranking_limit=ranking_limit,
                web_base_url=self.web_base_url,
            )
            return _DispatchOutcome(image_path=image_path)

        character_filter: tuple[str, ...] | None = None
        character_filter_scope = CharacterFilterScope.MAIN
        if pending.character_filter is not None:
            names: list[str] = []
            for wanted in pending.character_filter.split():
                resolution = resolve_character_name(
                    wanted, ranking_character_names(ranking)
                )
                if resolution.status is CharacterResolutionStatus.AMBIGUOUS:
                    options = " / ".join(resolution.candidates)
                    return _DispatchOutcome(
                        message=(
                            f"「{_shorten(resolution.query)}」可能是：{options}，"
                            "请写全名。"
                        )
                    )
                if resolution.status is CharacterResolutionStatus.NOT_FOUND:
                    return _DispatchOutcome(
                        message=(
                            f"「{ranking.boss_name}」的公开排名里没有"
                            f"「{_shorten(resolution.query)}」。"
                        )
                    )
                if resolution.name not in names:
                    names.append(resolution.name)
            character_filter = tuple(names)
            if len(names) == 1:
                # No main-C records is the normal case for supports, so widen
                # the filter to the whole roster instead of answering
                # "nothing found".
                character_filter_scope = pick_character_filter_scope(
                    ranking, names[0]
                )
                if character_filter_scope is CharacterFilterScope.NONE:
                    return _DispatchOutcome(
                        message=(
                            f"「{ranking.boss_name}」的公开排名里没有带"
                            f"「{names[0]}」的记录。"
                        )
                    )
            else:
                # Several names ask for teams fielding all of them; a team has
                # one main C, so this is a roster question by definition.
                character_filter_scope = CharacterFilterScope.ROSTER
                if not any(
                    all(
                        any(
                            entry.character_name == name
                            for entry in row.roster_entries
                        )
                        for name in names
                    )
                    for row in ranking.rows
                ):
                    return _DispatchOutcome(
                        message=(
                            f"「{ranking.boss_name}」的公开排名里没有同时带上"
                            f"「{'、'.join(names)}」的记录。"
                        )
                    )
        image_path = await renderer.render_ranking(
            ranking,
            query=query,
            ranking_limit=ranking_limit,
            web_base_url=self.web_base_url,
            character_filter=character_filter,
            character_filter_scope=character_filter_scope,
        )
        return _DispatchOutcome(image_path=image_path)

    async def _render_battle_view(
        self,
        battle_id: str,
        view: CandidateView,
        *,
        query: str,
    ) -> _DispatchOutcome:
        """Draw the page a 战报 / 配装 / 技能 / 技能轴 request asked for.

        The timeline reads the public export, the other three the battle
        detail. Older uploads carry no roster loadout, skill statistics or
        cast sequence; those get a short text instead of an empty page.
        """

        renderer = self._require_renderer()
        if view is CandidateView.TIMELINE:
            try:
                export = await self._get_battle_export(battle_id)
            except ZmdLogsAPIError as exc:
                if exc.status_code == 422 and exc.code == _EXPORT_UNSUPPORTED:
                    logger.warning("ZmdLogBot API request failed: %s", exc.code)
                    return _DispatchOutcome(message=_NO_TIMELINE_MESSAGE)
                if exc.status_code == 429:
                    logger.warning("ZmdLogBot API request failed: %s", exc.code)
                    return _DispatchOutcome(message=_TIMELINE_RATE_LIMITED_MESSAGE)
                raise
            # The detail only adds the BUFF 覆盖 band; the page stands without.
            image_path = await renderer.render_timeline(
                export,
                query=query,
                web_base_url=self.web_base_url,
                battle=await self._battle_detail_if_available(battle_id),
            )
            return _DispatchOutcome(image_path=image_path)
        battle = await self._get_battle_detail(battle_id)
        if view is CandidateView.LOADOUT:
            if not battle.roster:
                return _DispatchOutcome(message=_NO_LOADOUT_MESSAGE)
            image_path = await renderer.render_loadout(
                battle, query=query, web_base_url=self.web_base_url
            )
        elif view is CandidateView.SKILLS:
            if not battle.skill_stats:
                return _DispatchOutcome(message=_NO_SKILL_STATS_MESSAGE)
            image_path = await renderer.render_skills(
                battle, query=query, web_base_url=self.web_base_url
            )
        else:
            export, note = await self._battle_export_for_card(battle_id)
            image_path = await renderer.render_battle(
                battle,
                query=query,
                web_base_url=self.web_base_url,
                export=export,
                export_note=note,
            )
        return _DispatchOutcome(image_path=image_path)

    async def _battle_export_for_card(
        self,
        battle_id: str,
    ) -> tuple[BattleExport | None, str | None]:
        """The cast sequence for the battle card, or a one-line reason without.

        Best effort: the card must never fail because the export did. An old
        upload and a rate limit get a note the card can print; anything else
        is logged and the section is simply left out.
        """

        try:
            return await self._get_battle_export(battle_id), None
        except ZmdLogsAPIError as exc:
            logger.warning("ZmdLogBot battle export unavailable: %s", exc.code)
            if exc.status_code == 422 and exc.code == _EXPORT_UNSUPPORTED:
                return None, _NO_TIMELINE_MESSAGE
            if exc.status_code == 429:
                return None, _TIMELINE_RATE_LIMITED_MESSAGE
            return None, None
        except ZmdLogsClientError as exc:
            logger.warning(
                "ZmdLogBot battle export unavailable: %s", type(exc).__name__
            )
            return None, None

    async def _render_compare(
        self,
        battle_id_a: str,
        battle_id_b: str,
        *,
        query: str,
        rank_a: int | None = None,
        rank_b: int | None = None,
    ) -> _DispatchOutcome:
        """Two battles side by side; both details are fetched concurrently."""

        if battle_id_a == battle_id_b:
            return _DispatchOutcome(message=_COMPARE_SAME_MESSAGE)
        renderer = self._require_renderer()
        first, second = await asyncio.gather(
            self._get_battle_detail(battle_id_a),
            self._get_battle_detail(battle_id_b),
        )
        if first.boss_name != second.boss_name:
            return _DispatchOutcome(
                message=_COMPARE_CROSS_MESSAGE.format(
                    first=_shorten(first.boss_name), second=_shorten(second.boss_name)
                )
            )
        image_path = await renderer.render_compare(
            first,
            second,
            query=query,
            web_base_url=self.web_base_url,
            rank_a=rank_a,
            rank_b=rank_b,
        )
        return _DispatchOutcome(image_path=image_path)

    async def _battle_detail_if_available(
        self,
        battle_id: str,
    ) -> BattleDetailSummary | None:
        """The battle detail for a page that can do without it."""

        try:
            return await self._get_battle_detail(battle_id)
        except ZmdLogsClientError as exc:
            logger.warning(
                "ZmdLogBot battle detail unavailable: %s",
                getattr(exc, "code", type(exc).__name__),
            )
            return None

    async def _reply_with_candidate(
        self,
        event: AstrMessageEvent,
        code: str,
        selection: str,
    ):
        resolved = self.candidates.resolve(
            code, selection, origin=self._event_origin(event)
        )
        if resolved is None:
            yield event.plain_result("这份候选列表已过期或序号无效，请重新查询。")
            return
        entry, choice = resolved
        if entry.view is CandidateView.WATCH:
            yield event.plain_result(
                await self._remember_watched_account(
                    event,
                    account_id=choice.target.key,
                    display_name=choice.target.name,
                )
            )
            return
        if entry.view is CandidateView.WATCH_BOARD:
            yield event.plain_result(
                await self._remember_watched_board(event, choice.target.key)
            )
            return

        async def render_pick() -> _DispatchOutcome:
            cards = await self._list_hot_bosses()
            return await self._render_choice(
                choice,
                cards,
                query=entry.query,
                pending=entry,
            )

        outcome, _ = await self._run_guarded(
            render_pick,
            api_error_message=self._board_api_error_message,
            failure_label="candidate",
        )
        yield self._outcome_result(event, outcome)

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
                return f"没有找到自定义别名「{_shorten(route.query)}」。"
            if not self._save_aliases(updated):
                return "别名文件写入失败，请检查数据目录权限。"
            return f"已删除别名「{route.query}」。"

        target_text, _, alias_text = route.query.partition(" ")
        aliases = tuple(dict.fromkeys(alias_text.split()))
        try:
            cards = await self._list_hot_bosses()
        except ZmdLogsClientError:
            return "ZMDLogs 暂时不可用，无法核对目标，请稍后重试。"
        matcher = self._matchers.matcher_for(cards, self.aliases)
        match = matcher.match(
            target_text,
            allowed_types=frozenset({TargetType.BOARD, TargetType.DUNGEON}),
        )
        choice = match.selected
        if match.status is not MatchStatus.MATCHED or choice is None:
            return (
                f"没有唯一匹配到「{_shorten(target_text)}」，"
                "请用更完整的榜单或副本名。"
            )
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

    async def _handle_watch_route(
        self,
        route: RouteRequest,
        event: AstrMessageEvent,
    ) -> str:
        """Maintain the watch list of this chat, in text; nothing is rendered."""

        origin = self._event_origin(event)
        if not origin:
            # Without a real origin every chat would share one list and the
            # notices would have nowhere to go.
            return _NO_ORIGIN_MESSAGE
        command = self._command_prefix(event) + "zmdlog"
        if route.kind is RouteKind.WATCH_LIST:
            # Reviewing and pruning stay available when polling is switched
            # off; only adding is refused.
            return format_watchlist(
                self.watchlist.accounts_for(origin),
                command=command,
                boards=self.watchlist.boards_for(origin),
            )
        if route.kind is RouteKind.WATCH_BOARD_REMOVE:
            boards = self.watchlist.resolve_board_matches(origin, route.query)
            if len(boards) > 1:
                names = "、".join(board.label for board in boards[:5])
                return (
                    f"「{_shorten(route.query)}」匹配到多个关注的榜单：{names}，"
                    "请改用序号。"
                )
            if not boards:
                return (
                    f"关注列表里没有榜单「{_shorten(route.query)}」，"
                    f"发送 {command} 关注 查看当前列表与序号。"
                )
            board = boards[0]
            if not board.removable_by(
                self._event_user_key(event),
                is_admin=self._event_is_admin(event),
            ):
                return "只有添加这条关注的人或机器人管理员可以取消它。"
            if not self._save_watchlist(
                self.watchlist.without_board(origin, board.boss_slug)
            ):
                return "关注列表写入失败，请检查数据目录权限。"
            self._forget_board_snapshot(board.boss_slug)
            return f"已取消关注榜单「{board.label}」。"
        if route.kind is RouteKind.WATCH_REMOVE:
            matches = self.watchlist.resolve_matches(origin, route.query)
            if len(matches) > 1:
                names = "、".join(entry.display_name for entry in matches[:5])
                return (
                    f"「{_shorten(route.query)}」匹配到多个关注：{names}，"
                    "请改用序号。"
                )
            if not matches:
                return (
                    f"关注列表里没有「{_shorten(route.query)}」，"
                    f"发送 {command} 关注 查看当前列表与序号。"
                )
            account = matches[0]
            if not account.removable_by(
                self._event_user_key(event),
                is_admin=self._event_is_admin(event),
            ):
                return "只有添加这条关注的人或机器人管理员可以取消它。"
            if not self._save_watchlist(
                self.watchlist.without_account(origin, account.account_id)
            ):
                return "关注列表写入失败，请检查数据目录权限。"
            self._forget_rank_snapshot(account.account_id)
            return f"已取消关注 {account.display_name}。"
        if not self.settings.rank_watch_enabled:
            return _WATCH_DISABLED_MESSAGE
        if route.kind is RouteKind.WATCH_BOARD_ADD:
            return await self._add_watched_board(route.query, event)
        return await self._add_watched_account(route.query, event)

    async def _add_watched_board(
        self,
        query: str,
        event: AstrMessageEvent,
    ) -> str:
        """Resolve a board keyword the way queries do, then remember it.

        Dungeon and scope hits are flattened to their boards: the watch is on
        one board's top three, so several boards become a pick list.
        """

        try:
            cards = await self._list_hot_bosses()
        except ZmdLogsClientError as exc:
            logger.warning("ZmdLogBot request failed: %s", type(exc).__name__)
            return _UPSTREAM_UNAVAILABLE_MESSAGE
        matcher = self._matchers.matcher_for(cards, self.aliases)
        match = matcher.match(query, allowed_types=_BOARD_QUERY_TARGETS)
        if match.status is MatchStatus.AMBIGUOUS:
            choices = match.candidates
        elif match.status is MatchStatus.MATCHED and match.selected is not None:
            choices = (match.selected,)
        else:
            choices = ()
        choices = matcher.expand_to_boards(choices)
        if not choices:
            return f"没有找到与「{_shorten(query)}」匹配的榜单。"
        if len(choices) == 1:
            return await self._remember_watched_board(event, choices[0].target.key)
        entry = self.candidates.remember(
            query,
            choices,
            view=CandidateView.WATCH_BOARD,
            origin=self._event_origin(event),
        )
        return format_candidates(entry, ttl_seconds=self.candidates.ttl_seconds)

    async def _remember_watched_board(
        self,
        event: AstrMessageEvent,
        boss_slug: str,
    ) -> str:
        origin = self._event_origin(event)
        if not origin:
            return _NO_ORIGIN_MESSAGE
        try:
            cards = await self._list_hot_bosses()
        except ZmdLogsClientError as exc:
            logger.warning("ZmdLogBot request failed: %s", type(exc).__name__)
            return _UPSTREAM_UNAVAILABLE_MESSAGE
        card = next((card for card in cards if card.boss_slug == boss_slug), None)
        if card is None:
            return "没有找到这个榜单，可能已下线或暂未公开。"
        updated, added = self.watchlist.with_board(
            origin,
            WatchedBoard(
                boss_slug=card.boss_slug,
                boss_name=card.boss_name,
                dungeon_name=card.dungeon_name,
                added_by=self._event_user_key(event),
                added_at=utc_now_text(),
            ),
        )
        if not self._save_watchlist(updated):
            return "关注列表写入失败，请检查数据目录权限。"
        boards = self.watchlist.boards_for(origin)
        position = next(
            (
                index
                for index, board in enumerate(boards, start=1)
                if board.boss_slug == boss_slug
            ),
            len(boards),
        )
        label = board_label(card.dungeon_name, card.boss_name)
        if not added:
            return f"榜单「{label}」已经在关注列表里（第 {position} 位）。"
        self._seed_board_snapshot(card)
        return (
            f"已关注榜单「{label}」，序号 {position}。"
            "前三名有新纪录时会在这里通报。"
        )

    def _seed_board_snapshot(self, card: HotBossCard) -> None:
        """Record the current top so the first notice needs one more cycle.

        Only when there is no baseline yet, for the same reason as accounts:
        another chat may already be waiting on the existing one.
        """

        if card.boss_slug in self.board_snapshots:
            return
        snapshots = dict(self.board_snapshots)
        snapshots[card.boss_slug] = build_board_snapshot(
            card, checked_at=utc_now_text()
        )
        self._save_board_snapshots(snapshots)

    def _forget_board_snapshot(self, boss_slug: str) -> None:
        if boss_slug in self.watchlist.origins_by_board():
            return
        if boss_slug not in self.board_snapshots:
            return
        snapshots = dict(self.board_snapshots)
        del snapshots[boss_slug]
        self._save_board_snapshots(snapshots)

    def _save_board_snapshots(self, snapshots: dict[str, BoardSnapshot]) -> None:
        self.board_snapshots = snapshots
        if self.board_snapshot_path is None:
            return
        if save_json(self.board_snapshot_path, board_snapshot_payload(snapshots)):
            self._board_snapshot_write_failed = False
            return
        if not self._board_snapshot_write_failed:
            self._board_snapshot_write_failed = True
            logger.warning(
                "ZmdLogBot could not persist the board snapshot; "
                "check the plugin data directory."
            )

    async def _run_board_watch_cycle(self) -> None:
        """One fresh ``hot-bosses`` read covers every watched board.

        The read bypasses the query cache on purpose: that cache may serve a
        stale payload or the on-disk snapshot, and diffing an *older* top list
        against the baseline would announce records that merely fell out of
        the top as new.
        """

        watched = self.watchlist.origins_by_board()
        if not watched:
            if self.board_snapshots:
                self._save_board_snapshots({})
            return
        try:
            cards, _ = await self.client.list_hot_bosses_with_payload()
        except ZmdLogsClientError as exc:
            logger.warning(
                "ZmdLogBot board watch skipped this cycle: %s",
                type(exc).__name__,
            )
            return
        by_slug = {card.boss_slug: card for card in cards}
        checked_at = utc_now_text()
        snapshots: dict[str, BoardSnapshot] = {}
        pending: dict[str, list[tuple[str, str]]] = {}
        for boss_slug, origins in watched.items():
            card = by_slug.get(boss_slug)
            if card is None:
                # Gone from the index: keep the baseline, announce nothing.
                continue
            previous = self.board_snapshots.get(boss_slug)
            snapshots[boss_slug] = build_board_snapshot(card, checked_at=checked_at)
            if not board_snapshot_is_usable(
                previous,
                now=checked_at,
                max_age_seconds=self.settings.rank_snapshot_max_age_seconds,
            ):
                continue
            change = find_top_run_changes(previous, card)
            if change is None:
                continue
            notice = format_board_notice(change, web_base_url=self.web_base_url)
            for origin in origins:
                pending.setdefault(origin, []).append((boss_slug, notice))
        # Same merge-on-live-state rule as the account cycle: 关注 / 取关 may
        # have run while the request was in flight.
        watching = self.watchlist.origins_by_board()
        merged = dict(self.board_snapshots)
        merged.update(snapshots)
        self._save_board_snapshots(
            {slug: snapshot for slug, snapshot in merged.items() if slug in watching}
        )
        for origin, entries in pending.items():
            notices = tuple(
                notice
                for boss_slug, notice in entries
                if origin in watching.get(boss_slug, ())
            )
            if notices:
                await self._send_notice(origin, join_board_notices(notices))

    async def _add_watched_account(
        self,
        query: str,
        event: AstrMessageEvent,
    ) -> str:
        """Resolve an id, link, or nickname, then remember it for this chat."""

        try:
            account_id = parse_account_reference(
                query,
                web_base_url=self.web_base_url,
            )
        except PublicReferenceError:
            account_id = None
        stripped = query.strip()
        try:
            if account_id is not None:
                account = await self._get_public_user_rankings(account_id)
                return await self._remember_watched_account(
                    event,
                    account_id=account.account_id,
                    display_name=account.account_display_name,
                )
            if not MIN_ACCOUNT_SEARCH_LENGTH <= len(stripped) <= 64:
                return (
                    "请提供公开昵称（至少 2 个字符）、accountId "
                    "或 ZMDLogs 账号主页链接。"
                )
            search = await self.client.search_public_accounts(
                stripped,
                limit=MAX_CANDIDATES,
            )
        except ZmdLogsAPIError as exc:
            logger.warning("ZmdLogBot API request failed: %s", exc.code)
            if exc.status_code == 404:
                return "没有找到这个公开账号，或该账号暂无公开榜单记录。"
            return "ZMDLogs 暂时不可用，请稍后重试。"
        except ZmdLogsClientError as exc:
            logger.warning("ZmdLogBot request failed: %s", type(exc).__name__)
            return "ZMDLogs 暂时不可用，请稍后重试。"
        if not search.accounts:
            return f"没有找到昵称包含「{_shorten(stripped)}」的公开账号。"
        if len(search.accounts) == 1 and not search.has_more:
            hit = search.accounts[0]
            return await self._remember_watched_account(
                event,
                account_id=hit.account_id,
                display_name=hit.account_display_name,
            )
        entry = self.candidates.remember(
            stripped,
            tuple(_account_choice(hit, stripped) for hit in search.accounts),
            view=CandidateView.WATCH,
            origin=self._event_origin(event),
        )
        return format_candidates(
            entry,
            ttl_seconds=self.candidates.ttl_seconds,
            note=(
                "还有更多同名结果未列出，可输入更完整的昵称。"
                if search.has_more
                else None
            ),
        )

    async def _remember_watched_account(
        self,
        event: AstrMessageEvent,
        *,
        account_id: str,
        display_name: str,
    ) -> str:
        origin = self._event_origin(event)
        if not origin:
            return _NO_ORIGIN_MESSAGE
        updated, added = self.watchlist.with_account(
            origin,
            WatchedAccount(
                account_id=account_id,
                display_name=display_name,
                added_by=self._event_user_key(event),
                added_at=utc_now_text(),
            ),
        )
        if not self._save_watchlist(updated):
            return "关注列表写入失败，请检查数据目录权限。"
        accounts = self.watchlist.accounts_for(origin)
        position = next(
            (
                index
                for index, account in enumerate(accounts, start=1)
                if account.account_id == account_id
            ),
            len(accounts),
        )
        if not added:
            return f"{display_name} 已经在关注列表里（第 {position} 位）。"
        await self._seed_rank_snapshot(account_id)
        return (
            f"已关注 {display_name}（{account_id}），序号 {position}。"
            "TA 掉出榜单原名次时会在这里通报。"
        )

    async def _seed_rank_snapshot(self, account_id: str) -> None:
        """Record current ranks so the first notice needs only one more cycle.

        Only when there is no baseline yet: refreshing an existing one would
        swallow a drop another chat is already waiting to be told about.
        """

        if account_id in self.rank_snapshots:
            return
        try:
            account = await self._get_public_user_rankings(account_id)
        except ZmdLogsClientError as exc:
            logger.warning(
                "ZmdLogBot could not seed a rank baseline: %s",
                type(exc).__name__,
            )
            return
        checked_at = utc_now_text()
        snapshots = dict(self.rank_snapshots)
        snapshots[account_id] = build_snapshot(account, checked_at=checked_at)
        self._save_rank_snapshots(snapshots)
        # The trace starts with the ranks held at 关注 time, so the trend page
        # has a left edge before the first move.
        history, changed = record_rankings(
            self.rank_history, account, checked_at=checked_at
        )
        if changed:
            self._save_rank_history(history)

    def _forget_rank_snapshot(self, account_id: str) -> None:
        """Drop the baseline and trace once nobody watches the account."""

        if account_id in self.watchlist.origins_by_account():
            return
        if account_id in self.rank_snapshots:
            snapshots = dict(self.rank_snapshots)
            del snapshots[account_id]
            self._save_rank_snapshots(snapshots)
        if account_id in self.rank_history:
            history = dict(self.rank_history)
            del history[account_id]
            self._save_rank_history(history)

    def _save_watchlist(self, updated: WatchList) -> bool:
        if self.watchlist_path is None or not save_json(
            self.watchlist_path,
            updated.to_payload(),
        ):
            return False
        self.watchlist = updated
        return True

    def _start_rank_watch(self) -> None:
        task = self._rank_watch_task
        if not self.settings.rank_watch_enabled or (
            task is not None and not task.done()
        ):
            return
        if self.watchlist_path is None:
            logger.warning(
                "ZmdLogBot rank watch is off: no writable plugin data directory."
            )
            return
        self._rank_watch_task = asyncio.create_task(self._rank_watch_loop())

    async def _rank_watch_loop(self) -> None:
        """Poll forever; one failed cycle must never end the loop."""

        while True:
            interval = self.settings.rank_watch_interval_seconds
            await asyncio.sleep(interval + random.uniform(0.0, interval * 0.1))
            try:
                await self._run_rank_watch_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ZmdLogBot rank watch cycle failed")
            try:
                await self._run_board_watch_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ZmdLogBot board watch cycle failed")

    async def _run_rank_watch_cycle(self) -> None:
        """One pass over every watched account, at most one request each."""

        watched = tuple(self.watchlist.origins_by_account().items())
        if not watched:
            if self.rank_snapshots:
                self._save_rank_snapshots({})
            return
        semaphore = asyncio.Semaphore(_RANK_WATCH_CONCURRENCY)
        results = await asyncio.gather(
            *(
                self._poll_watched_account(account_id, semaphore)
                for account_id, _ in watched
            ),
            return_exceptions=True,
        )
        snapshots: dict[str, AccountSnapshot] = {}
        pending: dict[str, list[tuple[str, str]]] = {}
        live_names: dict[str, str] = {}
        history = self.rank_history
        history_changed = False
        for (account_id, origins), result in zip(watched, results, strict=True):
            if isinstance(result, tuple):
                snapshot, notice, account = result
                snapshots[account_id] = snapshot
                live_names[account_id] = account.account_display_name
                history, changed = record_rankings(
                    history,
                    account,
                    checked_at=snapshot.checked_at or utc_now_text(),
                )
                history_changed = history_changed or changed
                if notice is not None:
                    for origin in origins:
                        pending.setdefault(origin, []).append(
                            (account_id, notice)
                        )
                continue
            if isinstance(result, BaseException):
                logger.warning(
                    "ZmdLogBot rank watch failed for one account: %s",
                    type(result).__name__,
                )
            # Unreachable this cycle: keep the last known ranks so the next
            # comparison runs against real data instead of against a gap.
            previous = self.rank_snapshots.get(account_id)
            if previous is not None:
                snapshots[account_id] = previous
        # 关注 and 取关 can edit both maps while this cycle is awaiting, so
        # merge on top of the current state instead of installing the map this
        # cycle started from, and forget whoever is no longer watched.
        watching = self.watchlist.origins_by_account()
        merged = dict(self.rank_snapshots)
        merged.update(snapshots)
        self._save_rank_snapshots(
            {
                account_id: snapshot
                for account_id, snapshot in merged.items()
                if account_id in watching
            }
        )
        kept_history = {
            account_id: entry
            for account_id, entry in history.items()
            if account_id in watching
        }
        if history_changed or len(kept_history) != len(history):
            self._save_rank_history(kept_history)
        renamed, changed = self.watchlist.with_display_names(live_names)
        if changed:
            self._save_watchlist(renamed)
        # One new record demotes every watched account below it, so send each
        # chat a single merged message instead of a burst of near-identical
        # ones — skipping anything unfollowed while the cycle was running.
        for origin, entries in pending.items():
            notices = tuple(
                notice
                for account_id, notice in entries
                if origin in watching.get(account_id, ())
            )
            if notices:
                await self._send_notice(origin, join_rank_drop_notices(notices))

    async def _poll_watched_account(
        self,
        account_id: str,
        semaphore: asyncio.Semaphore,
    ) -> tuple[AccountSnapshot, str | None, PublicUserRankings] | None:
        """Fetch one account and return its ranks, notice text and the response.

        Every request this account needs stays inside the semaphore, and
        sending is left to the caller so that one chat receives one merged
        message per cycle rather than one message per watched account.
        """

        async with semaphore:
            try:
                account = await self._get_public_user_rankings(account_id)
            except ZmdLogsClientError as exc:
                logger.warning(
                    "ZmdLogBot rank watch skipped one account: %s",
                    type(exc).__name__,
                )
                return None
            checked_at = utc_now_text()
            snapshot = build_snapshot(account, checked_at=checked_at)
            previous = self.rank_snapshots.get(account_id)
            if not snapshot_is_usable(
                previous,
                now=checked_at,
                max_age_seconds=self.settings.rank_snapshot_max_age_seconds,
            ):
                # Too old to compare against, so re-seed quietly instead of
                # announcing everything that moved while nobody was looking.
                return snapshot, None, account
            drops = find_rank_drops(
                previous,
                account,
                rank_threshold=self.settings.rank_watch_rank_threshold,
            )
            if not drops:
                return snapshot, None, account
            notice = format_rank_drop_notice(
                account.account_display_name,
                await self._describe_drops(
                    drops,
                    account_id=account.account_id,
                    since=previous.checked_at,
                ),
                web_base_url=self.web_base_url,
            )
        return snapshot, notice, account

    async def _describe_drops(
        self,
        drops: tuple[RankDrop, ...],
        *,
        account_id: str,
        since: str | None,
    ) -> tuple[RankDrop, ...]:
        """Look up what appeared above the account, for the drops shown.

        Only the boards the notice actually prints are fetched, so one account
        can never cost more than ``MAX_DROPS_PER_NOTICE`` extra requests.
        """

        described: list[RankDrop] = []
        for drop in drops[:MAX_DROPS_PER_NOTICE]:
            try:
                ranking = await self._get_boss_ranking(drop.boss_slug)
            except ZmdLogsClientError:
                described.append(drop)
                continue
            described.append(
                replace(
                    drop,
                    new_record_above=find_new_record_above(
                        ranking,
                        account_id=account_id,
                        fallback_rank=drop.current_rank,
                        since=since,
                    ),
                )
            )
        return tuple(described) + drops[MAX_DROPS_PER_NOTICE:]

    async def _send_notice(self, origin: str, text: str) -> None:
        if Plain is None:
            logger.warning(
                "ZmdLogBot cannot build a rank notice on this AstrBot version."
            )
            return
        try:
            # One unresponsive adapter must not stall the whole cycle, and a
            # refused send reports itself by returning False rather than raising.
            delivered = await asyncio.wait_for(
                self.context.send_message(origin, MessageChain([Plain(text)])),
                timeout=_NOTICE_SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("ZmdLogBot timed out delivering a rank notice.")
            return
        except Exception as exc:
            logger.warning(
                "ZmdLogBot could not deliver a rank notice: %s",
                type(exc).__name__,
            )
            return
        if delivered is False:
            logger.warning(
                "ZmdLogBot rank notice was refused; the chat may be gone."
            )

    def _save_rank_snapshots(
        self,
        snapshots: dict[str, AccountSnapshot],
    ) -> None:
        """Persist the baseline; a silent failure would replay old notices.

        Written every cycle rather than only when a rank moved: ``checked_at``
        is what bounds staleness, so a stored baseline that stops advancing
        would eventually be rejected after a restart and silently re-seeded.
        """

        self.rank_snapshots = snapshots
        if self.rank_snapshot_path is None:
            return
        if save_json(self.rank_snapshot_path, snapshot_payload(snapshots)):
            self._rank_snapshot_write_failed = False
            return
        if not self._rank_snapshot_write_failed:
            self._rank_snapshot_write_failed = True
            logger.warning(
                "ZmdLogBot could not persist the rank snapshot; "
                "check the plugin data directory."
            )

    @staticmethod
    def _event_origin(event: AstrMessageEvent) -> str:
        """The chat an event came from; empty when the platform gives none.

        Callers decide what an empty origin means for them — the watch routes
        refuse, the candidate store keys on it, auto-expand only dedupes.
        """

        return getattr(event, "unified_msg_origin", "") or ""

    @staticmethod
    def _event_user_key(event: AstrMessageEvent) -> str:
        """Platform-scoped sender key, used only for "who may remove this"."""

        platform = getattr(event, "get_platform_name", None)
        sender = getattr(event, "get_sender_id", None)
        try:
            platform_name = platform() if callable(platform) else ""
            sender_id = sender() if callable(sender) else ""
        except Exception:
            return ""
        if not platform_name or not sender_id:
            return ""
        return f"{platform_name}:{sender_id}"

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

        configured = Path(self.settings.alias_file_path)
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
        if not self.settings.fallback_to_astrbot_renderer or html is None:
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

    async def _get_battle_export(self, battle_id: str) -> BattleExport:
        result = await self.battle_export_cache.get_or_load(
            battle_id,
            lambda: self.client.get_battle_export(battle_id),
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
                now + self.settings.battle_link_dedupe_seconds
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
            if route.kind in {RouteKind.ACCOUNT_QUERY, RouteKind.TREND_QUERY}:
                return "没有找到这个公开账号，或该账号暂无公开榜单记录。"
            if (
                route.kind in _BATTLE_STYLE_ROUTES
                or route.kind is RouteKind.COMPARE_QUERY
            ):
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

        task = self._rank_watch_task
        if task is not None:
            self._rank_watch_task = None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("ZmdLogBot rank watch task ended with an error.")
        await self.hot_boss_cache.close()
        await self.boss_ranking_cache.close()
        await self.account_cache.close()
        await self.battle_cache.close()
        await self.battle_export_cache.close()
        await self.character_stats_cache.close()
        await self.character_boss_cache.close()
        try:
            await self.client.close()
        finally:
            if self.renderer is not None:
                await self.renderer.close()
        logger.info("ZmdLogBot plugin terminated.")


def _shorten(text: str, limit: int = _ECHO_LIMIT) -> str:
    """Cut user text echoed in a reply so a huge message is never repeated."""

    return text if len(text) <= limit else text[: limit - 1] + "…"


def _battle_link_error_message(error: ZmdLogsAPIError) -> str:
    if error.status_code == 404:
        return "链接对应的公开战报不存在、未公开或已删除。"
    return _UPSTREAM_UNAVAILABLE_MESSAGE


def _account_choice(hit, query: str) -> MatchChoice:
    """Wrap one search hit in the candidate shape the pick list understands."""

    return _account_choice_from(hit.account_id, hit.account_display_name, query)


def _account_choice_from(
    account_id: str,
    display_name: str,
    query: str,
) -> MatchChoice:
    return MatchChoice(
        target=MatchTarget(
            target_type=TargetType.ACCOUNT,
            key=account_id,
            name=display_name,
            dungeon_names=(),
            boss_slugs=(),
            query_text=query,
        ),
        level=MatchLevel.STANDARD_EXACT,
        score=1.0,
        matched_text=display_name,
    )


def _route_view(route: RouteRequest) -> CandidateView:
    if route.kind is RouteKind.CHARACTER_STATS:
        return CandidateView.CHARACTER_STATS
    if route.kind is RouteKind.ROSTER_QUERY:
        return CandidateView.ROSTER
    if route.kind is RouteKind.BATTLE_QUERY:
        return CandidateView.BATTLE
    if route.kind is RouteKind.LOADOUT_QUERY:
        return CandidateView.LOADOUT
    if route.kind is RouteKind.SKILL_QUERY:
        return CandidateView.SKILLS
    if route.kind is RouteKind.TIMELINE_QUERY:
        return CandidateView.TIMELINE
    if route.kind is RouteKind.COMPARE_QUERY:
        return CandidateView.COMPARE
    if route.kind is RouteKind.TREND_QUERY:
        return CandidateView.TREND
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
        battle_rank=route.battle_rank,
        compare_rank=route.compare_rank if route.compare_rank is not None else 2,
    )
