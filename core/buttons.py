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
list expired or the bot restarted. 榜单's list of every dungeon is the one
exception to the first rule: its button is the dungeon's name alone, as a
user would type it, because that is the command the list stands in for.
A 角色档案 pick names the character by its name, as its sibling buttons
do, and the board by its slug after ``--榜单``.

A list gets one button per row while five fit. Only 榜单's list is longer,
and it shares the five rows as a notice does; a dungeon then shows the part
of its name after the phase (灼痛疤痕), since half a row cuts a name before
the part that tells the 1期 dungeons apart. Past twenty-five picks, the rest
have no button and are picked by quoting the list.

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

Beside the jump button, up to three command buttons open the same thing's
other pages (``_SIBLINGS``), built like a pick's: the key and the view word,
and a board's metric — a board's battle too, since 战报 reads its rank off
the board the metric names. A page the drawing showed this thing does not
have — an old upload's loadout, an account's trend before any is recorded —
gets no button. A battle's pages are the ones its picture's foot lists, by
the same names (``core/battle_views``).

A board's ranking has a keyboard of its own, three rows with the jump
button last. The first is its views: 阵容, 角色统计, 第一名战报 and
对比第一名 — the last ``对比 <slug> 我``, which compares whoever sends it,
or taps it, with first place. Both first-place buttons mean the whole
board's #1 under the page's metric, never the first row a filter left,
so like the other views they carry the metric and no filter. The second
row turns the page: 全部 and 下一页, each repeating the page's ``--角色``,
``--属性`` and metric, so the page it draws is the one this page was
filtered to (``PageTarget.record_count`` says whether a next page exists;
the last page has none). The 全部 picture has neither. One that stopped at
thirty rows and left the rest to the site gets, as its second row, the
board on the site, labelled with how many rows wait there; being the same
link as the jump button, it stands in for it rather than repeat it a row
below.

A dungeon's podiums are the one picture with no page on the site, so no
jump button; under it instead is a command button per board of the dungeon
(or the phase), the board's slug as a user would type it, in the board
list's order, with the metric the request asked for. They share the five
rows past five boards, and a board on a shared row drops the part of its
name every board shares — 危境再现's boards differ only after the dungeon's
name, 战争回响's only before their common difficulty. Past twenty-five, the
rest have no button and are typed. A ranking page offers no other board of
its dungeon: those would crowd out its own views.

A board notice carries a jump button for every battle it prints a link to,
under the label ``core/watch`` gave it (``战报 1``). In the markdown the
label takes the printed link's place, at the end of the line above it —
the line that says whose record it is — so text and button name each
battle the same way; the plain text keeps its links. The keyboard holds
five rows of five: a full merge names at most nine battles (three boards'
top three), one per row while five fit; a notice naming more than
twenty-five goes as plain text.

