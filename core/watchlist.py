"""Per-group watch lists of public accounts and public boards.

A watch list is a subscription to public ranking changes, not a claim that an
account belongs to anyone: the stored fields are the public ``accountId`` (or
board slug), the public name snapshot, who added the entry (so they can remove
it again) and when. Nothing here identifies a player.
"""

import re
from dataclasses import dataclass, replace
from typing import Any

from .matcher import fold_text
from .timestamps import utc_now_text

WATCHLIST_VERSION = 2
_BOARD_PART_RE = re.compile(r"\s*·\s*")

__all__ = ["utc_now_text"]


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
class WatchedAccount:
    account_id: str
    display_name: str
    # Platform user key of whoever added the entry. Used only for the
    # "adder or admin may remove" rule, never rendered or announced.
    added_by: str
    added_at: str

    def removable_by(self, requester_key: str, *, is_admin: bool) -> bool:
        return is_admin or (bool(requester_key) and requester_key == self.added_by)


@dataclass(frozen=True, slots=True)
class WatchedBoard:
    """A board whose top three is watched for new records."""

    boss_slug: str
    boss_name: str
    dungeon_name: str
    added_by: str
    added_at: str

    @property
    def label(self) -> str:
        return board_label(self.dungeon_name, self.boss_name)

    def removable_by(self, requester_key: str, *, is_admin: bool) -> bool:
        return is_admin or (bool(requester_key) and requester_key == self.added_by)


