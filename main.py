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

from . import qq_official
from .core import facts, messages
from .core.account_binding import AccountBinding
from .core.alias_admin import AliasAdmin
from .core.buttons import (
    notice_message,
    pick_list_message,
    read_button_command,
    result_image_message,
    result_keyboard,
    site_page_message,
)
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
from .core.origins import restore_group_origin
from .core.outcome import Outcome
from .core.persistence import load_json, save_json
from .core.queries import (
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
    read_plugin_version,
    read_png_dimensions,
)
from .core.routing import (
    ALIAS_ROUTES,
    BINDING_ROUTES,
    CONFIGURATION_ROUTES,
    RouteParseError,
    RouteRequest,
    parse_zmdlog_payload,
)
from .core.settings import load_settings
from .core.toolbox import ToolService
from .core.watch import Notice

_NO_RESULT = "本次查询未产生结果。"
_UNPARSABLE = "指令参数无法解析，请检查后重试。"
_NOTICE_SEND_TIMEOUT_SECONDS = 30.0
# A tool picture waits for a streamed reply to finish going out. QQ official
# keeps one send buffer per event: a send during the stream replaced the
# model's text with the picture, and the stream's closing flush sent the
# picture a second time. The agent is done when the wait starts, so only that
# flush remains; a stream still open past this drops the picture.
_STREAMED_REPLY_WAIT_SECONDS = 30.0
_STREAMED_REPLY_POLL_SECONDS = 0.2
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
_USER_KEY_ALIASES = {"qq_official_v2": qq_official.PLATFORM_NAME}
_BATTLE_LINK_FILTER = (
    r"https?://[^\s<>\"']+/(?:battle|share|axis)/btl_[A-Za-z0-9_-]+"
)


