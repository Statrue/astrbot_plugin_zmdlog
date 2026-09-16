"""What changed on the boards: new records, and first places changing hands.

The ranking index re-reads every board a few times an hour. The difference
between the copy it held and the copy it fetched is exactly what upstream
never tells anyone: which records are new, and which first place was just
taken from whom. This module turns that difference into events, keeps them
bounded, reads and writes the one JSON file they live in, and counts board
activity straight from the records' own battle times, which needs no log.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import BossRanking, BossRankingRow
from .persistence import JsonStore
from .timestamps import parse_timestamp, utc_now_text

EVENTS_VERSION = 1
# A bound on the file, not on the story: a thousand events is weeks of
# uploads across every board, and older than a month nobody asks about.
MAX_EVENTS = 1000
MAX_EVENT_AGE_DAYS = 30
NEW_RECORD = "new"
CHAMPION_CHANGE = "champion"


@dataclass(frozen=True, slots=True)
class RecordEvent:
    """One record the index saw appear, or one first place it saw change."""

    seen_at: str
    kind: str
    boss_slug: str
    boss_name: str
    dungeon_name: str
    battle_id: str
    rank: int
    character_name: str
    account_id: str
    account_display_name: str
    duration_ms: int
    dps: float
    battle_end_at: str
    roster: tuple[str, ...]
    # Champion changes only: the record that held first place before.
    previous_battle_id: str = ""
    previous_account_display_name: str = ""
    previous_character_name: str = ""
    previous_duration_ms: int = 0
    # Which of the board's two rankings the event was seen on; a record
    # entering both boards is one event on each.
    metric: str = "dps"


@dataclass(frozen=True, slots=True)
class BoardActivity:
    """How many of a board's records were fought inside a window."""

    boss_slug: str
    boss_name: str
    dungeon_name: str
    count: int
    total: int


def diff_rankings(
    previous: BossRanking,
    current: BossRanking,
    *,
    seen_at: str,
) -> tuple[RecordEvent, ...]:
    """The events between two reads of one board, in rank order.

    A record is new when its battle id was not in the previous copy. A new
    record at rank one also displaces the previous first place, which is
    the one change a board read *can* prove — unlike an account's rank
    drop, where the row at its old rank is usually a bystander.
    """

    known = {row.battle_id for row in previous.rows}
    events: list[RecordEvent] = []
    for row in current.rows:
        if row.battle_id in known:
            continue
        events.append(_event(NEW_RECORD, current, row, seen_at))
        if row.rank == 1 and previous.rows:
            before = previous.rows[0]
            if before.battle_id != row.battle_id:
                events.append(
                    _event(
                        CHAMPION_CHANGE,
                        current,
                        row,
                        seen_at,
                        previous_battle_id=before.battle_id,
                        previous_account_display_name=before.account_display_name,
                        previous_character_name=before.character_name,
                        previous_duration_ms=before.duration_ms,
                    )
                )
    return tuple(events)


def _event(
    kind: str,
    ranking: BossRanking,
    row: BossRankingRow,
    seen_at: str,
    **previous: Any,
) -> RecordEvent:
    return RecordEvent(
        seen_at=seen_at,
        kind=kind,
        boss_slug=ranking.boss_slug,
        boss_name=ranking.boss_name,
        dungeon_name=ranking.dungeon_name,
        battle_id=row.battle_id,
        rank=row.rank,
        character_name=row.character_name,
        account_id=row.account_id,
        account_display_name=row.account_display_name,
        duration_ms=row.duration_ms,
        dps=row.dps,
        battle_end_at=row.battle_end_at,
        roster=tuple(entry.character_name for entry in row.roster_entries)
        or tuple(row.roster_summary),
        metric=ranking.metric,
        **previous,
    )


def prune_events(
    events: Iterable[RecordEvent],
    *,
    now: datetime,
) -> tuple[RecordEvent, ...]:
    """Keep the newest ``MAX_EVENTS`` seen within ``MAX_EVENT_AGE_DAYS``."""

    cutoff = now - timedelta(days=MAX_EVENT_AGE_DAYS)
    kept = [
        event
        for event in events
        if (seen := parse_timestamp(event.seen_at)) is not None and seen >= cutoff
    ]
    kept.sort(key=lambda event: event.seen_at)
    return tuple(kept[-MAX_EVENTS:])


def events_payload(events: Iterable[RecordEvent]) -> dict[str, Any]:
    return {
        "version": EVENTS_VERSION,
        "events": [
            {name: _plain(getattr(event, name)) for name in _FIELD_NAMES}
            for event in events
        ],
    }