@dataclass(frozen=True, slots=True)
class WatchList:
    """Immutable snapshot of the watched accounts and boards of every group."""

    groups: tuple[tuple[str, tuple[WatchedAccount, ...]], ...] = ()
    board_groups: tuple[tuple[str, tuple[WatchedBoard, ...]], ...] = ()

    @classmethod
    def empty(cls) -> "WatchList":
        return cls()

    def accounts_for(self, origin: str) -> tuple[WatchedAccount, ...]:
        for group_origin, accounts in self.groups:
            if group_origin == origin:
                return accounts
        return ()

    def boards_for(self, origin: str) -> tuple[WatchedBoard, ...]:
        for group_origin, boards in self.board_groups:
            if group_origin == origin:
                return boards
        return ()

    @property
    def total_accounts(self) -> int:
        return sum(len(accounts) for _, accounts in self.groups)

    @property
    def total_boards(self) -> int:
        return sum(len(boards) for _, boards in self.board_groups)

    def origins_by_account(self) -> dict[str, tuple[str, ...]]:
        """Invert the list so one poll per account can fan out to its groups."""

        origins: dict[str, list[str]] = {}
        for origin, accounts in self.groups:
            for account in accounts:
                bucket = origins.setdefault(account.account_id, [])
                if origin not in bucket:
                    bucket.append(origin)
        return {
            account_id: tuple(group_origins)
            for account_id, group_origins in origins.items()
        }

    def origins_by_board(self) -> dict[str, tuple[str, ...]]:
        """Invert the board list; every watched board is read from one request."""

        origins: dict[str, list[str]] = {}
        for origin, boards in self.board_groups:
            for board in boards:
                bucket = origins.setdefault(board.boss_slug, [])
                if origin not in bucket:
                    bucket.append(origin)
        return {slug: tuple(group_origins) for slug, group_origins in origins.items()}

    def with_account(
        self,
        origin: str,
        account: WatchedAccount,
    ) -> tuple["WatchList", bool]:
        """Add ``account`` to ``origin``; the flag is False when it was there.

        An account already on the list only has its nickname snapshot
        refreshed, so re-adding never reorders the list nor takes the entry
        away from whoever added it first.
        """

        groups: list[tuple[str, tuple[WatchedAccount, ...]]] = []
        added = True
        seen_origin = False
        for group_origin, accounts in self.groups:
            if group_origin != origin:
                groups.append((group_origin, accounts))
                continue
            seen_origin = True
            updated: list[WatchedAccount] = []
            for existing in accounts:
                if existing.account_id == account.account_id:
                    added = False
                    updated.append(
                        replace(existing, display_name=account.display_name)
                    )
                else:
                    updated.append(existing)
            if added:
                updated.append(account)
            groups.append((group_origin, tuple(updated)))
        if not seen_origin:
            groups.append((origin, (account,)))
        return replace(self, groups=tuple(groups)), added

    def with_board(
        self,
        origin: str,
        board: WatchedBoard,
    ) -> tuple["WatchList", bool]:
        """Add ``board`` to ``origin``; same re-add semantics as accounts."""

        groups: list[tuple[str, tuple[WatchedBoard, ...]]] = []
        added = True
        seen_origin = False
        for group_origin, boards in self.board_groups:
            if group_origin != origin:
                groups.append((group_origin, boards))
                continue
            seen_origin = True
            updated: list[WatchedBoard] = []
            for existing in boards:
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
            groups.append((group_origin, tuple(updated)))
        if not seen_origin:
            groups.append((origin, (board,)))
        return replace(self, board_groups=tuple(groups)), added

    def with_display_names(
        self,
        names: dict[str, str],
    ) -> tuple["WatchList", bool]:
        """Refresh the stored nickname snapshots; the flag says if anything moved.

        The poll cycle sees the live nickname, so a player who renames stays
        addressable by the name the notice actually shows.
        """

        changed = False
        groups: list[tuple[str, tuple[WatchedAccount, ...]]] = []
        for origin, accounts in self.groups:
            updated = []
            for account in accounts:
                name = names.get(account.account_id)
                if name and name != account.display_name:
                    changed = True
                    updated.append(replace(account, display_name=name))
                else:
                    updated.append(account)
            groups.append((origin, tuple(updated)))
        return (replace(self, groups=tuple(groups)), True) if changed else (self, False)

    def without_account(self, origin: str, account_id: str) -> "WatchList":
        groups: list[tuple[str, tuple[WatchedAccount, ...]]] = []
        for group_origin, accounts in self.groups:
            if group_origin != origin:
                groups.append((group_origin, accounts))
                continue
            kept = tuple(
                account
                for account in accounts
                if account.account_id != account_id
            )
            if kept:
                groups.append((group_origin, kept))
        return replace(self, groups=tuple(groups))

    def without_board(self, origin: str, boss_slug: str) -> "WatchList":
        groups: list[tuple[str, tuple[WatchedBoard, ...]]] = []
        for group_origin, boards in self.board_groups:
            if group_origin != origin:
                groups.append((group_origin, boards))
                continue
            kept = tuple(board for board in boards if board.boss_slug != boss_slug)
            if kept:
                groups.append((group_origin, kept))
        return replace(self, board_groups=tuple(groups))

    def resolve_matches(
        self,
        origin: str,
        selector: str,
    ) -> tuple[WatchedAccount, ...]:
        """Every watched account a 序号 / accountId / 昵称 could mean.

        Returning the whole match set lets the caller tell "no such entry" from
        "several entries match", which are different problems for the user.
        """

        accounts = self.accounts_for(origin)
        stripped = selector.strip()
        if not accounts or not stripped:
            return ()
        index = _list_index(stripped)
        if index is not None:
            return (accounts[index - 1],) if 1 <= index <= len(accounts) else ()
        if stripped.isascii() and stripped.isdecimal():
            return ()
        for account in accounts:
            if account.account_id == stripped:
                return (account,)
        folded = fold_text(stripped)
        exact = tuple(
            account
            for account in accounts
            if fold_text(account.display_name) == folded
        )
        if exact:
            return exact
        return tuple(
            account
            for account in accounts
            if folded and folded in fold_text(account.display_name)
        )

    def resolve(self, origin: str, selector: str) -> WatchedAccount | None:
        """The single watched account a selector means, if it is unambiguous."""

        matches = self.resolve_matches(origin, selector)
        return matches[0] if len(matches) == 1 else None

    def resolve_board_matches(
        self,
        origin: str,
        selector: str,
    ) -> tuple[WatchedBoard, ...]:
        """Every watched board a 序号 / slug / name fragment could mean."""

        boards = self.boards_for(origin)
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
            board
            for board in boards
            if folded and folded in fold_text(board.label)
        )

    def resolve_board(self, origin: str, selector: str) -> WatchedBoard | None:
        matches = self.resolve_board_matches(origin, selector)
        return matches[0] if len(matches) == 1 else None

    def to_payload(self) -> dict[str, Any]:
        origins = list(dict.fromkeys(
            [origin for origin, _ in self.groups]
            + [origin for origin, _ in self.board_groups]
        ))
        return {
            "version": WATCHLIST_VERSION,
            "groups": {
                origin: {
                    "accounts": [
                        {
                            "accountId": account.account_id,
                            "displayName": account.display_name,
                            "addedBy": account.added_by,
                            "addedAt": account.added_at,
                        }
                        for account in self.accounts_for(origin)
                    ],
                    "boards": [
                        {
                            "bossSlug": board.boss_slug,
                            "bossName": board.boss_name,
                            "dungeonName": board.dungeon_name,
                            "addedBy": board.added_by,
                            "addedAt": board.added_at,
                        }
                        for board in self.boards_for(origin)
                    ],
                }
                for origin in origins
            },
        }


