"""Board watch: the 关注 / 取关 commands, the polling loop and its files.

One :class:`RankWatcher` per plugin owns the watch list, the board baselines
and the loop that polls them. A chat watches boards only — a list of them,
or 全部榜单 less an exclusion list (``core/watchlist``) — and is told when a
watched board's top three gains a record. Accounts are not watched: every
account's rank trend is recorded from the ranking index's board reads
(``core/history``), and bindings play no part in notices. It is free of
AstrBot: the chat an event came from, the sender's identity and the way a
notice is delivered all arrive as parameters, so the whole machine can be
driven from tests with fakes.

Two rules run through every cycle:

* One fresh ``hot-bosses`` read per cycle covers every watched board, and
  under 全部榜单 it is also what says which boards there are — so a board
  the site opens later is watched from the first cycle that sees it.
* Results are merged onto the *live* state, never installed wholesale:
  关注 / 取关 can run while a cycle is awaiting, and installing the map the
  cycle started from would drop a baseline just seeded and notify a chat
  that has since unfollowed.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import messages
from .candidates import CandidateStore, CandidateView
from .client import ZmdLogsClient, ZmdLogsClientError
from .datasource import ZmdLogsDataSource
from .logs import LogSink
from .matcher import (
    BOARD_QUERY_TARGETS,
    MatchChoice,
    MatchStatus,
    RankingMatcher,
)
from .messages import shorten
from .models import HotBossCard
from .outcome import Outcome
from .persistence import JsonStore
from .routing import RouteKind, RouteRequest
from .settings import PluginSettings
from .timestamps import utc_now_text
from .watch import (
    BoardSnapshot,
    Notice,
    board_snapshot_is_usable,
    board_snapshot_payload,
    build_board_snapshot,
    find_top_run_changes,
    format_board_notice,
    join_board_notices,
    parse_board_snapshot_payload,
)
from .watchlist import (
    ChatWatch,
    WatchedBoard,
    WatchList,
    board_label,
    format_watchlist,
    parse_watchlist,
)

WATCHLIST_FILE = "watchlist.json"
BOARD_SNAPSHOT_FILE = "board-snapshot.json"

# Returns whether the chat actually received the notice. A cycle only
# advances a baseline past what it managed to deliver.
Notify = Callable[[str, Notice], Awaitable[bool]]
BoardMatcher = Callable[[tuple[HotBossCard, ...]], RankingMatcher]


class RankWatcher:
    """Watch lists, board baselines and the loop that feeds them."""

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
        self.board_snapshot_store = JsonStore(
            _in(data_dir, BOARD_SNAPSHOT_FILE), label="board snapshot", warn=warn
        )
        self.watchlist: WatchList = parse_watchlist(self.watchlist_store.load())
        self._file_watchlist_under_groups()
        self.board_snapshots: dict[str, BoardSnapshot] = (
            parse_board_snapshot_payload(self.board_snapshot_store.load())
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
                await self.run_board_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("ZmdLogBot board watch cycle failed")

    # --- 关注 / 取关 ------------------------------------------------------------

    async def handle_route(
        self,
        route: RouteRequest,
        *,
        origin: str,
        requester_key: str,
        is_admin: bool,
        command: str,
    ) -> Outcome:
        """Maintain the board watch of one chat, in text; nothing is rendered.

        ``origin`` is the chat the command came from, ``requester_key`` the
        platform-scoped sender key, ``command`` the prefixed command name
        echoed in hints. Only an addition can post a pick list, which the
        outcome then carries.
        """

        if not origin:
            # Without a real origin every chat would share one list and the
            # notices would have nowhere to go.
            return Outcome(message=messages.NO_ORIGIN)
        kind = route.kind
        if kind is RouteKind.WATCH_LIST:
            # Reviewing and pruning stay available when polling is switched
            # off; only adding is refused.
            return Outcome(
                message=format_watchlist(self.watchlist.chat(origin), command=command)
            )
        if kind is RouteKind.UNWATCH_ALL:
            return Outcome(
                message=self._unwatch_all(origin, requester_key, is_admin=is_admin)
            )
        if kind is RouteKind.WATCH_REMOVE:
            return Outcome(
                message=await self._remove(
                    route.query,
                    origin,
                    requester_key,
                    is_admin=is_admin,
                    command=command,
                )
            )
        if not self.enabled:
            return Outcome(message=messages.WATCH_DISABLED)
        if kind is RouteKind.WATCH_ALL:
            return Outcome(
                message=await self._watch_all(origin, requester_key, command=command)
            )
        return await self._add_board(route.query, origin, requester_key)

    async def _add_board(
        self, query: str, origin: str, requester_key: str
    ) -> Outcome:
        """Resolve a board keyword the way queries do, then remember it.

        Dungeon and scope hits are flattened to their boards: the watch is on
        one board's top three, so several boards become a pick list. A
        keyword that names no board — an account's name included, since
        accounts are no longer watched — is a board that was not found.
        """

        found = await self._board_choices(query)
        if found is None:
            return Outcome(message=messages.UPSTREAM_UNAVAILABLE)
        _, choices = found
        if not choices:
            return Outcome(message=_no_board(query))
        if len(choices) == 1:
            added = await self.remember_board(
                origin, requester_key, choices[0].target.key
            )
            return Outcome(message=added)
        entry = self._candidates.remember(
            query,
            choices,
            view=CandidateView.WATCH_BOARD,
            origin=origin,
        )
        return Outcome.pick_list(entry, ttl_seconds=self._candidates.ttl_seconds)

    async def remember_board(
        self,
        origin: str,
        requester_key: str,
        boss_slug: str,
    ) -> str:
        """Add one board to a chat's watch; also the target of a pick reply.

        Under 全部榜单 the board is already covered, so adding it lifts its
        exclusion if it has one.
        """

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
        label = board_label(card.dungeon_name, card.boss_name)
        chat = self.watchlist.chat(origin)
        if chat.all_boards is not None:
            if chat.covers(boss_slug):
                return f"已经关注了全部榜单，「{label}」也在其中。"
            if not self._save_chat(origin, chat.including(boss_slug)):
                return messages.WATCHLIST_WRITE_FAILED
            self._seed_board_snapshots((card,))
            return f"已恢复关注榜单「{label}」，不再排除它。"
        updated, added = chat.with_board(
            _watched(card, requester_key)
        )
        if not self._save_chat(origin, updated):
            return messages.WATCHLIST_WRITE_FAILED
        position = next(
            index
            for index, board in enumerate(updated.boards, start=1)
            if board.boss_slug == boss_slug
        )
        if not added:
            return f"榜单「{label}」已经在关注列表里（第 {position} 位）。"
        self._seed_board_snapshots((card,))
        return (
            f"已关注榜单「{label}」，序号 {position}。"
            "前三名有新纪录时会在这里通报。"
        )

    async def _watch_all(
        self, origin: str, requester_key: str, *, command: str
    ) -> str:
        """关注 全部: every board, the ones the site opens later too."""

        chat = self.watchlist.chat(origin)
        if chat.all_boards is not None:
            if not chat.excluded:
                return "已经关注了全部榜单。"
            return (
                f"已经关注了全部榜单（排除了 {len(chat.excluded)} 张，"
                f"用 {command} 关注 <榜单关键词> 恢复）。"
            )
        updated = chat.following_all(added_by=requester_key, added_at=utc_now_text())
        if not self._save_chat(origin, updated):
            return messages.WATCHLIST_WRITE_FAILED
        # Seeding is a courtesy: a board without a baseline is seeded by the
        # first cycle that reads it, which only delays its first notice.
        try:
            cards = await self._data.list_hot_bosses()
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot could not seed board baselines: %s", type(exc).__name__
            )
        else:
            self._seed_board_snapshots(cards)
        return (
            "已关注全部榜单，以后新出的榜单也会自动包含。"
            "任一榜单前三名有新纪录时会在这里通报；"
            f"不想看的榜单用 {command} 取关 <榜单关键词> 排除。"
        )

    async def _remove(
        self,
        query: str,
        origin: str,
        requester_key: str,
        *,
        is_admin: bool,
        command: str,
    ) -> str:
        """取关 one board: off the list, or — under 全部榜单 — excluded."""

        chat = self.watchlist.chat(origin)
        if chat.all_boards is not None:
            return await self._exclude(
                query, origin, chat, requester_key, is_admin=is_admin, command=command
            )
        boards = chat.resolve_board_matches(query)
        if len(boards) > 1:
            names = "、".join(board.label for board in boards[:5])
            return f"「{shorten(query)}」匹配到多个关注的榜单：{names}，请改用序号。"
        if not boards:
            return (
                f"关注列表里没有榜单「{shorten(query)}」，"
                f"发送 {command} 关注 查看当前列表与序号。"
            )
        board = boards[0]
        if not board.removable_by(requester_key, is_admin=is_admin):
            return messages.WATCH_REMOVE_FORBIDDEN
        if not self._save_chat(origin, chat.without_board(board.boss_slug)):
            return messages.WATCHLIST_WRITE_FAILED
        return f"已取消关注榜单「{board.label}」。"

    async def _exclude(
        self,
        query: str,
        origin: str,
        chat: ChatWatch,
        requester_key: str,
        *,
        is_admin: bool,
        command: str,
    ) -> str:
        """Under 全部榜单, 取关 <关键词> excludes the one board it names.

        The keyword is resolved against every board, not against a list: the
        chat has none. An exclusion narrows the 全部榜单 entry, so it is
        whoever added that entry (or an admin) who may record one.
        """

        stripped = query.strip()
        if stripped.isascii() and stripped.isdecimal():
            return (
                "这里关注的是全部榜单，没有序号；"
                f"排除一张榜请用 {command} 取关 <榜单关键词>。"
            )
        if not chat.all_boards.removable_by(requester_key, is_admin=is_admin):
            return messages.WATCH_REMOVE_FORBIDDEN
        found = await self._board_choices(query)
        if found is None:
            return messages.UPSTREAM_UNAVAILABLE
        cards, choices = found
        if not choices:
            return _no_board(query)
        if len(choices) > 1:
            names = "、".join(choice.target.name for choice in choices[:5])
            return (
                f"「{shorten(query)}」匹配到多个榜单：{names}，"
                "请换一个只指向一张榜的关键词。"
            )
        slug = choices[0].target.key
        card = next((card for card in cards if card.boss_slug == slug), None)
        if card is None:
            return messages.BOARD_NOT_FOUND
        label = board_label(card.dungeon_name, card.boss_name)
        if not chat.covers(slug):
            return f"「{label}」已经排除了。"
        if not self._save_chat(origin, chat.excluding(_watched(card, requester_key))):
            return messages.WATCHLIST_WRITE_FAILED
        return (
            f"已在全部榜单中排除「{label}」，它的新纪录不再通报；"
            f"用 {command} 关注 <榜单关键词> 可以恢复。"
        )

    def _unwatch_all(
        self, origin: str, requester_key: str, *, is_admin: bool
    ) -> str:
        """取关 全部: the chat's whole watch, if every entry is theirs."""

        chat = self.watchlist.chat(origin)
        if chat.is_empty:
            return "这里还没有关注任何榜单。"
        if not chat.removable_entirely_by(requester_key, is_admin=is_admin):
            return messages.WATCH_REMOVE_FORBIDDEN
        if not self._save_chat(origin, ChatWatch()):
            return messages.WATCHLIST_WRITE_FAILED
        return "已取消这里的全部榜单关注。"

    async def _board_choices(
        self, query: str
    ) -> tuple[tuple[HotBossCard, ...], tuple[MatchChoice, ...]] | None:
        """The board list and the boards a keyword names in it, flattened.

        None when the board list cannot be read.
        """

        try:
            cards = await self._data.list_hot_bosses()
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot request failed: %s", type(exc).__name__
            )
            return None
        matcher = self._board_matcher(cards)
        match = matcher.match(query, allowed_types=BOARD_QUERY_TARGETS)
        if match.status is MatchStatus.AMBIGUOUS:
            choices = match.candidates
        elif match.status is MatchStatus.MATCHED and match.selected is not None:
            choices = (match.selected,)
        else:
            choices = ()
        return cards, matcher.expand_to_boards(choices)

    # --- baselines -----------------------------------------------------------------

    def _seed_board_snapshots(self, cards: tuple[HotBossCard, ...]) -> None:
        """Record the current top so the first notice needs one more cycle.

        Only where there is no baseline yet: another chat may already be
        waiting on the existing one.
        """

        missing = [card for card in cards if card.boss_slug not in self.board_snapshots]
        if not missing:
            return
        snapshots = dict(self.board_snapshots)
        checked_at = utc_now_text()
        for card in missing:
            snapshots[card.boss_slug] = build_board_snapshot(
                card, checked_at=checked_at
            )
        self._save_board_snapshots(snapshots)

    # --- persistence -------------------------------------------------------------

    def _file_watchlist_under_groups(self) -> None:
        """Once per load: entries kept under 隔离对话 move to their groups.

        The moved list is used even if the write fails; the move is repeated
        on the next load, so nothing is lost by it.
        """

        watchlist, moved = self.watchlist.with_group_origins()
        if not moved:
            return
        self.watchlist = watchlist
        if self.watchlist_store.save(watchlist.to_payload()):
            self._logger.info(
                "ZmdLogBot watch list: moved %s entries from member chats to groups.",
                moved,
            )

    def _save_chat(self, origin: str, chat: ChatWatch) -> bool:
        """Store one chat's watch; baselines nobody watches any more go."""

        updated = self.watchlist.with_chat(origin, chat)
        if not self.watchlist_store.save(updated.to_payload()):
            return False
        self.watchlist = updated
        unwatched = [slug for slug in self.board_snapshots if not updated.watches(slug)]
        if unwatched:
            self._save_board_snapshots(
                {
                    slug: snapshot
                    for slug, snapshot in self.board_snapshots.items()
                    if slug not in unwatched
                }
            )
        return True

    def _save_board_snapshots(self, snapshots: dict[str, BoardSnapshot]) -> None:
        """Persist the baseline; a silent failure would replay old notices."""

        self.board_snapshots = snapshots
        self.board_snapshot_store.save(board_snapshot_payload(snapshots))

    # --- cycle ------------------------------------------------------------------

    async def run_board_cycle(self) -> None:
        """One fresh ``hot-bosses`` read covers every watched board.

        The read bypasses the query cache on purpose: that cache may serve a
        stale payload or the on-disk snapshot, and diffing an *older* top list
        against the baseline would announce records that merely fell out of
        the top as new.
        """

        if not self.watchlist.chats:
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
        catalog = tuple(card.boss_slug for card in cards)
        watched = self.watchlist.origins_by_board(catalog)
        by_slug = {card.boss_slug: card for card in cards}
        checked_at = utc_now_text()
        snapshots: dict[str, BoardSnapshot] = {}
        pending: dict[str, list[tuple[str, Notice]]] = {}
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
                # A first sighting — a board just opened, or watched under
                # 全部榜单 for the first time — or a baseline too old to
                # compare against: seed quietly instead of announcing it.
                continue
            change = find_top_run_changes(previous, card)
            if change is None:
                continue
            notice = format_board_notice(
                change, web_base_url=self._settings.web_base_url
            )
            for origin in origins:
                pending.setdefault(origin, []).append((boss_slug, notice))
        # 关注 / 取关 may have run while the request was in flight, so merge
        # on top of the current state and forget whatever is unwatched now.
        watching = self.watchlist.origins_by_board(catalog)
        undelivered = await self._deliver(pending, watching)
        merged = dict(self.board_snapshots)
        merged.update(
            {
                boss_slug: snapshot
                for boss_slug, snapshot in snapshots.items()
                if boss_slug not in undelivered
            }
        )
        self._save_board_snapshots(
            {
                slug: snapshot
                for slug, snapshot in merged.items()
                if self.watchlist.watches(slug)
            }
        )

    async def _deliver(
        self,
        pending: dict[str, list[tuple[str, Notice]]],
        watching: dict[str, tuple[str, ...]],
    ) -> set[str]:
        """Send one merged message per chat; return the boards that did not land.

        Delivery has to happen *before* the baseline moves. Saving first and
        sending second loses the event for good when a send fails: the next
        cycle compares against the top that was already stored and sees
        nothing to report, and a watch that silently drops the one thing it
        exists to report is worse than no watch. A board whose message did
        not arrive keeps its old baseline, so the next cycle computes the
        same change and tries again; ``board_snapshot_is_usable`` bounds
        that, because a baseline that stops advancing is eventually too old
        to compare against and is re-seeded silently.

        The cost is that a board several chats watch can be announced twice
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
            notice = join_board_notices(tuple(notice for _, notice in wanted))
            if not await self.notify(origin, notice):
                undelivered.update(key for key, _ in wanted)
        return undelivered


def _watched(card: HotBossCard, requester_key: str) -> WatchedBoard:
    return WatchedBoard(
        boss_slug=card.boss_slug,
        boss_name=card.boss_name,
        dungeon_name=card.dungeon_name,
        added_by=requester_key,
        added_at=utc_now_text(),
    )


def _no_board(query: str) -> str:
    return f"没有找到与「{shorten(query)}」匹配的榜单。"


def _in(data_dir: Path | None, file_name: str) -> Path | None:
    return None if data_dir is None else data_dir / file_name
