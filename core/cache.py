"""Small async TTL cache with single-flight loading and bounded stale reuse."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = _positive_duration(ttl_seconds, "ttl_seconds")
        self.stale_ttl_seconds = _non_negative_duration(
            stale_ttl_seconds,
            "stale_ttl_seconds",
        )
        self._clock = clock
        self._entries: dict[K, _CacheEntry[V]] = {}
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
            entry = self._entries.get(key)
            if entry is not None and now < entry.fresh_until:
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
            return CacheResult(value, CacheState.LOADED)
        finally:
            async with self._lock:
                if self._inflight.get(key) is current_task:
                    self._inflight.pop(key, None)


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
