"""The board watch's two artefacts: a chat's batch, and the snapshot file.

A :class:`NoticeBatch` is what one chat is sent at the end of an interval:
every :class:`~core.board_changes.NoticeEntry` collected for it, drawn as
one 顶屁股通告 picture (``presentation.build_notice_page``). Nothing here
talks to the network or knows a platform.

``board-snapshot.json`` keeps, per watched board, the top N the watch last
delivered everything up to (:class:`~core.board_changes.BoardSnapshot`), so
a restart can compare its first read of the board with it. The file before
1.3.0 kept the top three of ``hot-bosses`` runs instead (version 1); it is
read as a partial snapshot of three, which ``core/board_changes`` knows how
to compare. A missing file and a corrupt one stay apart, as everywhere
(``core/persistence``): a corrupt one is set aside and the boards start
over silently.
"""

from dataclasses import dataclass
from typing import Any

from .board_changes import BoardSnapshot, NoticeEntry

# The default of ``rank_watch_rank_threshold``: how far down a board a new
# record is news.
DEFAULT_RANK_THRESHOLD = 10
BOARD_SNAPSHOT_VERSION = 2


@dataclass(frozen=True, slots=True)
class NoticeBatch:
    """One chat's entries for one picture, and the interval they cover.

    ``window_start`` and ``window_end`` are ISO stamps; ``top_n`` the N the
    entries were found against.
    """

    entries: tuple[NoticeEntry, ...]
    window_start: str
    window_end: str
    top_n: int


def parse_board_snapshot_payload(payload: Any) -> dict[str, BoardSnapshot]:
    """Read the stored snapshots, dropping anything malformed instead of raising.

    A version 1 entry is its ``runs``' battle ids: the board's top three as
    ``hot-bosses`` listed them, never the whole board.
    """

    if not isinstance(payload, dict):
        return {}
    boards = payload.get("boards")
    if not isinstance(boards, dict):
        return {}
    snapshots: dict[str, BoardSnapshot] = {}
    for boss_slug, entry in boards.items():
        if not isinstance(boss_slug, str) or not isinstance(entry, dict):
            continue
        if "battleIds" in entry:
            ids = entry.get("battleIds")
            whole = entry.get("whole") is True
        else:
            runs = entry.get("runs")
            ids = (
                [run.get("battleId") for run in runs if isinstance(run, dict)]
                if isinstance(runs, list)
                else None
            )
            whole = False
        if not isinstance(ids, list):
            continue
        checked_at = entry.get("checkedAt")
        snapshots[boss_slug] = BoardSnapshot(
            battle_ids=tuple(
                battle_id
                for battle_id in ids
                if isinstance(battle_id, str) and battle_id
            ),
            # A stamp that does not read is a snapshot too old to compare.
            checked_at=checked_at if isinstance(checked_at, str) else "",
            whole=whole,
        )
    return snapshots


def board_snapshot_payload(snapshots: dict[str, BoardSnapshot]) -> dict[str, Any]:
    return {
        "version": BOARD_SNAPSHOT_VERSION,
        "boards": {
            boss_slug: {
                "checkedAt": snapshot.checked_at,
                "battleIds": list(snapshot.battle_ids),
                "whole": snapshot.whole,
            }
            for boss_slug, snapshot in snapshots.items()
        },
    }
