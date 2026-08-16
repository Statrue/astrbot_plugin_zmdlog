import unittest

from core.identifiers import (
    PublicReferenceError,
    extract_battle_references,
    parse_account_reference,
    parse_battle_reference,
)


class PublicReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_url = "https://zmdlogs.com"

    def test_account_accepts_exact_id_and_records_url(self) -> None:
        account_id = "usr_1234567890abcdef"
        self.assertEqual(
            parse_account_reference(account_id, web_base_url=self.base_url),
            account_id,
        )
        self.assertEqual(
            parse_account_reference(
                f"https://zmdlogs.com/records/{account_id}?from=chat",
                web_base_url=self.base_url,
            ),
            account_id,
        )

    def test_account_rejects_nickname_and_untrusted_host(self) -> None:
        for value in (
            "测试账号",
            "https://example.com/records/usr_1234567890abcdef",
        ):
            with self.subTest(value=value):
                with self.assertRaises(PublicReferenceError):
                    parse_account_reference(value, web_base_url=self.base_url)

    def test_battle_accepts_id_and_supported_page_urls(self) -> None:
        battle_id = "btl_upload_abcdef123456"
        references = (
            battle_id,
            f"https://zmdlogs.com/battle/{battle_id}?metric=dps",
            f"https://zmdlogs.com/share/{battle_id}",
            f"https://zmdlogs.com/axis/{battle_id}",
            f"https://zmdlogs.com/axis/{battle_id}/editor",
        )
        for reference in references:
            with self.subTest(reference=reference):
                self.assertEqual(
                    parse_battle_reference(
                        reference,
                        web_base_url=self.base_url,
                    ),
                    battle_id,
                )

    def test_message_extraction_deduplicates_and_ignores_other_hosts(self) -> None:
        first = "btl_upload_abcdef123456"
        second = "btl_upload_999999999999"
        message = (
            f"看这个 https://zmdlogs.com/battle/{first}?metric=dps，"
            f"重复 https://zmdlogs.com/share/{first} "
            f"外站 https://example.com/battle/{second} "
            f"另一个 https://zmdlogs.com/axis/{second}"
        )

        self.assertEqual(
            extract_battle_references(
                message,
                web_base_url=self.base_url,
                limit=2,
            ),
            (first, second),
        )


if __name__ == "__main__":
    unittest.main()
