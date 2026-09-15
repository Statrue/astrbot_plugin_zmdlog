"""账号绑定: the binding book, the code flow, the client call and the 群榜 page."""

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path

import httpx

from core import messages
from core.account_binding import BINDINGS_FILE, AccountBinding
from core.bindings import (
    MAX_ACCOUNTS_PER_USER,
    USED_CODE_TTL_SECONDS,
    BindingBook,
    BoundAccount,
    code_digest,
    format_bindings,
    is_group_origin,
    normalize_binding_code,
    parse_bindings,
)
from core.client import (
    InvalidPublicIdentifierError,
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsClientError,
)
from core.models import AccountSearchHit, parse_boss_ranking
from core.persistence import load_json
from core.presentation import build_group_board_page
from core.render import TemplateRenderer
from core.routing import parse_zmdlog_payload
from core.settings import PluginSettings
from core.standings import group_standings
from tests.helpers import ranking_payload_with_rows

GROUP = "aiocqhttp:GroupMessage:1"
OTHER_GROUP = "aiocqhttp:GroupMessage:2"
PRIVATE = "aiocqhttp:FriendMessage:9"
USER = "aiocqhttp:111"
OTHER_USER = "aiocqhttp:222"
NOW = "2026-09-15T10:00:00+00:00"
LATER = "2026-09-15T10:20:00+00:00"
CODE = "ZMD-7K4M-QX2E"
WEB = "https://zmdlogs.com"


def run(coro):
    return asyncio.run(coro)


def bound(account_id: str = "usr_a", name: str = "CPU 0") -> BoundAccount:
    return BoundAccount(account_id=account_id, display_name=name, bound_at=NOW)


class CodeTests(unittest.TestCase):
    def test_codes_are_folded_to_what_the_site_accepts(self) -> None:
        # Lowercase, fullwidth letters and a long dash are what people type.
        typed_forms = (
            "zmd-7k4m-qx2e", " ZMD-7K4M-QX2E ", "ＺＭＤ－7K4M－QX2E", "ZMD—7K4M—QX2E"
        )
        for typed in typed_forms:
            with self.subTest(typed=typed):
                self.assertEqual(normalize_binding_code(typed), CODE)
        # 0 / 1 / I / O are not in the alphabet; a nickname is not a code.
        for typed in ("ZMD-0000-0000", "ZMD-7K4M", "CPU 0", "", "usr_a"):
            with self.subTest(typed=typed):
                self.assertIsNone(normalize_binding_code(typed))

    def test_the_digest_never_contains_the_code(self) -> None:
        digest = code_digest(CODE)
        self.assertEqual(len(digest), 64)
        self.assertNotIn("7K4M", digest)

    def test_only_group_origins_have_members(self) -> None:
        self.assertTrue(is_group_origin(GROUP))
        self.assertFalse(is_group_origin(PRIVATE))
        self.assertFalse(is_group_origin(""))


def add(book: BindingBook, user: str, account: BoundAccount, *, origin=GROUP, now=NOW):
    """with_account with the test's defaults; returns the book and the status."""

    return book.with_account(user, account, origin=origin, now=now)