def _list_index(selector: str) -> int | None:
    """A 1-based list position typed as digits, or None for anything else.

    int() refuses to convert absurdly long digit strings, and a list position
    is never one, so reject before converting.
    """

    if not (selector.isascii() and selector.isdecimal()) or len(selector) > 9:
        return None
    return int(selector)


def parse_watchlist(payload: Any) -> WatchList:
    """Read a stored payload, dropping anything malformed instead of raising.

    Version 1 stored a bare account list per group; version 2 stores
    ``{"accounts": [...], "boards": [...]}``. Both shapes are accepted.
    """

    if not isinstance(payload, dict):
        return WatchList.empty()
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, dict):
        return WatchList.empty()
    groups: list[tuple[str, tuple[WatchedAccount, ...]]] = []
    board_groups: list[tuple[str, tuple[WatchedBoard, ...]]] = []
    for origin, entries in raw_groups.items():
        if not isinstance(origin, str) or not origin:
            continue
        if isinstance(entries, list):
            account_entries, board_entries = entries, []
        elif isinstance(entries, dict):
            account_entries = entries.get("accounts")
            board_entries = entries.get("boards")
        else:
            continue
        accounts = _parse_accounts(account_entries)
        if accounts:
            groups.append((origin, accounts))
        boards = _parse_boards(board_entries)
        if boards:
            board_groups.append((origin, boards))
    return WatchList(tuple(groups), tuple(board_groups))


def _parse_accounts(entries: Any) -> tuple[WatchedAccount, ...]:
    if not isinstance(entries, list):
        return ()
    accounts: list[WatchedAccount] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        account_id = entry.get("accountId")
        if not isinstance(account_id, str) or not account_id:
            continue
        if account_id in seen:
            continue
        seen.add(account_id)
        display_name = entry.get("displayName")
        accounts.append(
            WatchedAccount(
                account_id=account_id,
                display_name=(
                    display_name
                    if isinstance(display_name, str) and display_name
                    else account_id
                ),
                added_by=_text(entry.get("addedBy")),
                added_at=_text(entry.get("addedAt")),
            )
        )
    return tuple(accounts)


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
    accounts: tuple[WatchedAccount, ...],
    *,
    command: str = "/zmdlog",
    boards: tuple[WatchedBoard, ...] = (),
) -> str:
    """Numbered lists; the numbers are what 取关 / 取关 榜单 address.

    ``command`` carries the resolved prefix so the hint can be copied as
    written, instead of naming a bare subcommand that does nothing alone.
    """

    if not accounts and not boards:
        return (
            "还没有关注任何账号或榜单。"
            f"用 {command} 关注 <昵称或accountId> 关注账号（名次掉了会通报），"
            f"用 {command} 关注 榜单 <榜单关键词> 关注榜单（前三名有新纪录会通报）。"
        )
    sections: list[str] = []
    if accounts:
        lines = [
            f"{index}. {account.display_name}（{account.account_id}）"
            for index, account in enumerate(accounts, start=1)
        ]
        sections.append("当前关注的账号：\n" + "\n".join(lines))
    if boards:
        lines = [
            f"{index}. {board.label}" for index, board in enumerate(boards, start=1)
        ]
        sections.append(
            "当前关注的榜单（前三名有新纪录时通报，取消用 取关 榜单 <序号>）：\n"
            + "\n".join(lines)
        )
    return "\n\n".join(sections)
