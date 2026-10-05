"""账号绑定: the binding book, the code flow and the client call."""

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
    normalize_binding_code,
    parse_bindings,
)
from core.client import (
    InvalidPublicIdentifierError,
    ZmdLogsAPIError,
    ZmdLogsClient,
    ZmdLogsClientError,
)
from core.models import AccountSearchHit
from core.outcome import SitePage
from core.persistence import load_json, save_json
from core.routing import parse_zmdlog_payload
from core.settings import PluginSettings

GROUP = "aiocqhttp:GroupMessage:1"
PRIVATE = "aiocqhttp:FriendMessage:9"
USER = "aiocqhttp:111"
OTHER_USER = "aiocqhttp:222"
NOW = "2026-09-15T10:00:00+00:00"
LATER = "2026-09-15T10:20:00+00:00"
CODE = "ZMD-7K4M-QX2E"


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


def add(book: BindingBook, user: str, account: BoundAccount, *, now=NOW):
    """with_account with the test's defaults; returns the book and the status."""

    return book.with_account(user, account, now=now)


class BookTests(unittest.TestCase):
    def test_a_new_account_is_appended_and_the_primary_stays(self) -> None:
        book, status = add(BindingBook.empty(), USER, bound("usr_a", "CPU 0"))
        self.assertEqual(status, "added")
        book, status = add(book, USER, bound("usr_b", "cpu0"), now=LATER)
        self.assertEqual(status, "added")

        mine = book.for_user(USER)
        self.assertEqual([a.account_id for a in mine.accounts], ["usr_a", "usr_b"])
        self.assertEqual(mine.primary.account_id, "usr_a")
        self.assertEqual(mine.updated_at, LATER)

    def test_rebinding_an_account_refreshes_its_nickname_in_place(self) -> None:
        book, _ = add(BindingBook.empty(), USER, bound("usr_a", "旧名"))
        book, _ = add(book, USER, bound("usr_b"))

        book, status = add(book, USER, bound("usr_a", "新名"), now=LATER)

        self.assertEqual(status, "refreshed")
        mine = book.for_user(USER)
        self.assertEqual([a.display_name for a in mine.accounts], ["新名", "CPU 0"])

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
        self.assertEqual(set(payload["users"][USER]), {"accounts", "updatedAt"})
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

    def _reply(self, text: str, *, origin: str = GROUP, user: str = USER):
        return run(
            self.service.handle_route(
                parse_zmdlog_payload(text),
                origin=origin,
                requester_key=user,
                command="/zmdlog",
            )
        )

    def _handle(self, text: str, *, origin: str = GROUP, user: str = USER) -> str:
        return self._reply(text, origin=origin, user=user).message

    def test_a_reply_that_sends_the_user_for_a_code_names_the_page(self) -> None:
        # Where the text says "go to the site and make a code", the site's
        # binding page can be offered one tap away; nowhere else.
        answers = self.client.answers
        answers["ZMD-EEEE-FFFF"] = ZmdLogsAPIError(404, "http_404", "Not Found")
        self._handle("绑定 ZMD-7K4M-QX2E", user=OTHER_USER)
        for text, user, sends in (
            ("解绑", USER, True),  # not bound yet
            ("主账号", USER, True),
            ("绑定", USER, True),  # no code
            ("绑定 ZMD-ZZZZ-ZZZZ", USER, True),  # no such code
            ("绑定 ZMD-7K4M-QX2E", USER, True),  # spent by another user
            ("绑定 ZMD-EEEE-FFFF", USER, False),  # the site has no codes
            ("主账号", OTHER_USER, False),  # bound: a list
        ):
            with self.subTest(text=text, user=user):
                reply = self._reply(text, user=user)

                self.assertIs(reply.site_page, SitePage.BINDING if sends else None)

    def test_a_valid_code_binds_and_the_file_records_only_public_data(self) -> None:
        reply = self._handle("绑定 zmd-7k4m-qx2e")

        self.assertIn("已绑定 CPU 0（usr_a）", reply)
        self.assertIn("1. CPU 0（usr_a）", reply)
        self.assertEqual(self.client.codes, [CODE], "sent as the site spells it")
        stored = load_json(self.root / BINDINGS_FILE)
        self.assertEqual(stored["users"][USER]["accounts"][0]["accountId"], "usr_a")
        self.assertNotIn(GROUP, (self.root / BINDINGS_FILE).read_text(encoding="utf-8"))
        self.assertNotIn(CODE, (self.root / BINDINGS_FILE).read_text(encoding="utf-8"))
        # The service reloads what it wrote.
        again = self._service(self.client)
        self.assertEqual(again.bindings_for(USER).primary.account_id, "usr_a")

    def test_every_binding_command_answers_in_group_chats_only(self) -> None:
        # The bot adds nobody as a friend, so a private chat is refused
        # before the code is even looked at, bound or not.
        for text in ("绑定 ZMD-7K4M-QX2E", "解绑", "主账号"):
            for origin in (PRIVATE, ""):
                with self.subTest(text=text, origin=origin):
                    reply = self._handle(text, origin=origin)
                    self.assertEqual(reply, messages.BINDING_GROUP_ONLY)
        self.assertEqual(self.client.codes, [], "no lookup for a refused chat")
        self.assertIsNone(self.service.bindings_for(USER))

        self.assertIn("已绑定", self._handle("绑定 ZMD-7K4M-QX2E"))
        refused = self._handle("解绑", origin=PRIVATE)
        self.assertEqual(refused, messages.BINDING_GROUP_ONLY)
        self.assertIsNotNone(self.service.bindings_for(USER), "still bound")

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
        self.assertEqual(reply.message, messages.BINDINGS_DISABLED)
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

    def test_a_file_from_before_1_3_0_keeps_its_bindings_and_drops_its_chats(
        self,
    ) -> None:
        # Up to 1.2 every user carried the chats they had used a binding
        # command in, the membership of a per-chat board since removed; the
        # list is read past, and the next write leaves it out.
        save_json(
            self.root / BINDINGS_FILE,
            {
                "version": 1,
                "users": {
                    USER: {
                        "accounts": [
                            {"accountId": "usr_a", "displayName": "CPU 0",
                             "boundAt": NOW},
                            {"accountId": "usr_b", "displayName": "cpu0",
                             "boundAt": NOW},
                        ],
                        "groups": [GROUP, "aiocqhttp:GroupMessage:111_2"],
                        "updatedAt": NOW,
                    },
                    OTHER_USER: {
                        "accounts": [
                            {"accountId": "usr_c", "displayName": "第三",
                             "boundAt": NOW},
                        ],
                        "groups": "not even a list",
                        "updatedAt": NOW,
                    },
                },
                "usedCodes": {code_digest(CODE): NOW},
            },
        )
        before = (self.root / BINDINGS_FILE).read_text(encoding="utf-8")

        service = self._service(self.client)

        mine = service.bindings_for(USER)
        self.assertEqual([a.account_id for a in mine.accounts], ["usr_a", "usr_b"])
        self.assertEqual(mine.primary.display_name, "CPU 0")
        self.assertEqual(
            [a.account_id for a in service.bindings_for(OTHER_USER).accounts],
            ["usr_c"],
        )
        self.assertTrue(service.book.code_used(code_digest(CODE), now=NOW))
        # Loading alone writes nothing.
        self.assertEqual(
            (self.root / BINDINGS_FILE).read_text(encoding="utf-8"), before
        )
        self.service = service
        self.assertIn("主账号已改为 cpu0（usr_b）", self._handle("主账号 2"))
        stored = load_json(self.root / BINDINGS_FILE)
        for user in (USER, OTHER_USER):
            with self.subTest(user=user):
                self.assertNotIn("groups", stored["users"][user])
        self.assertEqual(
            [a["accountId"] for a in stored["users"][USER]["accounts"]],
            ["usr_b", "usr_a"],
        )

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
        self.assertEqual(reply.message, messages.BINDINGS_WRITE_FAILED)
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


if __name__ == "__main__":
    unittest.main()
