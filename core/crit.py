"""暴击期望: what a battle's damage would have been at its recorded crit rates.

Uploads from parser v57 on (2026-09-25) carry, on every skill hit, the crit
parameters it was rolled with (`hitContext.critical`, UPSTREAM.md). Holding
the skills and the hits fixed as they happened, each hit splits into a part
that did not depend on the roll and a part a crit adds with probability
``p``; summing those gives the expected total and its variance. The
maths is the site's own, read in its Web Worker, so the card's figures are
the ones its 暴击分析 panel prints.

Two deliberate differences from the site:

* The site refuses the whole computation on any of five inconsistent hits
  (a rate outside [0, 1], a crit at rate 0, a miss at rate 1, an uncapped
  figure or a cap below the damage dealt). Here such a hit is dropped and
  lowers the coverage instead — one bad hit must not take away a section
  the card could otherwise draw, the same rule the rest of the telemetry
  follows.
* No luck percentile. The site's exact distribution needs a histogram
  convolution over thousands of hits, and the expected total with its
  deviation already answers "打得好还是运气好".

Pure and import-free: the parse fills :class:`CritRoll` and the recipe turns
damage points into :class:`CritHit`, so this module is tested on hand-written
hits alone.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CritRoll:
    """The crit parameters one hit was rolled with, as the upload recorded them."""

    is_critical: bool
    # The chance to crit, 0–1.
    rate: float
    # What a crit adds, as a fraction: 0.5 is +50%.
    damage_bonus: float
    # The damage before the cap; the hit's own damage when absent.
    uncapped_damage: float | None = None
    # No cap when absent.
    damage_cap: float | None = None


@dataclass(frozen=True, slots=True)
class CritHit:
    """One skill hit that dealt damage; ``roll`` is None when it was not recorded."""

    damage: int
    roll: CritRoll | None = None
    character: str | None = None


@dataclass(frozen=True, slots=True)
class CritTotals:
    """One row of the section: one character's hits, or the whole team's."""

    # None for the team row.
    character: str | None
    # Of the analysed hits only, so it compares with the expectation.
    actual_damage: int
    expected_damage: float
    standard_deviation: float
    expected_dps: float
    # Analysed damage over all the skill damage the hits dealt.
    coverage: float
    analysed_hits: int
    # Hits whose parameters contradicted their own damage.
    discarded_hits: int

    @property
    def relative_difference(self) -> float:
        """实际偏差: how far the actual total landed from the expected one."""

        if self.expected_damage <= 0:
            return 0.0
        return (self.actual_damage - self.expected_damage) / self.expected_damage


@dataclass(frozen=True, slots=True)
class CritExpectation:
    team: CritTotals
    # Characters with at least one analysed hit, heaviest hitters first.
    characters: tuple[CritTotals, ...]


class _Tally:
    """Running sums for one row; the names follow the site's worker."""

    __slots__ = ("actual", "fixed", "mean", "variance", "skill_damage",
                 "analysed_hits", "discarded_hits")

    def __init__(self) -> None:
        # Damage of the analysed hits.
        self.actual = 0
        self.fixed = 0.0
        self.mean = 0.0
        self.variance = 0.0
        # Damage of every hit, analysed or not: the coverage's denominator.
        self.skill_damage = 0
        self.analysed_hits = 0
        self.discarded_hits = 0

    def add(self, damage: int, roll: CritRoll | None) -> None:
        self.skill_damage += damage
        if roll is None:
            return
        if not _consistent(damage, roll):
            self.discarded_hits += 1
            return
        n = roll.uncapped_damage if roll.uncapped_damage is not None else damage
        cap = roll.damage_cap if roll.damage_cap is not None else math.inf
        p, scale = roll.rate, 1 + roll.damage_bonus
        # The roll without a crit, and what a crit would have added to it.
        base = n / scale if roll.is_critical else n
        low = min(base, cap)
        extra = min(base * scale, cap) - low
        if p == 0 or extra == 0:
            self.fixed += low
        elif p == 1:
            self.fixed += low + extra
        else:
            self.fixed += low
            self.mean += extra * p
            self.variance += extra * extra * p * (1 - p)
        self.actual += damage
        self.analysed_hits += 1

    def totals(self, character: str | None, duration_ms: int) -> CritTotals:
        expected = self.fixed + self.mean
        return CritTotals(
            character=character,
            actual_damage=self.actual,
            expected_damage=expected,
            standard_deviation=math.sqrt(self.variance),
            expected_dps=1000 * expected / max(duration_ms, 1),
            coverage=(
                self.actual / self.skill_damage if self.skill_damage > 0 else 0.0
            ),
            analysed_hits=self.analysed_hits,
            discarded_hits=self.discarded_hits,
        )


def coverage_percent(coverage: float) -> float:
    """The coverage in percent to one decimal, rounded down: 99.97% of the
    damage analysed must not read as all of it."""

    # The epsilon absorbs float noise: 0.57 · 1000 is 569.999…
    return math.floor(coverage * 1000 + 1e-9) / 10


def _consistent(damage: int, roll: CritRoll) -> bool:
    """False for the five contradictions the site refuses outright, and for a
    bonus of -100% or less, which would divide by zero."""

    p = roll.rate
    return not (
        not 0 <= p <= 1
        or (p == 0 and roll.is_critical)
        or (p == 1 and not roll.is_critical)
        or (roll.uncapped_damage is not None and roll.uncapped_damage < damage)
        or (roll.damage_cap is not None and roll.damage_cap < damage)
        or roll.damage_bonus <= -1
    )


def build_crit_expectation(
    hits: Iterable[CritHit], *, duration_ms: int
) -> CritExpectation | None:
    """Per character and for the team, or None when no hit could be analysed.

    Only hits that dealt damage count, as on the site; the caller passes the
    skill hits, never the buff lane.
    """

    team = _Tally()
    by_character: dict[str, _Tally] = {}
    for item in hits:
        if item.damage <= 0:
            continue
        team.add(item.damage, item.roll)
        if item.character is not None:
            by_character.setdefault(item.character, _Tally()).add(
                item.damage, item.roll
            )
    if team.analysed_hits == 0:
        return None
    characters = sorted(
        (
            tally.totals(name, duration_ms)
            for name, tally in by_character.items()
            if tally.analysed_hits > 0
        ),
        key=lambda row: (-row.actual_damage, row.character or ""),
    )
    return CritExpectation(
        team=team.totals(None, duration_ms), characters=tuple(characters)
    )
