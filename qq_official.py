"""The QQ official bot, where AstrBot cannot reach: the plugin's own botpy calls.

AstrBot's built-in ``qq_official`` adapter (the WebSocket one) runs on botpy,
and AstrBot 4.28.1 can send neither a keyboard nor a markdown message of the
plugin's choosing through it, nor receive a button tap, nor push to a group
reliably; upstream PRs for all four are unmerged. So the plugin calls botpy
itself, and everything that does so lives in this one module — when AstrBot
grows a public button API and ships the push fix, this is the file to
delete.

Only ``qq_official`` counts. ``qq_official_webhook`` is treated like any other
platform (the webhook adapter is not recommended, and gets no buttons), and
``qq_official_v2`` is a different adapter with no botpy underneath.

What goes into a message is decided in ``core/buttons``; this module only
sends it, to a ``Chat``: a group or a private chat, through the ``bot`` of
the adapter that received what is being answered. A reply answers a message
(``msg_id``) or a button tap (``event_id``), with a sequence number clear of
the 1–10000 AstrBot draws its own from. Any exception is a failed send,
reported by its type alone — the caller then answers the way it would on
any other platform.

A board notice answers nothing, and is pushed from here as well
(``chat_to_push``). AstrBot 4.28.1 skips a push to a group its adapter has seen
no message from since it started, and reports it sent all the same (AstrBot
#9831): after every restart, the notices due before anyone spoke were lost.
Sent here, a push that fails says so. Delete this path once AstrBot ships
PR #10152.

A picture with a keyboard is a markdown image, and a markdown image needs a
link. The one the platform gives out is the ``raw_url`` of a chunked upload's
merge response, which AstrBot's uploader reads and drops; ``upload_image``
runs that uploader and keeps the link off the merge call. Neither the link's
use as a markdown image nor the storage honouring a content-type override is
documented, which is why every caller falls back to the native picture.

Button taps (``install_callbacks``, behind a default-off switch) need two
things AstrBot's adapter does not do: subscribe the connection to
``INTERACTION_CREATE`` (intent ``1<<26``) and handle the event, which botpy
already parses and dispatches as ``on_interaction_create``. The patch wraps
the adapter class's constructor and, on each adapter built after it, adds
the intent and the handler — to that instance only, before its connection
opens, and only if the instance looks the way the patch expects. Anything
else is left exactly as AstrBot built it, and its buttons keep filling the
command in (``Callbacks.live`` says which a chat's connection is):

- an instance of another shape (no botpy client, no numeric intents, no way
  to acknowledge a tap) — a warning, never a guess at the connection;
- qqoffice_expand enabled — it wraps the same constructor and acknowledges
  taps itself, and a tap can be acknowledged once, so buttons would work
  one time in two. Asked at construction, when both plugins are loaded,
  not at this plugin's load, so their load order does not matter;
- an AstrBot that subscribes or handles taps itself — it wins.

A tap is acknowledged first (the platform shows 操作失败 after three
seconds without one) and answered afterwards, however long the page takes,
as if the tapper had sent the command: ``Click.sender_id`` is the tapper,
so the 我 of a ranking's 对比第一名 is whoever tapped it.
The handler on a client stays for the client's life, but only forwards to
the hook on the adapter class, which each plugin load replaces and each
unload removes: a reload re-points live connections without touching them,
and an unload leaves them quiet. The patch is put back on every load, so an
AstrBot upgraded from the WebUI is patched like the one before; a switch
change only reaches a connection opened after it.

Once AstrBot handles taps itself, delete the patch, and
``main._USER_KEY_ALIASES`` with it: the v2 adapter is what people install for
its taps, and without that there is no third-party adapter worth keying a
person on. Bindings made through it are already stored as ``qq_official``, so
moving back to the built-in adapter keeps them.

The protocol is from the official documentation and botpy (MIT); the
AGPL-licensed community patches were read and not copied.
"""

import asyncio
import functools
import importlib
import itertools
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.buttons import ButtonMessage
from .core.logs import LogSink

