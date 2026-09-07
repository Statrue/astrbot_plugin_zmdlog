"""AstrBot entry point for ZmdLogBot."""

import asyncio
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:  # StarTools.get_data_dir is missing on older AstrBot releases.
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover - depends on host AstrBot version
    StarTools = None

try:  # Plain lives under different api surfaces across AstrBot releases.
    from astrbot.api.message_components import Image, Plain
except ImportError:  # pragma: no cover - depends on host AstrBot version
    try:
        from astrbot.core.message.components import Image, Plain
    except ImportError:
        # Only rank notices and tool pictures need these; every query must
        # keep working without them.
        Image = Plain = None

from .core import facts, messages
from .core.alias_admin import AliasAdmin
from .core.candidates import (
    CandidateStore,
    CandidateView,
    extract_code,
    parse_selection,
)
from .core.client import (
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsClientError,
)
from .core.datasource import ZmdLogsDataSource
from .core.identifiers import (
    extract_battle_references,
)
from .core.matcher import (
    AliasConfig,
    MatcherCache,
)
from .core.persistence import load_json, save_json
from .core.queries import (
    Outcome,
    QueryService,
    api_error_message,
    battle_link_error_message,
    board_api_error_message,
    tool_api_error_message,
)
from .core.rank_watch import RankWatcher
from .core.render import (
    LongImageRenderer,
    RenderError,
    TemplateConfigurationError,
)
from .core.routing import (
    RouteKind,
    RouteParseError,
    parse_zmdlog_payload,
)
from .core.settings import load_settings
from .core.toolbox import ToolService

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
# The pictures the tools of one turn drew, kept on the event until the model
# has finished: exactly one is sent then, and several mean none is.
_TOOL_PICTURES = "_zmdlog_tool_pictures"
# What one tool may put into the model's context. The picture carries the
# detail; a wall of text past this only crowds out the conversation.
_MAX_TOOL_REPLY_CHARS = 4_500
_CJK_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CJK_NUMBER_RE = re.compile(r"([一二两三四五六七八九])?(十)?([一二三四五六七八九])?")
_PLUGIN_DATA_NAME = "astrbot_plugin_zmdlog"
_BATTLE_LINK_FILTER = (
    r"https?://[^\s<>\"']+/(?:battle|share|axis)/btl_[A-Za-z0-9_-]+"
)


