"""Typed adapters for the public ZMDLogs ranking responses."""

from dataclasses import dataclass
from typing import Any, Literal


class ModelValidationError(ValueError):
    """Raised when an upstream response does not match the public contract."""


@dataclass(frozen=True, slots=True)
class ContractTag:
    tag_id: int
    score: int
    name: str | None = None
    description: str | None = None
    icon_url: str | None = None


@dataclass(frozen=True, slots=True)
class HotBossRun:
    battle_id: str
    duration_ms: int
    uploader_nickname: str
    character_name: str
    character_key: str | None = None
    character_profession: str | None = None
    character_avatar_url: str | None = None
    score_percent: int | None = None
    contract_tag_score: int | None = None
    contract_tags: tuple[ContractTag, ...] = ()


@dataclass(frozen=True, slots=True)
class HotBossCard:
    boss_slug: str
    boss_key: str
    boss_name: str
    dungeon_name: str
    top_speed_runs: tuple[HotBossRun, ...]


@dataclass(frozen=True, slots=True)
class BossRankingRosterEntry:
    character_name: str
    profession: str
    character_key: str | None = None
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class BossProfessionUsageEntry:
    character_name: str
    usage_percent: float
    character_key: str | None = None
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class BossProfessionGroup:
    profession: str
    entries: tuple[BossProfessionUsageEntry, ...]


@dataclass(frozen=True, slots=True)
class BossRankingRow:
    rank: int
    score_percent: int
    battle_id: str
    battle_end_at: str
    character_name: str
    character_profession: str
    account_id: str
    account_display_name: str
    dps: float
    duration_ms: int
    roster_summary: tuple[str, ...]
    roster_entries: tuple[BossRankingRosterEntry, ...]
    character_key: str | None = None
    character_avatar_url: str | None = None
    contract_tag_score: int | None = None
    contract_tags: tuple[ContractTag, ...] = ()


@dataclass(frozen=True, slots=True)
class BossRanking:
    boss_slug: str
    boss_name: str
    dungeon_name: str
    profession_groups: tuple[BossProfessionGroup, ...]
    rows: tuple[BossRankingRow, ...]
    metric: Literal["dps"] = "dps"


def parse_hot_bosses(payload: Any) -> tuple[HotBossCard, ...]:
    """Adapt the ``GET /api/home/hot-bosses`` response."""

    items = _list(payload, "hot-bosses")
    return tuple(_parse_hot_boss_card(item, index) for index, item in enumerate(items))


def parse_boss_ranking(payload: Any) -> BossRanking:
    """Adapt a ranking response and enforce the ZmdLogBot DPS-only contract."""

    item = _mapping(payload, "ranking")
    metric = _string(item.get("metric"), "ranking.metric")
    if metric != "dps":
        raise ModelValidationError("ranking.metric must be 'dps'")

    groups = _list(item.get("professionGroups"), "ranking.professionGroups")
    rows = _list(item.get("rows"), "ranking.rows")
    return BossRanking(
        boss_slug=_string(item.get("bossSlug"), "ranking.bossSlug"),
        boss_name=_string(item.get("bossName"), "ranking.bossName"),
        dungeon_name=_string(item.get("dungeonName"), "ranking.dungeonName"),
        profession_groups=tuple(
            _parse_profession_group(group, index)
            for index, group in enumerate(groups)
        ),
        rows=tuple(_parse_ranking_row(row, index) for index, row in enumerate(rows)),
    )


def _parse_contract_tags(value: Any, path: str) -> tuple[ContractTag, ...]:
    return tuple(
        _parse_contract_tag(item, f"{path}[{index}]")
        for index, item in enumerate(_list(value, path))
    )


def _parse_contract_tag(value: Any, path: str) -> ContractTag:
    item = _mapping(value, path)
    return ContractTag(
        tag_id=_integer(item.get("tagId"), f"{path}.tagId"),
        score=_integer(item.get("score"), f"{path}.score"),
        name=_optional_string(item.get("name"), f"{path}.name"),
        description=_optional_string(
            item.get("description"),
            f"{path}.description",
        ),
        icon_url=_optional_string(item.get("iconUrl"), f"{path}.iconUrl"),
    )


def _parse_hot_boss_card(value: Any, index: int) -> HotBossCard:
    path = f"hot-bosses[{index}]"
    item = _mapping(value, path)
    runs = _list(item.get("topSpeedRuns"), f"{path}.topSpeedRuns")
    return HotBossCard(
        boss_slug=_string(item.get("bossSlug"), f"{path}.bossSlug"),
        boss_key=_string(item.get("bossKey"), f"{path}.bossKey"),
        boss_name=_string(item.get("bossName"), f"{path}.bossName"),
        dungeon_name=_string(item.get("dungeonName"), f"{path}.dungeonName"),
        top_speed_runs=tuple(
            _parse_hot_boss_run(run, f"{path}.topSpeedRuns[{run_index}]")
            for run_index, run in enumerate(runs)
        ),
    )


