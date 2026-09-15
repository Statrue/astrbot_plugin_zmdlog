"""Verified bindings between platform users and public ZMDLogs accounts. Pure.

A binding is the one piece of user data the plugin keeps: the platform
user key (``aiocqhttp:12345``), the public ``accountId`` and nickname
snapshot of every account that user proved control of, which chats they
used the binding commands in, and when. Nothing else — no QQ nickname, no
site credentials — and every account here was verified with a binding code
the site issued to whoever was logged into it (``ACCOUNT_BINDING_CODE_API``).

The book also remembers the digests of the codes already redeemed: the
site's lookup does not consume a code, so within its ten-minute life a
second message carrying the same code would otherwise bind the same
account to a second user.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any

from .matcher import fold_text
from .timestamps import parse_timestamp

BINDINGS_VERSION = 1
# One person can own several site accounts; five is more than anyone has
# asked for, and a cap keeps one user from filling the file.
MAX_ACCOUNTS_PER_USER = 5
MAX_BOUND_USERS = 5000
# Upstream keeps a code for ten minutes; a redeemed one is remembered a
# little longer so a restart inside that window cannot reopen it.
USED_CODE_TTL_SECONDS = 15 * 60
# The site's format: uppercase only, a 32-character alphabet without 0/1/I/O.
BINDING_CODE_RE = re.compile(r"^ZMD-[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$")
_DASHES = "‐‑‒–—―－"


def normalize_binding_code(text: str) -> str | None:
    """The code as the site accepts it, or None when the text is not one.

    People type lowercase, fullwidth letters or a long dash; the site takes
    uppercase ASCII only, so those are folded before the format check.
    """

    value = unicodedata.normalize("NFKC", text or "").strip().upper()
    for dash in _DASHES:
        value = value.replace(dash, "-")
    return value if BINDING_CODE_RE.match(value) else None


def code_digest(code: str) -> str:
    """What the book stores about a redeemed code: never the code itself."""

    return hashlib.sha256(code.encode("ascii")).hexdigest()


def is_group_origin(origin: str) -> bool:
    """Whether an AstrBot ``unified_msg_origin`` names a group chat.

    The origin is ``platform:MessageType:session`` and the group type is
    spelled ``GroupMessage`` on every platform, so this is the one portable
    group test there is; a private chat has no members to put on a board.
    """

    return ":GroupMessage:" in (origin or "")


@dataclass(frozen=True, slots=True)
class BoundAccount:
    account_id: str
    display_name: str
    bound_at: str


@dataclass(frozen=True, slots=True)
class UserBindings:
    """One user's accounts (the first is the primary) and the chats they used."""

    accounts: tuple[BoundAccount, ...]
    groups: tuple[str, ...] = ()
    updated_at: str = ""

    @property
    def primary(self) -> BoundAccount | None:
        return self.accounts[0] if self.accounts else None

    def resolve_matches(self, selector: str) -> tuple[BoundAccount, ...]:
        """Every bound account a 序号 / accountId / 昵称 could mean.

        The whole match set comes back so the caller can tell "no such
        entry" from "several match", the same way the watch list does.
        """

        stripped = selector.strip()
        if not stripped or not self.accounts:
            return ()
        if stripped.isascii() and stripped.isdecimal():
            if len(stripped) > 9:
                return ()
            index = int(stripped)
            if 1 <= index <= len(self.accounts):
                return (self.accounts[index - 1],)
            return ()
        for account in self.accounts:
            if account.account_id == stripped:
                return (account,)
        folded = fold_text(stripped)
        exact = tuple(
            account
            for account in self.accounts
            if fold_text(account.display_name) == folded
        )
        if exact:
            return exact
        return tuple(
            account
            for account in self.accounts
            if folded and folded in fold_text(account.display_name)
        )

    def resolve(self, selector: str) -> BoundAccount | None:
        matches = self.resolve_matches(selector)
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class BindingBook:
    """Immutable snapshot of every user's bindings and the redeemed codes."""

    users: tuple[tuple[str, UserBindings], ...] = ()
    # (sha256 of the code, when it was redeemed)
    used_codes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def empty(cls) -> "BindingBook":
        return cls()

    def for_user(self, user_key: str) -> UserBindings | None:
        for key, bindings in self.users:
            if key == user_key:
                return bindings
        return None

    @property
    def total_users(self) -> int:
        return len(self.users)

    def with_account(
        self,
        user_key: str,
        account: BoundAccount,
        *,
        origin: str,
        now: str,
    ) -> tuple["BindingBook", str]:
        """Append ``account`` to the user's list; the flag says what happened.

        ``"added"`` for a new entry at the end of the list (the primary
        stays what it was), ``"refreshed"`` when the account was already
        bound and only its nickname snapshot moved, ``"full"`` when the
        user's list is at the cap and nothing changed.
        """

        current = self.for_user(user_key)
        if current is None:
            if self.total_users >= MAX_BOUND_USERS:
                return self, "full"
            current = UserBindings(accounts=(), groups=(), updated_at=now)
        accounts = list(current.accounts)
        status = "added"
        for index, existing in enumerate(accounts):
            if existing.account_id == account.account_id:
                accounts[index] = replace(existing, display_name=account.display_name)
                status = "refreshed"
                break
        else:
            if len(accounts) >= MAX_ACCOUNTS_PER_USER:
                return self, "full"
            accounts.append(account)
        updated = replace(
            current,
            accounts=tuple(accounts),
            groups=_with_origin(current.groups, origin),
            updated_at=now,
        )
        return self._with_user(user_key, updated), status

    def with_primary(
        self, user_key: str, account_id: str, *, now: str
    ) -> "BindingBook":
        """Move one bound account to the front; the front is the primary."""

        current = self.for_user(user_key)
        if current is None:
            return self
        chosen = next(
            (entry for entry in current.accounts if entry.account_id == account_id),
            None,
        )
        if chosen is None or current.accounts[0] is chosen:
            return self
        others = tuple(
            entry for entry in current.accounts if entry.account_id != account_id
        )
        return self._with_user(
            user_key, replace(current, accounts=(chosen, *others), updated_at=now)
        )

    def with_group(self, user_key: str, origin: str, *, now: str) -> "BindingBook":
        """Note that a bound user used a binding command in ``origin``.

        This is the whole membership rule of the group board: no platform
        gives a portable member list, so a chat's members are the bound
        users who have shown up in it.
        """

        current = self.for_user(user_key)
        if current is None or not origin or origin in current.groups:
            return self
        updated = replace(
            current, groups=_with_origin(current.groups, origin), updated_at=now
        )
        return self._with_user(user_key, updated)

    def without_account(
        self, user_key: str, account_id: str, *, now: str
    ) -> "BindingBook":
        current = self.for_user(user_key)
        if current is None:
            return self
        kept = tuple(
            entry for entry in current.accounts if entry.account_id != account_id
        )
        if len(kept) == len(current.accounts):
            return self
        if not kept:
            return self.without_user(user_key)
        updated = replace(current, accounts=kept, updated_at=now)
        return self._with_user(user_key, updated)

    def without_user(self, user_key: str) -> "BindingBook":
        kept = tuple((key, value) for key, value in self.users if key != user_key)
        return replace(self, users=kept)

    def members_of(self, origin: str) -> tuple[tuple[str, UserBindings], ...]:
        """The bound users who used a binding command in ``origin``, newest first."""

        members = [
            (key, bindings) for key, bindings in self.users if origin in bindings.groups
        ]
        members.sort(key=lambda item: item[1].updated_at, reverse=True)
        return tuple(members)

    def accounts_in(self, origin: str) -> tuple[BoundAccount, ...]:
        """Every account bound by a member of ``origin``, each once.

        Two users bound to one account are one public account; the board
        shows public nicknames, not who bound them.
        """

        seen: dict[str, BoundAccount] = {}
        for _, bindings in self.members_of(origin):
            for account in bindings.accounts:
                seen.setdefault(account.account_id, account)
        return tuple(seen.values())

    def code_used(self, digest: str, *, now: str) -> bool:
        cutoff = parse_timestamp(now)
        for used_digest, used_at in self.used_codes:
            if used_digest != digest:
                continue
            when = parse_timestamp(used_at)
            if cutoff is None or when is None:
                return True
            return (cutoff - when).total_seconds() < USED_CODE_TTL_SECONDS
        return False

    def with_used_code(self, digest: str, *, now: str) -> "BindingBook":
        """Remember a redeemed code and forget the ones past their life."""

        cutoff = parse_timestamp(now)
        kept = []
        for used_digest, used_at in self.used_codes:
            if used_digest == digest:
                continue
            when = parse_timestamp(used_at)
            if cutoff is not None and when is not None:
                if (cutoff - when).total_seconds() >= USED_CODE_TTL_SECONDS:
                    continue
            kept.append((used_digest, used_at))
        kept.append((digest, now))
        return replace(self, used_codes=tuple(kept))

    def _with_user(self, user_key: str, bindings: UserBindings) -> "BindingBook":
        users = list(self.users)
        for index, (key, _) in enumerate(users):
            if key == user_key:
                users[index] = (user_key, bindings)
                break
        else:
            users.append((user_key, bindings))
        return replace(self, users=tuple(users))

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": BINDINGS_VERSION,
            "users": {
                key: {
                    "accounts": [
                        {
                            "accountId": account.account_id,
                            "displayName": account.display_name,
                            "boundAt": account.bound_at,
                        }
                        for account in bindings.accounts
                    ],
                    "groups": list(bindings.groups),
                    "updatedAt": bindings.updated_at,
                }
                for key, bindings in self.users
            },
            "usedCodes": {digest: used_at for digest, used_at in self.used_codes},
        }


