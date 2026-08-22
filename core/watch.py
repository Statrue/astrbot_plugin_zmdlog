"""Detect and describe public rank drops for watched accounts.

One request to ``users/{id}/rankings`` carries the rank of that account on every
board, so a whole watch list costs one request per account per cycle. Nothing
here talks to the network.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import BossRanking, PublicUserRankings
from .presentation import format_number, public_url

DEFAULT_RANK_THRESHOLD = 10
SNAPSHOT_VERSION = 2
MAX_DROPS_PER_NOTICE = 5
_MAX_ACCOUNTS_PER_MESSAGE = 3
_BOARD_PART_RE = re.compile(r"\s*·\s*")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Board ranks of one account plus when they were read."""

    ranks: dict[str, int]
    # Missing only for a hand-edited file. Without it no attribution is
    # attempted and no staleness bound can be applied, so the entry is treated
    # as a first sighting instead of a baseline.
    checked_at: str | None = None


@dataclass(frozen=True, slots=True)
class NewRecordAbove:
    """A record posted since the last check that now outranks the account.

    Deliberately NOT called an overtaker: see :func:`find_new_record_above`.
    """

    account_display_name: str
    character_name: str
    dps: float
    battle_id: str
    # How many such records appeared. The wording changes above one because no
    # single record can then be singled out.
    record_count: int = 1


@dataclass(frozen=True, slots=True)
class RankDrop:
    boss_slug: str
    boss_name: str
    dungeon_name: str
    previous_rank: int
    current_rank: int
    new_record_above: NewRecordAbove | None = None


def build_snapshot(
    account: PublicUserRankings,
    *,
    checked_at: str,
) -> AccountSnapshot:
    return AccountSnapshot(ranks=snapshot_ranks(account), checked_at=checked_at)


def snapshot_ranks(account: PublicUserRankings) -> dict[str, int]:
    """Board rank of the account, keyed by slug, as stored between cycles."""

    return {entry.boss_slug: entry.rank for entry in account.rankings}


def snapshot_is_usable(
    snapshot: AccountSnapshot | None,
    *,
    now: str,
    max_age_seconds: float,
) -> bool:
    """Whether ``snapshot`` is recent enough to be compared against.

    A baseline from before a long outage or a long shutdown would make every
    rank lost in the meantime look like breaking news, so an old one is
    discarded and the account is re-seeded silently instead.
    """

    if snapshot is None or not snapshot.ranks:
        return False
    checked_at = parse_timestamp(snapshot.checked_at)
    current = parse_timestamp(now)
    if checked_at is None or current is None:
        return False
    return 0 <= (current - checked_at).total_seconds() <= max_age_seconds


def find_rank_drops(
    previous: AccountSnapshot | None,
    account: PublicUserRankings,
    *,
    rank_threshold: int = DEFAULT_RANK_THRESHOLD,
) -> tuple[RankDrop, ...]:
    """Boards where the account lost ground since ``previous``.

    A board missing from ``previous`` is a first sighting and never an event,
    otherwise a newly watched account would announce its whole history at once.
    Improvements stay silent by design, and a drop only matters when the
    account was inside the top ``rank_threshold`` to begin with.
    """

    if previous is None or not previous.ranks:
        return ()
    drops: list[RankDrop] = []
    for entry in account.rankings:
        was = previous.ranks.get(entry.boss_slug)
        if was is None or entry.rank <= was or was > rank_threshold:
            continue
        drops.append(
            RankDrop(
                boss_slug=entry.boss_slug,
                boss_name=entry.boss_name,
                dungeon_name=entry.dungeon_name,
                previous_rank=was,
                current_rank=entry.rank,
            )
        )
    return tuple(
        sorted(drops, key=lambda drop: (drop.previous_rank, drop.boss_name))
    )