def _agent_done_hook():
    """``filter.on_agent_done``: fires once, when the model has finished a turn.

    Older AstrBot releases have no such hook; there the tools answer in text
    alone, which beats failing to load.
    """

    register = getattr(filter, "on_agent_done", None)
    if register is None:  # pragma: no cover - depends on host AstrBot version
        return lambda handler: handler
    return register()


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

        def board_matcher(cards):
            # Late-bound: an alias edit installs a new configuration.
            return self._matchers.matcher_for(cards, self.alias_admin.aliases)

        self.alias_admin = AliasAdmin(
            path=self._resolve_alias_path(),
            data=self.data,
            board_matcher=board_matcher,
            logger=logger,
        )
        self.watcher = RankWatcher(
            client=self.client,
            data=self.data,
            settings=settings,
            data_dir=self.data_dir,
            board_matcher=board_matcher,
            candidates=self.candidates,
            notify=self._send_notice,
            logger=logger,
        )
        self.queries = QueryService(
            client=self.client,
            data=self.data,
            renderer=self._require_renderer,
            candidates=self.candidates,
            board_matcher=board_matcher,
            watcher=self.watcher,
            settings=settings,
            logger=logger,
        )
        self.tools = ToolService(
            client=self.client,
            data=self.data,
            renderer=self._require_renderer,
            board_matcher=board_matcher,
            settings=settings,
            logger=logger,
            watcher=self.watcher,
        )
        self._auto_expand_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[None]] = set()
        if getattr(filter, "on_agent_done", None) is None:
            logger.warning(
                "ZmdLogBot: this AstrBot has no on_agent_done hook; the LLM "
                "tools answer in text alone."
            )
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

        self.data.start()
        self.watcher.start()

    @filter.on_astrbot_loaded()
    async def on_astrbot_ready(self) -> None:
        """Start Chromium and, on a cold boot, the index and the rank watcher."""

        self.data.start()
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
                    message = await self.alias_admin.handle(
                        route, is_admin=self._event_is_admin(event)
                    )
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
            lambda: self.queries.dispatch(
                route,
                command_prefix=self._command_prefix(event),
                origin=self._event_origin(event),
            ),
            api_error_message=lambda exc: api_error_message(route, exc),
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

        outcome, transient = await self._run_guarded(
            lambda: self.queries.render_battle_card(battle_id),
            api_error_message=battle_link_error_message,
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
        action: Callable[[], Awaitable[Outcome]],
        *,
        api_error_message: Callable[[ZmdLogsAPIError], str],
        failure_label: str,
    ) -> tuple[Outcome, bool]:
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
                Outcome(message=api_error_message(exc)),
                exc.status_code != 404,
            )
        except ZmdLogsClientError as exc:
            logger.warning(
                "ZmdLogBot %s request failed: %s",
                failure_label,
                type(exc).__name__,
            )
            return Outcome(message=messages.UPSTREAM_UNAVAILABLE), True
        except RenderError as exc:
            logger.error(
                "ZmdLogBot %s rendering failed: %s",
                failure_label,
                type(exc).__name__,
            )
            fallback_path = await self._render_with_astrbot(exc)
            if fallback_path is not None:
                return Outcome(image_path=fallback_path), False
            return Outcome(message=messages.RENDER_FAILURE), True
        except Exception:
            logger.exception("ZmdLogBot unexpected %s failure", failure_label)
            return Outcome(message=messages.UNEXPECTED_FAILURE), False

    @staticmethod
    def _outcome_result(event: AstrMessageEvent, outcome: Outcome):
        if outcome.image_path is not None:
            return event.image_result(outcome.image_path)
        return event.plain_result(outcome.message or "本次查询未产生结果。")

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

        outcome, _ = await self._run_guarded(
            lambda: self.queries.render_pick(entry, choice),
            api_error_message=board_api_error_message,
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

    async def _send_notice(self, origin: str, text: str) -> bool:
        """Deliver one rank-watch notice; the only push path in the plugin.

        The caller only advances a baseline past what this reports as sent,
        so every failure path has to answer False rather than swallow.
        """

        if Plain is None:
            logger.warning(
                "ZmdLogBot cannot build a rank notice on this AstrBot version."
            )
            return False
        try:
            # One unresponsive adapter must not stall the whole cycle, and a
            # refused send reports itself by returning False rather than raising.
            delivered = await asyncio.wait_for(
                self.context.send_message(origin, MessageChain([Plain(text)])),
                timeout=_NOTICE_SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("ZmdLogBot timed out delivering a rank notice.")
            return False
        except Exception as exc:
            logger.warning(
                "ZmdLogBot could not deliver a rank notice: %s",
                type(exc).__name__,
            )
            return False
        if delivered is False:
            logger.warning(
                "ZmdLogBot rank notice was refused; the chat may be gone."
            )
            return False
        return True

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

    # --- LLM tools ----------------------------------------------------------
    #
    # Four tools, one per subject, never one per feature: AstrBot sends every
    # active tool's schema with every LLM request, and a model choosing among
    # overlapping tools picks the wrong one. Each draws the page zmdlog would
    # have drawn and returns the facts behind it as text; the picture goes
    # out when the model has finished, if the turn drew exactly one. It
    # carries the numbers so the model never has to retype them.

    @filter.llm_tool(name="zmdlogs_board_ranking")
    async def zmdlogs_board_ranking(
        self,
        event: AstrMessageEvent,
        board: str = "",
        character: str = "",
        limit: str = "",
        element: str = "",
        range: str = "",
        profession: str = "",
    ):
        """查询终末地某个首领榜单的公开速通记录：前几名的用时、DPS、主C、阵容、
        战斗日期、落后第一几秒和 battleId，最快/中位/平均用时，各职业位的角色出场率，
        出现过的阵容组合各几次。board 填首领或副本名；填副本或期数（如“影拓丰碑4期”）
        则列出该副本下每个榜的前三；填“全部”则列出所有榜的第一名。
        问的是某个角色（干员）的第一、冠军、排名时改用 zmdlogs_character_standings。
        给了角色名则只看带这个角色的记录，并统计它最常和谁同队。
        榜单留空则回答“最近有什么新纪录”：哪些榜的第一名被谁刷新了、
        新上传了哪些记录、哪个榜最近最活跃。
        只查一个对象时会自动附长图，图里有完整数值，你不要复述数字，只解读；
        这次回答里查了多个对象就不附图，不要让用户看图。
        数据只有玩家自愿上传的公开成功记录，不是全服统计，没有失败样本，
        出场次数和名次都不代表谁更强，回答时要说明。

        Args:
            board(string): 榜单、副本或期数关键词，例如“罗丹”“呼吼炽焰”“丰碑4”；
                “全部”看所有榜的第一名
            character(string): 只看带这个角色的记录并统计它的同队伙伴，
                例如“提弗洛斯”，不筛选就留空
            limit(string): 列出前几名，默认 10，最多 30
            element(string): 只看主C 为该属性的记录，填 物理、灼热、寒冷、自然 或 电磁，
                不筛选就留空
            range(string): 填 7d、14d 或 30d：给了榜单就只看这段时间打出的记录和
                第一名变化，榜单留空则看这段时间的新纪录（默认 7d）
            profession(string): 只看主C 为该职业的记录，填 先锋、近卫、重装、术士、
                突击 或 辅助，不筛选就留空
        """

        return await self._run_tool(
            event,
            lambda: self.tools.board(
                board,
                character=character,
                limit=_positive_int(limit, facts.DEFAULT_ROW_LIMIT),
                element=element,
                time_range=range,
                profession=profession,
            ),
        )

    @filter.llm_tool(name="zmdlogs_battle_report")
    async def zmdlogs_battle_report(
        self,
        event: AstrMessageEvent,
        battle: str,
        compare_with: str = "",
    ):
        """查询终末地某一场公开战报：用时、全队与各角色 DPS、伤害占比、暴击率、
        最大单次，
        每人的等级潜能、武器精炼、套装、技能等级，主要伤害来源、BUFF 覆盖率、
        各类招式施放次数和每人的开场顺序。给了第二场就改为对比同一首领的两场：
        用时差、DPS 差、阵容差异、同名角色的养成与装备差异；跨首领没有可比性。
        只查一个对象时会自动附长图，图里有完整数值，你不要复述数字，只解读；
        这次回答里查了多个对象就不附图，不要让用户看图。工具只列出记录本身和两份记录的差异，
        哪一处造成了时间差公开数据无法判定，不要替它下因果结论；
        “大家给某角色配什么”没有统计，只能逐场看战报。
        要按名次找某一场，先用榜单工具拿到那一名的 battleId。
        数据全部来自 ZMDLogs 上玩家自愿上传的公开记录，不是全服统计，回答时要说明。

        Args:
            battle(string): battleId（形如 btl_upload_xxxx）或 ZMDLogs 战报链接
            compare_with(string): 要对比的第二场的 battleId 或链接，只看一场就留空
        """

        return await self._run_tool(
            event, lambda: self.tools.battle(battle, compare_with=compare_with)
        )

    @filter.llm_tool(name="zmdlogs_character_standings")
    async def zmdlogs_character_standings(
        self,
        event: AstrMessageEvent,
        character: str = "",
        board: str = "",
        element: str = "",
        range: str = "",
        profession: str = "",
        potential: str = "",
    ):
        """凡是问某个角色（干员）的“第一、冠军、第一名、排第几、成绩、上了哪些榜、
        最常和谁同队”，例如“别礼的第一呢”“洛茜有几个冠军”，都用这个工具，把名字原样
        填进 character：名字是不是角色由工具判断并回答，你不要自己猜。
        查询终末地某个角色在公开记录里的表现：带它的队伍在每个榜单的最好名次、
        用时、DPS 和阵容（这是队伍的成绩，任何星级的角色都能查），最常同队的角色，
        六星干员再附上 DPS 分布：中位数、四分位、样本量和各榜名次；
        给了榜单则只看那个榜的 DPS 分布，角色名留空而给了榜单则是该榜每个六星的分布，
        board 填“全部”则是全部副本合计。range 和 potential 只作用于 DPS 分布。
        只查一个对象时会自动附长图，图里有完整数值，你不要复述数字，只解读；
        这次回答里查了多个对象就不附图，不要让用户看图。
        DPS 名次只看去极值后的正常样本，正常样本不足的角色没有名次。
        角色名和榜单都留空则回答“谁的冠军最多/最少”：每个角色的队伍在全部榜单
        拿下的第一名、前三、前十各几个；填了 profession 就把该职业的角色全部列出，
        0 个也列，从没上过榜的也点名。角色的属性、职业、技能说明是图鉴问题，不在这里。
        这些数字来自公开速通记录，受玩家水平和配装影响，不是角色强度的判据；
        不回答“谁更强”“循环 DPS”“专武收益”。
        数据全部来自 ZMDLogs 上玩家自愿上传的公开记录，不是全服统计，回答时要说明。

        Args:
            character(string): 角色全名，例如“提弗洛斯”“余烬”；留空看全角色冠军榜
                或某榜的分布
            board(string): 只看某个榜单的 DPS 分布，例如“罗丹”；“全部”为全部副本合计；
                留空看它在所有榜单的表现
            element(string): 角色名留空时只看该属性的角色，填 物理、灼热、寒冷、自然
                或 电磁；问“物理队有什么冠军”就填 物理
            range(string): 填 7d、14d 或 30d：冠军榜只算这段时间的记录，DPS 分布
                只算这段时间的样本；不限时间留空
            profession(string): 角色名留空时只看该职业的角色，填 先锋、近卫、重装、
                术士（术师）、突击 或 辅助
            potential(string): DPS 分布的潜能档，填 0（零潜）、1-5（有潜能，合并）
                或留空（全部）；ZMDLogs 不按具体层数拆分
        """

        return await self._run_tool(
            event,
            lambda: self.tools.character(
                character,
                board,
                element,
                range,
                profession=profession,
                potential=potential,
            ),
        )

    @filter.llm_tool(name="zmdlogs_account_records")
    async def zmdlogs_account_records(
        self,
        event: AstrMessageEvent,
        account: str = "",
        range: str = "",
    ):
        """查询终末地某个公开账号（玩家、昵称）在各首领榜单的最好成绩：名次、用时、
        DPS、阵容、战斗日期和 battleId，它的公开记录数、冠军数、常用主C 和常用阵容，
        以及名次变化（掉了几名、最好和最差名次）——名次变化只有被本群关注过的账号才有记录。
        账号留空则回答“哪个玩家冠军最多、谁上传最多”（玩家排名）：各公开账号的第一名、前三、前十各几个。
        角色（干员）的成绩不在这里，用 zmdlogs_character_standings。
        只查一个对象时会自动附长图，图里有完整数值，你不要复述数字，只解读；
        这次回答里查了多个对象就不附图，不要让用户看图。
        只有把记录设为公开的玩家才查得到。
        数据全部来自 ZMDLogs 上玩家自愿上传的公开记录，不是全服统计，回答时要说明。

        Args:
            account(string): 公开昵称、accountId 或 ZMDLogs 账号主页链接；
                留空看玩家排名
            range(string): 填 7d、14d 或 30d：给了账号则只看这段时间打出的最好记录
                和名次变化，账号留空则只算这段时间的记录；不限时间留空
        """

        return await self._run_tool(
            event, lambda: self.tools.account(account, range)
        )

    async def _run_tool(self, event: AstrMessageEvent, action) -> str:
        """Run one tool: hand its facts to the model, keep its picture for later.

        A turn sends at most one picture, and only when its tools drew
        exactly one. A model answering a question often calls two or three
        tools, and three long images in a row is spam; and when it asks one
        tool about several subjects — four characters compared — none of
        their pages is *the* answer, so sending the first would be an
        arbitrary pick that reads as the wrong one. Which picture, if any,
        is decided when the model has finished, in ``send_tool_picture``.
        """

        try:
            answer = await action()
        except ZmdLogsAPIError as exc:
            logger.warning("ZmdLogBot tool API request failed: %s", exc.code)
            return tool_api_error_message(exc)
        except ZmdLogsClientError as exc:
            logger.warning(
                "ZmdLogBot tool request failed: %s", type(exc).__name__
            )
            return messages.UPSTREAM_UNAVAILABLE
        except Exception:
            logger.exception("ZmdLogBot unexpected tool failure")
            return messages.UNEXPECTED_FAILURE
        text = _shorten_tool_reply(answer.text)
        if not answer.image_path:
            return text
        pictures = getattr(event, _TOOL_PICTURES, None)
        if pictures is None:
            pictures = []
            setattr(event, _TOOL_PICTURES, pictures)
        pictures.append(answer.image_path)
        if len(pictures) > 1:
            # Said in every result after the first, so the model does not
            # point the reader at a picture that will not come.
            return text + "\n" + messages.TOOL_PICTURE_WITHHELD
        return text

    @_agent_done_hook()
    async def send_tool_picture(
        self, event: AstrMessageEvent, run_context=None, response=None
    ) -> None:
        """Once the model has finished a turn, send the one picture its tools drew.

        Nothing goes out when they drew several: the text the model got
        carries every subject's facts, and no one page would be the answer.
        """

        pictures = getattr(event, _TOOL_PICTURES, None)
        if not pictures:
            return
        setattr(event, _TOOL_PICTURES, [])
        if len(pictures) != 1:
            return
        if Image is None:
            logger.warning(
                "ZmdLogBot cannot attach a tool image on this AstrBot version."
            )
            return
        # In the background: the platform upload then overlaps the reply the
        # pipeline is about to send instead of holding it back.
        self._spawn(self._send_tool_image(event, pictures[0]))

    async def _send_tool_image(self, event: AstrMessageEvent, path: str) -> None:
        try:
            await asyncio.wait_for(
                event.send(MessageChain([Image.fromFileSystem(path)])),
                timeout=_NOTICE_SEND_TIMEOUT_SECONDS,
            )
            # Direct sends bypass the pipeline's own send log; one line here
            # is what lets a log review tell a sent picture from a lost one.
            logger.info("ZmdLogBot sent the turn's tool picture.")
        except Exception as exc:
            logger.warning(
                "ZmdLogBot could not send a tool image: %s", type(exc).__name__
            )

    def _spawn(self, coro) -> None:
        """Run ``coro`` to completion in the background; cancelled at terminate."""

        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def terminate(self) -> None:
        """Release HTTP, browser, and generated-image resources."""

        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.watcher.stop()
        await self.data.close()
        try:
            await self.client.close()
        finally:
            if self.renderer is not None:
                await self.renderer.close()
        logger.info("ZmdLogBot plugin terminated.")


def _shorten_tool_reply(text: str) -> str:
    """Cap what one tool puts into the model's context, at a line boundary.

    A cut in the middle of a row read as a row; the source line at the end
    is what tells the model the numbers are a leaderboard's, so it is put
    back after the cut.
    """

    if len(text) <= _MAX_TOOL_REPLY_CHARS:
        return text
    cut = text[:_MAX_TOOL_REPLY_CHARS]
    head, _, _ = cut.rpartition("\n")
    kept = (head or cut).rstrip()
    return facts.with_source(kept + "\n（篇幅所限，其余略；完整内容见图）")


def _positive_int(raw: str, default: int) -> int:
    """A count the model wrote as a string — 5, 前5, 五名, １０ — else the default."""

    text = unicodedata.normalize("NFKC", str(raw)).strip()
    digits = "".join(character for character in text if character.isdigit())
    if digits:
        value = int(digits[:4])
        return value if value > 0 else default
    for match in _CJK_NUMBER_RE.finditer(text):
        if not match.group(0):
            continue
        tens, ten, ones = match.groups()
        value = 0
        if ten:
            value = (_CJK_DIGITS[tens] if tens else 1) * 10
        elif tens:
            value = _CJK_DIGITS[tens]
        if ones:
            value += _CJK_DIGITS[ones]
        return value if value > 0 else default
    return default