With callbacks on (a default-off switch; ``qq_official`` explains the
patch), a command button whose command draws a page becomes a *callback*
button: the tap goes to the bot, which answers with the page at once. It
carries the same command, not a list number, for the same reason. A
configuration command (关注, 绑定, 别名) stays a fill-in button, because
the tap handler refuses those — whatever a button carries arrives from
the tapping client, so it is read like a typed message
(``read_button_command``) and trusted no further.
"""

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin

from .battle_views import BATTLE_VIEWS
from .candidates import (
    CandidateView,
    PendingCandidates,
    code_line,
    describe_choice,
    list_heading,
)
from .matcher import MatchChoice, TargetType
from .metrics import DEFAULT_METRIC
from .outcome import PageSubject, PageTarget, SitePage
from .presentation import public_url, safe_http_url
from .routing import (
    ALL_PAGES,
    CONFIGURATION_ROUTES,
    DEFAULT_STATS_POTENTIAL,
    DEFAULT_STATS_RANGE,
    DEFAULT_TREND_RANGE,
    MAX_RANKING_ROWS,
    parse_zmdlog_payload,
    ranking_page_count,
)
from .watch import Notice

# Measured on the QQ client: fifteen characters show in full. The row number
# goes in front of the name and is not counted.
MAX_LABEL_NAME_CHARS = 15
BUTTON_HINT = "点下方按钮，或引用本条消息回复序号"
UNSUPPORTED_TIP = "请升级 QQ 后使用按钮"
JUMP_LABEL = "在 ZMDLogs 打开"
# Keyboard action types: open a link, send the data to the bot, or fill a
# command into the input box.
_ACTION_JUMP = 0
_ACTION_CALLBACK = 1
_ACTION_COMMAND = 2
_COMMAND_NAME = "zmdlog"
# What a button's data may be to be read as a command. Ours stay far under
# both; a wake prefix is a character or two.
MAX_BUTTON_DATA_CHARS = 512
_MAX_PREFIX_CHARS = 8
# What a keyboard may hold, per the platform: five rows of five buttons.
MAX_KEYBOARD_ROWS = 5
MAX_ROW_BUTTONS = 5
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
    CandidateView.WATCH_BOARD: "关注",
}
# ... and for an account target; the other views never list accounts.
_ACCOUNT_WORDS = {
    CandidateView.RANKING: "账号",
    CandidateView.TREND: "趋势",
}
# ... and for a character, whom a command names by its name, not its key.
_CHARACTER_WORDS = {
    CandidateView.CHARACTER_PROFILE: "角色档案",
    CandidateView.CHARACTER_STATS: "角色统计",
    CandidateView.CHARACTER_STANDINGS: "角色排名",
}
# ... and for a battle: its V2 pages by the words core/battle_views gives
# them, the pre-1.3.0 ones as a board's.
_BATTLE_WORDS = {
    **_BOARD_WORDS,
    **{entry.view: entry.word for entry in BATTLE_VIEWS},
}
_SUBJECT_WORDS = {
    PageSubject.ACCOUNT: _ACCOUNT_WORDS,
    PageSubject.BATTLE: _BATTLE_WORDS,
    PageSubject.CHARACTER: _CHARACTER_WORDS,
}
_BATTLE_VIEWS = frozenset(
    {
        CandidateView.BATTLE,
        CandidateView.LOADOUT,
        CandidateView.SKILLS,
        CandidateView.TIMELINE,
    }
)
# What each page with a target offers besides its jump button: the other
# views of the same thing, at most three, settled per page. A battle's
# pages are one family, the board's pages another, an account and its
# trend a third. A page about no one thing has no entry, and no button;
# nor does a dungeon's, whose buttons are its boards.
#
# A battle's V2 pages (摘要 and the 详细视图) offer exactly the others its
# foot lists — core/battle_views decides which exist, the target which this
# battle can draw. The pre-1.3.0 views below keep their old buttons until
# each is replaced; they reach the 摘要 but it does not reach them.
_SIBLINGS: dict[tuple[PageSubject, CandidateView], tuple[CandidateView, ...]] = {
    **{
        (PageSubject.BATTLE, entry.view): tuple(
            other.view for other in BATTLE_VIEWS if other is not entry
        )
        for entry in BATTLE_VIEWS
    },
    (PageSubject.BATTLE, CandidateView.LOADOUT): (
        CandidateView.BATTLE,
        CandidateView.SKILLS,
        CandidateView.TIMELINE,
    ),
    (PageSubject.BATTLE, CandidateView.SKILLS): (
        CandidateView.BATTLE,
        CandidateView.LOADOUT,
        CandidateView.TIMELINE,
    ),
    (PageSubject.BATTLE, CandidateView.TIMELINE): (
        CandidateView.BATTLE,
        CandidateView.LOADOUT,
        CandidateView.SKILLS,
    ),
    # 战报 on a board is its first place's battle, 对比 the tapper's own
    # best record against it.
    (PageSubject.BOARD, CandidateView.RANKING): (
        CandidateView.ROSTER,
        CandidateView.CHARACTER_STATS,
        CandidateView.BATTLE,
        CandidateView.COMPARE,
    ),
    (PageSubject.BOARD, CandidateView.ROSTER): (
        CandidateView.RANKING,
        CandidateView.CHARACTER_STATS,
    ),
    (PageSubject.BOARD, CandidateView.CHARACTER_STATS): (
        CandidateView.RANKING,
        CandidateView.ROSTER,
    ),
    (PageSubject.ACCOUNT, CandidateView.RANKING): (CandidateView.TREND,),
    (PageSubject.ACCOUNT, CandidateView.TREND): (CandidateView.RANKING,),
    (PageSubject.CHARACTER, CandidateView.CHARACTER_PROFILE): (
        CandidateView.CHARACTER_STATS,
        CandidateView.CHARACTER_STANDINGS,
    ),
}
# What the button to each site page says it is for.
_SITE_PAGE_LABELS = {SitePage.BINDING: "去 ZMDLogs 生成绑定码"}
# A sibling's label is its command word, except where that word alone would
# not say which page it opens.
_SIBLING_LABELS = {
    **{(PageSubject.BATTLE, entry.view): entry.label for entry in BATTLE_VIEWS},
    (PageSubject.BOARD, CandidateView.BATTLE): "第一名战报",
    (PageSubject.BOARD, CandidateView.COMPARE): "对比第一名",
    (PageSubject.ACCOUNT, CandidateView.TREND): "名次趋势",
}
# A ranking's page buttons, and the site button that stands in for them
# under a 全部 picture that left rows to the site.
ALL_ROWS_LABEL = "全部"
NEXT_PAGE_LABEL = "下一页"
SITE_REST_LABEL = "官网查看其余 {count} 条"
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


@dataclass(frozen=True, slots=True)
class ButtonCommand:
    """The zmdlog command a button carries.

    ``prefix`` is the wake prefix it was written with (``/``), so buttons in
    the answer are written the same way; ``payload`` is everything after the
    command name, as the command handler reads a typed message.
    """

    prefix: str
    payload: str


def read_button_command(data: object) -> ButtonCommand | None:
    """``data`` read as a typed ``zmdlog`` command; None when it is not one."""

    if not isinstance(data, str) or len(data) > MAX_BUTTON_DATA_CHARS:
        return None
    head, _, payload = " ".join(data.split()).partition(" ")
    prefix = head.removesuffix(_COMMAND_NAME)
    if prefix == head or len(prefix) > _MAX_PREFIX_CHARS:
        return None
    return ButtonCommand(prefix, payload)


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
    if entry.view is CandidateView.DUNGEONS:
        if target.target_type is not TargetType.DUNGEON:
            return None
        return f"{command} {target.name}"
    if entry.view is CandidateView.CHARACTER_PROFILE:
        if target.target_type is not TargetType.BOARD or not entry.profile_character:
            return None
        parts = [command, "角色档案", entry.profile_character, "--榜单", target.key]
        if entry.stats_range != DEFAULT_STATS_RANGE:
            parts += ["--范围", entry.stats_range]
        return " ".join(parts)
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
        # carried (``CPU --口径 rdps`` can list accounts), and 账号 refuses
        # every option; only 趋势 takes one, its window.
        trend = entry.view is CandidateView.TREND
        if trend and entry.stats_range != DEFAULT_TREND_RANGE:
            parts += ["--范围", entry.stats_range]
        return " ".join(parts)
    if entry.view in _BATTLE_VIEWS and entry.battle_rank != 1:
        parts.append(str(entry.battle_rank))
    if entry.view is CandidateView.COMPARE:
        if entry.compare_self:
            # Its sender, or its tapper, is the 我 it compares.
            parts.append("我")
            if entry.compare_rank != 1:
                parts.append(str(entry.compare_rank))
        elif (entry.battle_rank, entry.compare_rank) != (1, 2):
            parts += [str(entry.battle_rank), str(entry.compare_rank)]
    if entry.ranking_page is not None and entry.view is CandidateView.RANKING:
        parts += ["--页", _page_word(entry.ranking_page)]
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
    callback: bool = False,
) -> ButtonMessage | None:
    """The pick list as markdown, one command button per pick under it.

    None when no pick can have a button; the plain list is all there is then.
    The quoted-reply code stays, as the last paragraph: the old way of
    picking keeps working next to the buttons. ``callback`` makes every
    button that draws a page answer the tap itself.
    """

    picks = [
        (index, choice, data)
        for index, choice in enumerate(entry.choices, start=1)
        if (data := pick_command(entry, choice, command=command)) is not None
    ][: MAX_KEYBOARD_ROWS * MAX_ROW_BUTTONS]
    if not picks:
        return None
    shared = len(picks) > MAX_KEYBOARD_ROWS
    buttons = [
        command_button(
            str(index),
            _label(index, _button_name(choice, shared=shared)),
            data,
            callback=callback,
        )
        for index, choice, data in picks
    ]
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
    return ButtonMessage(
        markdown="\n\n".join(blocks), keyboard=_keyboard_rows(_fill_rows(buttons))
    )


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


def site_page_message(
    text: str, page: SitePage, *, web_base_url: str
) -> ButtonMessage | None:
    """``text`` as markdown, a button to ``page`` of the site under it.

    None when the site's address makes no safe link; the plain text is all
    there is then.
    """

    url = safe_http_url(urljoin(f"{web_base_url.rstrip('/')}/", page.value))
    if url is None:
        return None
    button = jump_button("open", _SITE_PAGE_LABELS[page], url)
    return ButtonMessage(markdown=escape_markdown(text), keyboard=keyboard([button]))


def notice_message(notice: Notice) -> ButtonMessage | None:
    """A board notice as markdown, a jump button per battle it prints.

    None when it prints no battle a button can open, or more than a keyboard
    holds; the plain notice, links and all, is all there is then.
    """

    links = [link for link in notice.links if safe_http_url(link.url) is not None]
    if not links or len(links) > MAX_KEYBOARD_ROWS * MAX_ROW_BUTTONS:
        return None
    labels = {link.url: link.label for link in links}
    lines: list[str] = []
    for line in notice.text.split("\n"):
        label = labels.get(line)
        if label is None:
            lines.append(escape_markdown(line))
        elif lines:
            lines[-1] += f" · {escape_markdown(label)}"
        else:
            lines.append(escape_markdown(label))
    buttons = [
        jump_button(f"battle-{number}", link.label, link.url)
        for number, link in enumerate(links, start=1)
    ]
    rows = _fill_rows(buttons)
    return ButtonMessage(markdown="\n".join(lines), keyboard=_keyboard_rows(rows))


def result_keyboard(
    target: PageTarget,
    *,
    web_base_url: str,
    command: str,
    callback: bool = False,
) -> dict[str, Any] | None:
    """The keyboard under a result picture; None when it can have no link.

    The jump button fills the first row; the page's other views of the same
    target (``_SIBLINGS``) share the second, as command buttons — callback
    buttons with ``callback``. A board's ranking puts its views first, its
    pages second and the jump last. A dungeon's podiums have no link and a
    button per board instead, and no keyboard only without boards.
    ``command`` is the prefixed command name (``/zmdlog``).
    """

    if target.subject is PageSubject.DUNGEON:
        return _board_keyboard(target, command=command, callback=callback)
    url = _jump_url(target, web_base_url=web_base_url)
    if url is None:
        return None
    siblings = [
        command_button(
            f"view-{view.value}", label, sibling_command, callback=callback
        )
        for view, label, sibling_command in _sibling_commands(target, command)
    ]
    if (target.subject, target.view) == (PageSubject.BOARD, CandidateView.RANKING):
        return _ranking_keyboard(
            target, siblings, url=url, command=command, callback=callback
        )
    rows = [[jump_button("open", JUMP_LABEL, url)]]
    if siblings:
        rows.append(siblings)
    return _keyboard_rows(rows)


def _ranking_keyboard(
    target: PageTarget,
    views: list[dict[str, Any]],
    *,
    url: str,
    command: str,
    callback: bool,
) -> dict[str, Any]:
    """A board's ranking: its views, then its pages, then the site.

    The pages are 全部 and the next one, each with the filters and the
    metric the page was drawn with; the last page has no next, and the
    全部 picture neither. A 全部 picture that left rows to the site gets
    a button there in their place, saying how many — and, being the same
    link, it is the site button too, so no third row repeats it.
    """

    rows = [views]
    if target.ranking_page == ALL_PAGES:
        rest = (target.record_count or 0) - MAX_RANKING_ROWS
        if rest > 0:
            label = SITE_REST_LABEL.format(count=rest)
            rows.append([jump_button("open", label, url)])
            return _keyboard_rows(rows)
    else:
        pages = [
            command_button(
                "page-all",
                ALL_ROWS_LABEL,
                _ranking_page_command(target, ALL_PAGES, command),
                callback=callback,
            )
        ]
        page = target.ranking_page if isinstance(target.ranking_page, int) else 1
        if target.record_count is not None and page < ranking_page_count(
            target.record_count
        ):
            pages.append(
                command_button(
                    "page-next",
                    NEXT_PAGE_LABEL,
                    _ranking_page_command(target, page + 1, command),
                    callback=callback,
                )
            )
        rows.append(pages)
    rows.append([jump_button("open", JUMP_LABEL, url)])
    return _keyboard_rows(rows)


def _ranking_page_command(target: PageTarget, page: int | str, command: str) -> str:
    """The command that draws ``page`` of ``target``'s ranking, filters kept."""

    parts = [command, _BOARD_WORDS[CandidateView.RANKING], target.key]
    parts += ["--页", _page_word(page)]
    if target.character_filter:
        parts += ["--角色", target.character_filter]
    if target.element_filter:
        parts += ["--属性", target.element_filter]
    if target.metric != DEFAULT_METRIC:
        parts += ["--口径", target.metric]
    return " ".join(parts)


