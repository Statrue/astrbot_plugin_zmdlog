import unittest

import httpx

from core.client import (
    InvalidBossSlugError,
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsProtocolError,
)

from tests.helpers import hot_bosses_payload, ranking_payload


class ZmdLogsClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_hot_bosses_retries_one_5xx(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, json={"error": {"code": "busy"}})
            return httpx.Response(200, json=hot_bosses_payload())

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        cards = await client.list_hot_bosses()

        self.assertEqual(calls, 2)
        self.assertEqual(len(cards), 1)

    async def test_network_error_is_retried_once(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json=hot_bosses_payload())

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        cards = await client.list_hot_bosses()

        self.assertEqual(calls, 2)
        self.assertEqual(cards[0].boss_name, "危境再现·三位一体")

    async def test_4xx_is_not_retried_or_exposed_verbatim(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(404, text="internal upstream details")

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        with self.assertRaises(ZmdLogsAPIError) as raised:
            await client.list_hot_bosses()

        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.code, "http_404")
        self.assertNotIn("internal upstream details", str(raised.exception))

    async def test_ranking_request_has_no_metric_parameter(self) -> None:
        seen_urls: list[httpx.URL] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(request.url)
            return httpx.Response(200, json=ranking_payload())

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        ranking = await client.get_boss_rankings("dung01_group_bossrush02")

        self.assertEqual(ranking.metric, "dps")
        self.assertEqual(len(seen_urls), 1)
        self.assertEqual(seen_urls[0].params, httpx.QueryParams())

    async def test_non_dps_response_is_a_protocol_error(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=ranking_payload(metric="rdps"))
        )
        client = ZmdLogsClient(transport=transport)
        self.addAsyncCleanup(client.close)

        with self.assertRaises(ZmdLogsProtocolError):
            await client.get_boss_rankings("dung01_group_bossrush02")

    async def test_invalid_json_is_a_protocol_error(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text="not-json")
        )
        client = ZmdLogsClient(transport=transport)
        self.addAsyncCleanup(client.close)

        with self.assertRaises(ZmdLogsProtocolError):
            await client.list_hot_bosses()

    async def test_invalid_slug_is_rejected_before_request(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=ranking_payload())

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        with self.assertRaises(InvalidBossSlugError):
            await client.get_boss_rankings("../private")
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
