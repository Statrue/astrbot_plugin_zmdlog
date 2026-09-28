"""The QQ official bot, where AstrBot cannot reach: the plugin's own botpy calls.

AstrBot's built-in ``qq_official`` adapter (the WebSocket one) runs on botpy,
and AstrBot 4.28.1 can send neither a keyboard nor a markdown message of the
plugin's choosing through it; upstream PRs for both are unmerged. So the
plugin calls botpy itself, through the ``bot.api`` of the event it is
answering, and everything that does so lives in this one module — when
AstrBot grows a public button API, this is the file to delete.

Only ``qq_official`` counts. ``qq_official_webhook`` is treated like any other
platform (the webhook adapter is not recommended, and gets no buttons), and
``qq_official_v2`` is a different adapter with no botpy underneath.

What goes into a message is decided in ``core/buttons``; this module only
sends it. A send is a passive reply to the message being answered (``msg_id``)
in a group or a private chat, with a sequence number clear of the 1–10000
AstrBot draws its own from. Any exception is a failed send, reported by its
type alone — the caller then answers the way it would on any other platform.
"""

import itertools
import random
from typing import Any

from .core.buttons import ButtonMessage
from .core.logs import LogSink

PLATFORM_NAME = "qq_official"
# One reply may be sent several times with one msg_id, each with its own
# msg_seq; AstrBot's replies pick theirs at random from 1..10000.
_MSG_SEQ = itertools.count(random.randint(20_000, 40_000))
_MSG_TYPE_MARKDOWN = 2


def is_official(event: Any) -> bool:
    """Whether ``event`` came through the built-in WebSocket adapter."""

    platform = getattr(event, "get_platform_name", None)
    try:
        return callable(platform) and platform() == PLATFORM_NAME
    except Exception:
        return False


async def send_markdown(event: Any, message: ButtonMessage, *, logger: LogSink) -> bool:
    """Reply to ``event`` with ``message``; False when it did not go out.

    A group message is answered in the group, a private message in the
    private chat. Anything else the adapter carries (guild channels, guild
    direct messages) gets False, as does every exception botpy raises.
    """

    message_obj = getattr(event, "message_obj", None)
    raw = getattr(message_obj, "raw_message", None)
    group_openid = getattr(raw, "group_openid", None)
    user_openid = getattr(getattr(raw, "author", None), "user_openid", None)
    payload = {
        "msg_type": _MSG_TYPE_MARKDOWN,
        "markdown": {"content": message.markdown},
        "keyboard": message.keyboard,
        "msg_id": getattr(message_obj, "message_id", None),
        "msg_seq": next(_MSG_SEQ),
    }
    try:
        # Read at send time, never kept: the adapter owns the client.
        api = event.bot.api
        if group_openid:
            await api.post_group_message(group_openid=group_openid, **payload)
        elif user_openid:
            await api.post_c2c_message(openid=user_openid, **payload)
        else:
            return False
    except Exception as exc:
        logger.warning(
            "ZmdLogBot QQ official button message failed: %s", type(exc).__name__
        )
        return False
    return True