def _board_keyboard(
    target: PageTarget, *, command: str, callback: bool
) -> dict[str, Any] | None:
    """A dungeon's boards, a command each in the board list's order; None if none."""

    boards = target.boards[: MAX_KEYBOARD_ROWS * MAX_ROW_BUTTONS]
    if not boards:
        return None
    names = [name for _, name in boards]
    if len(boards) > MAX_KEYBOARD_ROWS:
        names = _drop_shared_parts(names)
    options = []
    if target.metric != DEFAULT_METRIC:
        options += ["--口径", target.metric]
    buttons = [
        command_button(
            f"board-{number}",
            _fit_label(name),
            " ".join([command, slug, *options]),
            callback=callback,
        )
        for number, ((slug, _), name) in enumerate(zip(boards, names), start=1)
    ]
    return _keyboard_rows(_fill_rows(buttons))


def _drop_shared_parts(names: list[str]) -> list[str]:
    """Each name less the ``·`` part every one of them begins or ends with.

    The boards of a dungeon share its name (危境再现·罗丹) or a difficulty
    (白刃穿水·残酷); neither tells two of them apart. A name keeps at least
    one part.
    """

    parts = [[part.strip() for part in name.split("·")] for name in names]
    for end in (0, -1):
        if all(len(split) > 1 for split in parts) and (
            len({split[end] for split in parts}) == 1
        ):
            parts = [split[1:] if end == 0 else split[:-1] for split in parts]
    return ["·".join(split) for split in parts]


