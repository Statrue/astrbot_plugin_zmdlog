"""Per-chat board watches: which boards' new records a chat is told about.

A chat's watch is its notice whitelist, in one of two shapes: an explicit
list of boards, or 全部榜单 — every board the site lists, including the ones
that appear later — less an exclusion list. Nothing else is kept: account
watches were removed in 1.3.0, and a file written before that has its
``accounts`` dropped when it is read (its ``boards`` stay).

Every entry records who added it and when; the adder (or an admin) is the
only one who may take it away again. Under 全部榜单 that is whoever asked
for every board: an exclusion narrows that entry, and lifting one is an
addition, open to anyone like any 关注. Nothing here identifies a player —
the stored fields are a board slug, its public names, a platform user key
and a timestamp.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from .matcher import fold_text
from .origins import group_origin_of

WATCHLIST_VERSION = 3
_BOARD_PART_RE = re.compile(r"\s*·\s*")


def board_label(dungeon_name: str, boss_name: str) -> str:
    """Name the board without repeating the segment both names share.

    Upstream dungeon and boss names overlap a lot (``影拓丰碑4期 · 山中见犼``
    plus ``山中见犼·苦难``), which reads badly in a one-line push.
    """

    parts: list[str] = []
    for part in (
        *_BOARD_PART_RE.split(dungeon_name),
        *_BOARD_PART_RE.split(boss_name),
    ):
        if part and part not in parts:
            parts.append(part)
    return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class WatchedBoard:
    """A board on a chat's list — or, under 全部榜单, off it."""

    boss_slug: str
    boss_name: str
    dungeon_name: str
    # Platform user key of whoever added the entry. Used only for the
    # "adder or admin may remove" rule, never rendered or announced.
    added_by: str
    added_at: str

    @property
    def label(self) -> str:
        return board_label(self.dungeon_name, self.boss_name)

    def removable_by(self, requester_key: str, *, is_admin: bool) -> bool:
        return _removable(self.added_by, requester_key, is_admin=is_admin)


@dataclass(frozen=True, slots=True)
class AllBoards:
    """关注 全部: who asked for every board, and when."""

    added_by: str
    added_at: str

    def removable_by(self, requester_key: str, *, is_admin: bool) -> bool:
        return _removable(self.added_by, requester_key, is_admin=is_admin)


