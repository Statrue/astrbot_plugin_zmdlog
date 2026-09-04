"""AstrBot entry point for ZmdLogBot."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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

from .core import messages
from .core.candidates import (
    MAX_CANDIDATES,
    CandidateStore,
    CandidateView,
    PendingCandidates,
    account_choice,
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
from .core.datasource import ZmdLogsDataSource
from .core.identifiers import (
    PublicReferenceError,
    extract_battle_references,
    parse_account_reference,
    parse_battle_reference,
)
from .core.matcher import (
    BOARD_QUERY_TARGETS,
    AliasConfig,
    AliasConfigError,
    MatchChoice,
    MatcherCache,
    MatchLevel,
    MatchStatus,
    TargetType,
    fold_text,
)
from .core.messages import shorten
from .core.models import (
    BattleDetailSummary,
    BattleExport,
    HotBossCard,
)
from .core.persistence import load_json, save_json
from .core.rank_watch import RankWatcher
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
_NOTICE_SEND_TIMEOUT_SECONDS = 30.0
_PLUGIN_DATA_NAME = "astrbot_plugin_zmdlog"
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
_CHARACTER_STATS_UNAVAILABLE = "character_statistics_not_available"
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
        self.data = ZmdLogsDataSource(
            self.client,
            settings=settings,
            data_dir=self.data_dir,
            logger=logger,
        )
        self.watcher = RankWatcher(
            client=self.client,
            data=self.data,
            settings=settings,
            data_dir=self.data_dir,
            board_matcher=lambda cards: self._matchers.matcher_for(
                cards, self.aliases
            ),
            candidates=self.candidates,
            notify=self._send_notice,
            logger=logger,
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
        were the only start path. ``RankWatcher.start`` is idempotent, and the
        loop sleeps a full interval before its first cycle, so starting here
        cannot race platform startup.
        """

        self.watcher.start()

    @filter.on_astrbot_loaded()
    async def on_astrbot_ready(self) -> None:
        """Start Chromium and, on a cold boot, the rank watcher."""

        self.watcher.start()
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
                    message = await self.watcher.handle_route(
                        route,
                        origin=self._event_origin(event),
                        requester_key=self._event_user_key(event),
                        is_admin=self._event_is_admin(event),
                        command=self._command_prefix(event) + "zmdlog",
                    )
            except ZmdLogsClientError as exc:
                logger.warning(
                    "ZmdLogBot request failed: %s", type(exc).__name__
                )
                message = messages.UPSTREAM_UNAVAILABLE
            except Exception:
                logger.exception("ZmdLogBot unexpected command failure")
                message = messages.UNEXPECTED_FAILURE
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
            battle = await self.data.get_battle_detail(battle_id)
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
            return _DispatchOutcome(message=messages.UPSTREAM_UNAVAILABLE), True
        except RenderError as exc:
            logger.error(
                "ZmdLogBot %s rendering failed: %s",
                failure_label,
                type(exc).__name__,
            )
            fallback_path = await self._render_with_astrbot(exc)
            if fallback_path is not None:
                return _DispatchOutcome(image_path=fallback_path), False
            return _DispatchOutcome(message=messages.RENDER_FAILURE), True
        except Exception:
            logger.exception("ZmdLogBot unexpected %s failure", failure_label)
            return _DispatchOutcome(message=messages.UNEXPECTED_FAILURE), False

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
            cards = await self.data.list_hot_bosses()
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
                        message=messages.ACCOUNT_REFERENCE_NEEDED
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
                return _DispatchOutcome(message=messages.COMPARE_REFERENCE_NEEDED)
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
            local = self.watcher.history_by_name(route.query)
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
                        account_choice(
                            item.account_id,
                            item.display_name,
                            query=route.query,
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
                    message=messages.ACCOUNT_REFERENCE_NEEDED
                )
            return outcome

        if route.kind is RouteKind.CHARACTER_STATS and not route.query.strip():
            stats = await self.data.get_character_statistics(
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
            cards = await self.data.list_hot_bosses()
        except ZmdLogsClientError:
            if self._looks_like_direct_slug(route.query):
                return await self._render_board(
                    route.query, query=route.query, pending=pending
                )
            raise

        matcher = self._matchers.matcher_for(cards, self.aliases)
        # The account version can extend the smart route's set without changing
        # the matcher or the explicit board route.
        match = matcher.match(route.query, allowed_types=BOARD_QUERY_TARGETS)

        shown_query = shorten(route.query)
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
                message=f"没有找到昵称包含「{shorten(stripped)}」的公开账号。"
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
            account_choice(hit.account_id, hit.account_display_name, query=stripped)
            for hit in search.accounts
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
        return tuple(
            account_choice(hit.account_id, hit.account_display_name, query=stripped)
            for hit in search.accounts
        )

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
            account = await self.data.get_public_user_rankings(account_id)
        except ZmdLogsAPIError as exc:
            if exc.status_code != 404:
                raise
            logger.warning("ZmdLogBot API request failed: %s", exc.code)
            return _DispatchOutcome(message=messages.ACCOUNT_NOT_FOUND)
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

        history = self.watcher.history_for(account_id)
        if history is None or not history.boards:
            return _DispatchOutcome(message=messages.TREND_NO_DATA)
        renderer = self._require_renderer()
        image_path = await renderer.render_trend(
            history,
            query=query,
            web_base_url=self.web_base_url,
            time_range=time_range,
            last_checked=self.watcher.last_checked(account_id),
        )
        return _DispatchOutcome(image_path=image_path)

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
            catalog = await self.data.get_character_statistics(
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
        stats = await self.data.get_character_boss_statistics(
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
            stats = await self.data.get_character_statistics(
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
            stats = await self.data.get_character_statistics(
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

        ranking = await self.data.get_boss_ranking(boss_slug)
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
                            f"「{shorten(resolution.query)}」可能是：{options}，"
                            "请写全名。"
                        )
                    )
                if resolution.status is CharacterResolutionStatus.NOT_FOUND:
                    return _DispatchOutcome(
                        message=(
                            f"「{ranking.boss_name}」的公开排名里没有"
                            f"「{shorten(resolution.query)}」。"
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
                export = await self.data.get_battle_export(battle_id)
            except ZmdLogsAPIError as exc:
                if exc.status_code == 422 and exc.code == _EXPORT_UNSUPPORTED:
                    logger.warning("ZmdLogBot API request failed: %s", exc.code)
                    return _DispatchOutcome(message=messages.NO_TIMELINE)
                if exc.status_code == 429:
                    logger.warning("ZmdLogBot API request failed: %s", exc.code)
                    return _DispatchOutcome(message=messages.TIMELINE_RATE_LIMITED)
                raise
            # The detail only adds the BUFF 覆盖 band; the page stands without.
            image_path = await renderer.render_timeline(
                export,
                query=query,
                web_base_url=self.web_base_url,
                battle=await self._battle_detail_if_available(battle_id),
            )
            return _DispatchOutcome(image_path=image_path)
        battle = await self.data.get_battle_detail(battle_id)
        if view is CandidateView.LOADOUT:
            if not battle.roster:
                return _DispatchOutcome(message=messages.NO_LOADOUT)
            image_path = await renderer.render_loadout(
                battle, query=query, web_base_url=self.web_base_url
            )
        elif view is CandidateView.SKILLS:
            if not battle.skill_stats:
                return _DispatchOutcome(message=messages.NO_SKILL_STATS)
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
            return await self.data.get_battle_export(battle_id), None
        except ZmdLogsAPIError as exc:
            logger.warning("ZmdLogBot battle export unavailable: %s", exc.code)
            if exc.status_code == 422 and exc.code == _EXPORT_UNSUPPORTED:
                return None, messages.NO_TIMELINE
            if exc.status_code == 429:
                return None, messages.TIMELINE_RATE_LIMITED
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
            return _DispatchOutcome(message=messages.COMPARE_SAME_BATTLE)
        renderer = self._require_renderer()
        first, second = await asyncio.gather(
            self.data.get_battle_detail(battle_id_a),
            self.data.get_battle_detail(battle_id_b),
        )
        if first.boss_name != second.boss_name:
            return _DispatchOutcome(
                message=messages.COMPARE_CROSS_BOSS.format(
                    first=shorten(first.boss_name), second=shorten(second.boss_name)
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
            return await self.data.get_battle_detail(battle_id)
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
        if entry.view in {CandidateView.WATCH, CandidateView.WATCH_BOARD}:
            origin = self._event_origin(event)
            requester = self._event_user_key(event)
            if entry.view is CandidateView.WATCH:
                message = await self.watcher.remember_account(
                    origin,
                    requester,
                    account_id=choice.target.key,
                    display_name=choice.target.name,
                )
            else:
                message = await self.watcher.remember_board(
                    origin, requester, choice.target.key
                )
            yield event.plain_result(message)
            return

        async def render_pick() -> _DispatchOutcome:
            cards = await self.data.list_hot_bosses()
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
                cards = await self.data.list_hot_bosses()
            except ZmdLogsClientError:
                cards = ()
            for card in cards:
                names[card.boss_slug] = card.boss_name
            return self._format_alias_list(names)
        if not self._event_is_admin(event):
            return messages.ALIAS_ADMIN_ONLY
        if route.kind is RouteKind.ALIAS_REMOVE:
            updated, removed = self.aliases.without_alias(route.query)
            if removed == 0:
                return f"没有找到自定义别名「{shorten(route.query)}」。"
            if not self._save_aliases(updated):
                return messages.ALIAS_WRITE_FAILED
            return f"已删除别名「{route.query}」。"

        target_text, _, alias_text = route.query.partition(" ")
        aliases = tuple(dict.fromkeys(alias_text.split()))
        try:
            cards = await self.data.list_hot_bosses()
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
                f"没有唯一匹配到「{shorten(target_text)}」，"
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
            return messages.ALIAS_WRITE_FAILED
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

    async def _send_notice(self, origin: str, text: str) -> None:
        """Deliver one rank-watch notice; the only push path in the plugin."""

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
                return messages.ACCOUNT_NOT_FOUND
            if (
                route.kind in _BATTLE_STYLE_ROUTES
                or route.kind is RouteKind.COMPARE_QUERY
            ):
                return messages.BATTLE_NOT_FOUND
            if route.kind in {
                RouteKind.RANKING_QUERY,
                RouteKind.SMART_QUERY,
                RouteKind.CHARACTER_STATS,
                RouteKind.ROSTER_QUERY,
            }:
                return cls._board_api_error_message(error)
        return messages.UPSTREAM_UNAVAILABLE

    @staticmethod
    def _board_api_error_message(error: ZmdLogsAPIError) -> str:
        if error.status_code == 404:
            if error.code == _CHARACTER_STATS_UNAVAILABLE:
                return messages.CRISIS_CONTRACT_NO_STATISTICS
            return messages.BOARD_NOT_FOUND
        return messages.UPSTREAM_UNAVAILABLE

    @staticmethod
    def _looks_like_direct_slug(query: str) -> bool:
        return is_valid_boss_slug(query) and ("_" in query or "-" in query)

    async def terminate(self) -> None:
        """Release HTTP, browser, and generated-image resources."""

        await self.watcher.stop()
        await self.data.close()
        try:
            await self.client.close()
        finally:
            if self.renderer is not None:
                await self.renderer.close()
        logger.info("ZmdLogBot plugin terminated.")


def _battle_link_error_message(error: ZmdLogsAPIError) -> str:
    if error.status_code == 404:
        return messages.BATTLE_LINK_NOT_FOUND
    return messages.UPSTREAM_UNAVAILABLE


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
