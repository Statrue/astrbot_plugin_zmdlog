"""Detect and describe public rank drops and new top records.

One request to ``users/{id}/rankings`` carries the rank of that account on every
board, so a whole account watch list costs one request per account per cycle;
one ``hot-bosses`` read carries the top three of every board, so a whole board
watch list costs one request per cycle. Nothing here talks to the network.
"""

from dataclasses import dataclass
from typing import Any

from .models import BossRanking, HotBossCard, PublicUserRankings
from .presentation import format_duration, format_number, public_url
from .timestamps import parse_timestamp
from .watchlist import board_label

DEFAULT_RANK_THRESHOLD = 10
SNAPSHOT_VERSION = 2
BOARD_SNAPSHOT_VERSION = 1
MAX_DROPS_PER_NOTICE = 5
_MAX_ACCOUNTS_PER_MESSAGE = 3
_MAX_BOARDS_PER_MESSAGE = 3


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


@dataclass(frozen=True, slots=True)
class BoardTopRun:
    """One of the top runs of a board, as much as a notice needs to name it."""

    battle_id: str
    uploader_nickname: str
    character_name: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class BoardSnapshot:
    """Top runs of one board in rank order plus when they were read."""

    runs: tuple[BoardTopRun, ...]
    checked_at: str | None = None


@dataclass(frozen=True, slots=True)
class BoardRunChange:
    rank: int
    run: BoardTopRun


@dataclass(frozen=True, slots=True)
class BoardChange:
    """Records that entered the top of a board since the last check.

    ``dropped_runs`` are the previous top runs no longer in the top: with new
    entries above them that is provable displacement, so the notice may say
    they fell out.
    """

    boss_slug: str
    boss_name: str
    dungeon_name: str
    new_runs: tuple[BoardRunChange, ...]
    dropped_runs: tuple[BoardRunChange, ...] = ()


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
    return _is_fresh(snapshot.checked_at, now=now, max_age_seconds=max_age_seconds)


def board_snapshot_is_usable(
    snapshot: BoardSnapshot | None,
    *,
    now: str,
    max_age_seconds: float,
) -> bool:
    """Like :func:`snapshot_is_usable` for boards.

    An empty top list is a real state here (a board with no public record
    yet), so only the timestamp decides.
    """

    if snapshot is None:
        return False
    return _is_fresh(snapshot.checked_at, now=now, max_age_seconds=max_age_seconds)


def _is_fresh(checked_at: str | None, *, now: str, max_age_seconds: float) -> bool:
    baseline = parse_timestamp(checked_at)
    current = parse_timestamp(now)
    if baseline is None or current is None:
        return False
    return 0 <= (current - baseline).total_seconds() <= max_age_seconds


def build_board_snapshot(card: HotBossCard, *, checked_at: str) -> BoardSnapshot:
    return BoardSnapshot(
        runs=tuple(
            BoardTopRun(
                battle_id=run.battle_id,
                uploader_nickname=run.uploader_nickname,
                character_name=run.character_name,
                duration_ms=run.duration_ms,
            )
            for run in card.top_speed_runs
        ),
        checked_at=checked_at,
    )


def find_top_run_changes(
    previous: BoardSnapshot | None,
    card: HotBossCard,
) -> BoardChange | None:
    """Records now in the board's top that were not there at the last check.

    A missing baseline is a first sighting and never an event. A top list
    that only lost entries (a deleted record) is not news either: nobody set a
    new record, so nothing is reported.
    """

    if previous is None:
        return None
    current = build_board_snapshot(card, checked_at="")
    previous_ids = {run.battle_id for run in previous.runs}
    current_ids = {run.battle_id for run in current.runs}
    new_runs = tuple(
        BoardRunChange(rank=rank, run=run)
        for rank, run in enumerate(current.runs, start=1)
        if run.battle_id not in previous_ids
    )
    if not new_runs:
        return None
    dropped = tuple(
        BoardRunChange(rank=rank, run=run)
        for rank, run in enumerate(previous.runs, start=1)
        if run.battle_id not in current_ids
    )
    return BoardChange(
        boss_slug=card.boss_slug,
        boss_name=card.boss_name,
        dungeon_name=card.dungeon_name,
        new_runs=new_runs,
        dropped_runs=dropped,
    )