def _with_origin(groups: tuple[str, ...], origin: str) -> tuple[str, ...]:
    if not origin or origin in groups:
        return groups
    return (*groups, origin)


def parse_bindings(payload: Any) -> BindingBook:
    """Read a stored payload, dropping anything malformed instead of raising."""

    if not isinstance(payload, dict):
        return BindingBook.empty()
    raw_users = payload.get("users")
    users: list[tuple[str, UserBindings]] = []
    if isinstance(raw_users, dict):
        for key, entry in raw_users.items():
            if not isinstance(key, str) or not key or not isinstance(entry, dict):
                continue
            accounts = _parse_accounts(entry.get("accounts"))
            if not accounts:
                continue
            groups = entry.get("groups")
            users.append(
                (
                    key,
                    UserBindings(
                        accounts=accounts,
                        groups=tuple(
                            origin
                            for origin in (groups if isinstance(groups, list) else ())
                            if isinstance(origin, str) and origin
                        ),
                        updated_at=_text(entry.get("updatedAt")),
                    ),
                )
            )
    raw_codes = payload.get("usedCodes")
    used = tuple(
        (digest, stamp)
        for digest, stamp in (raw_codes.items() if isinstance(raw_codes, dict) else ())
        if isinstance(digest, str) and digest and isinstance(stamp, str)
    )
    return BindingBook(tuple(users), used)