@dataclass(frozen=True, slots=True)
class ChatWatch:
    """One chat's whitelist: ``boards``, or ``all_boards`` less ``excluded``.

    The two shapes never mix: following every board absorbs the explicit
    list, and an exclusion only exists under 全部榜单.
    """

    boards: tuple[WatchedBoard, ...] = ()
    all_boards: AllBoards | None = None
    excluded: tuple[WatchedBoard, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.all_boards is None and not self.boards

    def covers(self, boss_slug: str) -> bool:
        """Whether a new record on this board is this chat's news."""

        if self.all_boards is not None:
            return all(entry.boss_slug != boss_slug for entry in self.excluded)
        return any(entry.boss_slug == boss_slug for entry in self.boards)

    def with_board(self, board: WatchedBoard) -> tuple["ChatWatch", bool]:
        """Add ``board`` to the explicit list; the flag is False when it was there.

        A board already on the list only has its name snapshot refreshed, so
        re-adding never reorders the list nor takes the entry away from
        whoever added it first.
        """

        updated, added = _with_entry(self.boards, board)
        return replace(self, boards=updated), added

    def without_board(self, boss_slug: str) -> "ChatWatch":
        return replace(self, boards=_without_entry(self.boards, boss_slug))

    def following_all(self, *, added_by: str, added_at: str) -> "ChatWatch":
        """Every board from now on; the explicit list is absorbed.

        Asked again, nothing moves: the first adder keeps the entry and the
        exclusions stay, which ``关注 <榜单>`` lifts one by one.
        """

        if self.all_boards is not None:
            return self
        return ChatWatch(all_boards=AllBoards(added_by, added_at))

    def excluding(self, board: WatchedBoard) -> "ChatWatch":
        updated, _ = _with_entry(self.excluded, board)
        return replace(self, excluded=updated)

    def including(self, boss_slug: str) -> "ChatWatch":
        return replace(self, excluded=_without_entry(self.excluded, boss_slug))

    def removable_entirely_by(self, requester_key: str, *, is_admin: bool) -> bool:
        """Whether 取关 全部 may clear this chat: every entry must be theirs."""

        if self.all_boards is not None:
            return self.all_boards.removable_by(requester_key, is_admin=is_admin)
        return all(
            entry.removable_by(requester_key, is_admin=is_admin)
            for entry in self.boards
        )

    def resolve_board_matches(self, selector: str) -> tuple[WatchedBoard, ...]:
        """Every listed board a 序号 / slug / name fragment could mean.

        Returning the whole match set lets the caller tell "no such entry"
        from "several entries match", which are different problems for the
        user.
        """

        boards = self.boards
        stripped = selector.strip()
        if not boards or not stripped:
            return ()
        index = _list_index(stripped)
        if index is not None:
            return (boards[index - 1],) if 1 <= index <= len(boards) else ()
        if stripped.isascii() and stripped.isdecimal():
            return ()
        for board in boards:
            if board.boss_slug == stripped:
                return (board,)
        folded = fold_text(stripped)
        exact = tuple(
            board
            for board in boards
            if folded in {fold_text(board.boss_name), fold_text(board.label)}
        )
        if exact:
            return exact
        return tuple(
            board for board in boards if folded and folded in fold_text(board.label)
        )


@dataclass(frozen=True, slots=True)
class WatchList:
    """Immutable snapshot of every chat's board watch; empty chats are dropped."""

    chats: tuple[tuple[str, ChatWatch], ...] = ()

    @classmethod
    def empty(cls) -> "WatchList":
        return cls()

    def chat(self, origin: str) -> ChatWatch:
        for chat_origin, chat in self.chats:
            if chat_origin == origin:
                return chat
        return ChatWatch()

    def with_chat(self, origin: str, chat: ChatWatch) -> "WatchList":
        """``origin``'s watch replaced by ``chat``, in place; an empty one goes."""

        chats: list[tuple[str, ChatWatch]] = []
        seen = False
        for chat_origin, existing in self.chats:
            if chat_origin != origin:
                chats.append((chat_origin, existing))
                continue
            seen = True
            if not chat.is_empty:
                chats.append((origin, chat))
        if not seen and not chat.is_empty:
            chats.append((origin, chat))
        return WatchList(tuple(chats))

    def origins_by_board(
        self, catalog: Iterable[str]
    ) -> dict[str, tuple[str, ...]]:
        """The chats to tell about each board.

        ``catalog`` is the boards the site lists now: a chat under 全部榜单
        follows every one of them it has not excluded, the new ones included.
        An explicitly listed board is there whether the catalog has it or not.
        """

        origins: dict[str, list[str]] = {}
        slugs = tuple(dict.fromkeys(catalog))
        for origin, chat in self.chats:
            watched = (
                (slug for slug in slugs if chat.covers(slug))
                if chat.all_boards is not None
                else (board.boss_slug for board in chat.boards)
            )
            for slug in watched:
                bucket = origins.setdefault(slug, [])
                if origin not in bucket:
                    bucket.append(origin)
        return {slug: tuple(chat_origins) for slug, chat_origins in origins.items()}

    def watches(self, boss_slug: str) -> bool:
        """Whether any chat would be told about this board."""

        return any(chat.covers(boss_slug) for _, chat in self.chats)

    def with_group_origins(self) -> tuple["WatchList", int]:
        """File every board kept under a member's 隔离对话 origin under its group.

        Each entry moves by its own ``added_by`` (``core/origins``). A board
        two members both watched collapses to the first one seen, which keeps
        its adder, and a board moved into a group that follows every board is
        already covered; the count is how many entries moved. Only explicit
        lists predate the fix — 全部榜单 is younger — so they are all there is
        to move.
        """

        merged: dict[str, ChatWatch] = {}
        moved = 0
        for origin, chat in self.chats:
            if chat.all_boards is not None:
                merged[origin] = _merged_all(merged.get(origin), chat)
                continue
            for board in chat.boards:
                target = group_origin_of(origin, board.added_by)
                if target is not None:
                    moved += 1
                into = merged.get(target or origin, ChatWatch())
                if into.all_boards is None and not into.covers(board.boss_slug):
                    into, _ = into.with_board(board)
                merged[target or origin] = into
        if not moved:
            return self, 0
        regrouped = WatchList.empty()
        for origin, chat in merged.items():
            regrouped = regrouped.with_chat(origin, chat)
        return regrouped, moved

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": WATCHLIST_VERSION,
            "groups": {origin: _chat_payload(chat) for origin, chat in self.chats},
        }


def _merged_all(existing: ChatWatch | None, chat: ChatWatch) -> ChatWatch:
    """A 全部榜单 watch absorbs whatever list was filed under its origin first."""

    if existing is None or existing.all_boards is None:
        return chat
    return existing


def _with_entry(
    entries: tuple[WatchedBoard, ...], board: WatchedBoard
) -> tuple[tuple[WatchedBoard, ...], bool]:
    updated: list[WatchedBoard] = []
    added = True
    for existing in entries:
        if existing.boss_slug == board.boss_slug:
            added = False
            updated.append(
                replace(
                    existing,
                    boss_name=board.boss_name,
                    dungeon_name=board.dungeon_name,
                )
            )
        else:
            updated.append(existing)
    if added:
        updated.append(board)
    return tuple(updated), added


def _without_entry(
    entries: tuple[WatchedBoard, ...], boss_slug: str
) -> tuple[WatchedBoard, ...]:
    return tuple(entry for entry in entries if entry.boss_slug != boss_slug)


