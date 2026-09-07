"""Tests for nickname search (0.4.1) and the option-usage error texts."""

import unittest
from pathlib import Path

import httpx

from core.candidates import CandidateStore, describe_choice, format_candidates
from core.client import (
    InvalidPublicIdentifierError,
    ZmdLogsClient,
    ZmdLogsProtocolError,
)
from core.matcher import MatchChoice, MatchLevel, MatchTarget, TargetType
from core.models import (
    ModelValidationError,
    parse_account_search,
    parse_public_user_rankings,
)
from core.presentation import build_account_page
from core.render import TemplateRenderer
from core.routing import RouteParseError, parse_zmdlog_payload
from tests.helpers import public_user_rankings_payload


def search_payload(**overrides) -> dict:
    payload = {
        "query": "CPU",
        "hasMore": False,
        "accounts": [
            {"accountId": "usr_space", "accountDisplayName": "CPU 0"},
            {"accountId": "usr_compact", "accountDisplayName": "cpu0"},
        ],
    }
    payload.update(overrides)
    return payload


class OptionUsageMessageTests(unittest.TestCase):
    def test_rejections_name_where_the_option_works(self) -> None:
        cases = {
            "榜单 斧柄纪年 --潜能 0": "仅适用于角色统计",
            "榜单 斧柄纪年 --范围 7d": "仅适用于角色统计",
            "角色统计 罗丹 --top 5": "仅适用于具体榜单和阵容查询",
            "账号 usr_x --角色 黎风": "仅适用于具体榜单查询",
        }
        for payload, fragment in cases.items():
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RouteParseError, fragment):
                    parse_zmdlog_payload(payload)


class AccountSearchModelTests(unittest.TestCase):
    def test_parse_roundtrip(self) -> None:
        search = parse_account_search(search_payload(hasMore=True))
        self.assertEqual(search.query, "CPU")
        self.assertTrue(search.has_more)
        self.assertEqual(
            [(hit.account_id, hit.account_display_name) for hit in search.accounts],
            [("usr_space", "CPU 0"), ("usr_compact", "cpu0")],
        )

    def test_missing_fields_are_rejected(self) -> None:
        with self.assertRaises(ModelValidationError):
            parse_account_search(search_payload(accounts=[{"accountId": "x"}]))
        with self.assertRaises(ModelValidationError):
            parse_account_search({"query": "x", "accounts": []})


class AccountSearchClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_shape_and_normalization(self) -> None:
        seen: list[httpx.URL] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json=search_payload())

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        search = await client.search_public_accounts("  ＣＰＵ  ", limit=5)

        self.assertEqual(seen[0].path, "/api/battles/users/search")
        # NFKC folds fullwidth letters; whitespace is stripped before sending.
        self.assertEqual(dict(seen[0].params), {"query": "CPU", "limit": "5"})
        self.assertEqual(len(search.accounts), 2)

    async def test_short_and_long_queries_are_rejected_before_request(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request expected")

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        for query in ("x", " 甲 ", "y" * 65):
            with self.subTest(query=query):
                with self.assertRaises(InvalidPublicIdentifierError):
                    await client.search_public_accounts(query)

    async def test_invalid_payload_is_protocol_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"accounts": "nope"})

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        with self.assertRaises(ZmdLogsProtocolError):
            await client.search_public_accounts("CPU")


def _account_choice(account_id: str, name: str) -> MatchChoice:
    return MatchChoice(
        target=MatchTarget(
            target_type=TargetType.ACCOUNT,
            key=account_id,
            name=name,
            dungeon_names=(),
            boss_slugs=(),
        ),
        level=MatchLevel.STANDARD_EXACT,
        score=1.0,
        matched_text=name,
    )


class AccountCandidateTests(unittest.TestCase):
    def test_describe_and_note(self) -> None:
        choices = (
            _account_choice("usr_space", "CPU 0"),
            _account_choice("usr_compact", "cpu0"),
        )
        self.assertEqual(describe_choice(choices[0]), "CPU 0 · 公开账号")

        store = CandidateStore(ttl_seconds=60)
        entry = store.remember("cpu", choices, now=0.0)
        text = format_candidates(
            entry,
            ttl_seconds=60,
            note="还有更多同名结果未列出，可输入更完整的昵称。",
        )
        self.assertIn("1. CPU 0 · 公开账号", text)
        self.assertIn("还有更多同名结果未列出", text)
        self.assertTrue(text.rstrip().endswith("分钟内有效"))

        resolved = store.resolve(entry.code, "2", now=1.0)
        assert resolved is not None
        self.assertEqual(resolved[1].target.key, "usr_compact")


class AccountMedalTests(unittest.TestCase):
    def test_top_three_rows_carry_rank_classes(self) -> None:
        payload = public_user_rankings_payload()
        base = payload["rankings"][0]
        payload["rankings"] = [
            {**base, "rank": rank, "bossSlug": f"slug{rank}", "bossName": f"boss{rank}"}
            for rank in (1, 2, 3, 4)
        ]
        account = parse_public_user_rankings(payload)
        page = build_account_page(
            account, query="q", web_base_url="https://zmdlogs.com"
        )
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])
        html = renderer.render_account(
            account, query="q", web_base_url="https://zmdlogs.com"
        )
        self.assertEqual(len(page.rows), 4)
        self.assertEqual(html.count('class="standings-row is-first"'), 1)
        self.assertEqual(html.count('class="standings-row is-podium"'), 2)
        self.assertEqual(html.count('class="standings-row"'), 1)


if __name__ == "__main__":
    unittest.main()
