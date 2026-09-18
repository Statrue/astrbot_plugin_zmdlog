"""危机合约 tags, grouped the way their names already group them.

A contract record carries the tags its player picked, 14–25 of them, and a
score that is exactly the sum of their scores. Each name carries one of three
family prefixes — 队列 (roster constraints), 改写 (rule rewrites), 环境
(arena effects) — so the grouping here reads the prefix and invents nothing;
a name without one lands in 其他 rather than being guessed at. Tags stay in
upstream order inside a family and are never merged by their base id: a tag's
name can change between tiers (环境：厌氧 at tier 1 is 环境：禁锢 at tier
3), and the tier in the id's last digit is not reliably the score (队列：衰竭
is tier 2 at score 3), so the page shows the name and the ``score`` field and
nothing derived.

Pure functions over the parsed model, shared by the battle card and the
battle tool's text so both count the same families the same way.
"""

from dataclasses import dataclass

from .models import ContractTag

CONTRACT_FAMILIES: tuple[str, ...] = ("队列", "改写", "环境")
OTHER_FAMILY = "其他"
# Raw ids are never printed as names; an unnamed tag says so instead.
UNNAMED_TAG = "名称未收录"

_FAMILY_SEPARATOR = "："


@dataclass(frozen=True, slots=True)
class ContractGroup:
    family: str
    tags: tuple[ContractTag, ...]

    @property
    def score(self) -> int:
        return sum(tag.score for tag in self.tags)


def contract_family(name: str | None) -> str:
    """The family a tag name declares, or 其他 when it declares none."""

    if name is None:
        return OTHER_FAMILY
    prefix, separator, _ = name.partition(_FAMILY_SEPARATOR)
    if separator and prefix in CONTRACT_FAMILIES:
        return prefix
    return OTHER_FAMILY


def tag_display_name(tag: ContractTag) -> str:
    name = (tag.name or "").strip()
    return name or UNNAMED_TAG


def group_contract_tags(tags: tuple[ContractTag, ...]) -> tuple[ContractGroup, ...]:
    """Non-empty families in canonical order, 其他 last, upstream order inside."""

    buckets: dict[str, list[ContractTag]] = {
        family: [] for family in (*CONTRACT_FAMILIES, OTHER_FAMILY)
    }
    for tag in tags:
        buckets[contract_family(tag.name)].append(tag)
    return tuple(
        ContractGroup(family=family, tags=tuple(members))
        for family, members in buckets.items()
        if members
    )


def contract_score_total(tags: tuple[ContractTag, ...]) -> int:
    return sum(tag.score for tag in tags)
