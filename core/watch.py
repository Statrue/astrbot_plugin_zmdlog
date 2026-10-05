"""Detect and describe new top records on the watched boards.

One ``hot-bosses`` read carries the top three of every board, so the whole
board watch costs one request per cycle, whether a chat watches one board
or 全部榜单. Nothing here talks to the network.

A notice is a :class:`Notice`: the text every platform is sent, and the
battles that text prints a link to, each under the label a button to it
wears (``战报 1``, ``战报 2`` …, numbered across the message a chat
receives). Where a message can carry buttons (``core/buttons``), the label
stands in for the printed link.
"""

from dataclasses import dataclass
from typing import Any

from .models import HotBossCard
from .presentation import format_duration, public_url
from .timestamps import parse_timestamp
from .watchlist import board_label

# The default of ``rank_watch_rank_threshold``: how far down a board a
# change is news.
DEFAULT_RANK_THRESHOLD = 10
BOARD_SNAPSHOT_VERSION = 1
_MAX_BOARDS_PER_MESSAGE = 3
_LINK_LABEL = "战报 {number}"


@dataclass(frozen=True, slots=True)
class NoticeLink:
    """A battle a notice prints, as its own line, and what a button to it says."""

    url: str
    label: str


@dataclass(frozen=True, slots=True)
class Notice:
    """One notice: its text, word for word, and the battles it links."""

    text: str
    links: tuple[NoticeLink, ...] = ()


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


def board_snapshot_is_usable(
    snapshot: BoardSnapshot | None,
    *,
    now: str,
    max_age_seconds: float,
) -> bool:
    """Whether ``snapshot`` is recent enough to be compared against.

    A baseline from before a long outage or a long shutdown would make every
    record set in the meantime look like breaking news, so an old one is
    discarded and the board is re-seeded silently instead. An empty top list
    is a real state (a board with no public record yet), so only the
    timestamp decides.
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


def format_board_notice(change: BoardChange, *, web_base_url: str) -> Notice:
    """One message for one board: every new top run, then who fell out."""

    lines = [
        f"🏁 「{board_label(change.dungeon_name, change.boss_name)}」"
        f"前三名有新纪录"
    ]
    urls: list[str] = []
    for entry in change.new_runs:
        lines.append("")
        lines.append(
            f"第 {entry.rank} 名 · {entry.run.uploader_nickname} · "
            f"主C {entry.run.character_name} · "
            f"用时 {format_duration(entry.run.duration_ms)}"
        )
        url = public_url(web_base_url, "battle", entry.run.battle_id)
        lines.append(url)
        urls.append(url)
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
    return Notice("\n".join(lines), _numbered(urls))


def join_board_notices(notices: tuple[Notice, ...]) -> Notice:
    """Merge one cycle worth of notices so a chat receives a single message.

    A chat following 全部榜单 would otherwise get a burst of pushes in the
    same instant, which is also what gets a bot rate-limited. A notice left
    out for space takes its links with it: a button may only open a battle
    the text shows.
    """

    shown = notices[:_MAX_BOARDS_PER_MESSAGE]
    text = "\n\n".join(notice.text for notice in shown)
    hidden = len(notices) - len(shown)
    if hidden > 0:
        text += f"\n\n另有 {hidden} 个关注的榜单也有新纪录。"
    urls = [link.url for notice in shown for link in notice.links]
    return Notice(text, _numbered(urls))


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


def _numbered(urls: list[str]) -> tuple[NoticeLink, ...]:
    """Each distinct link once, labelled by its first place in the text."""

    return tuple(
        NoticeLink(url, _LINK_LABEL.format(number=number))
        for number, url in enumerate(dict.fromkeys(urls), start=1)
    )
