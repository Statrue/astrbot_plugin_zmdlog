"""PROTOTYPE, throwaway: lives on branch prototype/qq-official-buttons only.

Question: on the QQ official bot (AstrBot's built-in ``qq_official`` /
``qq_official_webhook`` adapter, 4.28.1), how does the candidate list look
and behave when it carries command buttons (keyboard action type 2)?

Side probes, run by ``zmdlog 按钮探测`` in a group:
  1. can an image message (msg_type 7) carry a keyboard?
  2. does markdown text with a zmdlogs.com link get rejected (URL whitelist)?
  3. does plain text (msg_type 0) with the same link get rejected?
  4. is a button label longer than 10 characters accepted?

AstrBot 4.28.1 cannot send a keyboard, so this calls botpy directly through
``event.bot.api.post_group_message``. Every failure is shown in the chat
rather than hidden, because seeing it is the point. Never merge this file.
"""

import base64
import itertools
import random
import struct
import zlib

from .core.candidates import CandidateView, PendingCandidates
from .core.matcher import MatchChoice, TargetType

_OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
# AstrBot draws its own passive-reply msg_seq from 1..10000; stay clear of it.
_SEQ = itertools.count(random.randint(20000, 40000))

_VIEW_WORDS = {
    CandidateView.RANKING: "",
    CandidateView.CHARACTER_STATS: "角色统计",
    CandidateView.ROSTER: "阵容",
    CandidateView.BATTLE: "战报",
    CandidateView.LOADOUT: "配装",
    CandidateView.SKILLS: "技能",
    CandidateView.TIMELINE: "技能轴",
    CandidateView.COMPARE: "对比",
    CandidateView.WATCH: "关注",
    CandidateView.WATCH_BOARD: "关注 榜单",
    CandidateView.TREND: "趋势",
    CandidateView.GROUP_BOARD: "群榜",
}
_BATTLE_VIEWS = frozenset(
    {
        CandidateView.BATTLE,
        CandidateView.LOADOUT,
        CandidateView.SKILLS,
        CandidateView.TIMELINE,
    }
)
_PICK_HINT = "，引用本条消息回复序号即可："
_BUTTON_HINT = "，点下方按钮，或引用本条消息回复序号："


def is_group_qq_official(event) -> bool:
    return event.get_platform_name() in _OFFICIAL_PLATFORMS and bool(
        getattr(event.message_obj, "group_id", None)
    )


def button_command(
    entry: PendingCandidates, choice: MatchChoice, command: str
) -> str:
    """The full command a button fills in: it must pin the target on its own,
    because a tap arrives as a fresh @ message that quotes nothing."""

    target = choice.target
    word = _VIEW_WORDS[entry.view]
    if target.target_type is TargetType.ACCOUNT:
        key = target.key  # accountId
        word = word or "账号"
    elif target.target_type is TargetType.BOARD:
        key = target.key  # boss slug, matches exactly one board
    else:
        # DUNGEON: its name. DUNGEON_SCOPE has no typeable key; open question.
        key = target.name
    parts = [command]
    if word:
        parts.append(word)
    parts.append(key)
    if entry.view in _BATTLE_VIEWS and entry.battle_rank != 1:
        parts.append(str(entry.battle_rank))
    if entry.view is CandidateView.COMPARE:
        parts += [str(entry.battle_rank), str(entry.compare_rank)]
    if entry.ranking_top:
        parts += ["--top", str(entry.ranking_top)]
    if entry.character_filter:
        parts += ["--角色", entry.character_filter]
    if entry.element_filter:
        parts += ["--属性", entry.element_filter]
    if entry.view in {CandidateView.CHARACTER_STATS, CandidateView.TREND}:
        if entry.stats_range != "all":
            parts += ["--范围", entry.stats_range]
    if entry.view is CandidateView.CHARACTER_STATS and entry.stats_potential != "all":
        parts += ["--潜能", entry.stats_potential]
    if entry.metric != "dps":
        parts += ["--口径", entry.metric]
    return " ".join(parts)


def _label(index: int, name: str, limit: int = 10) -> str:
    text = f"{index} {' '.join(name.split())}"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _button(button_id: str, label: str, data: str) -> dict:
    return {
        "id": button_id,
        "render_data": {"label": label, "visited_label": label, "style": 1},
        "action": {
            "type": 2,
            "permission": {"type": 2},
            "data": data,
            "unsupport_tips": "请升级 QQ 后使用按钮",
        },
    }


def _keyboard(buttons: list[dict]) -> dict:
    return {"content": {"rows": [{"buttons": [button]} for button in buttons]}}


async def _post(event, **payload):
    # Read the api at send time: the webhook adapter swaps it in at run().
    return await event.bot.api.post_group_message(
        group_openid=event.message_obj.group_id,
        msg_id=event.message_obj.message_id,
        msg_seq=next(_SEQ),
        **payload,
    )


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


async def send_candidates(
    event, text: str, entry: PendingCandidates, command: str
) -> str | None:
    """Send the candidate list as markdown with one button per candidate.

    Returns None when sent, or the error to show in the fallback text.
    """

    buttons = [
        _button(
            str(index),
            _label(index, choice.target.name),
            button_command(entry, choice, command),
        )
        for index, choice in enumerate(entry.choices, start=1)
    ]
    try:
        await _post(
            event,
            msg_type=2,
            markdown={"content": text.replace(_PICK_HINT, _BUTTON_HINT)},
            keyboard=_keyboard(buttons),
        )
    except Exception as exc:
        return _describe(exc)
    return None