def _user_agent() -> str:
    """``astrbot_plugin_zmdlog/<version>``; the bare name if the manifest is odd.

    Every request the plugin makes carries it, so the site it reads can see
    which version is calling and ask for a change on a specific one.
    """

    try:
        version = read_plugin_version(Path(__file__).parent / "metadata.yaml")
    except Exception:  # pragma: no cover - a broken manifest must not stop load
        return "astrbot_plugin_zmdlog"
    return f"astrbot_plugin_zmdlog/{version}"


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
            user_agent=_user_agent(),
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
        self.bindings = AccountBinding(
            client=self.client,
            settings=settings,
            data_dir=self.data_dir,
            logger=logger,
        )
        self.queries = QueryService(
            client=self.client,
            data=self.data,
            renderer=self._require_renderer,
            candidates=self.candidates,
            board_matcher=board_matcher,
            trend=self.data.rank_trend,
            settings=settings,
            logger=logger,
            bindings=self.bindings,
        )
        self.tools = ToolService(
            client=self.client,
            data=self.data,
            renderer=self._require_renderer,
            board_matcher=board_matcher,
            settings=settings,
            logger=logger,
            trend=self.data.rank_trend,
        )
        self._auto_expand_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._callbacks: qq_official.Callbacks | None = None
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
        self._install_callbacks()

    def _install_callbacks(self) -> None:
        """Patch the QQ official adapter for button taps, when switched on.

        On every load, like the watcher: a patch left off by one load is
        never missing from the next, and an AstrBot upgraded from the WebUI
        is patched like the one before it. ``qq_official`` explains what the
        patch does and when it stands aside.
        """

        self._remove_callbacks()
        settings = self.settings
        if not settings.qq_official_callbacks or settings.disable_qq_official_buttons:
            return
        self._callbacks = qq_official.install_callbacks(
            self._answer_click,
            accepts=lambda data: read_button_command(data) is not None,
            expand_active=self._expand_enabled,
            logger=logger,
        )

    def _remove_callbacks(self) -> None:
        if self._callbacks is not None:
            self._callbacks.remove()
            self._callbacks = None

    def _expand_enabled(self) -> bool:
        """Whether qqoffice_expand is loaded and on: it patches the same
        adapter and answers the same taps."""

        try:
            stars = tuple(self.context.get_all_stars() or ())
        except Exception:
            return False
        return any(
            getattr(star, "activated", False)
            and qq_official.EXPAND_PLUGIN
            in (getattr(star, "name", None), getattr(star, "root_dir_name", None))
            for star in stars
        )

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

        route = _parse_route(payload)
        if isinstance(route, str):
            yield self._text_result(event, route)
            return
        if route.kind in CONFIGURATION_ROUTES:
            # These reply in text and never reach _dispatch, so they need their
            # own guard: an unexpected error must not surface as a traceback.
            reply: Outcome | None = None
            try:
                if route.kind in ALIAS_ROUTES:
                    message = await self.alias_admin.handle(
                        route, is_admin=self._event_is_admin(event)
                    )
                elif route.kind in BINDING_ROUTES:
                    reply = await self.bindings.handle_route(
                        route,
                        origin=self._event_origin(event),
                        requester_key=self._event_user_key(event),
                        command=self._command_prefix(event) + "zmdlog",
                    )
                else:
                    reply = await self.watcher.handle_route(
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
            if reply is not None:
                # 关注 can post a pick list, and 绑定 send the user to the
                # site for a code: both get buttons like any other reply.
                async for result in self._reply(event, reply):
                    yield result
                return
            yield self._text_result(event, message)
            return
        outcome, _ = await self._run_guarded(
            lambda: self.queries.dispatch(
                route,
                command_prefix=self._command_prefix(event),
                origin=self._event_origin(event),
                requester_key=self._event_user_key(event),
                official=self._answers_as_official(event),
            ),
            api_error_message=lambda exc: api_error_message(route, exc),
            failure_label="command",
        )
        async for result in self._reply(event, outcome):
            yield result

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
        async for result in self._reply(event, outcome):
            yield result

    async def _run_guarded(
        self,
        action: Callable[[], Awaitable[Outcome]],
        *,
        api_error_message: Callable[[ZmdLogsAPIError], str],
        failure_label: str,
    ) -> tuple[Outcome, bool]:
        """Run one query action and turn every failure into a short reply.

        This is the single error ladder behind the command, the quoted
        candidate pick, the auto-expand handler and the button tap, so they
        can never drift apart again. The flag says whether the failure was
        transient, i.e. a retry could plausibly succeed; only the auto-expand
        cooldown reads it.
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

    async def _reply(self, event: AstrMessageEvent, outcome: Outcome):
        """Answer a command with ``outcome``, buttons and all where they fit.

        On the QQ official bot, whatever ``_send_with_buttons`` can send goes
        out by the plugin's own hand, and the event is then stopped, so
        AstrBot neither sends anything more nor asks the model. Everywhere
        else, and whenever that cannot be sent, the reply is the one it has
        always been.
        """

        if self._answers_as_official(event) and await self._send_with_buttons(
            qq_official.chat_of(event),
            outcome,
            command=self._command_prefix(event) + "zmdlog",
        ):
            event.stop_event()
            return
        yield self._outcome_result(event, outcome)

    def _answers_as_official(self, event: AstrMessageEvent) -> bool:
        """Whether ``event`` is answered the QQ official bot's way.

        Not merely whether it came from that bot: the switch turns all of it
        off, and the bot then answers the way it did before any of it.
        """

        return not self.settings.disable_qq_official_buttons and (
            qq_official.is_official(event)
        )

    async def _send_with_buttons(
        self, chat: qq_official.Chat, outcome: Outcome, *, command: str
    ) -> bool:
        """Send ``outcome`` into a QQ official ``chat`` with its buttons.

        A pick list goes out as markdown with a button per pick, a picture
        about one thing as a markdown image with a button to that thing's
        ZMDLogs page (a dungeon's podiums, to each of its boards' rankings),
        and a text that sends the reader to the site with a button to the
        page it names. Where the chat's connection takes taps,
        a button that draws a page answers the tap itself. False when
        ``outcome`` has no buttons or they could not be sent. ``command`` is
        the prefixed command name the buttons write.
        """

        callback = self._callbacks is not None and self._callbacks.live(chat)
        if outcome.candidates is not None and await self._send_pick_buttons(
            chat, outcome, command=command, callback=callback
        ):
            return True
        if outcome.target is not None and await self._send_result_image(
            chat, outcome, command=command, callback=callback
        ):
            return True
        return outcome.site_page is not None and await self._send_site_page(
            chat, outcome
        )

    async def _send_pick_buttons(
        self,
        chat: qq_official.Chat,
        outcome: Outcome,
        *,
        command: str,
        callback: bool,
    ) -> bool:
        """Send ``outcome``'s pick list with its buttons; False if not sent."""

        message = pick_list_message(
            outcome.candidates,
            command=command,
            ttl_seconds=self.candidates.ttl_seconds,
            note=outcome.candidate_note,
            callback=callback,
        )
        if message is None:
            return False
        return await qq_official.send_markdown(chat, message, logger=logger)

    async def _send_result_image(
        self,
        chat: qq_official.Chat,
        outcome: Outcome,
        *,
        command: str,
        callback: bool,
    ) -> bool:
        """Send ``outcome``'s picture as markdown with its buttons under it.

        False when it did not go out, and the native picture is due: the
        page carries no scale, its target no keyboard (no safe link, or a
        dungeon no board), or the upload or the send failed.
        """

        if outcome.image_scale is None:
            return False
        keyboard = result_keyboard(
            outcome.target,
            web_base_url=self.web_base_url,
            command=command,
            callback=callback,
        )
        if keyboard is None:
            return False
        try:
            size = read_png_dimensions(Path(outcome.image_path))
        except RenderError as exc:
            logger.warning(
                "ZmdLogBot cannot size a result image: %s", type(exc).__name__
            )
            return False
        upload = await qq_official.upload_image(
            chat, outcome.image_path, logger=logger
        )
        if upload is None:
            return False
        if upload.raw_url is None:
            logger.warning("ZmdLogBot QQ official image upload returned no link.")
            return False
        message = result_image_message(
            upload.raw_url, size=size, scale=outcome.image_scale, keyboard=keyboard
        )
        if message is None:
            logger.warning("ZmdLogBot QQ official image link is unusable.")
            return False
        return await qq_official.send_markdown(chat, message, logger=logger)

    async def _send_site_page(
        self, chat: qq_official.Chat, outcome: Outcome
    ) -> bool:
        """Send ``outcome``'s text with a button to the site page it names."""

        if outcome.message is None:
            return False
        message = site_page_message(
            outcome.message, outcome.site_page, web_base_url=self.web_base_url
        )
        if message is None:
            return False
        return await qq_official.send_markdown(chat, message, logger=logger)

    async def _answer_click(self, click: qq_official.Click) -> None:
        """Answer a tapped callback button with what its command draws.

        The button carries a whole command, read the way a typed one is, and
        only a query runs: the data is whatever the tapping client sends, so
        a command that would add a watch, bind an account or edit an alias
        is refused. Errors take the typed command's ladder and wording. The
        answer goes to the chat tapped in, as a reply to the tap; without
        buttons, as plain text or a native picture.
        """

        request = read_button_command(click.data)
        if request is None:
            return
        route = _parse_route(request.payload)
        if isinstance(route, str):
            outcome = Outcome(message=route)
        elif route.kind in CONFIGURATION_ROUTES:
            logger.warning("ZmdLogBot refused a button callback that is no query.")
            return
        else:
            outcome, _ = await self._run_guarded(
                lambda: self.queries.dispatch(
                    route,
                    command_prefix=request.prefix,
                    origin=click.origin,
                    requester_key=_user_key(
                        qq_official.PLATFORM_NAME, click.sender_id
                    ),
                    official=True,
                ),
                api_error_message=lambda exc: api_error_message(route, exc),
                failure_label="callback",
            )
        chat = click.chat
        if await self._send_with_buttons(
            chat, outcome, command=request.prefix + "zmdlog"
        ):
            return
        if outcome.image_path is not None:
            await qq_official.send_image(chat, outcome.image_path, logger=logger)
        else:
            await qq_official.send_text(
                chat, outcome.message or _NO_RESULT, logger=logger
            )

    def _outcome_result(self, event: AstrMessageEvent, outcome: Outcome):
        if outcome.image_path is not None:
            return event.image_result(outcome.image_path)
        return self._text_result(event, outcome.message or _NO_RESULT)

    def _text_result(self, event: AstrMessageEvent, text: str):
        """A text reply; on the QQ official bot, one sent as plain text.

        That adapter sends markdown unless told otherwise, so a nickname's
        ``*`` or ``_`` set the reply in bold or italic. Of the replies, only
        the ones sent with buttons are markdown: they escape what they
        print, and the picture prints no text.
        """

        result = event.plain_result(text)
        use_markdown = getattr(result, "use_markdown", None)
        # Older AstrBot releases cannot say so; the reply still goes out.
        if use_markdown is not None and self._answers_as_official(event):
            use_markdown(False)
        return result

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
            yield self._text_result(event, "这份候选列表已过期或序号无效，请重新查询。")
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
            yield self._text_result(event, message)
            return

        outcome, _ = await self._run_guarded(
            lambda: self.queries.render_pick(
                entry,
                choice,
                requester_key=self._event_user_key(event),
                command=self._command_prefix(event) + "zmdlog",
            ),
            api_error_message=board_api_error_message,
            failure_label="candidate",
        )
        async for result in self._reply(event, outcome):
            yield result

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

    async def _send_notice(self, origin: str, notice: Notice) -> bool:
        """Deliver one rank-watch notice; the only push path in the plugin.

        The caller only advances a baseline past what this reports as sent,
        so every failure path has to answer False rather than swallow.
        That is why a notice to the QQ official bot is sent by the plugin's
        own hand: AstrBot reports some pushes there sent that it skipped
        (``qq_official`` says which, and until when). Everywhere else it is
        the notice's text alone.
        """

        chat = qq_official.chat_to_push(self.context, origin)
        if chat is not None:
            delivery = self._push_official_notice(chat, notice)
        elif Plain is not None:
            delivery = self.context.send_message(
                origin, MessageChain([Plain(notice.text)])
            )
        else:
            logger.warning(
                "ZmdLogBot cannot build a rank notice on this AstrBot version."
            )
            return False
        try:
            # One unresponsive adapter must not stall the whole cycle, and a
            # refused send reports itself by returning False rather than raising.
            delivered = await asyncio.wait_for(
                delivery, timeout=_NOTICE_SEND_TIMEOUT_SECONDS
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
                "ZmdLogBot rank notice was not delivered; the chat may be gone "
                "or closed to pushes."
            )
            return False
        return True

    async def _push_official_notice(
        self, chat: qq_official.Chat, notice: Notice
    ) -> bool:
        """Push ``notice`` with a jump button per battle it names.

        As plain text, links and all, when it names none, when the buttons
        are switched off, or when the message with them did not go out;
        False only when the plain text did not go out either. A send the
        platform took but that still raised is sent twice: told twice beats
        never told.
        """

        message = (
            None
            if self.settings.disable_qq_official_buttons
            else notice_message(notice)
        )
        if message is not None and await qq_official.send_markdown(
            chat, message, logger=logger
        ):
            return True
        return await qq_official.send_text(
            chat, notice.text, logger=logger, what="rank notice"
        )

    @staticmethod
    def _event_origin(event: AstrMessageEvent) -> str:
        """The chat an event came from; empty when the platform gives none.

        Callers decide what an empty origin means for them — the watch routes
        refuse, the candidate store keys on it, auto-expand only dedupes.

        Always the group's, even with 隔离对话 on: AstrBot then rewrites a
        group message's origin per member and flags it, and the adapter's own
        session id is put back (``core/origins`` explains why).
        """

        origin = getattr(event, "unified_msg_origin", "") or ""
        get_extra = getattr(event, "get_extra", None)
        try:
            isolated = callable(get_extra) and bool(get_extra("_session_isolated"))
        except Exception:
            isolated = False
        if not isolated:
            return origin
        message_obj = getattr(event, "message_obj", None)
        adapter_session_id = getattr(message_obj, "session_id", "") or ""
        return restore_group_origin(origin, str(adapter_session_id))

    @staticmethod
    def _event_user_key(event: AstrMessageEvent) -> str:
        """Platform-scoped sender key: whose bindings, who may remove a 关注."""

        platform = getattr(event, "get_platform_name", None)
        sender = getattr(event, "get_sender_id", None)
        try:
            platform_name = platform() if callable(platform) else ""
            sender_id = sender() if callable(sender) else ""
        except Exception:
            return ""
        return _user_key(platform_name, sender_id)

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
        metric: str = "",
    ):
        """查询终末地某个首领榜单的公开速通记录：前几名的用时、DPS、主C、阵容、
        战斗日期、落后第一几秒和 battleId，最快/中位/平均用时，各职业位的角色出场率，
        出现过的阵容组合各几次。board 填首领或副本名；填副本或期数（如“影拓丰碑4期”）
        则列出该副本下每个榜的前三；填“全部”则列出所有榜的第一名（不附图）。
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
            metric(string): 口径，默认 dps（直伤）；问“rDPS 榜”“团队贡献榜”时填
                rdps——只收录能算出 rDPS 的记录，目前很少，主C 按 rDPS 最高者算
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
                metric=metric,
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
        最大单次，暴击期望（固定本场技能与命中、只算暴击波动的期望总伤及实际偏差，
        新客户端上传的战报才有），
        每人的等级潜能、武器精炼、套装、技能等级，主要伤害来源、BUFF 覆盖率、
        各类招式施放次数和每人的开场顺序，危机合约场还有合约分数与本场词条分布。
        给了第二场就改为对比同一首领的两场：
        用时差、DPS 差、阵容差异、同名角色的养成与装备差异；跨首领没有可比性。
        只查一个对象时会自动附长图，图里有完整数值，你不要复述数字，只解读；
        这次回答里查了多个对象就不附图，不要让用户看图。工具只列出记录本身和两份记录的差异，
        哪一处造成了时间差公开数据无法判定，不要替它下因果结论；
        “大家给某角色配什么”问角色工具（zmdlogs_character_standings，view 填 档案）。
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
        metric: str = "",
        view: str = "",
    ):
        """凡是问某个角色（干员）的“第一、冠军、第一名、排第几、成绩、上了哪些榜、
        最常和谁同队、大家怎么养、配什么武器装备”，例如“别礼的第一呢”“洛茜有几个冠军”
        “莱万汀一般几潜配什么”，都用这个工具，把名字原样
        填进 character：名字是不是角色由工具判断并回答，你不要自己猜。
        查询终末地某个角色在公开记录里的表现：带它的队伍在每个榜单的最好名次、
        用时、DPS 和阵容（这是队伍的成绩，任何星级的角色都能查），最常同队的角色，
        写几个角色名则是同时带上他们的队伍在各榜的最好名次，
        六星干员再附上 DPS 分布：中位数、四分位、样本量和各榜名次；
        给了榜单则只看那个榜的 DPS 分布，角色名留空而给了榜单则是该榜每个六星的分布，
        board 填“全部”则是全部副本合计。potential 只作用于 DPS 分布。
        只写一个角色名时，文字开头总有两行摘要：公开通关记录里最常见的养成组合
        （潜能+精炼）和最常见的武器及各自占比，按全部榜单算，图里没有，可以直接说。
        问“大家怎么养、配什么武器装备、带谁、在哪些榜跑得快”时 view 填 档案：
        改附角色档案图，文字给养成组合、武器、装备、常见队友的占比和各榜通关名次
        （按带它的队伍最快通关给所有角色排的名次，不是 DPS 名次）；
        同时给了榜单就只看那个榜，并列出该榜带它的记录。
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
            character(string): 角色全名，例如“提弗洛斯”“余烬”；写几个名字
                （空格分隔）就只看同时带上他们的队伍在各榜的最好名次；留空看全角色冠军榜
                或某榜的分布
            board(string): 只看某个榜单的 DPS 分布（view=档案 时是该榜的档案），
                例如“罗丹”；“全部”为全部副本合计；留空看它在所有榜单的表现
            element(string): 角色名留空时只看该属性的角色，填 物理、灼热、寒冷、自然
                或 电磁；问“物理队有什么冠军”就填 物理
            range(string): 填 7d、14d 或 30d：冠军榜、DPS 分布、两行摘要和档案
                都只算这段时间的记录；不限时间留空
            profession(string): 角色名留空时只看该职业的角色，填 先锋、近卫、重装、
                术士（术师）、突击 或 辅助
            potential(string): DPS 分布的潜能档，填 0（零潜）、1-5（有潜能，合并）
                或留空（全部）；ZMDLogs 不按具体层数拆分
            metric(string): 口径，默认 dps（直伤）；问“rDPS”“团队贡献”时填 rdps——
                名次和分布都改看 rDPS 榜，那里只收录能算出 rDPS 的记录，目前很少
            view(string): 填 档案 看角色档案（养成、武器、装备、队友占比和各榜通关
                名次），只认一个角色名；不填是默认的名次与 DPS 分布
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
                metric=metric,
                view=view,
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
        以及名次变化（掉了几名、最好和最差名次，
        从机器人读到它上榜起记录，保留 90 天）。
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
        # pipeline is about to send instead of holding it back. A streamed
        # reply is the exception, and the picture waits it out.
        self._spawn(self._send_tool_image(event, pictures[0]))

    async def _send_tool_image(self, event: AstrMessageEvent, path: str) -> None:
        if not await _streamed_reply_delivered(event):
            logger.warning(
                "ZmdLogBot dropped a tool image: the reply was still streaming."
            )
            return
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
        self._remove_callbacks()
        await self.watcher.stop()
        await self.data.close()
        try:
            await self.client.close()
        finally:
            if self.renderer is not None:
                await self.renderer.close()
        logger.info("ZmdLogBot plugin terminated.")


