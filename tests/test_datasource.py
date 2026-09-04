import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path

from core.client import ZmdLogsClientError
from core.datasource import HOT_BOSSES_SNAPSHOT, ZmdLogsDataSource
from core.models import parse_hot_bosses
from core.persistence import JsonStore, load_json
from core.settings import PluginSettings
from tests.helpers import hot_bosses_payload


class FakeClient:
    def __init__(self, payload=None, *, fail=False) -> None:
        self.payload = payload
        self.fail = fail
        self.calls = 0

    async def list_hot_bosses_with_payload(self):
        self.calls += 1
        if self.fail:
            raise ZmdLogsClientError("offline")
        return parse_hot_bosses(self.payload), self.payload


class CapturingLogger(logging.Logger):
    def __init__(self) -> None:
        super().__init__("test")
        self.messages: list[str] = []

    def handle(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def run(coro):
    return asyncio.run(coro)


class DataSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.logger = CapturingLogger()

    def _source(self, client, *, data_dir=None) -> ZmdLogsDataSource:
        return ZmdLogsDataSource(
            client,
            settings=PluginSettings(ranking_cache_ttl_seconds=60),
            data_dir=self.root if data_dir is None else data_dir,
            logger=self.logger,
        )

    def test_a_good_read_is_cached_and_snapshotted(self) -> None:
        client = FakeClient(hot_bosses_payload())
        source = self._source(client)

        async def scenario():
            first = await source.list_hot_bosses()
            second = await source.list_hot_bosses()
            await source.close()
            return first, second

        first, second = run(scenario())
        # The same tuple object while fresh is what lets MatcherCache reuse
        # its index by identity.
        self.assertIs(first, second)
        self.assertEqual(client.calls, 1)
        self.assertEqual(
            load_json(self.root / HOT_BOSSES_SNAPSHOT), hot_bosses_payload()
        )
        self.assertEqual(self.logger.messages, [])

    def test_the_snapshot_serves_the_index_when_upstream_is_down(self) -> None:
        (self.root / HOT_BOSSES_SNAPSHOT).write_text(
            json.dumps(hot_bosses_payload()), encoding="utf-8"
        )
        source = self._source(FakeClient(fail=True))

        cards = run(source.list_hot_bosses())

        self.assertEqual(len(cards), len(hot_bosses_payload()))
        self.assertEqual(len(self.logger.messages), 1)
        self.assertIn("local snapshot", self.logger.messages[0])
        run(source.close())

    def test_a_corrupt_snapshot_does_not_hide_the_upstream_failure(self) -> None:
        (self.root / HOT_BOSSES_SNAPSHOT).write_text(
            '{"not": "cards"}', encoding="utf-8"
        )
        source = self._source(FakeClient(fail=True))

        with self.assertRaises(ZmdLogsClientError):
            run(source.list_hot_bosses())
        run(source.close())

    def test_without_a_data_dir_nothing_is_written_or_read(self) -> None:
        source = ZmdLogsDataSource(
            FakeClient(fail=True),
            settings=PluginSettings(),
            data_dir=None,
            logger=self.logger,
        )

        with self.assertRaises(ZmdLogsClientError):
            run(source.list_hot_bosses())
        self.assertEqual(list(self.root.iterdir()), [])
        run(source.close())


class JsonStoreTests(unittest.TestCase):
    def test_round_trip_and_missing_path(self) -> None:
        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStore(
                Path(directory) / "state.json", label="state", warn=warnings.append
            )
            self.assertTrue(store.available)
            self.assertIsNone(store.load())
            self.assertTrue(store.save({"a": 1}))
            self.assertEqual(store.load(), {"a": 1})
        absent = JsonStore(None, label="state", warn=warnings.append)
        self.assertFalse(absent.available)
        self.assertIsNone(absent.load())
        self.assertFalse(absent.save({"a": 1}))
        self.assertEqual(warnings, [])

    def test_a_failing_write_warns_once_per_streak(self) -> None:
        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "state.json"
            blocker.mkdir()  # a directory where the file should go: every write fails
            store = JsonStore(blocker, label="rank snapshot", warn=warnings.append)

            self.assertFalse(store.save({"a": 1}))
            self.assertFalse(store.save({"a": 2}))
            self.assertEqual(len(warnings), 1)
            self.assertIn("rank snapshot", warnings[0])

            blocker.rmdir()
            self.assertTrue(store.save({"a": 3}))
            blocker.unlink()
            blocker.mkdir()
            self.assertFalse(store.save({"a": 4}))
            # A new failure after a success is a new streak.
            self.assertEqual(len(warnings), 2)


if __name__ == "__main__":
    unittest.main()