class BookTests(unittest.TestCase):
    def test_a_new_account_is_appended_and_the_primary_stays(self) -> None:
        book, status = add(BindingBook.empty(), USER, bound("usr_a", "CPU 0"))
        self.assertEqual(status, "added")
        book, status = add(book, USER, bound("usr_b", "cpu0"), now=LATER)
        self.assertEqual(status, "added")

        mine = book.for_user(USER)
        self.assertEqual([a.account_id for a in mine.accounts], ["usr_a", "usr_b"])
        self.assertEqual(mine.primary.account_id, "usr_a")
        self.assertEqual(mine.groups, (GROUP,))
        self.assertEqual(mine.updated_at, LATER)

    def test_rebinding_an_account_refreshes_its_nickname_in_place(self) -> None:
        book, _ = add(BindingBook.empty(), USER, bound("usr_a", "旧名"))
        book, _ = add(book, USER, bound("usr_b"))

        book, status = add(
            book, USER, bound("usr_a", "新名"), origin=OTHER_GROUP, now=LATER
        )

        self.assertEqual(status, "refreshed")
        mine = book.for_user(USER)
        self.assertEqual([a.display_name for a in mine.accounts], ["新名", "CPU 0"])
        self.assertEqual(mine.groups, (GROUP, OTHER_GROUP))

    def test_the_cap_refuses_the_sixth_account_without_changing_anything(self) -> None:
        book = BindingBook.empty()
        for index in range(MAX_ACCOUNTS_PER_USER):
            book, status = add(book, USER, bound(f"usr_{index}"))
            self.assertEqual(status, "added")

        full, status = add(book, USER, bound("usr_more"), now=LATER)

        self.assertEqual(status, "full")
        self.assertIs(full, book)

    def test_primary_unbind_and_unbind_all(self) -> None:
        book, _ = add(BindingBook.empty(), USER, bound("usr_a"))
        book, _ = add(book, USER, bound("usr_b", "第二"))
        book, _ = add(book, OTHER_USER, bound("usr_c"))

        book = book.with_primary(USER, "usr_b", now=LATER)
        self.assertEqual(book.for_user(USER).primary.account_id, "usr_b")
        self.assertIs(book.with_primary(USER, "usr_b", now=LATER), book)
        self.assertIs(book.with_primary(USER, "usr_nope", now=LATER), book)

        book = book.without_account(USER, "usr_b", now=LATER)
        remaining = book.for_user(USER).accounts
        self.assertEqual([a.account_id for a in remaining], ["usr_a"])
        # The last account gone is the user gone.
        book = book.without_account(USER, "usr_a", now=LATER)
        self.assertIsNone(book.for_user(USER))
        self.assertIsNotNone(book.for_user(OTHER_USER))
        self.assertIsNone(book.without_user(OTHER_USER).for_user(OTHER_USER))

    def test_selectors_resolve_by_index_id_or_nickname(self) -> None:
        book, _ = add(BindingBook.empty(), USER, bound("usr_a", "CPU 0"))
        book, _ = add(book, USER, bound("usr_b", "cpu0"))
        mine = book.for_user(USER)

        self.assertEqual(mine.resolve("2").account_id, "usr_b")
        self.assertEqual(mine.resolve("usr_a").account_id, "usr_a")
        self.assertEqual(mine.resolve("CPU 0").account_id, "usr_a")
        # Folded, the two nicknames collide: exact spelling decides, a
        # fragment matching both is ambiguous.
        self.assertEqual(mine.resolve("cpu0").account_id, "usr_b")
        self.assertEqual(len(mine.resolve_matches("cpu")), 2)
        self.assertIsNone(mine.resolve("3"))
        self.assertIsNone(mine.resolve("9" * 12))
        self.assertIsNone(mine.resolve(""))

    def test_membership_is_where_a_bound_user_showed_up(self) -> None:
        book, _ = add(BindingBook.empty(), USER, bound("usr_a"))
        book, _ = add(book, OTHER_USER, bound("usr_a"), origin=OTHER_GROUP, now=LATER)
        book, _ = add(book, OTHER_USER, bound("usr_b"), origin=OTHER_GROUP, now=LATER)

        book = book.with_group(OTHER_USER, GROUP, now=LATER)
        self.assertIs(book.with_group(OTHER_USER, GROUP, now=LATER), book)
        self.assertIs(book.with_group("aiocqhttp:nobody", GROUP, now=LATER), book)

        members = book.members_of(GROUP)
        self.assertEqual([key for key, _ in members], [OTHER_USER, USER])
        # Two users bound to usr_a are one public account on the board.
        self.assertEqual(
            [a.account_id for a in book.accounts_in(GROUP)], ["usr_a", "usr_b"]
        )
        self.assertEqual(book.accounts_in(PRIVATE), ())

    def test_used_codes_are_remembered_for_their_life_and_then_forgotten(self) -> None:
        digest = code_digest(CODE)
        book = BindingBook.empty().with_used_code(digest, now=NOW)

        self.assertTrue(book.code_used(digest, now=NOW))
        self.assertTrue(book.code_used(digest, now="2026-09-15T10:10:00+00:00"))
        expired = "2026-09-15T10:15:00+00:00"
        self.assertFalse(book.code_used(digest, now=expired))
        self.assertFalse(book.code_used(code_digest("ZMD-AAAA-BBBB"), now=NOW))
        # Recording another code past the life prunes the spent one.
        pruned = book.with_used_code(code_digest("ZMD-AAAA-BBBB"), now=expired)
        self.assertEqual(
            [entry[0] for entry in pruned.used_codes], [code_digest("ZMD-AAAA-BBBB")]
        )
        self.assertGreaterEqual(USED_CODE_TTL_SECONDS, 10 * 60)

    def test_the_payload_round_trips_and_a_bad_one_reads_as_empty(self) -> None:
        book, _ = add(BindingBook.empty(), USER, bound("usr_a", "CPU 0"))
        book = book.with_used_code(code_digest(CODE), now=NOW)

        payload = book.to_payload()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(
            payload["users"][USER]["accounts"],
            [{"accountId": "usr_a", "displayName": "CPU 0", "boundAt": NOW}],
        )
        self.assertNotIn(CODE, repr(payload))
        self.assertEqual(parse_bindings(payload), book)
        self.assertEqual(parse_bindings("nonsense"), BindingBook.empty())
        odd = {"users": {"k": {"accounts": "no"}}}
        self.assertEqual(parse_bindings(odd), BindingBook.empty())
        # A user whose accounts are all malformed is dropped, not kept empty.
        nameless = {"users": {USER: {"accounts": [{"displayName": "x"}]}}}
        self.assertEqual(parse_bindings(nameless), BindingBook.empty())

    def test_the_list_text_marks_the_primary_and_explains_the_numbers(self) -> None:
        self.assertIn("还没有绑定账号", format_bindings(None))
        book, _ = add(BindingBook.empty(), USER, bound("usr_a", "CPU 0"))
        one = format_bindings(book.for_user(USER), command="!zmdlog")
        self.assertIn("1. CPU 0（usr_a）", one)
        self.assertNotIn("★", one)
        book, _ = add(book, USER, bound("usr_b", "cpu0"))
        two = format_bindings(book.for_user(USER), command="!zmdlog")
        self.assertIn("1. CPU 0（usr_a） ★主账号", two)
        self.assertIn("2. cpu0（usr_b）", two)
        self.assertIn("!zmdlog 主账号 <序号>", two)