def _tiny_png() -> bytes:
    width, height = 160, 48
    row = b"\x00" + bytes((250, 204, 21)) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def run_probes(event, web_base_url: str, command: str) -> str:
    """Four sends on this message's passive-reply budget (5), then a summary."""

    link = f"{web_base_url.rstrip('/')}/"
    probe_button = [_button("1", "1 探测按钮", f"{command} 榜单")]
    results: list[str] = []

    async def attempt(label: str, send) -> None:
        try:
            await send()
        except Exception as exc:
            results.append(f"{label}：❌ {_describe(exc)}")
        else:
            results.append(f"{label}：✅ 已发出")

    async def image_with_keyboard():
        media = await event.upload_group_and_c2c_image(
            base64.b64encode(_tiny_png()).decode("ascii"),
            1,
            group_openid=event.message_obj.group_id,
        )
        await _post(event, msg_type=7, media=media, keyboard=_keyboard(probe_button))

    await attempt("1 图片消息带按钮", image_with_keyboard)
    await attempt(
        "2 markdown 带链接",
        lambda: _post(
            event,
            msg_type=2,
            markdown={"content": f"探测 2：markdown 里的链接 {link}"},
        ),
    )
    await attempt(
        "3 纯文本带链接",
        lambda: _post(event, msg_type=0, content=f"探测 3：纯文本里的链接 {link}"),
    )
    await attempt(
        "4 按钮文字 15 字",
        lambda: _post(
            event,
            msg_type=2,
            markdown={"content": "探测 4：按钮文字超过 10 个字"},
            keyboard=_keyboard(
                [_button("1", "一二三四五六七八九十一二三四五", f"{command} 榜单")]
            ),
        ),
    )
    lines = ["按钮原型 · 探测结果（原型分支，勿合并）", *results]
    lines.append("❌ 的错误码见 AstrBot 日志里「[botpy] 接口请求异常」那一行。")
    return "\n".join(lines)


def _tall_png(width: int = 640, height: int = 1600) -> bytes:
    """A long yellow-to-grey strip, shaped like a rendered result page."""

    rows = []
    for y in range(height):
        shade = 250 - (y * 150 // height)
        rows.append(b"\x00" + bytes((shade, shade, 40)) * width)
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def _upload_for_raw_url(event, png: bytes) -> str:
    """Chunked-upload ``png`` and return the merge response's ``raw_url``.

    AstrBot's uploader reads that response but keeps only file_info; wrap its
    one HTTP call to keep the COS link it drops.
    """

    import tempfile
    from pathlib import Path

    from astrbot.core.platform.sources.qqofficial.qqofficial_chunked_upload import (
        QQOfficialChunkedUploader,
    )

    class _Uploader(QQOfficialChunkedUploader):
        raw_url = ""

        async def _request_json(self, method, path, body):
            response = await super()._request_json(method, path, body)
            if path.endswith("/files") and isinstance(response, dict):
                merged = response.get("data", response)
                if isinstance(merged, dict):
                    self.raw_url = str(merged.get("raw_url") or "")
            return response

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "probe.png"
        path.write_bytes(png)
        uploader = _Uploader(event.bot.api._http)
        await uploader.upload_group(
            path, 1, "probe.png", event.message_obj.group_id
        )
    if not uploader.raw_url:
        raise RuntimeError("合并响应里没有 raw_url")
    return uploader.raw_url


async def run_probes_two(event, command: str) -> str:
    """Two sends plus a summary: markdown image + keyboard, and ``enter``."""

    results: list[str] = []

    async def markdown_image_with_keyboard():
        url = await _upload_for_raw_url(event, _tall_png())
        host = url.split("/")[2] if url.count("/") >= 2 else "?"
        results.append(f"  raw_url 域名：{host}")
        await _post(
            event,
            msg_type=2,
            markdown={
                "content": (
                    "探测 5：markdown 里的图片，下面带按钮\n\n"
                    f"![img #640px #1600px]({url})"
                )
            },
            keyboard=_keyboard([_button("1", "1 查看榜单列表", f"{command} 榜单")]),
        )

    enter_button = _button("1", "点我：会不会自动发出", f"{command} 榜单")
    enter_button["action"]["enter"] = True

    for label, send in (
        ("5 markdown 图片带按钮", markdown_image_with_keyboard),
        (
            "6 群里 enter 自动发送",
            lambda: _post(
                event,
                msg_type=2,
                markdown={"content": "探测 6：点下面按钮，看是直接发出还是只填进去"},
                keyboard=_keyboard([enter_button]),
            ),
        ),
    ):
        try:
            await send()
        except Exception as exc:
            results.append(f"{label}：❌ {_describe(exc)}")
        else:
            results.append(f"{label}：✅ 已发出")
    lines = ["按钮原型 · 探测结果 2（原型分支，勿合并）", *results]
    lines.append("5 要看图片下面有没有按钮；6 要点一下按钮看是否自动发出。")
    return "\n".join(lines)
