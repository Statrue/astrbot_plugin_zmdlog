"""Message buttons, as plain data: what they fill in or open, and their JSON.

The QQ official bot can hang a keyboard under a markdown message. Everything
about what goes on it is decided here, with no platform in sight; the one
module that talks to the platform (``qq_official`` at the repository root)
only sends what this builds, and when it cannot, the reply is the one every
other platform gets — the plain list, the native picture.

A pick-list button is a *command* button: a tap fills the full command into
the input box, the user sends it, and it arrives as an ordinary message. The
command names its target the way nothing else can be mistaken for — an
account by its accountId, a board by its slug — and repeats every option of
the original request that is not a default, so the page it draws is the one
the list was offering. It carries no list number: the candidate store lives
in memory for ten minutes, and a numbered button would stop working when the
list expired or the bot restarted.

A dungeon scope has no key a user could type (its key is synthetic), so a
scope pick gets no button at all rather than one that opens something else.
The matcher only ever returns a scope as a direct hit, so no list carries one
today; the rule makes the behaviour certain if that ever changes.

The list's text is sent as markdown, so everything that came from outside —
the typed query, upstream nicknames and board names — is escaped: a nickname
cannot bold itself, turn into a link, or forge list items.

A result picture about one thing (an account, a battle, a board) is a
markdown image with a *jump* button under it, opening that thing's page on
ZMDLogs; a picture can carry no keyboard of its own, the platform drops it
without a word. The image is declared at its CSS size, the capture's pixels
over the scale the renderer used: a declared shape shorter than the picture
is cropped around its middle, taking the page header and the first rows, so
a half pixel rounds the height up. The link is the platform's own storage
URL, asked to answer as ``image/png`` — by default it answers
``application/octet-stream``, which the client draws as a broken image.
"""

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from .candidates import (
    CandidateView,
    PendingCandidates,
    code_line,
    describe_choice,
    list_heading,
)
from .matcher import MatchChoice, TargetType
from .metrics import DEFAULT_METRIC
from .outcome import PageSubject, PageTarget
from .presentation import public_url, safe_http_url
from .routing import DEFAULT_STATS_POTENTIAL, DEFAULT_STATS_RANGE, DEFAULT_TREND_RANGE

# Measured on the QQ client: fifteen characters show in full. The row number
# goes in front of the name and is not counted.
MAX_LABEL_NAME_CHARS = 15
BUTTON_HINT = "点下方按钮，或引用本条消息回复序号"
UNSUPPORTED_TIP = "请升级 QQ 后使用按钮"
JUMP_LABEL = "在 ZMDLogs 打开"
# Keyboard action types: open a link, or fill a command into the input box.
_ACTION_JUMP = 0
_ACTION_COMMAND = 2
# Permission type 2: anyone in the chat may tap, not only who asked.
_EVERYONE = {"type": 2}
_STYLE_BLUE = 1

# The command word that draws each view for a board or dungeon target.
_BOARD_WORDS = {
    CandidateView.RANKING: "榜单",
    CandidateView.CHARACTER_STATS: "角色统计",
    CandidateView.ROSTER: "阵容",
    CandidateView.BATTLE: "战报",
    CandidateView.LOADOUT: "配装",
    CandidateView.SKILLS: "技能",
    CandidateView.TIMELINE: "技能轴",
    CandidateView.COMPARE: "对比",
    CandidateView.WATCH_BOARD: "关注 榜单",
    CandidateView.GROUP_BOARD: "群榜",
}
# ... and for an account target; the other views never list accounts.
_ACCOUNT_WORDS = {
    CandidateView.RANKING: "账号",
    CandidateView.TREND: "趋势",
    CandidateView.WATCH: "关注",
}
_BATTLE_VIEWS = frozenset(
    {
        CandidateView.BATTLE,
        CandidateView.LOADOUT,
        CandidateView.SKILLS,
        CandidateView.TIMELINE,
    }
)
_PNG_CONTENT_TYPE = "response-content-type=image%2Fpng"
# URL characters that cannot end a markdown image: no space, no bracket or
# parenthesis, no fragment (the content-type parameter must follow the query).
_PLAIN_URL_RE = re.compile(r"[A-Za-z0-9\-._~:/?@!$&'*+,;=%]+")
# CommonMark lets a backslash escape any ASCII punctuation, and only those.
_MARKDOWN_SPECIAL_RE = re.compile(r"([!-/:-@\[-`{-~])")