def _parse_route(payload: str) -> RouteRequest | str:
    """``payload`` routed, or the short text a user gets when it cannot be."""

    try:
        return parse_zmdlog_payload(payload)
    except RouteParseError as exc:
        return str(exc)
    except ValueError:
        # Defence in depth: a parse-layer ValueError that is not a
        # RouteParseError must still answer briefly, never as a traceback.
        return _UNPARSABLE


def _user_key(platform_name: str, sender_id: str) -> str:
    """``platform:sender``, the key 关注 and 绑定 know a person by; empty
    when either part is missing.

    ``qq_official_v2`` is the official bot behind another adapter, with the
    same member_openid for a sender, so it keys as ``qq_official`` and
    switching adapters keeps every binding. The webhook adapter is left as it
    is, and the wild bot's ids are a different namespace altogether. The
    alias goes when AstrBot handles button taps itself (see ``qq_official``)."""

    if not platform_name or not sender_id:
        return ""
    platform_name = _USER_KEY_ALIASES.get(platform_name, platform_name)
    return f"{platform_name}:{sender_id}"


async def _streamed_reply_delivered(event: AstrMessageEvent) -> bool:
    """Wait out a reply the pipeline is still streaming; False if it never ends.

    No hook fires after a streamed reply has gone out. What does change is
    the event's result: AstrBot swaps ``STREAMING_RESULT`` for
    ``STREAMING_FINISH`` only once the adapter's ``send_streaming`` has
    returned. It is compared by name, like the other version-dependent
    lookups here, so a release without the enum still loads.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STREAMED_REPLY_WAIT_SECONDS
    while (
        getattr(
            getattr(event.get_result(), "result_content_type", None), "name", None
        )
        == "STREAMING_RESULT"
    ):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_STREAMED_REPLY_POLL_SECONDS)
    return True


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