PLATFORM_NAME = "qq_official"
EXPAND_PLUGIN = "astrbot_plugin_qqoffice_expand"
INTERACTION_INTENT = 1 << 26
# One reply may be sent several times with one msg_id, each with its own
# msg_seq; AstrBot's replies pick theirs at random from 1..10000.
_MSG_SEQ = itertools.count(random.randint(20_000, 40_000))
_MSG_TYPE_TEXT = 0
_MSG_TYPE_MARKDOWN = 2
_MSG_TYPE_MEDIA = 7
_FILE_TYPE_IMAGE = 1
# An interaction of type 11 is a message button; 0 acknowledges it as done.
_BUTTON_TAP = 11
_ACK_DONE = 0
# The platform waits three seconds for the acknowledgement.
_ACK_TIMEOUT_SECONDS = 2.5
_ADAPTER_MODULE = "astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter"
_ADAPTER_CLASS = "QQOfficialPlatformAdapter"
# On the adapter class, the one object every load of this module sees: the
# current load's ``Callbacks``, which a patched client's handler looks up at
# each tap.
_HOOK_ATTR = "_zmdlog_callbacks"
# On a botpy client this module subscribed to taps.
_LIVE_ATTR = "_zmdlog_taps"
# On the adapter class: ``(wrapper, constructor it wraps)`` of the last
# load's wrapper. Recognised by identity, never by a mark on the function:
# functools.wraps copies a function's attributes, so another plugin's
# wrapper around ours would carry the mark too.
_WRAPPER_ATTR = "_zmdlog_constructor"
# AstrBot names a chat ``<platform id>:<message type>:<session id>``, and a
# group's session id is its openid, a private chat's the user's.
_GROUP_ORIGIN = "{platform}:GroupMessage:{openid}"
_PRIVATE_ORIGIN = "{platform}:FriendMessage:{openid}"
_GROUP_MESSAGE = "GroupMessage"
_FRIEND_MESSAGE = "FriendMessage"
# On the adapter: the scene each session id was last seen in, forgotten on
# restart. A guild channel's origin reads like a group's; this tells them
# apart.
_SCENES_ATTR = "_session_scene"
_CHANNEL_SCENE = "channel"


@dataclass(frozen=True, slots=True)
class Chat:
    """Where a message goes, and what it answers, if anything.

    ``bot`` is the adapter's botpy client; its ``api`` is read at send time,
    never kept, because the adapter owns it. One of ``group_openid`` and
    ``user_openid`` names the chat; a guild channel has neither and cannot
    be answered here. A chat with neither ``msg_id`` nor ``event_id`` is
    pushed to.
    """

    bot: Any
    group_openid: str | None = None
    user_openid: str | None = None
    msg_id: str | None = None
    event_id: str | None = None


@dataclass(frozen=True, slots=True)
class Click:
    """A tapped callback button: what it carries, and who tapped it where.

    ``origin`` names the chat the way AstrBot names it for a message from
    there, and ``sender_id`` is the tapper's openid, the id a message from
    them carries; neither is tied to the message the button hangs under.
    """

    chat: Chat
    data: str
    origin: str
    sender_id: str


@dataclass(frozen=True, slots=True)
class Upload:
    """A PNG stored with the platform: ``media`` for a native picture
    message, ``raw_url`` its link when the merge response gave one."""

    media: Any
    raw_url: str | None


def is_official(event: Any) -> bool:
    """Whether ``event`` came through the built-in WebSocket adapter."""

    platform = getattr(event, "get_platform_name", None)
    try:
        return callable(platform) and platform() == PLATFORM_NAME
    except Exception:
        return False


def chat_of(event: Any) -> Chat:
    """The chat ``event`` came from, a reply answering its message."""

    message_obj = getattr(event, "message_obj", None)
    raw = getattr(message_obj, "raw_message", None)
    return Chat(
        bot=getattr(event, "bot", None),
        group_openid=getattr(raw, "group_openid", None),
        user_openid=getattr(getattr(raw, "author", None), "user_openid", None),
        msg_id=getattr(message_obj, "message_id", None),
    )


