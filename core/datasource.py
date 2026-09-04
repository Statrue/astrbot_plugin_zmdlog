"""Cached reads of the public ZMDLogs API.

One :class:`AsyncTTLCache` per endpoint family merges concurrent loads of
the same key, and the board index (``hot-bosses``) is the only read that
may serve a stale entry or the on-disk snapshot when upstream is down: it
is what every keyword query starts from, so a short outage must not turn
every command into an error.
"""

from pathlib import Path

from .cache import AsyncTTLCache, CacheState
from .client import ZmdLogsClient, ZmdLogsClientError
from .logs import LogSink
from .models import (
    BattleDetailSummary,
    BattleExport,
    BossRanking,
    CharacterBossStatistics,
    CharacterStatistics,
    HotBossCard,
    PublicUserRankings,
    parse_hot_bosses,
)
from .persistence import load_json, save_json
from .settings import PluginSettings

HOT_BOSSES_SNAPSHOT = "hot-bosses.json"
_HOT_BOSSES_KEY = "all_board_top3"


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
        self.boss_ranking_cache = AsyncTTLCache[str, BossRanking](ranking_ttl)
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
        self.battle_cache = AsyncTTLCache[str, BattleDetailSummary](battle_ttl)
        self.battle_export_cache = AsyncTTLCache[str, BattleExport](battle_ttl)
        self._caches = (
            self.hot_boss_cache,
            self.boss_ranking_cache,
            self.account_cache,
            self.character_stats_cache,
            self.character_boss_cache,
            self.battle_cache,
            self.battle_export_cache,
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

    async def _fetch_hot_bosses(self) -> tuple[HotBossCard, ...]:
        """Fetch the board index; fall back to the on-disk snapshot if needed."""

        snapshot_path = self._snapshot_path
        try:
            cards, payload = await self.client.list_hot_bosses_with_payload()
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
        if snapshot_path is not None:
            save_json(snapshot_path, payload)
        return cards

    async def get_boss_ranking(self, boss_slug: str) -> BossRanking:
        result = await self.boss_ranking_cache.get_or_load(
            boss_slug,
            lambda: self.client.get_boss_rankings(boss_slug),
        )
        return result.value

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
        """Cancel in-flight loads and drop every cached value."""

        for cache in self._caches:
            await cache.close()
