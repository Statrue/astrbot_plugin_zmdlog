"""The 绑定 / 解绑 / 主账号 commands, and the book behind 我的 and 群榜.

One :class:`AccountBinding` per plugin owns ``bindings.json`` and the lock
that makes redeeming a code atomic: two messages carrying the same code
serialise here, and the second finds the code's digest already spent. It
is free of AstrBot — the chat, the sender key and the command prefix all
arrive as parameters — so the whole flow runs in tests against a fake
client.

Only a binding code binds. A nickname or an accountId is refused outright:
an unverified binding was judged an unacceptable impersonation surface
(2026-09-03), and the site now issues codes to whoever is logged into the
account, so nothing weaker is needed.
"""

import asyncio
from pathlib import Path

from . import messages
from .bindings import (
    MAX_ACCOUNTS_PER_USER,
    BindingBook,
    BoundAccount,
    UserBindings,
    code_digest,
    format_bindings,
    is_group_origin,
    normalize_binding_code,
    parse_bindings,
)
from .client import ZmdLogsAPIError, ZmdLogsClient, ZmdLogsClientError
from .logs import LogSink
from .messages import shorten
from .persistence import JsonStore
from .routing import RouteKind, RouteRequest
from .settings import PluginSettings
from .timestamps import utc_now_text

BINDINGS_FILE = "bindings.json"
BINDING_CODE_INVALID = "binding_code_invalid"
_EVERYTHING = frozenset({"全部", "所有", "all"})