def chat_to_push(context: Any, origin: str) -> Chat | None:
    """The chat ``origin`` names, when a push there is this module's to send.

    It is when ``context`` — AstrBot's, which knows each adapter instance by
    the id an origin starts with — has the built-in WebSocket adapter under
    that id. None leaves the push to AstrBot: another platform, an id no
    instance has, an instance without a botpy client, and an origin naming
    neither a group nor a private chat, a guild channel included. The
    adapter's memory of scenes tells a channel from a group; after a
    restart it remembers none, and the chat is taken for a group — a
    channel's push then fails, and is retried, until someone speaks there.
    """

    parts = origin.split(":", 2)
    if len(parts) != 3:
        return None
    platform_id, message_type, openid = parts
    if message_type == _GROUP_MESSAGE:
        # As AstrBot's own push reads it: a group origin written under
        # 隔离对话 before ``core/origins`` is ``<member>_<group>``.
        openid = openid.rsplit("_", 1)[-1]
    if not openid:
        return None
    lookup = getattr(context, "get_platform_inst", None)
    try:
        platform = lookup(platform_id) if callable(lookup) else None
        if platform is None or platform.meta().name != PLATFORM_NAME:
            return None
    except Exception:
        return None
    bot = getattr(platform, "client", None)
    if bot is None:
        return None
    scenes = getattr(platform, _SCENES_ATTR, None)
    scene = scenes.get(openid) if isinstance(scenes, Mapping) else None
    if message_type == _GROUP_MESSAGE and scene != _CHANNEL_SCENE:
        return Chat(bot=bot, group_openid=openid)
    if message_type == _FRIEND_MESSAGE:
        return Chat(bot=bot, user_openid=openid)
    return None


async def send_markdown(chat: Chat, message: ButtonMessage, *, logger: LogSink) -> bool:
    """Send ``message`` into ``chat``; False when it did not go out."""

    payload = {
        "msg_type": _MSG_TYPE_MARKDOWN,
        "markdown": {"content": message.markdown},
        "keyboard": message.keyboard,
    }
    return await _post(chat, payload, what="button message", logger=logger)


async def send_text(
    chat: Chat, text: str, *, logger: LogSink, what: str = "text reply"
) -> bool:
    """Send ``text`` into ``chat`` as plain text; False when it did not go out.

    ``what`` names it in the warning a failure logs.
    """

    payload = {"msg_type": _MSG_TYPE_TEXT, "content": text}
    return await _post(chat, payload, what=what, logger=logger)


async def send_image(chat: Chat, path: str, *, logger: LogSink) -> bool:
    """Send the PNG at ``path`` into ``chat`` as a native picture."""

    upload = await upload_image(chat, path, logger=logger)
    if upload is None:
        return False
    payload = {"msg_type": _MSG_TYPE_MEDIA, "media": upload.media}
    return await _post(chat, payload, what="picture", logger=logger)


async def _post(
    chat: Chat, payload: dict[str, Any], *, what: str, logger: LogSink
) -> bool:
    """Send one message into ``chat``; False when it did not go out.

    A group message is answered in the group, a private one in the private
    chat; a chat that is neither gets False, as does every exception botpy
    raises.
    """

    reply = dict(payload, msg_seq=next(_MSG_SEQ))
    if chat.event_id is not None:
        reply["event_id"] = chat.event_id
    elif chat.msg_id is not None:
        reply["msg_id"] = chat.msg_id
    try:
        api = chat.bot.api
        if chat.group_openid:
            await api.post_group_message(group_openid=chat.group_openid, **reply)
        elif chat.user_openid:
            await api.post_c2c_message(openid=chat.user_openid, **reply)
        else:
            return False
    except Exception as exc:
        logger.warning(
            "ZmdLogBot QQ official %s failed: %s", what, type(exc).__name__
        )
        return False
    return True


async def upload_image(chat: Chat, path: str, *, logger: LogSink) -> Upload | None:
    """Upload the PNG at ``path`` into ``chat``.

    None when it cannot be stored: a chat that is neither a group nor a
    private one, an AstrBot without the chunked uploader, or any failure on
    the way. The link is never logged: it is signed, and anyone holding it
    can fetch the picture.
    """

    if not chat.group_openid and not chat.user_openid:
        return None
    links: list[object] = []
    try:
        from astrbot.core.platform.sources.qqofficial.qqofficial_chunked_upload import (
            QQOfficialChunkedUploader,
        )

        uploader = QQOfficialChunkedUploader(chat.bot.api._http)
        request_json = uploader._request_json

        async def keep_link(method: str, api_path: str, body: Mapping[str, Any]):
            response = await request_json(method, api_path, body)
            if api_path.endswith("/files") and isinstance(response, Mapping):
                merged = response.get("data", response)
                if isinstance(merged, Mapping):
                    links.append(merged.get("raw_url"))
            return response

        uploader._request_json = keep_link
        file = Path(path)
        if chat.group_openid:
            media = await uploader.upload_group(
                file, _FILE_TYPE_IMAGE, file.name, chat.group_openid
            )
        else:
            media = await uploader.upload_c2c(
                file, _FILE_TYPE_IMAGE, file.name, chat.user_openid
            )
    except Exception as exc:
        logger.warning(
            "ZmdLogBot QQ official image upload failed: %s", type(exc).__name__
        )
        return None
    link = links[-1] if links else None
    return Upload(media, link if isinstance(link, str) and link else None)


