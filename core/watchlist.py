"""Per-group watch lists of public accounts.

A watch list is a subscription to the public ranking changes of a public
account, not a claim that the account belongs to anyone: the stored fields are
the public ``accountId``, the public nickname snapshot, who added the entry (so
they can remove it again) and when. Nothing here identifies a player.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .matcher import fold_text

WATCHLIST_VERSION = 1


def utc_now_text() -> str:
    """Timestamp for new entries, in the same shape the payload stores."""

    return datetime.now(UTC).replace(microsecond=0).isoformat()


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
class WatchList:
    """Immutable snapshot of the watched accounts of every group."""

    groups: tuple[tuple[str, tuple[WatchedAccount, ...]], ...] = ()

    @classmethod
    def empty(cls) -> "WatchList":
        return cls()

    def accounts_for(self, origin: str) -> tuple[WatchedAccount, ...]:
        for group_origin, accounts in self.groups:
            if group_origin == origin:
                return accounts
        return ()

    @property
    def total_accounts(self) -> int:
        return sum(len(accounts) for _, accounts in self.groups)

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
        return WatchList(tuple(groups)), added

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
        return (WatchList(tuple(groups)), True) if changed else (self, False)

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
        return WatchList(tuple(groups))

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
        if stripped.isascii() and stripped.isdecimal():
            # int() refuses to convert absurdly long digit strings, and a list
            # position is never one, so reject before converting.
            if len(stripped) > 9:
                return ()
            index = int(stripped)
            return (accounts[index - 1],) if 1 <= index <= len(accounts) else ()
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

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": WATCHLIST_VERSION,
            "groups": {
                origin: [
                    {
                        "accountId": account.account_id,
                        "displayName": account.display_name,
                        "addedBy": account.added_by,
                        "addedAt": account.added_at,
                    }
                    for account in accounts
                ]
                for origin, accounts in self.groups
            },
        }


def parse_watchlist(payload: Any) -> WatchList:
    """Read a stored payload, dropping anything malformed instead of raising."""

    if not isinstance(payload, dict):
        return WatchList.empty()
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, dict):
        return WatchList.empty()
    groups: list[tuple[str, tuple[WatchedAccount, ...]]] = []
    for origin, entries in raw_groups.items():
        if not isinstance(origin, str) or not origin:
            continue
        if not isinstance(entries, list):
            continue
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
            added_by = entry.get("addedBy")
            added_at = entry.get("addedAt")
            accounts.append(
                WatchedAccount(
                    account_id=account_id,
                    display_name=(
                        display_name
                        if isinstance(display_name, str) and display_name
                        else account_id
                    ),
                    added_by=added_by if isinstance(added_by, str) else "",
                    added_at=added_at if isinstance(added_at, str) else "",
                )
            )
        if accounts:
            groups.append((origin, tuple(accounts)))
    return WatchList(tuple(groups))


def format_watchlist(
    accounts: tuple[WatchedAccount, ...],
    *,
    command: str = "/zmdlog",
) -> str:
    """Numbered list; the numbers are what 取关 addresses.

    ``command`` carries the resolved prefix so the hint can be copied as
    written, instead of naming a bare subcommand that does nothing alone.
    """

    if not accounts:
        return (
            "还没有关注任何账号。"
            f"用 {command} 关注 <昵称或accountId> 添加，"
            "名次掉了会在这里通报。"
        )
    lines = [
        f"{index}. {account.display_name}（{account.account_id}）"
        for index, account in enumerate(accounts, start=1)
    ]
    return "当前关注的账号：\n" + "\n".join(lines)