def _removable(added_by: str, requester_key: str, *, is_admin: bool) -> bool:
    return is_admin or (bool(requester_key) and requester_key == added_by)


def _list_index(selector: str) -> int | None:
    """A 1-based list position typed as digits, or None for anything else.

    int() refuses to convert absurdly long digit strings, and a list position
    is never one, so reject before converting.
    """

    if not (selector.isascii() and selector.isdecimal()) or len(selector) > 9:
        return None
    return int(selector)


def _chat_payload(chat: ChatWatch) -> dict[str, Any]:
    if chat.all_boards is not None:
        return {
            "allBoards": {
                "addedBy": chat.all_boards.added_by,
                "addedAt": chat.all_boards.added_at,
            },
            "excludedBoards": [_board_payload(entry) for entry in chat.excluded],
        }
    return {"boards": [_board_payload(entry) for entry in chat.boards]}


def _board_payload(board: WatchedBoard) -> dict[str, str]:
    return {
        "bossSlug": board.boss_slug,
        "bossName": board.boss_name,
        "dungeonName": board.dungeon_name,
        "addedBy": board.added_by,
        "addedAt": board.added_at,
    }


def parse_watchlist(payload: Any) -> WatchList:
    """Read a stored payload, dropping anything malformed instead of raising.

    Version 3 stores ``{"boards": [...]}`` or ``{"allBoards": {...},
    "excludedBoards": [...]}`` per chat. Older files are read as they stand:
    version 2's ``{"accounts": [...], "boards": [...]}`` keeps its boards,
    and version 1 — a bare account list per chat — has nothing left to keep.
    Account entries are dropped without a word: account 关注 no longer
    exists. A file that holds both shapes for one chat (only a hand edit
    makes one) is read as 全部榜单, the broader of the two.
    """

    if not isinstance(payload, dict):
        return WatchList.empty()
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, dict):
        return WatchList.empty()
    watchlist = WatchList.empty()
    for origin, entry in raw_groups.items():
        if not isinstance(origin, str) or not origin or not isinstance(entry, dict):
            continue
        raw_all = entry.get("allBoards")
        if isinstance(raw_all, dict):
            chat = ChatWatch(
                all_boards=AllBoards(
                    added_by=_text(raw_all.get("addedBy")),
                    added_at=_text(raw_all.get("addedAt")),
                ),
                excluded=_parse_boards(entry.get("excludedBoards")),
            )
        else:
            chat = ChatWatch(boards=_parse_boards(entry.get("boards")))
        watchlist = watchlist.with_chat(origin, chat)
    return watchlist


def _parse_boards(entries: Any) -> tuple[WatchedBoard, ...]:
    if not isinstance(entries, list):
        return ()
    boards: list[WatchedBoard] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        boss_slug = entry.get("bossSlug")
        if not isinstance(boss_slug, str) or not boss_slug:
            continue
        if boss_slug in seen:
            continue
        seen.add(boss_slug)
        boards.append(
            WatchedBoard(
                boss_slug=boss_slug,
                boss_name=_text(entry.get("bossName")) or boss_slug,
                dungeon_name=_text(entry.get("dungeonName")),
                added_by=_text(entry.get("addedBy")),
                added_at=_text(entry.get("addedAt")),
            )
        )
    return tuple(boards)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def format_watchlist(
    chat: ChatWatch, *, command: str = "/zmdlog", top_n: int
) -> str:
    """One chat's watch as text; the numbers are what 取关 addresses.

    ``command`` carries the resolved prefix so a hint can be copied as
    written, instead of naming a bare subcommand that does nothing alone;
    ``top_n`` is how far down a board a new record is news. Exclusions
    are not numbered: a number under 全部榜单 would have nothing to
    address.
    """

    if chat.all_boards is not None:
        lines = [
            "当前关注：全部榜单（以后新出的榜单也包含，"
            f"新纪录进入前 {top_n} 名时通报）。"
        ]
        if chat.excluded:
            lines.append(
                "已排除：\n"
                + "\n".join(f"- {entry.label}" for entry in chat.excluded)
            )
        lines.append(
            f"排除一张榜用 {command} 取关 <榜单关键词>，"
            f"恢复用 {command} 关注 <榜单关键词>，"
            f"全部取消用 {command} 取关 全部。"
        )
        return "\n\n".join(lines)
    if chat.boards:
        lines = [
            f"{index}. {board.label}"
            for index, board in enumerate(chat.boards, start=1)
        ]
        return (
            f"当前关注的榜单（新纪录进入前 {top_n} 名时通报，"
            "取消用 取关 <序号或榜单关键词>）：\n" + "\n".join(lines)
        )
    return (
        "还没有关注任何榜单。"
        f"用 {command} 关注 <榜单关键词> 关注一张榜，"
        f"或 {command} 关注 全部（以后新出的榜单也会包含）；"
        f"关注的榜单有新纪录进入前 {top_n} 名时会在这里通报。"
    )