def _sibling_commands(
    target: PageTarget, command: str
) -> list[tuple[CandidateView, str, str]]:
    """``(view, label, command)`` of every other view ``target``'s page offers.

    A board keeps its metric: the other views of an rDPS board are its rDPS
    views, its 第一名战报 and 对比第一名 the rDPS board's first place. Its
    page and its row filters stay with the ranking: no other view is paged
    or filtered, and the ranking a sibling opens starts on its first page.
    Its statistics window and potential belong to the statistics page. A
    character is named by its name, and its window goes to its 角色统计,
    which reads the same one. A battle or an account command takes no
    option.
    """

    words = _SUBJECT_WORDS.get(target.subject, _BOARD_WORDS)
    other_metric = (
        target.subject is PageSubject.BOARD and target.metric != DEFAULT_METRIC
    )
    windowed = (
        target.subject is PageSubject.CHARACTER
        and target.stats_range != DEFAULT_STATS_RANGE
    )
    offered = []
    for view in _SIBLINGS.get((target.subject, target.view), ()):
        if view in target.unavailable:
            continue
        parts = [command, words[view], target.name or target.key]
        if target.subject is PageSubject.BOARD and view is CandidateView.COMPARE:
            # Whoever sends it, or taps it, is the 我 set against first place.
            parts.append("我")
        if other_metric:
            parts += ["--口径", target.metric]
        if windowed and view is CandidateView.CHARACTER_STATS:
            parts += ["--范围", target.stats_range]
        label = _SIBLING_LABELS.get((target.subject, view), words[view])
        offered.append((view, label, " ".join(parts)))
    return offered


