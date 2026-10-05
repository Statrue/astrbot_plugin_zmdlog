"""Board watch: the 关注 / 取关 commands, the notice batches and their files.

One :class:`RankWatcher` per plugin owns the watch list, the board snapshots
and the loop that sends what the board reads found. A chat watches boards
only — a list of them, or 全部 less an exclusion list (``core/watchlist``)
— and is sent a 顶屁股通告 picture when a new record enters a watched
board's top N (``rank_watch_rank_threshold``). It is free of AstrBot: the
chat an event came from, the sender's identity and the way a batch is drawn
and delivered all arrive as parameters, so the whole machine can be driven
from tests with fakes.

Nothing here reads upstream for the watch. The ranking index re-reads every
board on its own schedule, and the data source hands each DPS read to
:func:`core.board_changes.board_changes` with what this watch brings
(:meth:`RankWatcher.notice_watch`: who watches, N, the board's snapshot);
the entries that come out are queued here per chat
(:meth:`RankWatcher.collect`). Under 全部 a board the site opens later is
watched from its first read.

Every ``rank_watch_interval_seconds`` (plus up to a tenth of it at random,
so the pushes do not land on the same second every time) each chat with
entries waiting is sent one picture of all of them
(:meth:`RankWatcher.run_notice_cycle`). Three rules run through it:

* **Cleared only once delivered.** A chat's entries leave its queue when the
  send reports them delivered; a failed send keeps them, and the next
  interval sends them again with whatever came since. Entries older than
  the snapshot age (max(3 × interval, 1 h)) are dropped unsent: a chat that
  cannot be reached for that long gets a fresh start, not a backlog.
* **A snapshot moves only past what was delivered.** At the end of each
  interval a board's snapshot becomes the latest read of it — unless some
  chat still has an entry of that board waiting. A restart loses the
  queues, which live in memory, but not the snapshot, so the first read
  after it finds the waiting records again. The cost is that a board
  several chats watch is told twice to the chats that did get it when
  another was unreachable across a restart. Told twice beats never told.
* **The watch list is read live.** 关注 / 取关 can run while a batch is
  being sent, so a chat is sent only the entries of boards it still
  watches, and a snapshot is kept only for a board somebody watches.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from . import messages
from .board_changes import (
    BoardChanges,
    BoardSnapshot,
    NoticeEntry,
    NoticeWatch,
    snapshot_is_fresh,
)
from .candidates import CandidateStore, CandidateView
from .client import ZmdLogsClientError
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
from .timestamps import parse_timestamp, utc_now_text
from .watch import (
    NoticeBatch,
    board_snapshot_payload,
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

# Draws and sends one chat's batch; returns whether the chat received it.
# Entries leave the queue, and snapshots move, only past what it delivered.
Notify = Callable[[str, NoticeBatch], Awaitable[bool]]
BoardMatcher = Callable[[tuple[HotBossCard, ...]], RankingMatcher]


class RankWatcher:
    """Watch lists, board snapshots, the queues and the loop that sends them."""

    def __init__(
        self,
        *,
        data: ZmdLogsDataSource | None,
        settings: PluginSettings,
        data_dir: Path | None,
        board_matcher: BoardMatcher,
        candidates: CandidateStore,
        notify: Notify,
        logger: LogSink,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._data = data
        self._settings = settings
        self._board_matcher = board_matcher
        self._candidates = candidates
        # Public so the host can swap the delivery path (tests capture it).
        self.notify = notify
        self._logger = logger
        self._now = now
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
        # Board slug → its top N at the latest read this process made: what
        # its snapshot becomes once nothing of the board is waiting.
        self._latest: dict[str, BoardSnapshot] = {}
        # Chat origin → the entries waiting for its next picture, in the
        # order they were found.
        self._pending: dict[str, list[NoticeEntry]] = {}
        self._window_start = self._stamp()
        self._task: asyncio.Task[None] | None = None
        if data is not None:
            data.board_watch = self

    # --- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Start the loop; idempotent, and a no-op without a data directory."""

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
        """Send every interval; one failed cycle must never end the loop."""

        while True:
            interval = self._settings.rank_watch_interval_seconds
            await asyncio.sleep(interval + random.uniform(0.0, interval * 0.1))
            try:
                await self.run_notice_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("ZmdLogBot board notice cycle failed")

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
            # Reviewing and pruning stay available when notices are switched
            # off; only adding is refused.
            return Outcome(
                message=format_watchlist(
                    self.watchlist.chat(origin), command=command, top_n=self._top_n
                )
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
                message=self._watch_all(origin, requester_key, command=command)
            )
        return await self._add_board(route.query, origin, requester_key)

    async def _add_board(
        self, query: str, origin: str, requester_key: str
    ) -> Outcome:
        """Resolve a board keyword the way queries do, then remember it.

        Dungeon and scope hits are flattened to their boards: the watch is on
        one board's top N, so several boards become a pick list. A
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
            self._seed_snapshots((boss_slug,))
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
        self._seed_snapshots((boss_slug,))
        return (
            f"已关注榜单「{label}」，序号 {position}。"
            f"新纪录进入前 {self._top_n} 名时会在这里通报。"
        )

    def _watch_all(self, origin: str, requester_key: str, *, command: str) -> str:
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
        self._seed_snapshots(tuple(self._latest))
        return (
            "已关注全部榜单，以后新出的榜单也会自动包含。"
            f"任一榜单有新纪录进入前 {self._top_n} 名时会在这里通报；"
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

    # --- what the board reads find ----------------------------------------------

    @property
    def _top_n(self) -> int:
        return self._settings.rank_watch_rank_threshold

    def notice_watch(self, boss_slug: str) -> NoticeWatch | None:
        """What a read of ``boss_slug`` is compared with; None while off."""

        if not self.enabled or not self.watchlist_store.available:
            return None
        return NoticeWatch(
            watchlist=self.watchlist,
            top_n=self._top_n,
            snapshot=self.board_snapshots.get(boss_slug),
            snapshot_max_age_seconds=self._settings.rank_snapshot_max_age_seconds,
        )

    def collect(self, changes: BoardChanges) -> None:
        """Queue a read's entries per chat and keep its top N for the snapshot."""

        if changes.snapshot is not None:
            self._latest[changes.boss_slug] = changes.snapshot
        for origin, entries in changes.notices.items():
            self._pending.setdefault(origin, []).extend(entries)

    def pending(self, origin: str) -> tuple[NoticeEntry, ...]:
        """The entries waiting for ``origin``'s next picture."""

        return tuple(self._pending.get(origin, ()))

    def _seed_snapshots(self, slugs: tuple[str, ...]) -> None:
        """Snapshot newly watched boards now, not at the interval's end.

        Only boards read since the start and not snapshotted yet — another
        chat may already be waiting on an existing snapshot. A courtesy: a
        board without one is snapshotted at the interval's end, and only a
        restart before then would lose what was found meanwhile.
        """

        stamp = self._stamp()
        missing = {
            slug: replace(self._latest[slug], checked_at=stamp)
            for slug in slugs
            if slug in self._latest
            and slug not in self.board_snapshots
            and self.watchlist.watches(slug)
        }
        if missing:
            self._save_board_snapshots({**self.board_snapshots, **missing})

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
        """Store one chat's watch; snapshots nobody watches any more go."""

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
        self.board_snapshots = snapshots
        self.board_snapshot_store.save(board_snapshot_payload(snapshots))

    # --- cycle ------------------------------------------------------------------

    async def run_notice_cycle(self) -> None:
        """Send every chat its waiting entries as one picture; move snapshots.

        The module docstring has the rules. The window a picture names runs
        from the previous cycle to this one, or from the oldest entry it
        carries when a failed send held that one over.
        """

        now = self._now().replace(microsecond=0)
        stamp = now.isoformat()
        window_start, self._window_start = self._window_start, stamp
        self._drop_expired(now)
        for origin in tuple(self._pending):
            chat = self.watchlist.chat(origin)
            queue = [
                entry for entry in self._pending[origin] if chat.covers(entry.boss_slug)
            ]
            if not queue:
                del self._pending[origin]
                continue
            self._pending[origin] = queue
            batch = NoticeBatch(
                entries=tuple(queue),
                window_start=min(
                    (window_start, *(entry.seen_at for entry in queue)),
                    key=_instant,
                ),
                window_end=stamp,
                top_n=self._top_n,
            )
            if not await self.notify(origin, batch):
                continue
            # What was found while the send was out queued up behind it.
            waiting = self._pending.get(origin, [])
            del waiting[: len(batch.entries)]
            if not waiting:
                self._pending.pop(origin, None)
        self._advance_snapshots(stamp)

    def _drop_expired(self, now: datetime) -> None:
        max_age = self._settings.rank_snapshot_max_age_seconds
        for origin, queue in tuple(self._pending.items()):
            kept = [
                entry
                for entry in queue
                if (seen := parse_timestamp(entry.seen_at)) is not None
                and (now - seen).total_seconds() <= max_age
            ]
            if len(kept) < len(queue):
                self._logger.warning(
                    "ZmdLogBot dropped %s board notices it could not deliver in time.",
                    len(queue) - len(kept),
                )
            if kept:
                self._pending[origin] = kept
            else:
                del self._pending[origin]

    def _advance_snapshots(self, stamp: str) -> None:
        """Every watched board's snapshot moves to its latest read, stamped
        now, unless an entry of it is still waiting. Unwatched boards' go,
        and so do snapshots too old to be compared with any more — a board
        the site stopped listing, one upstream has failed to serve for an
        hour: either way its next read is a new start."""

        waiting = {
            entry.boss_slug for queue in self._pending.values() for entry in queue
        }
        watches = self.watchlist.watches
        max_age = self._settings.rank_snapshot_max_age_seconds
        snapshots = {
            slug: snapshot
            for slug, snapshot in self.board_snapshots.items()
            if watches(slug)
            and snapshot_is_fresh(snapshot, now=stamp, max_age_seconds=max_age)
        }
        snapshots.update(
            {
                slug: replace(latest, checked_at=stamp)
                for slug, latest in self._latest.items()
                if watches(slug) and slug not in waiting
            }
        )
        if snapshots or self.board_snapshots:
            self._save_board_snapshots(snapshots)

    def _stamp(self) -> str:
        """Now, to the second, as every board read is stamped
        (``utc_now_text``): kept to the microsecond, a snapshot would read
        as later than a read made after it within the same second — a
        snapshot from the future, which is never fresh."""

        return self._now().replace(microsecond=0).isoformat()


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


def _instant(stamp: str) -> float:
    parsed = parse_timestamp(stamp)
    return parsed.timestamp() if parsed is not None else float("inf")
