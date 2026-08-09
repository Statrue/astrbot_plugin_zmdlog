import asyncio
import unittest

from core.cache import AsyncTTLCache, CacheState


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class AsyncTTLCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_share_one_loader(self) -> None:
        cache = AsyncTTLCache[str, str](60)
        started = asyncio.Event()
        release = asyncio.Event()
        load_count = 0

        async def loader() -> str:
            nonlocal load_count
            load_count += 1
            started.set()
            await release.wait()
            return "value"

        tasks = tuple(
            asyncio.create_task(cache.get_or_load("key", loader))
            for _ in range(12)
        )
        await started.wait()
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*tasks)

        self.assertEqual(load_count, 1)
        self.assertTrue(all(result.value == "value" for result in results))
        self.assertTrue(
            all(result.state is CacheState.LOADED for result in results)
        )
        hit = await cache.get_or_load("key", loader)
        self.assertEqual(hit.state, CacheState.HIT)
        self.assertEqual(load_count, 1)
        await cache.close()

    async def test_stale_value_is_bounded_to_the_extra_ttl(self) -> None:
        clock = _Clock()
        cache = AsyncTTLCache[str, str](
            5,
            stale_ttl_seconds=5,
            clock=clock,
        )

        async def first_loader() -> str:
            return "last-success"

        async def failing_loader() -> str:
            raise RuntimeError("upstream failed")

        loaded = await cache.get_or_load("hot", first_loader)
        self.assertEqual(loaded.state, CacheState.LOADED)

        clock.advance(6)
        stale = await cache.get_or_load(
            "hot",
            failing_loader,
            allow_stale_on_error=True,
        )
        self.assertEqual(stale.state, CacheState.STALE)
        self.assertEqual(stale.value, "last-success")

        clock.advance(5)
        with self.assertRaises(RuntimeError):
            await cache.get_or_load(
                "hot",
                failing_loader,
                allow_stale_on_error=True,
            )
        await cache.close()

    async def test_different_keys_load_independently(self) -> None:
        cache = AsyncTTLCache[str, str](60)
        calls: list[str] = []

        async def load(value: str) -> str:
            calls.append(value)
            await asyncio.sleep(0)
            return value

        left, right = await asyncio.gather(
            cache.get_or_load("left", lambda: load("left")),
            cache.get_or_load("right", lambda: load("right")),
        )

        self.assertEqual({left.value, right.value}, {"left", "right"})
        self.assertCountEqual(calls, ["left", "right"])
        await cache.close()

    async def test_invalid_durations_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AsyncTTLCache[str, str](0)
        with self.assertRaises(ValueError):
            AsyncTTLCache[str, str](1, stale_ttl_seconds=-1)


if __name__ == "__main__":
    unittest.main()
