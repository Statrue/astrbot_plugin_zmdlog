"""Cached reads of the public ZMDLogs API.

One :class:`AsyncTTLCache` per endpoint family merges concurrent loads of
the same key, and the board index (``hot-bosses``) is the only read that
may serve a stale entry or the on-disk snapshot when upstream is down: it
is what every keyword query starts from, so a short outage must not turn
every command into an error.
"""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from .cache import AsyncTTLCache, CacheState
from .client import ZmdLogsClient, ZmdLogsClientError
from .events import EventLog
from .logs import LogSink
from .models import (
    BattleDetailSummary,
    BattleExport,
    BossRanking,
    CharacterBossStatistics,
    CharacterStatistics,
    CharacterType,
    HotBossCard,
    PublicUserRankings,
    parse_hot_bosses,
)
from .persistence import JsonStore, load_json, save_json
from .ranking_index import RankingIndex, account_rankings
from .settings import PluginSettings

HOT_BOSSES_SNAPSHOT = "hot-bosses.json"
RECORD_EVENTS_FILE = "record-events.json"
_HOT_BOSSES_KEY = "all_board_top3"
# A parsed battle carries its damage points and buff spans and measures
# 50-130 KB, an order of magnitude more than any other cached value, so the
# two battle caches get a tighter cap than the shared default.
BATTLE_CACHE_MAX_ENTRIES = 64
# Static game data: suits and six-star characters change when the game adds
# content, which is months apart, never when someone uploads a run. Both
# catalogs are therefore kept for as long as the process runs in practice,
# and the character catalog is re-read when a name is not in it, because a
# missing name is what a new character looks like.
STATIC_CATALOG_TTL_SECONDS = 30 * 24 * 3600.0
EQUIP_CATALOG_TTL_SECONDS = STATIC_CATALOG_TTL_SECONDS
# A wrong name must not re-read the five-second global statistics response
# every time someone mistypes; one refresh per this interval is enough.
CATALOG_REFRESH_MIN_INTERVAL_SECONDS = 10 * 60.0
_EQUIP_CATALOG_KEY = "equip_suits"
_CHARACTER_TYPES_KEY = "character_types"


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
        settings: PluginSettings,
        data_dir: Path | None,
        logger: LogSink,
    ) -> None:
        self.client = client
        self._snapshot_path = (
            None if data_dir is None else data_dir / HOT_BOSSES_SNAPSHOT
        )
        self._logger = logger
        ranking_ttl = settings.ranking_cache_ttl_seconds
        stats_ttl = settings.character_stats_cache_ttl_seconds
        battle_ttl = settings.battle_cache_ttl_seconds
        self.hot_boss_cache = AsyncTTLCache[str, tuple[HotBossCard, ...]](
            ranking_ttl,
            stale_ttl_seconds=ranking_ttl,
        )
        self._ranking_max_age = ranking_ttl
        self.account_cache = AsyncTTLCache[str, PublicUserRankings](
            settings.account_cache_ttl_seconds,
        )
        self.character_stats_cache = AsyncTTLCache[
            tuple[str, str, str],
            CharacterStatistics,
        ](stats_ttl)
        self.character_boss_cache = AsyncTTLCache[
            tuple[str, str, str],
            CharacterBossStatistics,
        ](stats_ttl)
        self.battle_cache = AsyncTTLCache[str, BattleDetailSummary](
            battle_ttl,
            max_entries=BATTLE_CACHE_MAX_ENTRIES,
        )
        self.battle_export_cache = AsyncTTLCache[str, BattleExport](
            battle_ttl,
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
        self._clock = time.monotonic
        # What every re-read of a board changed, kept in one bounded file.
        self.event_log = EventLog(
            JsonStore(
                None if data_dir is None else data_dir / RECORD_EVENTS_FILE,
                label="record events",
                warn=logger.warning,
            )
        )
        # Every board ranking is read through the index; see ranking_index.py.
        self.ranking_index = RankingIndex(
            fetch_ranking=self._fetch_boss_ranking,
            fetch_boards=self.refresh_hot_bosses,
            pace_seconds=settings.ranking_index_pace_seconds,
            logger=logger,
            on_refresh=self.event_log.record,
        )
        self._ranking_index_enabled = settings.ranking_index_enabled
        self._caches = (
            self.hot_boss_cache,
            self.account_cache,
            self.character_stats_cache,
            self.character_boss_cache,
            self.battle_cache,
            self.battle_export_cache,
            self.equip_catalog_cache,
            self.character_type_cache,
        )

    async def list_hot_bosses(self) -> tuple[HotBossCard, ...]:
        """The board index; stale or from disk when upstream is unreachable."""

        result = await self.hot_boss_cache.get_or_load(
            _HOT_BOSSES_KEY,
            self._fetch_hot_bosses,
            allow_stale_on_error=True,
        )
        if result.state is CacheState.STALE:
            self._logger.warning(
                "ZmdLogBot is using stale hot-bosses data after refresh failure."
            )
        return result.value

    async def refresh_hot_bosses(self) -> tuple[HotBossCard, ...]:
        """Read the board index from upstream now and warm the cache with it.

        The ranking index calls this on its own schedule, which is what keeps
        a keyword query from ever paying the cold hot-bosses read.
        """

        cards = await self._fetch_hot_bosses_upstream()
        # Fresh until the index reads again (plus a margin), whatever the query
        # TTL is: a shorter freshness would leave a gap before the next read
        # in which a keyword query pays the cold read after all.
        await self.hot_boss_cache.put(
            _HOT_BOSSES_KEY,
            cards,
            ttl_seconds=max(
                self._ranking_max_age,
                self.ranking_index.signal_period_seconds + 5.0,
            ),
        )
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

    async def get_equip_suits(self) -> dict[str, str]:
        """Suit id to display name, from the game data catalog.

        The one read whose answer is the same for every battle, so it is
        kept for a month and may be served stale: a gear label going missing
        for a whole page is worse than a label a while out of date.
        """

        result = await self.equip_catalog_cache.get_or_load(
            _EQUIP_CATALOG_KEY,
            self._fetch_equip_suits,
            allow_stale_on_error=True,
        )
        if result.state is CacheState.STALE:
            self._logger.warning(
                "ZmdLogBot is using a stale equip catalog after refresh failure."
            )
        return result.value

    async def _fetch_equip_suits(self) -> dict[str, str]:
        suits = await self.client.get_equip_catalog()
        return {suit.suit_id: suit.name for suit in suits}

    async def get_character_types(self) -> dict[str, CharacterType]:
        """Name to element and weapon type; kept for a month like the suits."""

        result = await self.character_type_cache.get_or_load(
            _CHARACTER_TYPES_KEY,
            self._fetch_character_types,
            allow_stale_on_error=True,
        )
        if result.state is CacheState.STALE:
            self._logger.warning(
                "ZmdLogBot is using a stale character catalog after refresh failure."
            )
        return result.value

    async def _fetch_character_types(self) -> dict[str, CharacterType]:
        return {entry.name: entry for entry in await self.client.get_character_types()}

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
        max_age: float | None = None,
    ) -> BossRanking:
        """One board's ranking, no older than ``max_age`` seconds.

        The default is the configured ranking cache TTL, which is what a
        board page expects; ``None`` takes whatever the index holds.
        """

        age = self._ranking_max_age if max_age is None else max_age
        return await self.ranking_index.get(boss_slug, max_age=age)

    async def get_account_rankings(self, account_id: str) -> PublicUserRankings:
        """The account's rank on every board, from the index when it is complete.

        This is what the rank watch polls: a whole watch list costs nothing
        once the index is filled. The endpoint answers when the index is not
        complete yet or holds no row for the account.
        """

        if self.ranking_index.complete:
            derived = account_rankings(self.ranking_index.entries(), account_id)
            if derived is not None:
                return derived
        return await self.get_public_user_rankings(account_id)

    async def _fetch_boss_ranking(self, boss_slug: str) -> BossRanking:
        return await self.client.get_boss_rankings(boss_slug)

    def start(self) -> None:
        """Start the background ranking index when it is enabled."""

        if self._ranking_index_enabled:
            self.ranking_index.start()

    async def get_character_statistics(
        self,
        boss_slug: str | None,
        *,
        time_range: str,
        potential: str,
    ) -> CharacterStatistics:
        key = (boss_slug or "all", time_range, potential)
        result = await self.character_stats_cache.get_or_load(
            key,
            lambda: self.client.get_character_statistics(
                boss_slug,
                time_range=time_range,
                potential=potential,
            ),
        )
        return result.value

    async def get_character_boss_statistics(
        self,
        character_key: str,
        *,
        time_range: str,
        potential: str,
    ) -> CharacterBossStatistics:
        key = (character_key, time_range, potential)
        result = await self.character_boss_cache.get_or_load(
            key,
            lambda: self.client.get_character_boss_statistics(
                character_key,
                time_range=time_range,
                potential=potential,
            ),
        )
        return result.value

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

    async def get_battle_export(self, battle_id: str) -> BattleExport:
        result = await self.battle_export_cache.get_or_load(
            battle_id,
            lambda: self.client.get_battle_export(battle_id),
        )
        return result.value

    async def close(self) -> None:
        """Stop the index, cancel in-flight loads and drop every cached value."""

        await self.ranking_index.stop()
        for cache in self._caches:
            await cache.close()