@dataclass(frozen=True, slots=True)
class ButtonMessage:
    """A markdown body and the keyboard that goes under it."""

    markdown: str
    keyboard: dict[str, Any]


def pick_command(
    entry: PendingCandidates,
    choice: MatchChoice,
    *,
    command: str,
) -> str | None:
    """The full command that draws ``choice`` the way ``entry`` asked.

    ``command`` is the prefixed command name (``/zmdlog``). None when the
    pick cannot be named by anything a user could type.
    """

    target = choice.target
    if target.target_type is TargetType.ACCOUNT:
        word = _ACCOUNT_WORDS.get(entry.view)
        key = target.key
    elif target.target_type is TargetType.BOARD:
        word = _BOARD_WORDS.get(entry.view)
        key = target.key
    elif target.target_type is TargetType.DUNGEON:
        word = _BOARD_WORDS.get(entry.view)
        key = target.name
    else:
        return None
    if word is None:
        return None
    parts = [command, word, key]
    if target.target_type is TargetType.ACCOUNT:
        # The account page is the same whatever board options the list
        # carried (``CPU --口径 rdps`` can list accounts), and 账号 / 关注
        # refuse every option; only 趋势 takes one, its window.
        trend = entry.view is CandidateView.TREND
        if trend and entry.stats_range != DEFAULT_TREND_RANGE:
            parts += ["--范围", entry.stats_range]
        return " ".join(parts)
    if entry.view in _BATTLE_VIEWS and entry.battle_rank != 1:
        parts.append(str(entry.battle_rank))
    if entry.view is CandidateView.COMPARE and (
        (entry.battle_rank, entry.compare_rank) != (1, 2)
    ):
        parts += [str(entry.battle_rank), str(entry.compare_rank)]
    if entry.ranking_top is not None:
        parts += ["--top", str(entry.ranking_top)]
    if entry.character_filter is not None:
        parts += ["--角色", entry.character_filter]
    if entry.element_filter is not None:
        parts += ["--属性", entry.element_filter]
    if entry.stats_range != DEFAULT_STATS_RANGE:
        parts += ["--范围", entry.stats_range]
    if entry.stats_potential != DEFAULT_STATS_POTENTIAL:
        parts += ["--潜能", entry.stats_potential]
    if entry.metric != DEFAULT_METRIC:
        parts += ["--口径", entry.metric]
    return " ".join(parts)


def pick_list_message(
    entry: PendingCandidates,
    *,
    command: str,
    ttl_seconds: float,
    note: str | None = None,
) -> ButtonMessage | None:
    """The pick list as markdown, one command button per pick under it.

    None when no pick can have a button; the plain list is all there is then.
    The quoted-reply code stays, as the last paragraph: the old way of
    picking keeps working next to the buttons.
    """

    buttons = []
    for index, choice in enumerate(entry.choices, start=1):
        data = pick_command(entry, choice, command=command)
        if data is not None:
            buttons.append(
                command_button(str(index), _label(index, choice.target.name), data)
            )
    if not buttons:
        return None
    items = "\n".join(
        f"{index}. {escape_markdown(describe_choice(choice))}"
        for index, choice in enumerate(entry.choices, start=1)
    )
    # Blank lines between the blocks: a line directly under the last item
    # would be read as that item's continuation and indented under it.
    blocks = [escape_markdown(list_heading(entry, hint=BUTTON_HINT)), items]
    if note:
        blocks.append(escape_markdown(note))
    blocks.append(code_line(entry, ttl_seconds=ttl_seconds))
    return ButtonMessage(markdown="\n\n".join(blocks), keyboard=keyboard(buttons))


