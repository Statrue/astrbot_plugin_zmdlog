"""What a chat is: AstrBot's ``unified_msg_origin``, and the one 隔离对话 hides.

An origin is ``platform_id:MessageType:session_id``. Everything this plugin
keeps per chat — the watch lists, the 群榜 membership, the candidate lists,
the auto-expand cooldown — is about the *group*: a watch list one member
cannot see is not a group's watch list.

AstrBot's 隔离对话 (``platform_settings.unique_session``) breaks that. With it
on, the waking stage rewrites a group message's session id to one per member
(``aiocqhttp`` gets ``<sender>_<group>``), so every member of one group
arrived with a different origin: 群榜 showed only whoever asked, and each
member kept a private watch list. The adapter's own session id survives on
``message_obj`` — the rewrite only touches ``event.session`` — and AstrBot
flags it with the ``_session_isolated`` extra, so ``main.py`` puts it back
with :func:`restore_group_origin` and nothing has to parse the rewritten id.

Records written before that fix still carry the per-member origin.
:func:`group_origin_of` maps one back, but only by stripping a member id it
is told — a watch entry's ``added_by``, a binding's user key — never by
guessing where a separator is: some platforms' own group ids contain the
separator (a Lark chat is ``oc_…``).
"""

GROUP_MARKER = ":GroupMessage:"

# How AstrBot 4.28.1 spells a member's isolated session id, keyed by platform
# type (``UNIQUE_SESSION_ID_BUILDERS`` in
# ``astrbot/core/pipeline/waking_check/stage.py``). DingTalk keeps only the
# sender, so its group cannot be recovered and it is left out.
_MEMBER_FIRST = {
    "aiocqhttp": "_",
    "slack": "_",
    "qq_official": "_",
    "qq_official_webhook": "_",
    "matrix": "_",
    "lark": "%",
}
_MEMBER_LAST = {"misskey": "_"}


def is_group_origin(origin: str) -> bool:
    """Whether an AstrBot ``unified_msg_origin`` names a group chat.

    The group type is spelled ``GroupMessage`` on every platform, so this is
    the one portable group test there is; a private chat has no members to
    put on a board.
    """

    return GROUP_MARKER in (origin or "")


def restore_group_origin(origin: str, adapter_session_id: str) -> str:
    """``origin`` with its session id put back to the adapter's own.

    Anything but a well-formed group origin with a session id to restore
    comes back unchanged.
    """

    parts = (origin or "").split(":", 2)
    if len(parts) != 3 or not adapter_session_id or not is_group_origin(origin):
        return origin
    platform_id, message_type, _ = parts
    return f"{platform_id}:{message_type}:{adapter_session_id}"


def group_origin_of(origin: str, member_key: str) -> str | None:
    """The group origin a member's isolated ``origin`` stood for, if it is one.

    ``member_key`` is the plugin's ``platform_type:sender_id`` key of the
    member who wrote the record. ``None`` when the origin is not that
    member's isolated form — including when it is already a group's.
    """

    parts = (origin or "").split(":", 2)
    platform_type, _, sender = (member_key or "").partition(":")
    if len(parts) != 3 or not sender or not is_group_origin(origin):
        return None
    platform_id, message_type, session = parts
    group = ""
    if platform_type in _MEMBER_FIRST:
        prefix = f"{sender}{_MEMBER_FIRST[platform_type]}"
        if session.startswith(prefix):
            group = session[len(prefix) :]
    elif platform_type in _MEMBER_LAST:
        suffix = f"{_MEMBER_LAST[platform_type]}{sender}"
        if session.endswith(suffix):
            group = session[: -len(suffix)]
    if not group:
        return None
    return f"{platform_id}:{message_type}:{group}"
