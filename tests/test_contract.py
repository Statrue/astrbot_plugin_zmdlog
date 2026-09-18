"""危机合约 tags: grouping, naming and the battle tool's one-line summary."""

import unittest

from core import facts
from core.contract import (
    OTHER_FAMILY,
    UNNAMED_TAG,
    contract_family,
    contract_score_total,
    group_contract_tags,
    tag_display_name,
)
from core.models import ContractTag, parse_battle_detail
from tests.helpers import battle_detail_payload, crisis_contract_tags


def contract_battle_payload() -> dict:
    """The shared battle fixture, turned into a contract record."""

    payload = battle_detail_payload()
    tags = crisis_contract_tags()
    payload["battle"]["contractTags"] = tags
    payload["battle"]["contractTagScore"] = sum(tag["score"] for tag in tags)
    return payload


class ContractGroupingTests(unittest.TestCase):
    def test_families_come_from_the_name_prefix_in_canonical_order(self) -> None:
        tags = (
            ContractTag(tag_id=103203, score=3, name="环境：震荡"),
            ContractTag(tag_id=100502, score=2, name="队列：折刃"),
            ContractTag(tag_id=101001, score=1, name="改写：屏障"),
            ContractTag(tag_id=101402, score=3, name="队列：衰竭"),
        )

        groups = group_contract_tags(tags)

        self.assertEqual([group.family for group in groups], ["队列", "改写", "环境"])
        # Upstream order inside a family, never re-sorted.
        self.assertEqual(
            [tag.name for tag in groups[0].tags], ["队列：折刃", "队列：衰竭"]
        )
        self.assertEqual([group.score for group in groups], [5, 1, 3])
        self.assertEqual(contract_score_total(tags), 9)

    def test_only_families_with_tags_appear(self) -> None:
        only = (ContractTag(tag_id=1, score=1, name="环境：切削"),)
        groups = group_contract_tags(only)

        self.assertEqual([group.family for group in groups], ["环境"])
        self.assertEqual(group_contract_tags(()), ())

    def test_unknown_prefix_and_missing_name_land_in_other_last(self) -> None:
        tags = (
            ContractTag(tag_id=1, score=1, name=None),
            ContractTag(tag_id=2, score=2, name="特殊：无前缀家族"),
            ContractTag(tag_id=3, score=1, name="队列：重负"),
            ContractTag(tag_id=4, score=1, name="没有分隔符"),
        )

        groups = group_contract_tags(tags)

        self.assertEqual([group.family for group in groups], ["队列", OTHER_FAMILY])
        self.assertEqual(len(groups[1].tags), 3)
        self.assertEqual(contract_family("队列："), "队列")
        self.assertEqual(contract_family("队列:半角冒号"), OTHER_FAMILY)
        self.assertEqual(contract_family(None), OTHER_FAMILY)

    def test_an_unnamed_tag_never_prints_its_id(self) -> None:
        self.assertEqual(
            tag_display_name(ContractTag(tag_id=100502, score=2, name=None)),
            UNNAMED_TAG,
        )
        self.assertEqual(
            tag_display_name(ContractTag(tag_id=100502, score=2, name="   ")),
            UNNAMED_TAG,
        )
        self.assertEqual(
            tag_display_name(ContractTag(tag_id=100502, score=2, name="队列：折刃")),
            "队列：折刃",
        )


class ContractParsingTests(unittest.TestCase):
    def test_real_shaped_tags_parse_and_sum_to_the_score(self) -> None:
        battle = parse_battle_detail(contract_battle_payload())

        self.assertEqual(battle.contract_tag_score, 14)
        self.assertEqual(len(battle.contract_tags), 6)
        self.assertEqual(contract_score_total(battle.contract_tags), 14)
        first = battle.contract_tags[0]
        self.assertEqual(first.tag_id, 100502)
        self.assertEqual(first.name, "队列：折刃")
        self.assertEqual(
            first.icon_url,
            "/images/contract-tag/icon_activity_contract_tag_208.png",
        )
        # The extra upstream keys (iconId, buffId, terms, values, …) are
        # ignored, and every score is on the 1–3 scale.
        for tag in battle.contract_tags:
            self.assertIn(tag.score, (1, 2, 3))


class ContractFactsTests(unittest.TestCase):
    def test_battle_text_states_the_score_and_family_counts_only(self) -> None:
        text = facts.format_battle(parse_battle_detail(contract_battle_payload()))

        self.assertIn("危机合约 14 分 · 6 条词条（队列 2 · 改写 2 · 环境 2）", text)
        # Names and descriptions are what the picture is for.
        self.assertNotIn("折刃", text)
        self.assertNotIn("禁止闪避", text)

    def test_a_score_without_tags_is_stated_alone(self) -> None:
        payload = battle_detail_payload()
        payload["battle"]["contractTagScore"] = 40
        payload["battle"]["contractTags"] = []

        text = facts.format_battle(parse_battle_detail(payload))

        self.assertIn("危机合约 40 分\n", text)
        self.assertNotIn("条词条", text)

    def test_an_ordinary_battle_says_nothing_about_contracts(self) -> None:
        text = facts.format_battle(parse_battle_detail(battle_detail_payload()))

        self.assertNotIn("危机合约", text)