def find_new_record_above(
    ranking: BossRanking,
    *,
    account_id: str,
    fallback_rank: int,
    since: str | None,
) -> NewRecordAbove | None:
    """The newest record now ranked above the account that postdates ``since``.

    This deliberately does not claim to identify who overtook the account, and
    the notice must not say so either. Two things cannot be known from a single
    board read: the row sitting at the old rank is usually a bystander who was
    pushed down by an insertion further up, and a record posted in the window
    may equally be a player who was already ahead improving their own time.
    ``battleEndAt`` is also when the battle ended rather than when it was
    uploaded, so a late upload can be the real cause while looking old.

    What can honestly be reported is exactly what this returns: a record that
    is now ahead and did not exist at the last check. ``None`` when nothing
    qualifies, which renders as a bare rank change.
    """

    baseline = parse_timestamp(since)
    if baseline is None:
        return None
    # Take the cutoff from this same response where possible: current_rank came
    # from a separate account read and the two can disagree while either cache
    # is warm, which would let a row that is not actually above slip in.
    cutoff = fallback_rank
    for row in ranking.rows:
        if row.account_id == account_id:
            cutoff = row.rank
            break
    newest_row = None
    newest_time = None
    count = 0
    for row in ranking.rows:
        if row.rank >= cutoff or row.account_id == account_id:
            continue
        landed = parse_timestamp(row.battle_end_at)
        if landed is None or landed <= baseline:
            continue
        count += 1
        if newest_time is None or landed > newest_time:
            newest_row, newest_time = row, landed
    if newest_row is None:
        return None
    return NewRecordAbove(
        account_display_name=newest_row.account_display_name,
        character_name=newest_row.character_name,
        dps=newest_row.dps,
        battle_id=newest_row.battle_id,
        record_count=count,
    )


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 stamp; ``None`` when it cannot be trusted.

    A stamp without an offset cannot be compared against one that has it, so it
    is rejected rather than assumed to be local time.
    """

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def format_rank_drop_notice(
    display_name: str,
    drops: tuple[RankDrop, ...],
    *,
    web_base_url: str,
) -> str:
    """One message covering every board this account just dropped on."""

    shown = drops[:MAX_DROPS_PER_NOTICE]
    lines = [f"📉 {display_name} 被顶屁股了"]
    for drop in shown:
        lines.append("")
        lines.append(
            f"「{board_label(drop.dungeon_name, drop.boss_name)}」"
            f"第 {drop.previous_rank} → 第 {drop.current_rank}"
        )
        record = drop.new_record_above
        if record is None:
            continue
        who = (
            f"{record.account_display_name} · "
            f"{format_number(record.dps)} DPS · "
            f"主C {record.character_name}"
        )
        if record.record_count > 1:
            lines.append(
                f"期间上方新增 {record.record_count} 条纪录，最新的是 {who}"
            )
        else:
            lines.append(f"期间上方新增纪录：{who}")
        lines.append(public_url(web_base_url, "battle", record.battle_id))
    hidden = len(drops) - len(shown)
    if hidden > 0:
        lines.append("")
        lines.append(f"另有 {hidden} 个榜单也掉了名次。")
    return "\n".join(lines)


def join_rank_drop_notices(notices: tuple[str, ...]) -> str:
    """Merge one cycle worth of notices so a chat receives a single message.

    One new record demotes every watched account below it, so a group watching
    its own members would otherwise get a burst of near-identical pushes in the
    same instant, which is also what gets a bot rate-limited.
    """

    shown = notices[:_MAX_ACCOUNTS_PER_MESSAGE]
    text = "\n\n".join(shown)
    hidden = len(notices) - len(shown)
    if hidden > 0:
        text += f"\n\n另有 {hidden} 个关注的账号也掉了名次。"
    return text


def board_label(dungeon_name: str, boss_name: str) -> str:
    """Name the board without repeating the segment both names share.

    Upstream dungeon and boss names overlap a lot (``影拓丰碑4期 · 山中见犼``
    plus ``山中见犼·苦难``), which reads badly in a one-line push.
    """

    parts: list[str] = []
    for part in (
        *_BOARD_PART_RE.split(dungeon_name),
        *_BOARD_PART_RE.split(boss_name),
    ):
        if part and part not in parts:
            parts.append(part)
    return " · ".join(parts)


def parse_snapshot_payload(payload: Any) -> dict[str, AccountSnapshot]:
    """Read the stored ranks, dropping anything malformed instead of raising."""

    if not isinstance(payload, dict):
        return {}
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    snapshots: dict[str, AccountSnapshot] = {}
    for account_id, entry in accounts.items():
        if not isinstance(account_id, str) or not isinstance(entry, dict):
            continue
        raw_ranks = entry.get("ranks")
        # The presence of "ranks" is what marks a current entry; an account
        # with no public records legitimately stores an empty mapping.
        if not isinstance(raw_ranks, dict):
            continue
        checked_at = entry.get("checkedAt")
        snapshots[account_id] = AccountSnapshot(
            ranks={
                slug: rank
                for slug, rank in raw_ranks.items()
                if isinstance(slug, str)
                and isinstance(rank, int)
                and not isinstance(rank, bool)
                and rank > 0
            },
            checked_at=checked_at if isinstance(checked_at, str) else None,
        )
    return snapshots


def snapshot_payload(snapshots: dict[str, AccountSnapshot]) -> dict[str, Any]:
    return {
        "version": SNAPSHOT_VERSION,
        "accounts": {
            account_id: {
                "checkedAt": snapshot.checked_at,
                "ranks": snapshot.ranks,
            }
            for account_id, snapshot in snapshots.items()
        },
    }
