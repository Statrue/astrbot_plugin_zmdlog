"""Cached reads of the public ZMDLogs API.

One :class:`AsyncTTLCache` per endpoint family merges concurrent loads of
the same key, and the board index (``hot-bosses``) is the only read that
may serve a stale entry or the on-disk snapshot when upstream is down: it
is what every keyword query starts from, so a short outage must not turn
every command into an error.
"""

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .board_changes import BoardChanges, NoticeWatch, board_changes
from .cache import AsyncTTLCache, CacheState
from .client import ZmdLogsAPIError, ZmdLogsClient, ZmdLogsClientError
from .events import EventLog
from .loadout import battle_suit_ids
from .logs import LogSink
from .metrics import METRIC_DPS
from .models import (
    BattleDetailSummary,
    BattleExport,
    BossRanking,
    BossRankingRow,
    CharacterBossStatistics,
    CharacterProfile,
    CharacterStatistics,
    CharacterType,
    HotBossCard,
    PublicUserRankings,
    parse_hot_bosses,
)
from .persistence import JsonStore, load_json, save_json
from .rank_trend import RankTrend
from .ranking_index import (
    SLOW_SIGNAL_SECONDS,
    RankingIndex,
    account_rankings,
    rows_by_battle,
)
from .timestamps import utc_now_text

HOT_BOSSES_SNAPSHOT = "hot-bosses.json"
# How long each endpoint family's answer is reused; fixed since #28, so a
# deployer cannot tune one into something broken.
CHARACTER_STATS_CACHE_TTL_SECONDS = 120.0
ACCOUNT_CACHE_TTL_SECONDS = 60.0
BATTLE_CACHE_TTL_SECONDS = 300.0
# The board list the index's signal reads stays fresh until the next signal
# has landed, at its slower pace and with that read's own latency, so a
# keyword query never pays the cold read while the index runs.
HOT_BOSSES_FRESH_SECONDS = SLOW_SIGNAL_SECONDS + 60.0
RECORD_EVENTS_FILE = "record-events.json"
RANK_HISTORY_FILE = "rank-history.json"
_HOT_BOSSES_KEY = "all_board_top3"
# A parsed battle carries its damage points and buff spans and measures
# 50-130 KB, an order of magnitude more than any other cached value, so the
# two battle caches get a tighter cap than the shared default.
BATTLE_CACHE_MAX_ENTRIES = 64
# A whole-history 角色档案 parses to about 130 KB (46 boards, 255 records on
# 2026-10-01), the battles' order of size, so it gets their cap.
PROFILE_CACHE_MAX_ENTRIES = 64
# Static game data: suits and characters change when the game adds content,
# which is months apart, never when someone uploads a run. The three catalogs
# are therefore kept for as long as the process runs in practice, and each is
# re-read when something it should name is not in it — a roster name without
# an element, a suit id without a name — because a missing entry is what new
# content looks like. Without that, a character released after start-up wore
# no ring and matched no --属性 for up to a month.
STATIC_CATALOG_TTL_SECONDS = 30 * 24 * 3600.0
EQUIP_CATALOG_TTL_SECONDS = STATIC_CATALOG_TTL_SECONDS
# A wrong name must not re-read a catalog every time someone mistypes (the
# global statistics response takes five seconds upstream); one refresh per
# this interval is enough.
CATALOG_REFRESH_MIN_INTERVAL_SECONDS = 10 * 60.0
_EQUIP_CATALOG_KEY = "equip_suits"
_CHARACTER_TYPES_KEY = "character_types"


class BoardWatch(Protocol):
    """The board watch as a board read sees it (``core/rank_watch``)."""

    def notice_watch(self, boss_slug: str) -> NoticeWatch | None: ...

    def collect(self, changes: BoardChanges) -> None: ...


@dataclass(frozen=True, slots=True)
class CharacterCatalogEntry:
    """One six-star character as the statistics endpoints know it."""

    name: str
    key: str