def _parse_hot_boss_run(value: Any, path: str) -> HotBossRun:
    item = _mapping(value, path)
    return HotBossRun(
        battle_id=_string(item.get("battleId"), f"{path}.battleId"),
        duration_ms=_integer(item.get("durationMs"), f"{path}.durationMs"),
        uploader_nickname=_string(
            item.get("uploaderNickname"),
            f"{path}.uploaderNickname",
        ),
        character_name=_string(
            item.get("characterName"),
            f"{path}.characterName",
        ),
        character_key=_optional_string(
            item.get("characterKey"),
            f"{path}.characterKey",
        ),
        character_profession=_optional_string(
            item.get("characterProfession"),
            f"{path}.characterProfession",
        ),
        character_avatar_url=_optional_string(
            item.get("characterAvatarUrl"),
            f"{path}.characterAvatarUrl",
        ),
        score_percent=_optional_integer(
            item.get("scorePercent"),
            f"{path}.scorePercent",
        ),
        contract_tag_score=_optional_integer(
            item.get("contractTagScore"),
            f"{path}.contractTagScore",
        ),
        contract_tags=_parse_contract_tags(
            item.get("contractTags", []),
            f"{path}.contractTags",
        ),
    )


def _parse_profession_group(value: Any, index: int) -> BossProfessionGroup:
    path = f"ranking.professionGroups[{index}]"
    item = _mapping(value, path)
    entries = _list(item.get("entries"), f"{path}.entries")
    return BossProfessionGroup(
        profession=_string(item.get("profession"), f"{path}.profession"),
        entries=tuple(
            _parse_profession_entry(entry, f"{path}.entries[{entry_index}]")
            for entry_index, entry in enumerate(entries)
        ),
    )


def _parse_profession_entry(value: Any, path: str) -> BossProfessionUsageEntry:
    item = _mapping(value, path)
    return BossProfessionUsageEntry(
        character_name=_string(
            item.get("characterName"),
            f"{path}.characterName",
        ),
        usage_percent=_number(item.get("usagePercent"), f"{path}.usagePercent"),
        character_key=_optional_string(
            item.get("characterKey"),
            f"{path}.characterKey",
        ),
        avatar_url=_optional_string(item.get("avatarUrl"), f"{path}.avatarUrl"),
    )


def _parse_ranking_row(value: Any, index: int) -> BossRankingRow:
    path = f"ranking.rows[{index}]"
    item = _mapping(value, path)
    roster_summary = _list(item.get("rosterSummary"), f"{path}.rosterSummary")
    roster_entries = _list(item.get("rosterEntries"), f"{path}.rosterEntries")
    return BossRankingRow(
        rank=_integer(item.get("rank"), f"{path}.rank"),
        score_percent=_integer(
            item.get("scorePercent"),
            f"{path}.scorePercent",
        ),
        battle_id=_string(item.get("battleId"), f"{path}.battleId"),
        battle_end_at=_string(item.get("battleEndAt"), f"{path}.battleEndAt"),
        character_name=_string(
            item.get("characterName"),
            f"{path}.characterName",
        ),
        character_profession=_string(
            item.get("characterProfession"),
            f"{path}.characterProfession",
        ),
        account_id=_string(item.get("accountId"), f"{path}.accountId"),
        account_display_name=_string(
            item.get("accountDisplayName"),
            f"{path}.accountDisplayName",
        ),
        dps=_number(item.get("dps"), f"{path}.dps"),
        duration_ms=_integer(item.get("durationMs"), f"{path}.durationMs"),
        roster_summary=tuple(
            _string(entry, f"{path}.rosterSummary[{entry_index}]")
            for entry_index, entry in enumerate(roster_summary)
        ),
        roster_entries=tuple(
            _parse_roster_entry(entry, f"{path}.rosterEntries[{entry_index}]")
            for entry_index, entry in enumerate(roster_entries)
        ),
        character_key=_optional_string(
            item.get("characterKey"),
            f"{path}.characterKey",
        ),
        character_avatar_url=_optional_string(
            item.get("characterAvatarUrl"),
            f"{path}.characterAvatarUrl",
        ),
        contract_tag_score=_optional_integer(
            item.get("contractTagScore"),
            f"{path}.contractTagScore",
        ),
        contract_tags=_parse_contract_tags(
            item.get("contractTags", []),
            f"{path}.contractTags",
        ),
    )


def _parse_roster_entry(value: Any, path: str) -> BossRankingRosterEntry:
    item = _mapping(value, path)
    return BossRankingRosterEntry(
        character_name=_string(
            item.get("characterName"),
            f"{path}.characterName",
        ),
        profession=_string(item.get("profession"), f"{path}.profession"),
        character_key=_optional_string(
            item.get("characterKey"),
            f"{path}.characterKey",
        ),
        avatar_url=_optional_string(item.get("avatarUrl"), f"{path}.avatarUrl"),
    )


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelValidationError(f"{path} must be an object")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ModelValidationError(f"{path} must be an array")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ModelValidationError(f"{path} must be a string")
    return value


def _optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, path)


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelValidationError(f"{path} must be an integer")
    return value


def _optional_integer(value: Any, path: str) -> int | None:
    if value is None:
        return None
    return _integer(value, path)


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModelValidationError(f"{path} must be a number")
    return float(value)