class AccountBinding:
    """Own the binding book and answer the three binding commands in text."""

    def __init__(
        self,
        *,
        client: ZmdLogsClient,
        settings: PluginSettings,
        data_dir: Path | None,
        logger: LogSink,
    ) -> None:
        self._client = client
        self._logger = logger
        self.enabled = settings.bindings_enabled
        self.group_board_max_accounts = settings.group_board_max_accounts
        self.store = JsonStore(
            None if data_dir is None else data_dir / BINDINGS_FILE,
            label="account bindings",
            warn=logger.warning,
        )
        self.book: BindingBook = parse_bindings(self.store.load())
        self._lock = asyncio.Lock()

    # --- reads for 我的 / 群榜 -----------------------------------------------------

    def bindings_for(self, requester_key: str) -> UserBindings | None:
        if not requester_key:
            return None
        return self.book.for_user(requester_key)

    def accounts_in(self, origin: str) -> tuple[tuple[BoundAccount, ...], int, int]:
        """The bound accounts of a chat's members: the ones the board reads,
        how many accounts there were, how many members.

        Capped at ``group_board_max_accounts``, newest member first, so the
        board's one ranking read stays one whatever the chat's size.
        """

        members = self.book.members_of(origin)
        accounts = self.book.accounts_in(origin)
        return accounts[: self.group_board_max_accounts], len(accounts), len(members)

    def remember_member(self, requester_key: str, origin: str) -> None:
        """A bound user used 我的 or 群榜 here: they are on this chat's board now.

        The callers already refuse a private chat; checked again here so a
        new caller cannot record one as a group by mistake.
        """

        if not requester_key or not is_group_origin(origin):
            return
        updated = self.book.with_group(requester_key, origin, now=utc_now_text())
        if updated is not self.book:
            self._save(updated)

    # --- 绑定 / 解绑 / 主账号 ---------------------------------------------------

    async def handle_route(
        self,
        route: RouteRequest,
        *,
        origin: str,
        requester_key: str,
        command: str,
    ) -> str:
        """Maintain one user's bindings, in text; nothing is rendered.

        Group chats only, like every binding command: the bot adds nobody
        as a friend, so a private chat is not a place it is used from.
        """

        if not is_group_origin(origin):
            return messages.BINDING_GROUP_ONLY
        if not requester_key:
            return messages.NO_SENDER
        if route.kind is RouteKind.BIND:
            return await self._bind(route.query, origin, requester_key, command)
        mine = self.book.for_user(requester_key)
        if mine is None:
            return messages.NOT_BOUND.format(command=command)
        if route.kind is RouteKind.UNBIND:
            return self._unbind(route.query, requester_key, mine, command)
        return self._set_primary(route.query, requester_key, mine, command)

    async def _bind(
        self, text: str, origin: str, requester_key: str, command: str
    ) -> str:
        if not self.enabled:
            return messages.BINDINGS_DISABLED
        code = normalize_binding_code(text)
        if code is None:
            return messages.BIND_CODE_NEEDED.format(command=command)
        digest = code_digest(code)
        async with self._lock:
            now = utc_now_text()
            if self.book.code_used(digest, now=now):
                return messages.BIND_CODE_USED
            try:
                hit = await self._client.get_binding_code_account(code)
            except ZmdLogsAPIError as exc:
                # The code itself is never logged; its outcome is.
                self._logger.warning("ZmdLogBot binding code refused: %s", exc.code)
                if exc.status_code == 404:
                    if exc.code == BINDING_CODE_INVALID:
                        return messages.BIND_CODE_INVALID
                    return messages.BIND_UNSUPPORTED
                if exc.status_code == 429:
                    return messages.RATE_LIMITED
                return messages.UPSTREAM_UNAVAILABLE
            except ZmdLogsClientError as exc:
                self._logger.warning(
                    "ZmdLogBot binding code lookup failed: %s", type(exc).__name__
                )
                return messages.UPSTREAM_UNAVAILABLE
            updated, status = self.book.with_account(
                requester_key,
                BoundAccount(
                    account_id=hit.account_id,
                    display_name=hit.account_display_name,
                    bound_at=now,
                ),
                # Binding here joins this group's board.
                origin=origin,
                now=now,
            )
            if status == "full":
                # Nothing was bound, so the code stays usable after a 解绑.
                return messages.BIND_LIMIT_REACHED.format(limit=MAX_ACCOUNTS_PER_USER)
            if not self._save(updated.with_used_code(digest, now=now)):
                return messages.BINDINGS_WRITE_FAILED
        mine = self.book.for_user(requester_key)
        assert mine is not None
        if status == "refreshed":
            position = _position(mine, hit.account_id)
            return (
                f"{hit.account_display_name}（{hit.account_id}）已经绑定过了"
                f"（第 {position} 位），昵称已更新。"
            )
        return (
            f"已绑定 {hit.account_display_name}（{hit.account_id}）。\n"
            + format_bindings(mine, command=command)
        )

    def _unbind(
        self, text: str, requester_key: str, mine: UserBindings, command: str
    ) -> str:
        selector = text.strip()
        if selector in _EVERYTHING or (not selector and len(mine.accounts) == 1):
            removed = mine.accounts
            if not self._save(self.book.without_user(requester_key)):
                return messages.BINDINGS_WRITE_FAILED
            if len(removed) == 1:
                return f"已解绑 {removed[0].display_name}（{removed[0].account_id}）。"
            return f"已解除全部 {len(removed)} 个绑定。"
        if not selector:
            return (
                "绑定了多个账号，请指定：解绑 <序号或昵称>，或 解绑 全部。\n"
                + format_bindings(mine, command=command)
            )
        matches = mine.resolve_matches(selector)
        if len(matches) > 1:
            names = "、".join(entry.display_name for entry in matches)
            return f"「{shorten(selector)}」匹配到多个绑定：{names}，请改用序号。"
        if not matches:
            return (
                f"绑定列表里没有「{shorten(selector)}」。\n"
                + format_bindings(mine, command=command)
            )
        account = matches[0]
        updated = self.book.without_account(
            requester_key, account.account_id, now=utc_now_text()
        )
        if not self._save(updated):
            return messages.BINDINGS_WRITE_FAILED
        reply = f"已解绑 {account.display_name}（{account.account_id}）。"
        remaining = self.book.for_user(requester_key)
        if remaining is not None:
            reply += "\n" + format_bindings(remaining, command=command)
        return reply

    def _set_primary(
        self, text: str, requester_key: str, mine: UserBindings, command: str
    ) -> str:
        selector = text.strip()
        if not selector:
            return format_bindings(mine, command=command)
        if len(mine.accounts) == 1:
            return "只绑定了一个账号，无需设置主账号。"
        matches = mine.resolve_matches(selector)
        if len(matches) > 1:
            names = "、".join(entry.display_name for entry in matches)
            return f"「{shorten(selector)}」匹配到多个绑定：{names}，请改用序号。"
        if not matches:
            return (
                f"绑定列表里没有「{shorten(selector)}」。\n"
                + format_bindings(mine, command=command)
            )
        account = matches[0]
        if mine.accounts[0].account_id == account.account_id:
            return f"{account.display_name} 已经是主账号。"
        updated = self.book.with_primary(
            requester_key, account.account_id, now=utc_now_text()
        )
        if not self._save(updated):
            return messages.BINDINGS_WRITE_FAILED
        return f"主账号已改为 {account.display_name}（{account.account_id}）。"

    # --- persistence -------------------------------------------------------------

    def _save(self, updated: BindingBook) -> bool:
        """Write first, install second: a failed write leaves the book as it was.

        Without a data directory nothing can be kept across a restart, and
        a binding that vanishes on reload is worse than none: it is refused.
        """

        if not self.store.save(updated.to_payload()):
            return False
        self.book = updated
        return True


def _position(mine: UserBindings, account_id: str) -> int:
    return next(
        (
            index
            for index, entry in enumerate(mine.accounts, start=1)
            if entry.account_id == account_id
        ),
        len(mine.accounts),
    )