def _page_word(page: int | str) -> str:
    """``--页``'s value as a user types it: the number, or 全部."""

    return "全部" if page == ALL_PAGES else str(page)


def _jump_url(target: PageTarget, *, web_base_url: str) -> str | None:
    """The ZMDLogs page of ``target``; None when no safe link can be made.

    The site's options are written only off their defaults, which are ours:
    a board page reads ``metric``, its statistics also ``range`` and
    ``potential``, a character page ``range`` and ``boss``. A battle page is
    the same whichever metric found it.
    """

    query: dict[str, str] = {}
    if target.subject is PageSubject.ACCOUNT:
        url = public_url(web_base_url, "records", target.key)
    elif target.subject is PageSubject.CHARACTER:
        url = public_url(web_base_url, "character", target.key)
        if target.stats_range != DEFAULT_STATS_RANGE:
            query["range"] = target.stats_range
        if target.boss_slug:
            query["boss"] = target.boss_slug
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


def command_button(
    button_id: str, label: str, data: str, *, callback: bool = False
) -> dict[str, Any]:
    """A button that fills ``data`` into the input box when tapped.

    With ``callback``, one whose command draws a page sends ``data`` to the
    bot instead, which answers with the page.
    """

    action = (
        _ACTION_CALLBACK if callback and _draws_a_page(data) else _ACTION_COMMAND
    )
    return _button(button_id, label, action, data)


