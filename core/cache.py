"""Small async TTL cache with single-flight loading and bounded stale reuse.

The TTL decides whether a value may be *reused*; on its own it never frees
anything, so a cache keyed by battle id would hold every battle the bot has
ever been asked about. Entries are therefore dropped once they can no
longer be served, and the map is capped, evicting whatever was used least
recently.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")

DEFAULT_MAX_ENTRIES = 256


class CacheState(str, Enum):
    HIT = "hit"
    LOADED = "loaded"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class CacheResult(Generic[V]):
    value: V
    state: CacheState


@dataclass(frozen=True, slots=True)
class _CacheEntry(Generic[V]):
    value: V
    fresh_until: float
    stale_until: float


class AsyncTTLCache(Generic[K, V]):
    """Cache values while merging concurrent loads for the same key."""

    def __init__(
        self,
        ttl_seconds: float,
        *,
        stale_ttl_seconds: float = 0,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = _positive_duration(ttl_seconds, "ttl_seconds")
        self.stale_ttl_seconds = _non_negative_duration(
            stale_ttl_seconds,
            "stale_ttl_seconds",
        )
        self.max_entries = _positive_count(max_entries, "max_entries")
        self._clock = clock
        self._entries: OrderedDict[K, _CacheEntry[V]] = OrderedDict()
        self._inflight: dict[K, asyncio.Task[CacheResult[V]]] = {}
        self._lock = asyncio.Lock()

    async def get_or_load(
        self,
        key: K,
        loader: Callable[[], Awaitable[V]],
        *,
        allow_stale_on_error: bool = False,
    ) -> CacheResult[V]:
        """Return a fresh value or await the one shared load for ``key``."""

        async with self._lock:
            now = self._clock()
            self._drop_unusable(now)
            entry = self._entries.get(key)
            if entry is not None and now < entry.fresh_until:
                self._entries.move_to_end(key)
                return CacheResult(entry.value, CacheState.HIT)

            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._load(
                        key,
                        loader,
                        allow_stale_on_error=allow_stale_on_error,
                    )
                )
                self._inflight[key] = task

        return await asyncio.shield(task)

    async def invalidate(self, key: K) -> None:
        async with self._lock:
            self._entries.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()

    async def close(self) -> None:
        async with self._lock:
            tasks = tuple(self._inflight.values())
            self._inflight.clear()
            self._entries.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _load(
        self,
        key: K,
        loader: Callable[[], Awaitable[V]],
        *,
        allow_stale_on_error: bool,
    ) -> CacheResult[V]:
        current_task = asyncio.current_task()
        try:
            value = await loader()
        except Exception:
            if allow_stale_on_error:
                async with self._lock:
                    entry = self._entries.get(key)
                    if entry is not None and self._clock() < entry.stale_until:
                        return CacheResult(entry.value, CacheState.STALE)
            raise
        else:
            now = self._clock()
            entry = _CacheEntry(
                value=value,
                fresh_until=now + self.ttl_seconds,
                stale_until=(
                    now + self.ttl_seconds + self.stale_ttl_seconds
                ),
            )
            async with self._lock:
                self._entries[key] = entry
                self._entries.move_to_end(key)
                self._drop_unusable(now)
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
            return CacheResult(value, CacheState.LOADED)
        finally:
            async with self._lock:
                if self._inflight.get(key) is current_task:
                    self._inflight.pop(key, None)


    def _drop_unusable(self, now: float) -> None:
        """Forget entries that can no longer be served, fresh or stale."""

        for key in [
            key
            for key, entry in self._entries.items()
            if now >= entry.stale_until
        ]:
            del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)


def _positive_count(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_duration(value: float, name: str) -> float:
    duration = _non_negative_duration(value, name)
    if duration == 0:
        raise ValueError(f"{name} must be positive")
    return duration


def _non_negative_duration(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    duration = float(value)
    if duration < 0:
        raise ValueError(f"{name} cannot be negative")
    return duration