def result_image_message(
    raw_url: str,
    *,
    size: tuple[int, int],
    scale: int,
    keyboard: dict[str, Any],
) -> ButtonMessage | None:
    """A result picture as a markdown image, ``keyboard`` under it.

    ``raw_url`` is where the platform stored the uploaded PNG, ``size`` its
    pixels and ``scale`` the device pixels per CSS pixel it was captured at.
    None when either cannot make a picture; the native one is sent then.
    """

    width, height = size
    if scale < 1 or width < scale or height < 1:
        return None
    # Written into the markdown unescaped, so only a plain web link will do:
    # a space, a bracket or a newline could end the image and start text.
    if safe_http_url(raw_url) is None or not _PLAIN_URL_RE.fullmatch(raw_url):
        return None
    # Width down, height up: the declared shape can only err taller.
    css_width = width // scale
    css_height = -(-height // scale)
    separator = "&" if "?" in raw_url else "?"
    image_url = f"{raw_url}{separator}{_PNG_CONTENT_TYPE}"
    markdown = f"![img #{css_width}px #{css_height}px]({image_url})"
    return ButtonMessage(markdown=markdown, keyboard=keyboard)


def result_keyboard(
    target: PageTarget, *, web_base_url: str
) -> dict[str, Any] | None:
    """The keyboard under a result picture; None when it can have no button."""

    url = _jump_url(target, web_base_url=web_base_url)
    if url is None:
        return None
    return keyboard([jump_button("open", JUMP_LABEL, url)])


def _jump_url(target: PageTarget, *, web_base_url: str) -> str | None:
    """The ZMDLogs page of ``target``; None when no safe link can be made.

    The site's options are written only off their defaults, which are ours:
    a board page reads ``metric``, its statistics also ``range`` and
    ``potential``. A battle page is the same whichever metric found it.
    """

    query: dict[str, str] = {}
    if target.subject is PageSubject.ACCOUNT:
        url = public_url(web_base_url, "records", target.key)
    elif target.subject is PageSubject.BATTLE:
        resource = "axis" if target.view is CandidateView.TIMELINE else "battle"
        url = public_url(web_base_url, resource, target.key)
    else:
        url = public_url(web_base_url, "boss", target.key)
        if target.metric != DEFAULT_METRIC:
            query["metric"] = target.metric
        if target.view is CandidateView.CHARACTER_STATS:
            url += "/statistics"
            if target.stats_range != DEFAULT_STATS_RANGE:
                query["range"] = target.stats_range
            if target.stats_potential != DEFAULT_STATS_POTENTIAL:
                query["potential"] = target.stats_potential
    if query:
        url += "?" + urlencode(query)
    return safe_http_url(url)


def command_button(button_id: str, label: str, data: str) -> dict[str, Any]:
    """A button that fills ``data`` into the input box when tapped."""

    return _button(button_id, label, _ACTION_COMMAND, data)


def jump_button(button_id: str, label: str, url: str) -> dict[str, Any]:
    """A button that opens ``url`` when tapped."""

    return _button(button_id, label, _ACTION_JUMP, url)


def _button(button_id: str, label: str, action: int, data: str) -> dict[str, Any]:
    return {
        "id": button_id,
        "render_data": {"label": label, "visited_label": label, "style": _STYLE_BLUE},
        "action": {
            "type": action,
            "permission": _EVERYONE,
            "data": data,
            "unsupport_tips": UNSUPPORTED_TIP,
        },
    }


def keyboard(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    """One button per row, full width, in the order given."""

    return {"content": {"rows": [{"buttons": [button]} for button in buttons]}}


def escape_markdown(text: str) -> str:
    """Make ``text`` read literally in markdown, whatever it contains."""

    return _MARKDOWN_SPECIAL_RE.sub(r"\\\1", text)


def _label(index: int, name: str) -> str:
    shown = " ".join(name.split())
    if len(shown) > MAX_LABEL_NAME_CHARS:
        shown = shown[: MAX_LABEL_NAME_CHARS - 1] + "…"
    return f"{index} {shown}"