def _draws_a_page(data: str) -> bool:
    """Whether the tap handler would run ``data``: a query, not a setting."""

    command = read_button_command(data)
    if command is None:
        return False
    try:
        route = parse_zmdlog_payload(command.payload)
    except ValueError:
        return False
    return route.kind not in CONFIGURATION_ROUTES


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

    return _keyboard_rows([[button] for button in buttons])


def _fill_rows(buttons: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """As few to a row as fit in five rows: a button is as wide as its row allows."""

    per_row = -(-len(buttons) // MAX_KEYBOARD_ROWS)
    return [
        buttons[start : start + per_row] for start in range(0, len(buttons), per_row)
    ]


def _keyboard_rows(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {"content": {"rows": [{"buttons": row} for row in rows]}}


def escape_markdown(text: str) -> str:
    """Make ``text`` read literally in markdown, whatever it contains."""

    return _MARKDOWN_SPECIAL_RE.sub(r"\\\1", text)


def _button_name(choice: MatchChoice, *, shared: bool) -> str:
    """What a pick's button calls it; a dungeon sharing a row, after its phase."""

    name = choice.target.name
    if shared and choice.target.target_type is TargetType.DUNGEON:
        return name.rpartition(" · ")[2]
    return name


def _label(index: int, name: str) -> str:
    return f"{index} {_fit_label(name)}"


def _fit_label(name: str) -> str:
    shown = " ".join(name.split())
    if len(shown) > MAX_LABEL_NAME_CHARS:
        shown = shown[: MAX_LABEL_NAME_CHARS - 1] + "…"
    return shown