class ZmdLogsDataSource:
    """The plugin's read model: every upstream read goes through here."""

    def __init__(
        self,
        client: ZmdLogsClient,
        *,
        data_dir: Path | None,
        logger: LogSink,
    ) -> None:
        self.client = client
        self._snapshot_path = (
            None if data_dir is None else data_dir / HOT_BOSSES_SNAPSHOT
        )
        self._logger = logger
        self.hot_boss_cache = AsyncTTLCache[str, tuple[HotBossCard, ...]](
            HOT_BOSSES_FRESH_SECONDS,
            stale_ttl_seconds=HOT_BOSSES_FRESH_SECONDS,
        )
        self.account_cache = AsyncTTLCache[str, PublicUserRankings](
            ACCOUNT_CACHE_TTL_SECONDS,
        )
        self.character_stats_cache = AsyncTTLCache[
            tuple[str, str, str],
            CharacterStatistics,
        ](CHARACTER_STATS_CACHE_TTL_SECONDS)
        self.character_boss_cache = AsyncTTLCache[
            tuple[str, str, str],
            CharacterBossStatistics,
        ](CHARACTER_STATS_CACHE_TTL_SECONDS)
        self.character_profile_cache = AsyncTTLCache[
            tuple[str, str, str],
            CharacterProfile,
        ](
            CHARACTER_STATS_CACHE_TTL_SECONDS,
            max_entries=PROFILE_CACHE_MAX_ENTRIES,
        )
        self.battle_cache = AsyncTTLCache[str, BattleDetailSummary](
            BATTLE_CACHE_TTL_SECONDS,
            max_entries=BATTLE_CACHE_MAX_ENTRIES,
        )
        self.battle_export_cache = AsyncTTLCache[str, BattleExport](
            BATTLE_CACHE_TTL_SECONDS,
            max_entries=BATTLE_CACHE_MAX_ENTRIES,
        )
        self.equip_catalog_cache = AsyncTTLCache[str, dict[str, str]](
            EQUIP_CATALOG_TTL_SECONDS,
            stale_ttl_seconds=EQUIP_CATALOG_TTL_SECONDS,
            max_entries=1,
        )
        self.character_type_cache = AsyncTTLCache[str, dict[str, CharacterType]](
            STATIC_CATALOG_TTL_SECONDS,
            stale_ttl_seconds=STATIC_CATALOG_TTL_SECONDS,
            max_entries=1,
        )
        self._character_catalog: tuple[CharacterCatalogEntry, ...] | None = None
        self._character_catalog_loaded_at = 0.0
        self._character_catalog_lock = asyncio.Lock()
        # When each game-data catalog was last asked for, refresh included,
        # so a miss re-reads it at most once per interval.
        self._character_types_checked_at = float("-inf")
        self._equip_suits_checked_at = float("-inf")
        self._clock = time.monotonic
        # What every re-read of a board changed, kept in one bounded file.
        self.event_log = EventLog(
            JsonStore(
                None if data_dir is None else data_dir / RECORD_EVENTS_FILE,
                label="record events",
                warn=logger.warning,
            )
        )
        # Every account's rank on every board, as the board reads saw it move.
        self.rank_trend = RankTrend(
            JsonStore(
                None if data_dir is None else data_dir / RANK_HISTORY_FILE,
                label="rank history",
                warn=logger.warning,
                compact=True,
            )
        )
        # The board watch (``core/rank_watch``), once one attaches itself:
        # it brings who watches each board to every DPS read, and collects
        # the notices the read gives.
        self.board_watch: BoardWatch | None = None
        # Every board ranking is read through the index; see ranking_index.py.
        self.ranking_index = RankingIndex(
            fetch_ranking=self._fetch_boss_ranking,
            fetch_boards=self.refresh_hot_bosses,
            logger=logger,
            on_refresh=self._board_read,
        )
        self._caches = (
            self.hot_boss_cache,
            self.account_cache,
            self.character_stats_cache,
            self.character_boss_cache,
            self.character_profile_cache,
            self.battle_cache,
            self.battle_export_cache,
            self.equip_catalog_cache,
            self.character_type_cache,
        )

    def _board_read(self, previous: BossRanking | None, current: BossRanking) -> None:
        """Hand one read of a board to everything that records board changes.

        The one place a board change is discovered (``core/board_changes``).
        Each consumer runs on its own: one that fails is logged and the
        others still see the read.
        """

        # Asked before the read is recorded: a record this read brings is
        # announced by it, and must still count as new for the notices. Only
        # a DPS read has notices.
        announced = (
            self.event_log.announced(METRIC_DPS)
            if current.metric == METRIC_DPS
            else frozenset()
        )
        if previous is not None:
            self._record_safely(
                "record events", self.event_log.record, previous, current
            )
        watch = self.board_watch
        changes = board_changes(
            previous,
            current,
            seen_at=utc_now_text(),
            last_ranks=self.rank_trend.last_ranks(current.boss_slug),
            watch=None if watch is None else watch.notice_watch(current.boss_slug),
            announced=announced,
        )
        if changes is None:
            return
        self._record_safely("rank trend", self.rank_trend.apply, changes)
        if watch is not None:
            self._record_safely("board notices", watch.collect, changes)

    def _record_safely(self, what: str, record, *args) -> None:
        try:
            record(*args)
        except Exception as exc:
            self._logger.warning(
                "ZmdLogBot could not record the %s: %s", what, type(exc).__name__
            )

    async def list_hot_bosses(self) -> tuple[HotBossCard, ...]:
        """The board index; stale or from disk when upstream is unreachable."""

        return await self._stale_tolerant(
            self.hot_boss_cache, _HOT_BOSSES_KEY, self._fetch_hot_bosses, "hot-bosses"
        )

    async def _stale_tolerant(self, cache, key, loader, what: str):
        """Read through a cache that may serve its last value when upstream fails.

        Three reads work this way — the board list and the two game-data
        catalogs — because a page missing a whole section is worse than one
        a while out of date; the staleness is logged so it is not invisible.
        """

        result = await cache.get_or_load(key, loader, allow_stale_on_error=True)
        if result.state is CacheState.STALE:
            self._logger.warning(
                "ZmdLogBot is using stale %s data after a refresh failure.", what
            )
        return result.value

    async def refresh_hot_bosses(self) -> tuple[HotBossCard, ...]:
        """Read the board index from upstream now and warm the cache with it.

        The ranking index calls this on its own schedule, which is what keeps
        a keyword query from ever paying the cold hot-bosses read.
        """

        cards = await self._fetch_hot_bosses_upstream()
        await self.hot_boss_cache.put(_HOT_BOSSES_KEY, cards)
        return cards

    async def _fetch_hot_bosses_upstream(self) -> tuple[HotBossCard, ...]:
        cards, payload = await self.client.list_hot_bosses_with_payload()
        if self._snapshot_path is not None:
            save_json(self._snapshot_path, payload)
        return cards

    async def _fetch_hot_bosses(self) -> tuple[HotBossCard, ...]:
        """Fetch the board index; fall back to the on-disk snapshot if needed."""

        snapshot_path = self._snapshot_path
        try:
            return await self._fetch_hot_bosses_upstream()
        except ZmdLogsClientError as upstream_error:
            payload = None if snapshot_path is None else load_json(snapshot_path)
            if payload is None:
                raise
            try:
                cards = parse_hot_bosses(payload)
            except Exception:
                # A corrupt snapshot must not mask the real upstream failure.
                raise upstream_error from None
            self._logger.warning(
                "ZmdLogBot is serving the board index from the local snapshot."
            )
            return cards

    async def get_equip_suits(
        self, *, wanted: Iterable[str] = ()
    ) -> dict[str, str]:
        """Suit id to display name, from the game data catalog.

        The one read whose answer is the same for every battle, so it is
        kept for a month and may be served stale: a gear label going missing
        for a whole page is worse than a label a while out of date. ``wanted``
        are the suit ids a page is about to print; one the catalog lacks asks
        for a bounded re-read, because a new suit looks exactly like that.
        """

        suits = await self._stale_tolerant(
            self.equip_catalog_cache,
            _EQUIP_CATALOG_KEY,
            self._fetch_equip_suits,
            "equip catalog",
        )
        if self._lacks(suits, wanted) and self._may_refresh(
            self._equip_suits_checked_at
        ):
            suits = await self._refresh_catalog(
                self.equip_catalog_cache,
                _EQUIP_CATALOG_KEY,
                self._fetch_equip_suits,
                "equip catalog",
                held=suits,
            )
        return suits

    async def equip_suits_for(
        self, *battles: BattleDetailSummary
    ) -> dict[str, str]:
        """The suit catalog for the gear these battles wore, or nothing.

        A gear page renders without it (the label falls back to what
        upstream wrote), so an unreachable catalog is a warning, not a
        failure. The battles name the suits the page is about to print,
        which is what lets the catalog notice one it has never heard of.
        """

        try:
            return await self.get_equip_suits(wanted=battle_suit_ids(*battles))
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot equip catalog unavailable: %s", type(exc).__name__
            )
            return {}

    async def _fetch_equip_suits(self) -> dict[str, str]:
        self._equip_suits_checked_at = self._clock()
        suits = await self.client.get_equip_catalog()
        return {suit.suit_id: suit.name for suit in suits}

    async def get_character_types(
        self, *, names: Iterable[str] = ()
    ) -> dict[str, CharacterType]:
        """Name to element and weapon type; kept for a month like the suits.

        ``names`` are the characters the caller is about to draw. One the
        catalog lacks is what a character released since the last read looks
        like, so it asks for one re-read, at most once per
        ``CATALOG_REFRESH_MIN_INTERVAL_SECONDS``; a typo costs nothing more.
        """

        types = await self._stale_tolerant(
            self.character_type_cache,
            _CHARACTER_TYPES_KEY,
            self._fetch_character_types,
            "character catalog",
        )
        if self._lacks(types, names) and self._may_refresh(
            self._character_types_checked_at
        ):
            types = await self._refresh_catalog(
                self.character_type_cache,
                _CHARACTER_TYPES_KEY,
                self._fetch_character_types,
                "character catalog",
                held=types,
            )
        return types

    async def _fetch_character_types(self) -> dict[str, CharacterType]:
        self._character_types_checked_at = self._clock()
        return {entry.name: entry for entry in await self.client.get_character_types()}

    @staticmethod
    def _lacks(catalog, keys: Iterable[str]) -> bool:
        return any(key and key not in catalog for key in keys)

    def _may_refresh(self, checked_at: float) -> bool:
        return self._clock() - checked_at >= CATALOG_REFRESH_MIN_INTERVAL_SECONDS

    async def _refresh_catalog(self, cache, key, loader, what: str, *, held):
        """Re-read one catalog for a missing entry; keep ``held`` on failure.

        The held copy is still the best answer there is, and the failure is
        logged rather than raised because the page asking is drawn without
        the catalog anyway.
        """

        try:
            value = await loader()
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot could not refresh the %s for a missing entry: %s",
                what,
                type(exc).__name__,
            )
            return held
        await cache.put(key, value)
        return value

    async def get_character_catalog(
        self, *, refresh: bool = False
    ) -> tuple[CharacterCatalogEntry, ...]:
        """Every six-star character's name and key, kept for the long run.

        The list comes from the global statistics response, which upstream
        takes seconds to compute; a name lookup must not pay that every two
        minutes. ``refresh`` asks for a re-read because a name was not found,
        bounded to one per ``CATALOG_REFRESH_MIN_INTERVAL_SECONDS``.
        """

        async with self._character_catalog_lock:
            now = self._clock()
            age = now - self._character_catalog_loaded_at
            expired = (
                self._character_catalog is None or age > STATIC_CATALOG_TTL_SECONDS
            )
            wanted = refresh and age >= CATALOG_REFRESH_MIN_INTERVAL_SECONDS
            if expired or wanted:
                try:
                    if expired:
                        # The first read shares the statistics cache, so a
                        # 角色统计 page just drawn costs no second request.
                        stats = await self.get_character_statistics(
                            None, time_range="all", potential="all"
                        )
                    else:
                        stats = await self.client.get_character_statistics(
                            None, time_range="all", potential="all"
                        )
                except ZmdLogsClientError:
                    if self._character_catalog is None:
                        raise
                    self._logger.warning(
                        "ZmdLogBot is keeping the character catalog "
                        "after a refresh failure."
                    )
                else:
                    self._character_catalog = tuple(
                        CharacterCatalogEntry(row.character_name, row.character_key)
                        for row in stats.rows
                    )
                    self._character_catalog_loaded_at = now
            assert self._character_catalog is not None
            return self._character_catalog

    async def get_boss_ranking(
        self,
        boss_slug: str,
        *,
        metric: str = METRIC_DPS,
        on_demand: bool = False,
    ) -> BossRanking:
        """One board's ranking, the copy the index holds.

        Upstream is asked, and waited for, only when the index holds none.
        ``on_demand`` is for a query about this one board — the board page,
        the board tool: a copy not checked for two minutes is then re-read in
        the background, so asking again shows what changed. Every other
        reader takes the copy as it is. ``metric`` picks the DPS or the rDPS board; the
        index holds both.
        """

        return await self.ranking_index.get(
            boss_slug, metric=metric, on_demand=on_demand
        )

    async def get_account_rankings(self, account_id: str) -> PublicUserRankings:
        """The account's rank on every board, from the index when it is complete.

        This is what the account page draws, and it costs nothing once the
        index is filled. The endpoint
        answers when the index is not complete yet or holds no row for the
        account.
        """

        if self.ranking_index.complete:
            derived = account_rankings(self.ranking_index.entries(), account_id)
            if derived is not None:
                return derived
        return await self.get_public_user_rankings(account_id)

    async def _fetch_boss_ranking(self, boss_slug: str, metric: str) -> BossRanking:
        return await self.client.get_boss_rankings(boss_slug, metric=metric)

    def start(self) -> None:
        """Start the background ranking index.

        Always: with every board read off the index, a copy nobody refreshed
        would be served as it was for as long as the process runs.
        """

        self.ranking_index.start()

    async def get_character_statistics(
        self,
        boss_slug: str | None,
        *,
        time_range: str,
        potential: str,
        metric: str = METRIC_DPS,
    ) -> CharacterStatistics:
        key = (boss_slug or "all", time_range, potential, metric)
        result = await self.character_stats_cache.get_or_load(
            key,
            lambda: self.client.get_character_statistics(
                boss_slug,
                time_range=time_range,
                potential=potential,
                metric=metric,
            ),
        )
        return result.value

    async def get_character_boss_statistics(
        self,
        character_key: str,
        *,
        time_range: str,
        potential: str,
        metric: str = METRIC_DPS,
    ) -> CharacterBossStatistics:
        key = (character_key, time_range, potential, metric)
        result = await self.character_boss_cache.get_or_load(
            key,
            lambda: self.client.get_character_boss_statistics(
                character_key,
                time_range=time_range,
                potential=potential,
                metric=metric,
            ),
        )
        return result.value

    async def get_character_profile(
        self,
        character_key: str,
        *,
        time_range: str,
        boss_slug: str | None = None,
    ) -> CharacterProfile:
        """One character's 角色档案, kept as long as the statistics are."""

        key = (character_key, time_range, boss_slug or "")
        result = await self.character_profile_cache.get_or_load(
            key,
            lambda: self.client.get_character_profile(
                character_key, time_range=time_range, boss_slug=boss_slug
            ),
        )
        return result.value

    async def character_elements(
        self, *, names: Iterable[str] = ()
    ) -> dict[str, str]:
        """Name to element label; empty when the catalog is unreachable.

        ``names`` (the characters about to be drawn) let a newcomer trigger
        the catalog's bounded re-read; see :meth:`get_character_types`.
        """

        try:
            types = await self.get_character_types(names=names)
        except ZmdLogsClientError:
            return {}
        return {name: entry.element for name, entry in types.items()}

    async def character_icons(
        self, *, names: Iterable[str] = ()
    ) -> dict[str, str]:
        """Name to portrait path; empty when the catalog is unreachable.

        Ranking rows carry a portrait per roster entry, but a record on a
        board the index does not hold reaches a page as names alone. The
        game-data catalog names the file for every character of every
        rarity, so the face survives.
        """

        try:
            types = await self.get_character_types(names=names)
        except ZmdLogsClientError:
            return {}
        return {
            name: entry.icon_path
            for name, entry in types.items()
            if entry.icon_path
        }

    async def character_professions(
        self, *, names: Iterable[str] = ()
    ) -> dict[str, str]:
        """Name to profession as the catalog spells it; empty when unreachable."""

        try:
            types = await self.get_character_types(names=names)
        except ZmdLogsClientError:
            return {}
        return {
            name: entry.profession for name, entry in types.items() if entry.profession
        }

    async def account_rankings_for_page(
        self, account_id: str
    ) -> tuple[
        PublicUserRankings, dict[str, BossRankingRow], frozenset[str] | None
    ]:
        """The account page's records, the index rows of them, and 全部榜单.

        The records are :meth:`get_account_rankings`'s: read off the index
        once it is complete and holds the account, which takes no request
        where ``users/{id}/rankings`` took 0.6–5 s upstream (#27). A
        half-filled index would drop boards without a word, so until it is
        complete the endpoint answers, as it does for an account seen only
        on boards outside 全部榜单, a first upload the index has not re-read
        yet, or an id that does not exist — asked before the fill is waited
        for, so a wrong id or an outage answers at once on a cold boot.

        Then it waits for a fill in progress, never for a failed one, for the
        index's rows of the same battles: the endpoint carries no main C.
        The slugs, ``None`` when the board list could not be read, tell such
        a board apart from one not read yet.
        """

        index = self.ranking_index
        account = await self.get_account_rankings(account_id)
        try:
            await index.wait_filled()
        except ZmdLogsClientError:
            pass
        rows = rows_by_battle(
            index.entries(), (row.battle_id for row in account.rankings)
        )
        return account, rows, frozenset(index.slugs) or None

    async def get_public_user_rankings(self, account_id: str) -> PublicUserRankings:
        result = await self.account_cache.get_or_load(
            account_id,
            lambda: self.client.get_public_user_rankings(account_id),
        )
        return result.value

    async def get_battle_detail(self, battle_id: str) -> BattleDetailSummary:
        result = await self.battle_cache.get_or_load(
            battle_id,
            lambda: self.client.get_battle_detail(battle_id),
        )
        return result.value

    async def battle_export_for_card(
        self, battle_id: str
    ) -> tuple[BattleExport | None, ZmdLogsAPIError | None]:
        """The cast sequence for 战报, or the API's reason without.

        Best effort: the 摘要 must never fail because the export did. An
        upstream refusal (an old upload, a rate limit) comes back so the
        page can tell an upload with no casts from one merely refused; an
        outage is logged and the casts are simply left out.
        """

        try:
            return await self.get_battle_export(battle_id), None
        except ZmdLogsAPIError as exc:
            self._logger.warning("ZmdLogBot battle export unavailable: %s", exc.code)
            return None, exc
        except ZmdLogsClientError as exc:
            self._logger.warning(
                "ZmdLogBot battle export unavailable: %s", type(exc).__name__
            )
            return None, None

    async def get_battle_export(self, battle_id: str) -> BattleExport:
        result = await self.battle_export_cache.get_or_load(
            battle_id,
            lambda: self.client.get_battle_export(battle_id),
        )
        return result.value

    async def close(self) -> None:
        """Stop the index, save the rank trend, drop every cached value."""

        await self.ranking_index.stop()
        self.rank_trend.flush()
        for cache in self._caches:
            await cache.close()