def format_board_notice(change: BoardChange, *, web_base_url: str) -> str:
    """One message for one board: every new top run, then who fell out."""

    lines = [
        f"🏁 「{board_label(change.dungeon_name, change.boss_name)}」"
        f"前三名有新纪录"
    ]
    for entry in change.new_runs:
        lines.append("")
        lines.append(
            f"第 {entry.rank} 名 · {entry.run.uploader_nickname} · "
            f"主C {entry.run.character_name} · "
            f"用时 {format_duration(entry.run.duration_ms)}"
        )
        lines.append(public_url(web_base_url, "battle", entry.run.battle_id))
    if change.dropped_runs:
        lines.append("")
        lines.append(
            "跌出前三："
            + "、".join(
                f"{entry.run.uploader_nickname}（原第 {entry.rank} · "
                f"主C {entry.run.character_name}）"
                for entry in change.dropped_runs
            )
        )
    return "\n".join(lines)


def join_board_notices(notices: tuple[str, ...]) -> str:
    """One message per chat per cycle, like :func:`join_rank_drop_notices`."""

    return _join_notices(
        notices,
        limit=_MAX_BOARDS_PER_MESSAGE,
        hidden_label="另有 {hidden} 个关注的榜单也有新纪录。",
    )


def parse_board_snapshot_payload(payload: Any) -> dict[str, BoardSnapshot]:
    """Read the stored top runs, dropping anything malformed instead of raising."""

    if not isinstance(payload, dict):
        return {}
    boards = payload.get("boards")
    if not isinstance(boards, dict):
        return {}
    snapshots: dict[str, BoardSnapshot] = {}
    for boss_slug, entry in boards.items():
        if not isinstance(boss_slug, str) or not isinstance(entry, dict):
            continue
        raw_runs = entry.get("runs")
        if not isinstance(raw_runs, list):
            continue
        runs: list[BoardTopRun] = []
        for raw in raw_runs:
            if not isinstance(raw, dict):
                continue
            battle_id = raw.get("battleId")
            duration = raw.get("durationMs")
            if not isinstance(battle_id, str) or not battle_id:
                continue
            if isinstance(duration, bool) or not isinstance(duration, int):
                duration = 0
            nickname = raw.get("uploaderNickname")
            character = raw.get("characterName")
            runs.append(
                BoardTopRun(
                    battle_id=battle_id,
                    uploader_nickname=nickname if isinstance(nickname, str) else "",
                    character_name=character if isinstance(character, str) else "",
                    duration_ms=max(0, duration),
                )
            )
        checked_at = entry.get("checkedAt")
        snapshots[boss_slug] = BoardSnapshot(
            runs=tuple(runs),
            checked_at=checked_at if isinstance(checked_at, str) else None,
        )
    return snapshots


def board_snapshot_payload(snapshots: dict[str, BoardSnapshot]) -> dict[str, Any]:
    return {
        "version": BOARD_SNAPSHOT_VERSION,
        "boards": {
            boss_slug: {
                "checkedAt": snapshot.checked_at,
                "runs": [
                    {
                        "battleId": run.battle_id,
                        "uploaderNickname": run.uploader_nickname,
                        "characterName": run.character_name,
                        "durationMs": run.duration_ms,
                    }
                    for run in snapshot.runs
                ],
            }
            for boss_slug, snapshot in snapshots.items()
        },
    }


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

    return _join_notices(
        notices,
        limit=_MAX_ACCOUNTS_PER_MESSAGE,
        hidden_label="另有 {hidden} 个关注的账号也掉了名次。",
    )


def _join_notices(notices: tuple[str, ...], *, limit: int, hidden_label: str) -> str:
    shown = notices[:limit]
    text = "\n\n".join(shown)
    hidden = len(notices) - len(shown)
    if hidden > 0:
        text += "\n\n" + hidden_label.format(hidden=hidden)
    return text


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
