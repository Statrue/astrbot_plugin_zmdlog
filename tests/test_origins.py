"""隔离对话: every member of a group is still in the group."""

import unittest

from core.origins import group_origin_of, is_group_origin, restore_group_origin

GROUP = "ZmdLogBot:GroupMessage:1"
PRIVATE = "ZmdLogBot:FriendMessage:9"


class GroupOriginTests(unittest.TestCase):
    def test_only_group_origins_have_members(self) -> None:
        self.assertTrue(is_group_origin(GROUP))
        self.assertFalse(is_group_origin(PRIVATE))
        self.assertFalse(is_group_origin(""))

    def test_the_adapter_session_id_is_put_back(self) -> None:
        self.assertEqual(
            restore_group_origin("ZmdLogBot:GroupMessage:111_1", "1"), GROUP
        )
        # Nothing to restore, or nothing to restore it into: unchanged.
        self.assertEqual(restore_group_origin(GROUP, ""), GROUP)
        self.assertEqual(restore_group_origin(PRIVATE, "1"), PRIVATE)
        self.assertEqual(restore_group_origin("", "1"), "")
        self.assertEqual(restore_group_origin("not-an-origin", "1"), "not-an-origin")


class StoredOriginTests(unittest.TestCase):
    def test_a_member_origin_maps_back_to_its_group(self) -> None:
        cases = (
            ("ZmdLogBot:GroupMessage:111_1", "aiocqhttp:111", GROUP),
            (
                "default_1:GroupMessage:MEMBER_GROUPOPENID",
                "qq_official:MEMBER",
                "default_1:GroupMessage:GROUPOPENID",
            ),
            ("lark:GroupMessage:ou_a%oc_b", "lark:ou_a", "lark:GroupMessage:oc_b"),
            ("mk:GroupMessage:room_ux", "misskey:ux", "mk:GroupMessage:room"),
            (
                "mx:GroupMessage:@a:hs.org_!room:hs.org",
                "matrix:@a:hs.org",
                "mx:GroupMessage:!room:hs.org",
            ),
        )
        for origin, member, group in cases:
            with self.subTest(origin=origin):
                self.assertEqual(group_origin_of(origin, member), group)

    def test_nothing_is_guessed_from_a_separator(self) -> None:
        # A Lark group id carries the separator itself; only a member id that
        # was named is ever stripped.
        self.assertIsNone(group_origin_of("lark:GroupMessage:oc_b", "lark:ou_a"))
        self.assertIsNone(group_origin_of("ZmdLogBot:GroupMessage:1", "aiocqhttp:111"))
        # Someone else's isolated origin is not this member's to move.
        self.assertIsNone(
            group_origin_of("ZmdLogBot:GroupMessage:222_1", "aiocqhttp:111")
        )
        self.assertIsNone(
            group_origin_of("ZmdLogBot:GroupMessage:1111_1", "aiocqhttp:111")
        )

    def test_what_cannot_be_mapped_is_left_alone(self) -> None:
        # DingTalk isolates by sender alone: the group is not in the origin.
        self.assertIsNone(group_origin_of("dt:GroupMessage:u1", "dingtalk:u1"))
        self.assertIsNone(group_origin_of("x:GroupMessage:u1_g", "unknown:u1"))
        member = "aiocqhttp:111"
        self.assertIsNone(group_origin_of("ZmdLogBot:FriendMessage:111_1", member))
        self.assertIsNone(group_origin_of("ZmdLogBot:GroupMessage:111_", member))
        self.assertIsNone(group_origin_of("ZmdLogBot:GroupMessage:111_1", ""))
        self.assertIsNone(group_origin_of("", member))