def parse_events_payload(payload: Any) -> tuple[RecordEvent, ...]:
    """Read the stored events, dropping anything malformed instead of raising."""

    if not isinstance(payload, dict):
        return ()
    items = payload.get("events")
    if not isinstance(items, list):
        return ()
    events: list[RecordEvent] = []
    for item in items:
        event = _parse_event(item)
        if event is not None:
            events.append(event)
    return tuple(events)


_FIELD_NAMES = tuple(field.name for field in fields(RecordEvent))


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _parse_event(item: Any) -> RecordEvent | None:
    if not isinstance(item, dict):
        return None
    try:
        return RecordEvent(
            seen_at=str(item["seen_at"]),
            kind=str(item["kind"]),
            boss_slug=str(item["boss_slug"]),
            boss_name=str(item["boss_name"]),
            dungeon_name=str(item.get("dungeon_name", "")),
            battle_id=str(item["battle_id"]),
            rank=int(item["rank"]),
            character_name=str(item.get("character_name", "")),
            account_id=str(item.get("account_id", "")),
            account_display_name=str(item.get("account_display_name", "")),
            duration_ms=int(item.get("duration_ms", 0)),
            dps=float(item.get("dps", 0.0)),
            battle_end_at=str(item.get("battle_end_at", "")),
            roster=tuple(str(name) for name in item.get("roster") or ()),
            previous_battle_id=str(item.get("previous_battle_id", "")),
            previous_account_display_name=str(
                item.get("previous_account_display_name", "")
            ),
            previous_character_name=str(item.get("previous_character_name", "")),
            previous_duration_ms=int(item.get("previous_duration_ms", 0)),
            metric=str(item.get("metric", "dps")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def board_activity(
    rankings: Iterable[BossRanking],
    *,
    since: datetime | None,
) -> tuple[BoardActivity, ...]:
    """Records fought since ``since`` per board, busiest first; all when None."""

    rows: list[BoardActivity] = []
    for ranking in rankings:
        count = 0
        for row in ranking.rows:
            fought = parse_timestamp(row.battle_end_at)
            if since is None or (fought is not None and fought >= since):
                count += 1
        rows.append(
            BoardActivity(
                boss_slug=ranking.boss_slug,
                boss_name=ranking.boss_name,
                dungeon_name=ranking.dungeon_name,
                count=count,
                total=len(ranking.rows),
            )
        )
    rows.sort(key=lambda item: (-item.count, -item.total, item.boss_name))
    return tuple(rows)


class EventLog:
    """The events the index produced, in memory and in one JSON file."""

    def __init__(
        self,
        store: JsonStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        stamp: Callable[[], str] = utc_now_text,
    ) -> None:
        self._store = store
        self._clock = clock
        self._stamp = stamp
        self.events: tuple[RecordEvent, ...] = prune_events(
            parse_events_payload(store.load()), now=clock()
        )

    def record(self, previous: BossRanking, current: BossRanking) -> None:
        """Turn one board re-read into events; save only when there are any."""

        fresh = diff_rankings(previous, current, seen_at=self._stamp())
        # Upstream drops a record below 60% of the board's median damage, so
        # a borderline one leaves and re-enters as the median moves; it is
        # new once — per ranking, since the rDPS board lists it separately.
        announced = {
            (event.metric, event.battle_id)
            for event in self.events
            if event.kind == NEW_RECORD
        }
        fresh = tuple(
            event
            for event in fresh
            if not (
                event.kind == NEW_RECORD
                and (event.metric, event.battle_id) in announced
            )
        )
        if not fresh:
            return
        self.events = prune_events((*self.events, *fresh), now=self._clock())
        self._store.save(events_payload(self.events))

    def recent(
        self,
        *,
        kind: str | None = None,
        since: datetime | None = None,
        metric: str | None = None,
    ) -> tuple[RecordEvent, ...]:
        """Events of ``kind`` on ``metric``'s boards since ``since``, newest first."""

        picked = []
        for event in reversed(self.events):
            if kind is not None and event.kind != kind:
                continue
            if metric is not None and event.metric != metric:
                continue
            if since is not None:
                seen = parse_timestamp(event.seen_at)
                if seen is None or seen < since:
                    continue
            picked.append(event)
        return tuple(picked)

    def oldest_seen_at(self) -> str | None:
        return self.events[0].seen_at if self.events else None