# --- button taps ------------------------------------------------------------


class Callbacks:
    """The button-tap patch one plugin load put in place.

    ``live`` says whether a chat's connection takes taps through it, and so
    whether its buttons may be callback buttons; ``remove`` takes it off.
    """

    def __init__(
        self,
        adapter_cls: type,
        on_click: Callable[[Click], Awaitable[None]],
        *,
        accepts: Callable[[str], bool],
        expand_active: Callable[[], bool],
        logger: LogSink,
    ) -> None:
        self._adapter_cls = adapter_cls
        self._on_click = on_click
        self._accepts = accepts
        self._expand_active = expand_active
        self._logger = logger
        self._wrapped: tuple[Callable[..., None], Callable[..., None]] | None = None

    def live(self, chat: Chat) -> bool:
        """Whether a tap on a button in ``chat`` reaches this load."""

        return (
            getattr(self._adapter_cls, _HOOK_ATTR, None) is self
            and getattr(chat.bot, _LIVE_ATTR, False) is True
        )

    def remove(self) -> None:
        """Take this load's patch off, and leave another load's in place.

        Adapters built since keep their subscription, and their taps go
        unanswered until a load puts a hook back. A constructor another
        plugin wrapped on top of ours cannot be restored; the wrapper then
        stays, and does nothing without a hook.
        """

        cls = self._adapter_cls
        if cls.__dict__.get(_HOOK_ATTR) is self:
            delattr(cls, _HOOK_ATTR)
        if self._wrapped is None or cls.__dict__.get(_WRAPPER_ATTR) is not (
            self._wrapped
        ):
            return
        wrapper, original = self._wrapped
        if cls.__dict__.get("__init__") is wrapper:
            cls.__init__ = original
            delattr(cls, _WRAPPER_ATTR)

    def _install(self) -> bool:
        cls = self._adapter_cls
        constructor = cls.__dict__.get("__init__")
        if not callable(constructor):
            return False
        # An earlier load's wrapper, left on (its unload never ran), is
        # replaced rather than wrapped again.
        recorded = cls.__dict__.get(_WRAPPER_ATTR)
        original = (
            recorded[1]
            if isinstance(recorded, tuple) and recorded[0] is constructor
            else constructor
        )

        @functools.wraps(original)
        def __init__(adapter: Any, *args: Any, **kwargs: Any) -> None:
            original(adapter, *args, **kwargs)
            hook = getattr(cls, _HOOK_ATTR, None)
            if hook is not None:
                hook._subscribe(adapter)

        self._wrapped = (__init__, original)
        setattr(cls, _WRAPPER_ATTR, self._wrapped)
        cls.__init__ = __init__
        setattr(cls, _HOOK_ATTR, self)
        return True

    def _subscribe(self, adapter: Any) -> None:
        """Subscribe a just-built adapter's connection to taps, if it is safe."""

        try:
            client = getattr(adapter, "client", None)
            if getattr(client, _LIVE_ATTR, False) is True:
                return
            if self._expand_active():
                self._logger.warning(
                    "ZmdLogBot: qqoffice_expand is enabled, so button callbacks "
                    "stay off and buttons fill the command in; see the README."
                )
                return
            intents = getattr(client, "intents", None)
            api = getattr(client, "api", None)
            if (
                type(intents) is not int
                or not callable(getattr(api, "on_interaction_result", None))
            ):
                self._logger.warning(
                    "ZmdLogBot: the qq_official adapter is not built the way the "
                    "callback patch expects; buttons fill the command in."
                )
                return
            if (
                intents & INTERACTION_INTENT
                or getattr(client, "on_interaction_create", None) is not None
            ):
                self._logger.info(
                    "ZmdLogBot: this AstrBot takes button callbacks itself; the "
                    "plugin's stay off."
                )
                return
            client.on_interaction_create = functools.partial(
                _forward_tap, self._adapter_cls, adapter
            )
            client.intents = intents | INTERACTION_INTENT
            setattr(client, _LIVE_ATTR, True)
        except Exception as exc:
            self._logger.warning(
                "ZmdLogBot could not patch the qq_official adapter: %s",
                type(exc).__name__,
            )

    async def _answer(self, adapter: Any, interaction: Any) -> None:
        """Acknowledge a tap that carries one of ours, then answer it."""

        click = _read_click(adapter, interaction)
        if click is None or not self._accepts(click.data):
            return
        try:
            await asyncio.wait_for(
                click.chat.bot.api.on_interaction_result(interaction.id, _ACK_DONE),
                timeout=_ACK_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # The tapper may see 操作失败; the page is still worth sending.
            self._logger.warning(
                "ZmdLogBot could not acknowledge a button callback: %s",
                type(exc).__name__,
            )
        try:
            await self._on_click(click)
        except Exception as exc:
            self._logger.warning(
                "ZmdLogBot button callback failed: %s", type(exc).__name__
            )


def install_callbacks(
    on_click: Callable[[Click], Awaitable[None]],
    *,
    accepts: Callable[[str], bool],
    expand_active: Callable[[], bool],
    logger: LogSink,
    adapter_cls: type | None = None,
) -> Callbacks | None:
    """Patch the built-in adapter so a tap reaches ``on_click``.

    ``accepts`` says which button data are this plugin's — only those are
    acknowledged — and ``expand_active`` whether qqoffice_expand is enabled,
    asked as each adapter is built. ``adapter_cls`` stands in for AstrBot's
    class in tests. None, with a warning, when botpy or the class is not
    the one the patch was written against; nothing is patched then.
    """

    cls = adapter_cls if adapter_cls is not None else _builtin_adapter_class()
    if cls is None or not _botpy_takes_taps():
        logger.warning(
            "ZmdLogBot: this AstrBot's qq_official adapter cannot take the "
            "callback patch; buttons fill the command in."
        )
        return None
    callbacks = Callbacks(
        cls, on_click, accepts=accepts, expand_active=expand_active, logger=logger
    )
    if not callbacks._install():
        logger.warning(
            "ZmdLogBot: the qq_official adapter has no constructor to patch; "
            "buttons fill the command in."
        )
        return None
    return callbacks


async def _forward_tap(adapter_cls: type, adapter: Any, interaction: Any) -> None:
    """A patched client's ``on_interaction_create``: hand the tap to the
    current load, if there is one. Kept this thin because it outlives the
    load that installed it."""

    hook = getattr(adapter_cls, _HOOK_ATTR, None)
    if hook is not None:
        await hook._answer(adapter, interaction)


def _read_click(adapter: Any, interaction: Any) -> Click | None:
    """The button tap ``interaction`` is; None for anything else."""

    try:
        if getattr(interaction, "type", None) != _BUTTON_TAP:
            return None
        data = interaction.data.resolved.button_data
        event_id = interaction.event_id
        platform_id = adapter.meta().id
        client = adapter.client
    except Exception:
        return None
    if not isinstance(data, str) or not isinstance(event_id, str):
        return None
    group = getattr(interaction, "group_openid", None)
    member = getattr(interaction, "group_member_openid", None)
    user = getattr(interaction, "user_openid", None)
    if group and member:
        chat = Chat(bot=client, group_openid=group, event_id=event_id)
        origin = _GROUP_ORIGIN.format(platform=platform_id, openid=group)
        return Click(chat, data, origin, member)
    if user:
        chat = Chat(bot=client, user_openid=user, event_id=event_id)
        origin = _PRIVATE_ORIGIN.format(platform=platform_id, openid=user)
        return Click(chat, data, origin, user)
    return None


def _builtin_adapter_class() -> type | None:
    try:
        module = importlib.import_module(_ADAPTER_MODULE)
    except Exception:
        return None
    cls = getattr(module, _ADAPTER_CLASS, None)
    return cls if isinstance(cls, type) else None


def _botpy_takes_taps() -> bool:
    """Whether botpy parses taps, flags their intent and acknowledges them."""

    try:
        from botpy.api import BotAPI
        from botpy.connection import ConnectionState
        from botpy.flags import Intents
    except Exception:
        return False
    return (
        Intents.VALID_FLAGS.get("interaction") == INTERACTION_INTENT
        and callable(getattr(ConnectionState, "parse_interaction_create", None))
        and callable(getattr(BotAPI, "on_interaction_result", None))
    )