def _parse_accounts(entries: Any) -> tuple[BoundAccount, ...]:
    if not isinstance(entries, list):
        return ()
    accounts: list[BoundAccount] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        account_id = entry.get("accountId")
        if not isinstance(account_id, str) or not account_id or account_id in seen:
            continue
        seen.add(account_id)
        display_name = entry.get("displayName")
        accounts.append(
            BoundAccount(
                account_id=account_id,
                display_name=(
                    display_name
                    if isinstance(display_name, str) and display_name
                    else account_id
                ),
                bound_at=_text(entry.get("boundAt")),
            )
        )
    return tuple(accounts[:MAX_ACCOUNTS_PER_USER])


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def format_bindings(bindings: UserBindings | None, *, command: str = "/zmdlog") -> str:
    """The user's numbered list; the numbers are what 我的 / 解绑 / 主账号 address."""

    if bindings is None or not bindings.accounts:
        return (
            "你还没有绑定账号。登录 ZMDLogs，在 账号菜单 → 机器人绑定 生成绑定码，"
            f"然后发送 {command} 绑定 ZMD-XXXX-XXXX。"
        )
    lines = ["当前绑定的账号（第 1 位是主账号，我的 默认看它）："]
    for index, account in enumerate(bindings.accounts, start=1):
        star = " ★主账号" if index == 1 and len(bindings.accounts) > 1 else ""
        lines.append(f"{index}. {account.display_name}（{account.account_id}）{star}")
    if len(bindings.accounts) > 1:
        lines.append(
            f"{command} 我的 <序号> 看某个号，{command} 主账号 <序号> 换主账号，"
            f"{command} 解绑 <序号或全部> 解除。"
        )
    return "\n".join(lines)
