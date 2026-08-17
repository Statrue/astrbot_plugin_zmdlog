import unittest

import httpx

from core.client import (
    InvalidBossSlugError,
    InvalidPublicIdentifierError,
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsProtocolError,
)
from tests.helpers import (
    battle_detail_payload,
    hot_bosses_payload,
    public_user_rankings_payload,
    ranking_payload,
)


class ZmdLogsClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_use_ascii_plugin_user_agent(self) -> None:
        seen_user_agents: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_user_agents.append(request.headers["User-Agent"])
            return httpx.Response(200, json=hot_bosses_payload())

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        await client.list_hot_bosses()

        self.assertEqual(
            seen_user_agents,
            ["astrbot_plugin_zmdlog"],
        )
        self.assertTrue(seen_user_agents[0].isascii())

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

    async def test_exact_account_rankings_use_public_path(self) -> None:
        seen_urls: list[httpx.URL] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(request.url)
            return httpx.Response(200, json=public_user_rankings_payload())

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        account = await client.get_public_user_rankings(
            "usr_1234567890abcdef"
        )

        self.assertEqual(account.account_display_name, "测试账号")
        self.assertEqual(account.rankings[0].rank, 2)
        self.assertEqual(
            seen_urls[0].path,
            "/api/battles/users/usr_1234567890abcdef/rankings",
        )

    async def test_battle_detail_keeps_summary_and_participants_only(self) -> None:
        client = ZmdLogsClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=battle_detail_payload(),
                )
            )
        )
        self.addAsyncCleanup(client.close)

        battle = await client.get_battle_detail(
            "btl_upload_abcdef123456"
        )

        self.assertEqual(battle.uploader_display_name, "测试账号")
        self.assertEqual(battle.total_dps, 110_061.2)
        self.assertEqual(len(battle.participants), 1)
        self.assertEqual(battle.participants[0].rdps, 26_428.42)
        self.assertTrue(battle.integrity_verified)

    async def test_invalid_public_ids_are_rejected_before_request(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        with self.assertRaises(InvalidPublicIdentifierError):
            await client.get_public_user_rankings("../private")
        with self.assertRaises(InvalidPublicIdentifierError):
            await client.get_battle_detail("https://example.com/battle")
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
