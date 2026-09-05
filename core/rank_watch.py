"""Rank watch: the 关注 / 取关 commands, their polling loop and its files.

One :class:`RankWatcher` per plugin owns the watch list, the rank and board
baselines, the trend trace, and the loop that polls them. It is free of
AstrBot: the chat an event came from, the sender's identity and the way a
notice is delivered all arrive as parameters, so the whole machine can be
driven from tests with fakes.

Two rules run through every cycle:

* One request per watched account per cycle — ``users/{id}/rankings``
  carries the account's rank on every board — and at most
  ``MAX_DROPS_PER_NOTICE`` board look-ups on top, all inside one semaphore.
* Results are merged onto the *live* state, never installed wholesale:
  关注 / 取关 can run while a cycle is awaiting, and installing the map the
  cycle started from would drop a baseline just seeded and notify a chat
  that has since unfollowed.

The notices only ever claim what one board read can prove; the wording
lives in :mod:`core.watch` and must stay weak (see CLAUDE.md).
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from . import messages
from .candidates import (
    MAX_CANDIDATES,
    CandidateStore,
    CandidateView,
    account_choice,
    format_candidates,
)
from .client import (
    MIN_ACCOUNT_SEARCH_LENGTH,
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsClientError,
    searchable_nickname,
)
from .datasource import ZmdLogsDataSource
from .history import (
    AccountHistory,
    history_payload,
    parse_history_payload,
    record_rankings,
)
from .identifiers import PublicReferenceError, parse_account_reference
from .logs import LogSink
from .matcher import (
    BOARD_QUERY_TARGETS,
    MatchStatus,
    RankingMatcher,
    fold_text,
)
from .messages import shorten
from .models import HotBossCard, PublicUserRankings
from .persistence import JsonStore
from .routing import RouteKind, RouteRequest
from .settings import PluginSettings
from .timestamps import utc_now_text
from .watch import (
    MAX_DROPS_PER_NOTICE,
    AccountSnapshot,
    BoardSnapshot,
    RankDrop,
    board_snapshot_is_usable,
    board_snapshot_payload,
    build_board_snapshot,
    build_snapshot,
    find_new_record_above,
    find_rank_drops,
    find_top_run_changes,
    format_board_notice,
    format_rank_drop_notice,
    join_board_notices,
    join_rank_drop_notices,
    parse_board_snapshot_payload,
    parse_snapshot_payload,
    snapshot_is_usable,
    snapshot_payload,
)
from .watchlist import (
    WatchedAccount,
    WatchedBoard,
    WatchList,
    board_label,
    format_watchlist,
    parse_watchlist,
)

WATCHLIST_FILE = "watchlist.json"
RANK_SNAPSHOT_FILE = "rank-snapshot.json"
BOARD_SNAPSHOT_FILE = "board-snapshot.json"
RANK_HISTORY_FILE = "rank-history.json"
_RANK_WATCH_CONCURRENCY = 2

# Returns whether the chat actually received the text. A cycle only
# advances a baseline past what it managed to deliver.
Notify = Callable[[str, str], Awaitable[bool]]
BoardMatcher = Callable[[tuple[HotBossCard, ...]], RankingMatcher]


class RankWatcher:
    """Watch lists, baselines, the trend trace and the loop that feeds them."""

    def __init__(
        self,
        *,
        client: ZmdLogsClient,
        data: ZmdLogsDataSource,
        settings: PluginSettings,
        data_dir: Path | None,
        board_matcher: BoardMatcher,
        candidates: CandidateStore,
        notify: Notify,
        logger: LogSink,
    ) -> None:
        self._client = client
        self._data = data
        self._settings = settings
        self._board_matcher = board_matcher
        self._candidates = candidates
        # Public so the host can swap the delivery path (tests capture it).
        self.notify = notify
        self._logger = logger
        self.enabled = settings.rank_watch_enabled
        warn = logger.warning
        self.watchlist_store = JsonStore(
            _in(data_dir, WATCHLIST_FILE), label="watch list", warn=warn
        )
        self.rank_snapshot_store = JsonStore(
            _in(data_dir, RANK_SNAPSHOT_FILE), label="rank snapshot", warn=warn
        )
        self.board_snapshot_store = JsonStore(
            _in(data_dir, BOARD_SNAPSHOT_FILE), label="board snapshot", warn=warn
        )
        self.history_store = JsonStore(
            _in(data_dir, RANK_HISTORY_FILE), label="rank history", warn=warn
        )
        self.watchlist: WatchList = parse_watchlist(self.watchlist_store.load())
        self.rank_snapshots: dict[str, AccountSnapshot] = parse_snapshot_payload(
            self.rank_snapshot_store.load()
        )
        self.board_snapshots: dict[str, BoardSnapshot] = (
            parse_board_snapshot_payload(self.board_snapshot_store.load())
        )
        self.rank_history: dict[str, AccountHistory] = parse_history_payload(
            self.history_store.load()
        )
        self._task: asyncio.Task[None] | None = None

    # --- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Start polling; idempotent, and a no-op without a data directory."""

        task = self._task
        if not self.enabled or (task is not None and not task.done()):
            return
        if not self.watchlist_store.available:
            self._logger.warning(
                "ZmdLogBot rank watch is off: no writable plugin data directory."
            )
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            self._logger.warning("ZmdLogBot rank watch task ended with an error.")

    async def _loop(self) -> None:
        """Poll forever; one failed cycle must never end the loop."""

        while True:
            interval = self._settings.rank_watch_interval_seconds
            await asyncio.sleep(interval + random.uniform(0.0, interval * 0.1))
            try:
                await self.run_account_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("ZmdLogBot rank watch cycle failed")
            try:
                await self.run_board_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("ZmdLogBot board watch cycle failed")

    # --- trend page reads --------------------------------------------------------

    def history_for(self, account_id: str) -> AccountHistory | None:
        return self.rank_history.get(account_id)

    def history_by_name(self, query: str) -> tuple[AccountHistory, ...]:
        """Watched accounts whose recorded nickname matches ``query``."""

        stripped = query.strip()
        if len(stripped) < MIN_ACCOUNT_SEARCH_LENGTH:
            return ()
        folded = fold_text(stripped)
        entries = tuple(self.rank_history.values())
        exact = tuple(
            entry for entry in entries if fold_text(entry.display_name) == folded
        )
        if exact:
            return exact
        return tuple(
            entry
            for entry in entries
            if folded and folded in fold_text(entry.display_name)
        )

    def last_checked(self, account_id: str) -> str | None:
        snapshot = self.rank_snapshots.get(account_id)
        return None if snapshot is None else snapshot.checked_at

    # --- 关注 / 取关 ------------------------------------------------------------

    async def handle_route(
        self,
        route: RouteRequest,
        *,
        origin: str,
        requester_key: str,
        is_admin: bool,
        command: str,
    ) -> str:
        """Maintain the watch list of one chat, in text; nothing is rendered.

        ``origin`` is the chat the command came from, ``requester_key`` the
        platform-scoped sender key, ``command`` the prefixed command name
        echoed in hints.
        """

        if not origin:
            # Without a real origin every chat would share one list and the
            # notices would have nowhere to go.
            return messages.NO_ORIGIN
        if route.kind is RouteKind.WATCH_LIST:
            # Reviewing and pruning stay available when polling is switched
            # off; only adding is refused.
            return format_watchlist(
                self.watchlist.accounts_for(origin),
                command=command,
                boards=self.watchlist.boards_for(origin),
            )
        if route.kind is RouteKind.WATCH_BOARD_REMOVE:
            boards = self.watchlist.resolve_board_matches(origin, route.query)
            if len(boards) > 1:
                names = "、".join(board.label for board in boards[:5])
                return (
                    f"「{shorten(route.query)}」匹配到多个关注的榜单：{names}，"
                    "请改用序号。"
                )
            if not boards:
                return (
                    f"关注列表里没有榜单「{shorten(route.query)}」，"
                    f"发送 {command} 关注 查看当前列表与序号。"
                )
            board = boards[0]
            if not board.removable_by(requester_key, is_admin=is_admin):
                return messages.WATCH_REMOVE_FORBIDDEN
            if not self._save_watchlist(
                self.watchlist.without_board(origin, board.boss_slug)
            ):
                return messages.WATCHLIST_WRITE_FAILED
            self._forget_board_snapshot(board.boss_slug)
            return f"已取消关注榜单「{board.label}」。"
        if route.kind is RouteKind.WATCH_REMOVE:
            matches = self.watchlist.resolve_matches(origin, route.query)
            if len(matches) > 1:
                names = "、".join(entry.display_name for entry in matches[:5])
                return (
                    f"「{shorten(route.query)}」匹配到多个关注：{names}，"
                    "请改用序号。"
                )
            if not matches:
                return (
                    f"关注列表里没有「{shorten(route.query)}」，"
                    f"发送 {command} 关注 查看当前列表与序号。"
                )
            account = matches[0]
            if not account.removable_by(requester_key, is_admin=is_admin):
                return messages.WATCH_REMOVE_FORBIDDEN
            if not self._save_watchlist(
                self.watchlist.without_account(origin, account.account_id)
            ):
                return messages.WATCHLIST_WRITE_FAILED
            self._forget_rank_snapshot(account.account_id)
            return f"已取消关注 {account.display_name}。"
        if not self.enabled:
            return messages.WATCH_DISABLED
        if route.kind is RouteKind.WATCH_BOARD_ADD:
            return await self._add_board(route.query, origin, requester_key)
        return await self._add_account(route.query, origin, requester_key)

    async def _add_board(self, query: str, origin: str, requester_key: str) -> str:
        """Resolve a board keyword the way queries do, then remember it.

        Dungeon and scope hits are flattened to their boards: the watch is on
        one board's top three, so several boards become a pick list.
        """

        try:
            cards = await self._data.list_hot_bosses()
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot request failed: %s", type(exc).__name__
            )
            return messages.UPSTREAM_UNAVAILABLE
        matcher = self._board_matcher(cards)
        match = matcher.match(query, allowed_types=BOARD_QUERY_TARGETS)
        if match.status is MatchStatus.AMBIGUOUS:
            choices = match.candidates
        elif match.status is MatchStatus.MATCHED and match.selected is not None:
            choices = (match.selected,)
        else:
            choices = ()
        choices = matcher.expand_to_boards(choices)
        if not choices:
            return f"没有找到与「{shorten(query)}」匹配的榜单。"
        if len(choices) == 1:
            return await self.remember_board(
                origin, requester_key, choices[0].target.key
            )
        entry = self._candidates.remember(
            query,
            choices,
            view=CandidateView.WATCH_BOARD,
            origin=origin,
        )
        return format_candidates(entry, ttl_seconds=self._candidates.ttl_seconds)

    async def remember_board(
        self,
        origin: str,
        requester_key: str,
        boss_slug: str,
    ) -> str:
        """Add one board to a chat's list; also the target of a pick reply."""

        if not origin:
            return messages.NO_ORIGIN
        try:
            cards = await self._data.list_hot_bosses()
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot request failed: %s", type(exc).__name__
            )
            return messages.UPSTREAM_UNAVAILABLE
        card = next((card for card in cards if card.boss_slug == boss_slug), None)
        if card is None:
            return messages.BOARD_NOT_FOUND
        updated, added = self.watchlist.with_board(
            origin,
            WatchedBoard(
                boss_slug=card.boss_slug,
                boss_name=card.boss_name,
                dungeon_name=card.dungeon_name,
                added_by=requester_key,
                added_at=utc_now_text(),
            ),
        )
        if not self._save_watchlist(updated):
            return messages.WATCHLIST_WRITE_FAILED
        boards = self.watchlist.boards_for(origin)
        position = next(
            (
                index
                for index, board in enumerate(boards, start=1)
                if board.boss_slug == boss_slug
            ),
            len(boards),
        )
        label = board_label(card.dungeon_name, card.boss_name)
        if not added:
            return f"榜单「{label}」已经在关注列表里（第 {position} 位）。"
        self._seed_board_snapshot(card)
        return (
            f"已关注榜单「{label}」，序号 {position}。"
            "前三名有新纪录时会在这里通报。"
        )

    async def _add_account(
        self,
        query: str,
        origin: str,
        requester_key: str,
    ) -> str:
        """Resolve an id, link, or nickname, then remember it for this chat."""

        try:
            account_id = parse_account_reference(
                query,
                web_base_url=self._settings.web_base_url,
            )
        except PublicReferenceError:
            account_id = None
        stripped = searchable_nickname(query)
        try:
            if account_id is not None:
                account = await self._data.get_public_user_rankings(account_id)
                return await self.remember_account(
                    origin,
                    requester_key,
                    account_id=account.account_id,
                    display_name=account.account_display_name,
                )
            if stripped is None:
                return messages.ACCOUNT_REFERENCE_NEEDED
            search = await self._client.search_public_accounts(
                stripped,
                limit=MAX_CANDIDATES,
            )
        except ZmdLogsAPIError as exc:
            self._logger.warning("ZmdLogBot API request failed: %s", exc.code)
            if exc.status_code == 404:
                return messages.ACCOUNT_NOT_FOUND
            return messages.UPSTREAM_UNAVAILABLE
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot request failed: %s", type(exc).__name__
            )
            return messages.UPSTREAM_UNAVAILABLE
        if not search.accounts:
            return f"没有找到昵称包含「{shorten(stripped)}」的公开账号。"
        if len(search.accounts) == 1 and not search.has_more:
            hit = search.accounts[0]
            return await self.remember_account(
                origin,
                requester_key,
                account_id=hit.account_id,
                display_name=hit.account_display_name,
            )
        entry = self._candidates.remember(
            stripped,
            tuple(
                account_choice(hit.account_id, hit.account_display_name, query=stripped)
                for hit in search.accounts
            ),
            view=CandidateView.WATCH,
            origin=origin,
        )
        return format_candidates(
            entry,
            ttl_seconds=self._candidates.ttl_seconds,
            note=(
                "还有更多同名结果未列出，可输入更完整的昵称。"
                if search.has_more
                else None
            ),
        )

    async def remember_account(
        self,
        origin: str,
        requester_key: str,
        *,
        account_id: str,
        display_name: str,
    ) -> str:
        """Add one account to a chat's list; also the target of a pick reply."""

        if not origin:
            return messages.NO_ORIGIN
        updated, added = self.watchlist.with_account(
            origin,
            WatchedAccount(
                account_id=account_id,
                display_name=display_name,
                added_by=requester_key,
                added_at=utc_now_text(),
            ),
        )
        if not self._save_watchlist(updated):
            return messages.WATCHLIST_WRITE_FAILED
        accounts = self.watchlist.accounts_for(origin)
        position = next(
            (
                index
                for index, account in enumerate(accounts, start=1)
                if account.account_id == account_id
            ),
            len(accounts),
        )
        if not added:
            return f"{display_name} 已经在关注列表里（第 {position} 位）。"
        await self._seed_rank_snapshot(account_id)
        return (
            f"已关注 {display_name}（{account_id}），序号 {position}。"
            "TA 掉出榜单原名次时会在这里通报。"
        )

    # --- baselines -----------------------------------------------------------------

    def _seed_board_snapshot(self, card: HotBossCard) -> None:
        """Record the current top so the first notice needs one more cycle.

        Only when there is no baseline yet, for the same reason as accounts:
        another chat may already be waiting on the existing one.
        """

        if card.boss_slug in self.board_snapshots:
            return
        snapshots = dict(self.board_snapshots)
        snapshots[card.boss_slug] = build_board_snapshot(
            card, checked_at=utc_now_text()
        )
        self._save_board_snapshots(snapshots)

    def _forget_board_snapshot(self, boss_slug: str) -> None:
        if boss_slug in self.watchlist.origins_by_board():
            return
        if boss_slug not in self.board_snapshots:
            return
        snapshots = dict(self.board_snapshots)
        del snapshots[boss_slug]
        self._save_board_snapshots(snapshots)

    async def _seed_rank_snapshot(self, account_id: str) -> None:
        """Record current ranks so the first notice needs only one more cycle.

        Only when there is no baseline yet: refreshing an existing one would
        swallow a drop another chat is already waiting to be told about.
        """

        if account_id in self.rank_snapshots:
            return
        try:
            account = await self._data.get_public_user_rankings(account_id)
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot could not seed a rank baseline: %s",
                type(exc).__name__,
            )
            return
        checked_at = utc_now_text()
        snapshots = dict(self.rank_snapshots)
        snapshots[account_id] = build_snapshot(account, checked_at=checked_at)
        self._save_rank_snapshots(snapshots)
        # The trace starts with the ranks held at 关注 time, so the trend page
        # has a left edge before the first move.
        history, changed = record_rankings(
            self.rank_history, account, checked_at=checked_at
        )
        if changed:
            self._save_history(history)

    def _forget_rank_snapshot(self, account_id: str) -> None:
        """Drop the baseline and trace once nobody watches the account."""

        if account_id in self.watchlist.origins_by_account():
            return
        if account_id in self.rank_snapshots:
            snapshots = dict(self.rank_snapshots)
            del snapshots[account_id]
            self._save_rank_snapshots(snapshots)
        if account_id in self.rank_history:
            history = dict(self.rank_history)
            del history[account_id]
            self._save_history(history)

    # --- persistence -------------------------------------------------------------

    def _save_watchlist(self, updated: WatchList) -> bool:
        if not self.watchlist_store.save(updated.to_payload()):
            return False
        self.watchlist = updated
        return True

    def _save_rank_snapshots(self, snapshots: dict[str, AccountSnapshot]) -> None:
        """Persist the baseline; a silent failure would replay old notices.

        Written every cycle rather than only when a rank moved: ``checked_at``
        is what bounds staleness, so a stored baseline that stops advancing
        would eventually be rejected after a restart and silently re-seeded.
        """

        self.rank_snapshots = snapshots
        self.rank_snapshot_store.save(snapshot_payload(snapshots))

    def _save_board_snapshots(self, snapshots: dict[str, BoardSnapshot]) -> None:
        self.board_snapshots = snapshots
        self.board_snapshot_store.save(board_snapshot_payload(snapshots))

    def _save_history(self, history: dict[str, AccountHistory]) -> None:
        self.rank_history = history
        self.history_store.save(history_payload(history))

    # --- cycles ----------------------------------------------------------------------

    async def run_account_cycle(self) -> None:
        """One pass over every watched account, at most one request each."""

        watched = tuple(self.watchlist.origins_by_account().items())
        if not watched:
            if self.rank_snapshots:
                self._save_rank_snapshots({})
            return
        semaphore = asyncio.Semaphore(_RANK_WATCH_CONCURRENCY)
        results = await asyncio.gather(
            *(
                self._poll_account(account_id, semaphore)
                for account_id, _ in watched
            ),
            return_exceptions=True,
        )
        snapshots: dict[str, AccountSnapshot] = {}
        pending: dict[str, list[tuple[str, str]]] = {}
        live_names: dict[str, str] = {}
        history = self.rank_history
        history_changed = False
        for (account_id, origins), result in zip(watched, results, strict=True):
            if isinstance(result, tuple):
                snapshot, notice, account = result
                snapshots[account_id] = snapshot
                live_names[account_id] = account.account_display_name
                history, changed = record_rankings(
                    history,
                    account,
                    checked_at=snapshot.checked_at or utc_now_text(),
                )
                history_changed = history_changed or changed
                if notice is not None:
                    for origin in origins:
                        pending.setdefault(origin, []).append((account_id, notice))
                continue
            if isinstance(result, BaseException):
                self._logger.warning(
                    "ZmdLogBot rank watch failed for one account: %s",
                    type(result).__name__,
                )
            # Unreachable this cycle: keep the last known ranks so the next
            # comparison runs against real data instead of against a gap.
            previous = self.rank_snapshots.get(account_id)
            if previous is not None:
                snapshots[account_id] = previous
        # 关注 and 取关 can edit both maps while this cycle is awaiting, so
        # merge on top of the current state instead of installing the map this
        # cycle started from, and forget whoever is no longer watched.
        watching = self.watchlist.origins_by_account()
        # One new record demotes every watched account below it, so send each
        # chat a single merged message instead of a burst of near-identical
        # ones — skipping anything unfollowed while the cycle was running.
        undelivered = await self._deliver(
            pending, watching, join_rank_drop_notices
        )
        merged = dict(self.rank_snapshots)
        merged.update(
            {
                account_id: snapshot
                for account_id, snapshot in snapshots.items()
                if account_id not in undelivered
            }
        )
        self._save_rank_snapshots(
            {
                account_id: snapshot
                for account_id, snapshot in merged.items()
                if account_id in watching
            }
        )
        kept_history = {
            account_id: entry
            for account_id, entry in history.items()
            if account_id in watching
        }
        if history_changed or len(kept_history) != len(history):
            self._save_history(kept_history)
        renamed, changed = self.watchlist.with_display_names(live_names)
        if changed:
            self._save_watchlist(renamed)

    async def _poll_account(
        self,
        account_id: str,
        semaphore: asyncio.Semaphore,
    ) -> tuple[AccountSnapshot, str | None, PublicUserRankings] | None:
        """Fetch one account and return its ranks, notice text and the response.

        Every request this account needs stays inside the semaphore, and
        sending is left to the caller so that one chat receives one merged
        message per cycle rather than one message per watched account.
        """

        async with semaphore:
            try:
                account = await self._data.get_public_user_rankings(account_id)
            except ZmdLogsClientError as exc:
                self._logger.warning(
                    "ZmdLogBot rank watch skipped one account: %s",
                    type(exc).__name__,
                )
                return None
            checked_at = utc_now_text()
            snapshot = build_snapshot(account, checked_at=checked_at)
            previous = self.rank_snapshots.get(account_id)
            if not snapshot_is_usable(
                previous,
                now=checked_at,
                max_age_seconds=self._settings.rank_snapshot_max_age_seconds,
            ):
                # Too old to compare against, so re-seed quietly instead of
                # announcing everything that moved while nobody was looking.
                return snapshot, None, account
            drops = find_rank_drops(
                previous,
                account,
                rank_threshold=self._settings.rank_watch_rank_threshold,
            )
            if not drops:
                return snapshot, None, account
            notice = format_rank_drop_notice(
                account.account_display_name,
                await self._describe_drops(
                    drops,
                    account_id=account.account_id,
                    since=previous.checked_at,
                ),
                web_base_url=self._settings.web_base_url,
            )
        return snapshot, notice, account

    async def _describe_drops(
        self,
        drops: tuple[RankDrop, ...],
        *,
        account_id: str,
        since: str | None,
    ) -> tuple[RankDrop, ...]:
        """Look up what appeared above the account, for the drops shown.

        Only the boards the notice actually prints are fetched, so one account
        can never cost more than ``MAX_DROPS_PER_NOTICE`` extra requests.
        """

        described: list[RankDrop] = []
        for drop in drops[:MAX_DROPS_PER_NOTICE]:
            try:
                ranking = await self._data.get_boss_ranking(drop.boss_slug)
            except ZmdLogsClientError:
                described.append(drop)
                continue
            described.append(
                replace(
                    drop,
                    new_record_above=find_new_record_above(
                        ranking,
                        account_id=account_id,
                        fallback_rank=drop.current_rank,
                        since=since,
                    ),
                )
            )
        return tuple(described) + drops[MAX_DROPS_PER_NOTICE:]

    async def run_board_cycle(self) -> None:
        """One fresh ``hot-bosses`` read covers every watched board.

        The read bypasses the query cache on purpose: that cache may serve a
        stale payload or the on-disk snapshot, and diffing an *older* top list
        against the baseline would announce records that merely fell out of
        the top as new.
        """

        watched = self.watchlist.origins_by_board()
        if not watched:
            if self.board_snapshots:
                self._save_board_snapshots({})
            return
        try:
            cards, _ = await self._client.list_hot_bosses_with_payload()
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot board watch skipped this cycle: %s",
                type(exc).__name__,
            )
            return
        by_slug = {card.boss_slug: card for card in cards}
        checked_at = utc_now_text()
        snapshots: dict[str, BoardSnapshot] = {}
        pending: dict[str, list[tuple[str, str]]] = {}
        for boss_slug, origins in watched.items():
            card = by_slug.get(boss_slug)
            if card is None:
                # Gone from the index: keep the baseline, announce nothing.
                continue
            previous = self.board_snapshots.get(boss_slug)
            snapshots[boss_slug] = build_board_snapshot(card, checked_at=checked_at)
            if not board_snapshot_is_usable(
                previous,
                now=checked_at,
                max_age_seconds=self._settings.rank_snapshot_max_age_seconds,
            ):
                continue
            change = find_top_run_changes(previous, card)
            if change is None:
                continue
            notice = format_board_notice(
                change, web_base_url=self._settings.web_base_url
            )
            for origin in origins:
                pending.setdefault(origin, []).append((boss_slug, notice))
        # Same merge-on-live-state rule as the account cycle: 关注 / 取关 may
        # have run while the request was in flight.
        watching = self.watchlist.origins_by_board()
        undelivered = await self._deliver(pending, watching, join_board_notices)
        merged = dict(self.board_snapshots)
        merged.update(
            {
                boss_slug: snapshot
                for boss_slug, snapshot in snapshots.items()
                if boss_slug not in undelivered
            }
        )
        self._save_board_snapshots(
            {slug: snapshot for slug, snapshot in merged.items() if slug in watching}
        )


    async def _deliver(
        self,
        pending: dict[str, list[tuple[str, str]]],
        watching: dict[str, tuple[str, ...]],
        join: Callable[[tuple[str, ...]], str],
    ) -> set[str]:
        """Send one merged message per chat; return the keys that did not land.

        Delivery has to happen *before* the baseline moves. Saving first and
        sending second loses the event for good when a send fails: the next
        cycle compares against the ranks that were already stored and sees
        nothing to report, and a rank watch that silently drops the one thing
        it exists to report is worse than no rank watch. A key whose message
        did not arrive keeps its old baseline, so the next cycle computes the
        same change and tries again; ``snapshot_is_usable`` bounds that,
        because a baseline that stops advancing is eventually too old to
        compare against and is re-seeded silently.

        The cost is that a key several chats watch can be announced twice
        when only one of those chats was unreachable. Told twice beats never
        told.
        """

        undelivered: set[str] = set()
        for origin, entries in pending.items():
            wanted = tuple(
                (key, notice)
                for key, notice in entries
                if origin in watching.get(key, ())
            )
            if not wanted:
                continue
            text = join(tuple(notice for _, notice in wanted))
            if not await self.notify(origin, text):
                undelivered.update(key for key, _ in wanted)
        return undelivered


def _in(data_dir: Path | None, file_name: str) -> Path | None:
    return None if data_dir is None else data_dir / file_name
