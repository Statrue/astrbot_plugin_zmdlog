"""New records, first places changing hands, and how busy each board is."""

from dataclasses import dataclass

from ..events import CHAMPION_CHANGE, NEW_RECORD, BoardActivity, RecordEvent
from .common import PageHeader, _format_datetime, format_duration, format_number
from .standings import _as_of_label

MAX_CHANGES = 20
MAX_RECORDS = 40


@dataclass(frozen=True, slots=True)
class ChampionChangeView:
    boss_name: str
    dungeon_name: str
    seen_label: str
    account_display_name: str
    character_name: str
    duration: str
    dps: str
    previous_account_display_name: str
    previous_character_name: str
    previous_duration: str
    battle_id: str


@dataclass(frozen=True, slots=True)
class NewRecordView:
    boss_name: str
    dungeon_name: str
    rank: int
    account_display_name: str
    character_name: str
    roster_label: str
    duration: str
    dps: str
    fought_label: str
    seen_label: str
    battle_id: str


@dataclass(frozen=True, slots=True)
class ActivityView:
    boss_name: str
    dungeon_name: str
    count: int
    total: int
    bar_width: float


@dataclass(frozen=True, slots=True)
class RecordsPage:
    header: PageHeader
    window_label: str
    as_of_label: str
    # When the log started, so an empty stream reads as "nothing yet", not
    # "nothing happened".
    log_since_label: str
    changes: tuple[ChampionChangeView, ...]
    change_count: int
    records: tuple[NewRecordView, ...]
    record_count: int
    activity: tuple[ActivityView, ...]
    active_total: int


def build_records_page(
    events: tuple[RecordEvent, ...],
    activity: tuple[BoardActivity, ...],
    *,
    query: str,
    window_label: str,
    age_seconds: float | None = None,
    log_since: str | None = None,
) -> RecordsPage:
    """Champion changes first, then new records, then per-board activity.

    ``events`` are already narrowed to the window and newest first.
    """

    changes = [event for event in events if event.kind == CHAMPION_CHANGE]
    records = [event for event in events if event.kind == NEW_RECORD]
    peak = max((item.count for item in activity), default=0)
    return RecordsPage(
        header=PageHeader(
            title="新纪录",
            subtitle=f"{window_label}各榜新出现的公开记录与第一名易主",
            query=query,
            matched_name=f"{window_label} · 新纪录与榜单活跃度",
            target_type="新纪录",
            footer_note=(
                "公开榜单 · 新纪录由索引每次重读发现 · 活跃度按记录的战斗时间数"
            ),
        ),
        window_label=window_label,
        as_of_label=_as_of_label(age_seconds),
        log_since_label=_format_datetime(log_since) if log_since else "",
        changes=tuple(
            ChampionChangeView(
                boss_name=event.boss_name,
                dungeon_name=event.dungeon_name,
                seen_label=_format_datetime(event.seen_at),
                account_display_name=event.account_display_name,
                character_name=event.character_name,
                duration=format_duration(event.duration_ms),
                dps=format_number(event.dps),
                previous_account_display_name=event.previous_account_display_name,
                previous_character_name=event.previous_character_name,
                previous_duration=format_duration(event.previous_duration_ms),
                battle_id=event.battle_id,
            )
            for event in changes[:MAX_CHANGES]
        ),
        change_count=len(changes),
        records=tuple(
            NewRecordView(
                boss_name=event.boss_name,
                dungeon_name=event.dungeon_name,
                rank=event.rank,
                account_display_name=event.account_display_name,
                character_name=event.character_name,
                roster_label="、".join(event.roster),
                duration=format_duration(event.duration_ms),
                dps=format_number(event.dps),
                fought_label=_format_datetime(event.battle_end_at),
                seen_label=_format_datetime(event.seen_at),
                battle_id=event.battle_id,
            )
            for event in records[:MAX_RECORDS]
        ),
        record_count=len(records),
        activity=tuple(
            ActivityView(
                boss_name=item.boss_name,
                dungeon_name=item.dungeon_name,
                count=item.count,
                total=item.total,
                bar_width=round(item.count / peak * 100, 2) if peak else 0.0,
            )
            for item in activity
            if item.count
        ),
        active_total=sum(item.count for item in activity),
    )