class FakeClient:
    """Answers the binding-code lookup with a mapping of code -> hit or error."""

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.codes: list[str] = []

    async def get_binding_code_account(self, code: str):
        self.codes.append(code)
        answer = self.answers.get(code)
        if answer is None:
            raise ZmdLogsAPIError(404, "binding_code_invalid", "无效")
        if isinstance(answer, Exception):
            raise answer
        return answer


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.client = FakeClient(
            {
                CODE: AccountSearchHit("usr_a", "CPU 0"),
                "ZMD-AAAA-BBBB": AccountSearchHit("usr_b", "cpu0"),
                "ZMD-CCCC-DDDD": AccountSearchHit("usr_a", "CPU 0 改名"),
            }
        )
        self.service = self._service(self.client)

    def _service(self, client, **settings) -> AccountBinding:
        return AccountBinding(
            client=client,
            settings=PluginSettings(**settings),
            data_dir=self.root,
            logger=logging.getLogger("t"),
        )

    def _handle(self, text: str, *, origin: str = GROUP, user: str = USER) -> str:
        return run(
            self.service.handle_route(
                parse_zmdlog_payload(text),
                origin=origin,
                requester_key=user,
                command="/zmdlog",
            )
        )

    def test_a_valid_code_binds_and_the_file_records_only_public_data(self) -> None:
        reply = self._handle("绑定 zmd-7k4m-qx2e")

        self.assertIn("已绑定 CPU 0（usr_a）", reply)
        self.assertIn("1. CPU 0（usr_a）", reply)
        self.assertEqual(self.client.codes, [CODE], "sent as the site spells it")
        stored = load_json(self.root / BINDINGS_FILE)
        self.assertEqual(stored["users"][USER]["accounts"][0]["accountId"], "usr_a")
        self.assertEqual(stored["users"][USER]["groups"], [GROUP])
        self.assertNotIn(CODE, (self.root / BINDINGS_FILE).read_text(encoding="utf-8"))
        # The service reloads what it wrote.
        again = self._service(self.client)
        self.assertEqual(again.bindings_for(USER).primary.account_id, "usr_a")

    def test_a_private_chat_is_never_recorded_as_a_group(self) -> None:
        reply = self._handle("绑定 ZMD-7K4M-QX2E", origin=PRIVATE)

        self.assertIn("已绑定", reply)
        self.assertEqual(self.service.bindings_for(USER).groups, ())
        self.service.remember_member(USER, PRIVATE)
        self.assertEqual(self.service.bindings_for(USER).groups, ())
        self.service.remember_member(USER, GROUP)
        self.assertEqual(self.service.bindings_for(USER).groups, (GROUP,))

    def test_a_spent_code_is_refused_for_another_user(self) -> None:
        self._handle("绑定 ZMD-7K4M-QX2E")

        reply = self._handle("绑定 ZMD-7K4M-QX2E", user=OTHER_USER)

        self.assertEqual(reply, messages.BIND_CODE_USED)
        self.assertEqual(len(self.client.codes), 1, "no second lookup")
        self.assertIsNone(self.service.bindings_for(OTHER_USER))

    def test_refusals_are_told_apart(self) -> None:
        self.assertIn("绑定只认绑定码", self._handle("绑定 CPU 0"))
        self.assertIn("绑定只认绑定码", self._handle("绑定"))
        self.assertIn("/zmdlog 绑定 ZMD-XXXX-XXXX", self._handle("绑定"))
        answers = self.client.answers
        self.assertEqual(self._handle("绑定 ZMD-ZZZZ-ZZZZ"), messages.BIND_CODE_INVALID)
        answers["ZMD-EEEE-FFFF"] = ZmdLogsAPIError(404, "http_404", "Not Found")
        self.assertEqual(self._handle("绑定 ZMD-EEEE-FFFF"), messages.BIND_UNSUPPORTED)
        answers["ZMD-GGGG-HHHH"] = ZmdLogsAPIError(429, "too_many_requests", "")
        self.assertEqual(self._handle("绑定 ZMD-GGGG-HHHH"), messages.RATE_LIMITED)
        answers["ZMD-JJJJ-KKKK"] = ZmdLogsClientError("offline")
        self.assertEqual(
            self._handle("绑定 ZMD-JJJJ-KKKK"), messages.UPSTREAM_UNAVAILABLE
        )
        self.assertEqual(
            self._handle("绑定 ZMD-7K4M-QX2E", user=""), messages.NO_SENDER
        )
        off = self._service(self.client, bindings_enabled=False)
        reply = run(
            off.handle_route(
                parse_zmdlog_payload("绑定 ZMD-7K4M-QX2E"),
                origin=GROUP,
                requester_key=USER,
                command="/zmdlog",
            )
        )
        self.assertEqual(reply, messages.BINDINGS_DISABLED)
        # A refused code was never spent.
        self.assertIn("已绑定", self._handle("绑定 ZMD-7K4M-QX2E"))

    def test_several_accounts_primary_and_unbinding(self) -> None:
        self._handle("绑定 ZMD-7K4M-QX2E")
        second = self._handle("绑定 ZMD-AAAA-BBBB")
        self.assertIn("1. CPU 0（usr_a） ★主账号", second)
        self.assertIn("2. cpu0（usr_b）", second)
        refreshed = self._handle("绑定 ZMD-CCCC-DDDD")
        self.assertIn("已经绑定过了（第 1 位）", refreshed)
        primary = self.service.bindings_for(USER).primary
        self.assertEqual(primary.display_name, "CPU 0 改名")

        self.assertIn("★主账号", self._handle("主账号"))
        self.assertIn("主账号已改为 cpu0（usr_b）", self._handle("主账号 2"))
        self.assertEqual(self._handle("主账号 cpu0"), "cpu0 已经是主账号。")
        self.assertIn("没有「没有的」", self._handle("主账号 没有的"))
        self.assertIn("请改用序号", self._handle("主账号 cpu"))

        self.assertIn("请指定", self._handle("解绑"))
        self.assertIn("已解绑 CPU 0 改名（usr_a）", self._handle("解绑 CPU 0 改名"))
        self.assertEqual(self._handle("主账号 1"), "只绑定了一个账号，无需设置主账号。")
        self.assertEqual(self._handle("解绑"), "已解绑 cpu0（usr_b）。")
        not_bound = messages.NOT_BOUND.format(command="/zmdlog")
        self.assertEqual(self._handle("解绑"), not_bound)
        self.assertEqual(self._handle("主账号"), not_bound)

        # A fresh code: the earlier one for usr_b was spent above.
        self.client.answers["ZMD-LLLL-MMMM"] = AccountSearchHit("usr_b", "cpu0")
        self._handle("绑定 ZMD-LLLL-MMMM")
        self.assertEqual(self._handle("解绑 全部"), "已解绑 cpu0（usr_b）。")

    def test_membership_and_the_group_board_cap(self) -> None:
        self._handle("绑定 ZMD-7K4M-QX2E")
        self._handle("绑定 ZMD-AAAA-BBBB", user=OTHER_USER, origin=OTHER_GROUP)

        accounts, total, members = self.service.accounts_in(GROUP)
        self.assertEqual([a.account_id for a in accounts], ["usr_a"])
        self.assertEqual((total, members), (1, 1))
        self.service.remember_member(OTHER_USER, GROUP)
        self.service.remember_member("aiocqhttp:nobody", GROUP)
        self.service.remember_member(USER, "")
        accounts, total, members = self.service.accounts_in(GROUP)
        self.assertEqual(sorted(a.account_id for a in accounts), ["usr_a", "usr_b"])
        self.assertEqual((total, members), (2, 2))
        stored = load_json(self.root / BINDINGS_FILE)
        self.assertEqual(stored["users"][OTHER_USER]["groups"], [OTHER_GROUP, GROUP])

        capped = self._service(self.client, group_board_max_accounts=1)
        accounts, total, members = capped.accounts_in(GROUP)
        self.assertEqual(len(accounts), 1)
        self.assertEqual((total, members), (2, 2))

    def test_without_a_data_directory_nothing_is_bound(self) -> None:
        service = AccountBinding(
            client=self.client,
            settings=PluginSettings(),
            data_dir=None,
            logger=logging.getLogger("t"),
        )
        reply = run(
            service.handle_route(
                parse_zmdlog_payload("绑定 ZMD-7K4M-QX2E"),
                origin=GROUP,
                requester_key=USER,
                command="/zmdlog",
            )
        )
        self.assertEqual(reply, messages.BINDINGS_WRITE_FAILED)
        self.assertIsNone(service.bindings_for(USER))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_code_is_a_query_parameter_and_stays_out_of_the_log(
        self,
    ) -> None:
        seen: list[httpx.URL] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(
                200,
                json={"accountId": "usr_example", "accountDisplayName": "公开昵称"},
                headers={"Cache-Control": "no-store"},
            )

        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        httpx_logger = logging.getLogger("httpx")
        capture = Capture()
        previous_level = httpx_logger.level
        httpx_logger.addHandler(capture)
        httpx_logger.setLevel(logging.INFO)
        self.addCleanup(httpx_logger.removeHandler, capture)
        self.addCleanup(httpx_logger.setLevel, previous_level)
        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)

        hit = await client.get_binding_code_account(CODE)

        self.assertEqual(
            (hit.account_id, hit.account_display_name), ("usr_example", "公开昵称")
        )
        self.assertEqual(seen[0].path, "/api/battles/users/binding-code")
        self.assertEqual(seen[0].params["code"], CODE)
        logged = [line for line in records if "binding-code" in line]
        self.assertTrue(logged, "httpx logs every request at INFO")
        self.assertNotIn(CODE, "".join(logged))
        self.assertIn("ZMD-****-****", logged[0])
        # A second client (a plugin reload) does not stack a second filter.
        ZmdLogsClient(transport=httpx.MockTransport(handler))
        names = [type(entry).__name__ for entry in httpx_logger.filters]
        self.assertEqual(names.count("_RedactBindingCode"), 1)
        with self.assertRaises(InvalidPublicIdentifierError):
            await client.get_binding_code_account("zmd-7k4m-qx2e")

    async def test_the_site_refusals_keep_their_codes(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"error": {"code": "binding_code_invalid", "message": "无效"}},
            )

        client = ZmdLogsClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        with self.assertRaises(ZmdLogsAPIError) as raised:
            await client.get_binding_code_account(CODE)
        self.assertEqual(raised.exception.code, "binding_code_invalid")


class GroupBoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ranking = parse_boss_ranking(ranking_payload_with_rows())
        # Fixture uploaders are usr_<rank padded to 32>; rank 2 and 4 share 黎风.
        self.ids = {f"usr_{rank:032d}" for rank in (4, 2, 5)}

    def test_each_account_gets_its_best_record_in_board_order(self) -> None:
        rows = group_standings(self.ranking, self.ids | {"usr_nobody"})

        self.assertEqual([row.rank for row in rows], [2, 4, 5])
        self.assertEqual(group_standings(self.ranking, set()), ())

    def test_the_page_and_the_template_show_the_chat_not_the_binders(self) -> None:
        rows = group_standings(self.ranking, self.ids)
        renderer = TemplateRenderer.from_plugin_root(Path(__file__).parents[1])

        page = build_group_board_page(
            self.ranking,
            rows,
            query="群榜 三位一体",
            web_base_url=WEB,
            member_count=2,
            account_count=4,
            display_limit=2,
            truncated=True,
            age_seconds=30,
        )
        html = renderer.render_group_board(
            self.ranking,
            rows,
            query="群榜 三位一体",
            member_count=2,
            account_count=4,
            display_limit=2,
            truncated=True,
            web_base_url=WEB,
            age_seconds=30,
        )

        self.assertEqual(page.header.target_type, "群榜")
        self.assertEqual([row.position for row in page.rows], [1, 2])
        self.assertEqual([row.rank for row in page.rows], [2, 4])
        self.assertEqual(page.rows[0].total_rows, 5)
        self.assertEqual(page.rows[0].account_display_name, "公开账号2")
        self.assertEqual((page.listed_count, page.shown_count), (3, 2))
        self.assertEqual((page.member_count, page.account_count), (2, 4))
        self.assertIn("群内排行", html)
        self.assertIn("公开账号2", html)
        self.assertIn("#2 / 5", html)
        self.assertIn("超过统计上限", html)
        self.assertIn("数据刚刚更新", html)
        self.assertNotIn(USER, html)
        empty = renderer.render_group_board(
            self.ranking, (), query="q", member_count=1, account_count=1
        )
        self.assertIn("都没有公开记录", empty)


if __name__ == "__main__":
    unittest.main()
