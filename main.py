"""AstrBot entry point for ZmdLogBot."""

import asyncio
import time
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
# Set on the event once a tool has attached a picture, so the tools after it
# in the same turn answer in text alone.
_TOOL_IMAGE_SENT = "_zmdlog_tool_image_sent"
# What one tool may put into the model's context. The picture carries the
# detail; a wall of text past this only crowds out the conversation.
_MAX_TOOL_REPLY_CHARS = 3_000
_PLUGIN_DATA_NAME = "astrbot_plugin_zmdlog"
_BATTLE_LINK_FILTER = (
    r"https?://[^\s<>\"']+/(?:battle|share|axis)/btl_[A-Za-z0-9_-]+"
)


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
        )
        self._auto_expand_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[None]] = set()
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
    # have drawn and sends it, then returns the facts behind it as text. The
    # picture carries the numbers so the model never has to retype them.

    @filter.llm_tool(name="query_endfield_board")
    async def query_endfield_board(
        self,
        event: AstrMessageEvent,
        board: str,
        character: str = "",
        limit: str = "",
        element: str = "",
    ):
        """查询终末地某个首领榜单的公开速通记录：前几名的用时、DPS、主C、
        阵容和 battleId，该榜各职业位的角色出场率，以及出现过的阵容组合各几次；
        给了角色名则只看带这个角色的记录，并统计它最常和谁同队。
        已自动发送榜单长图，图里有完整数值，你不要复述数字，只解读。
        数据只有公开上传的成功记录，没有失败样本，出场次数和名次都不代表谁更强。
        数据全部来自 ZMDLogs 上玩家自愿上传的公开记录，不是全服统计，回答时要说明。

        Args:
            board(string): 榜单或副本关键词，例如“罗丹”“呼吼炽焰”
            character(string): 只看阵容里带这个角色的记录并统计它的同队伙伴，
                例如“提弗洛斯”，不筛选就留空
            limit(string): 列出前几名，默认 10，最多 30
            element(string): 只看主C 为该属性的记录，填 物理、灼热、寒冷、自然 或 电磁，
                不筛选就留空
        """

        return await self._run_tool(
            event,
            lambda: self.tools.board(
                board,
                character=character,
                limit=_positive_int(limit, facts.DEFAULT_ROW_LIMIT),
                element=element,
            ),
        )

    @filter.llm_tool(name="query_endfield_battle")
    async def query_endfield_battle(
        self,
        event: AstrMessageEvent,
        battle: str,
        compare_with: str = "",
    ):
        """查询终末地某一场公开战报：用时、全队与各角色 DPS 和伤害占比、
        每人的等级潜能武器精炼技能等级、主要伤害来源、BUFF 覆盖率、各类招式施放次数。
        给了第二场就改为对比同一首领的两场：用时差、DPS 差、阵容差异、
        同名角色的养成与装备差异；跨首领没有可比性。已自动发送长图，
        图里有完整数值，你不要复述数字，只解读。工具只列出记录本身和两份记录的差异，
        哪一处造成了时间差公开数据无法判定，不要替它下因果结论。
        要按名次找某一场，先用榜单工具拿到那一名的 battleId。
        数据全部来自 ZMDLogs 上玩家自愿上传的公开记录，不是全服统计，回答时要说明。

        Args:
            battle(string): battleId（形如 btl_upload_xxxx）或 ZMDLogs 战报链接
            compare_with(string): 要对比的第二场的 battleId 或链接，只看一场就留空
        """

        return await self._run_tool(
            event, lambda: self.tools.battle(battle, compare_with=compare_with)
        )

    @filter.llm_tool(name="query_endfield_character")
    async def query_endfield_character(
        self,
        event: AstrMessageEvent,
        character: str = "",
        board: str = "",
        element: str = "",
    ):
        """查询终末地某个角色在公开记录里的表现：带它的队伍在每个榜单的最好名次、
        用时、DPS 和阵容（这是队伍的成绩，任何星级的角色都能查），六星干员再附上
        DPS 分布：中位数、四分位、样本量和各榜名次；给了榜单则只看那个榜的 DPS 分布。
        已自动发送长图，图里有完整数值，你不要复述数字，只解读。
        DPS 名次只看去极值后的正常样本，正常样本不足的角色没有名次，
        记录多但分布很散时也会这样。角色名留空则回答“谁的冠军最多”：
        每个角色的队伍在全部榜单拿下的第一名、前三、前十各几个。
        这些数字来自公开速通记录，受玩家水平和配装影响，不是角色强度的判据。
        数据全部来自 ZMDLogs 上玩家自愿上传的公开记录，不是全服统计，回答时要说明。

        Args:
            character(string): 角色全名，例如“提弗洛斯”“余烬”；留空看全角色冠军榜
            board(string): 只看某个榜单，例如“罗丹”，留空则看它在所有榜单的表现
            element(string): 角色名留空时只看该属性的角色，填 物理、灼热、寒冷、自然
                或 电磁；问“物理队有什么冠军”就填 物理
        """

        return await self._run_tool(
            event, lambda: self.tools.character(character, board, element)
        )

    @filter.llm_tool(name="query_endfield_account")
    async def query_endfield_account(self, event: AstrMessageEvent, account: str):
        """查询终末地某个公开账号在各首领榜单的最好成绩：名次、用时、DPS、
        阵容和 battleId。已自动发送账号长图，图里有完整数值，你不要复述数字，只解读。
        只有把记录设为公开的玩家才查得到。
        数据全部来自 ZMDLogs 上玩家自愿上传的公开记录，不是全服统计，回答时要说明。

        Args:
            account(string): 公开昵称、accountId 或 ZMDLogs 账号主页链接
        """

        return await self._run_tool(event, lambda: self.tools.account(account))

    async def _run_tool(self, event: AstrMessageEvent, action) -> str:
        """Run one tool: send its picture, hand its facts back to the model.

        At most one picture per turn. A model answering a question often
        calls two or three tools, and three long images in a row is spam;
        the first tool that has one wins and the rest answer in text.
        """

        try:
            answer = await action()
        except ZmdLogsAPIError as exc:
            logger.warning("ZmdLogBot tool API request failed: %s", exc.code)
            return messages.UPSTREAM_UNAVAILABLE
        except ZmdLogsClientError as exc:
            logger.warning(
                "ZmdLogBot tool request failed: %s", type(exc).__name__
            )
            return messages.UPSTREAM_UNAVAILABLE
        except Exception:
            logger.exception("ZmdLogBot unexpected tool failure")
            return messages.UNEXPECTED_FAILURE
        if answer.image_path and not getattr(event, _TOOL_IMAGE_SENT, False):
            if Image is None:
                logger.warning(
                    "ZmdLogBot cannot attach a tool image on this AstrBot version."
                )
            else:
                # The upload runs in the background so the text reaches the
                # model now: its second round trip and the platform upload
                # overlap instead of queueing, and the picture still lands
                # seconds before the model finishes writing.
                setattr(event, _TOOL_IMAGE_SENT, True)
                self._spawn(self._send_tool_image(event, answer.image_path))
        return _shorten_tool_reply(answer.text)

    async def _send_tool_image(self, event: AstrMessageEvent, path: str) -> None:
        try:
            await asyncio.wait_for(
                event.send(MessageChain([Image.fromFileSystem(path)])),
                timeout=_NOTICE_SEND_TIMEOUT_SECONDS,
            )
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

        for task in tuple(self._background_tasks):
            task.cancel()
        await self.watcher.stop()
        await self.data.close()
        try:
            await self.client.close()
        finally:
            if self.renderer is not None:
                await self.renderer.close()
        logger.info("ZmdLogBot plugin terminated.")


def _shorten_tool_reply(text: str) -> str:
    """Cap what one tool puts into the model's context."""

    if len(text) <= _MAX_TOOL_REPLY_CHARS:
        return text
    return text[: _MAX_TOOL_REPLY_CHARS - 1] + "…"


def _positive_int(raw: str, default: int) -> int:
    """A count the model wrote as a string; anything odd falls back."""

    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
