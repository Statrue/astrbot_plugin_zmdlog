"""Typed adapters for the public ZMDLogs ranking responses."""

from collections.abc import Mapping
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


@dataclass(frozen=True, slots=True)
class PublicUserRanking:
    boss_slug: str
    boss_name: str
    dungeon_name: str
    battle_id: str
    rank: int
    score_percent: int
    duration_ms: int
    total_dps: float
    battle_end_at: str
    roster_summary: tuple[str, ...]
    contract_tag_score: int | None = None
    contract_tags: tuple[ContractTag, ...] = ()


@dataclass(frozen=True, slots=True)
class PublicUserRankings:
    account_id: str
    account_display_name: str
    rankings: tuple[PublicUserRanking, ...]


@dataclass(frozen=True, slots=True)
class AccountSearchHit:
    account_id: str
    account_display_name: str


@dataclass(frozen=True, slots=True)
class AccountSearch:
    query: str
    has_more: bool
    accounts: tuple[AccountSearchHit, ...]


@dataclass(frozen=True, slots=True)
class BattleParticipant:
    character_name: str
    account_display_name: str
    total_damage: int
    dps: float
    rdps: float
    character_key: str | None = None
    character_profession: str | None = None
    character_avatar_url: str | None = None
    max_hit: int | None = None
    crit_rate: float | None = None


@dataclass(frozen=True, slots=True)
class BattleWeaponSkill:
    """One weapon affix and its level (``sk_wpn_*`` is the weapon's own skill)."""

    skill_key: str
    level: int | None = None
    potential_level: int | None = None


@dataclass(frozen=True, slots=True)
class BattleWeapon:
    name: str
    template: str | None = None
    level: int | None = None
    refine: int | None = None
    icon_url: str | None = None
    skills: tuple[BattleWeaponSkill, ...] = ()


@dataclass(frozen=True, slots=True)
class BattleEquipStat:
    """One gear stat line; upstream leaves these untyped, so fields are lenient."""

    name: str
    value: float
    slot: str | None = None
    level: int | None = None


@dataclass(frozen=True, slots=True)
class BattleEquip:
    slot: int
    # Falls back to the raw item id upstream when the piece name is unknown.
    piece_name: str
    item_id: str | None = None
    suit_name: str | None = None
    part_name: str | None = None
    icon_url: str | None = None
    # (affix index, enhancement level) pairs in index order; untyped upstream.
    enhance_levels: tuple[tuple[int, int], ...] = ()
    stats: tuple[BattleEquipStat, ...] = ()


@dataclass(frozen=True, slots=True)
class BattleRosterSkill:
    skill_key: str
    level: int


@dataclass(frozen=True, slots=True)
class BattleRosterEntry:
    """One deployed character with the loadout recorded at upload time."""

    slot: int
    character_name: str
    account_display_name: str
    character_key: str | None = None
    character_profession: str | None = None
    character_avatar_url: str | None = None
    character_element: str | None = None
    character_level: int | None = None
    character_potential: int | None = None
    weapon: BattleWeapon | None = None
    equips: tuple[BattleEquip, ...] = ()
    skills: tuple[BattleRosterSkill, ...] = ()


@dataclass(frozen=True, slots=True)
class BattleSkillStat:
    """Damage of one skill (or damage source) of one character in a battle."""

    character_name: str
    skill_name: str
    cast_count: int
    total_damage: int
    avg_damage: float
    max_damage: int
    skill_key: str | None = None


@dataclass(frozen=True, slots=True)
class BattleDamagePoint:
    """One damage tick: when it landed, who dealt it, how much."""

    at_ms: int
    character_name: str
    value: int


@dataclass(frozen=True, slots=True)
class BattleBuffEffect:
    """One effect of a buff (``zone`` is upstream's slot, ``rate`` a fraction)."""

    zone: str | None = None
    element: str | None = None
    rate: float | None = None


@dataclass(frozen=True, slots=True)
class BattleBuff:
    """One buff a character received, or one debuff they put on an enemy."""

    name: str
    target_name: str
    start_ms: int
    event_key: str | None = None
    source_name: str | None = None
    duration_ms: int | None = None
    effects: tuple[BattleBuffEffect, ...] = ()
    # True for ``debuffsApplied``: the target is the boss, not a teammate.
    on_enemy: bool = False


@dataclass(frozen=True, slots=True)
class BattleDetailSummary:
    battle_id: str
    uploader_user_id: str
    uploader_display_name: str
    dungeon_name: str
    boss_name: str
    battle_end_at: str
    duration_ms: int
    total_damage: int
    total_dps: float
    participants: tuple[BattleParticipant, ...]
    parser_version: str
    rules_version: str
    time_source: str | None
    official_timer_start_seen: bool | None
    official_timer_end_seen: bool | None
    integrity_verified: bool
    contract_tag_score: int | None = None
    contract_tags: tuple[ContractTag, ...] = ()
    roster: tuple[BattleRosterEntry, ...] = ()
    skill_stats: tuple[BattleSkillStat, ...] = ()
    # Optional telemetry read from ``timelineEvents`` / ``characterStates``.
    # Both are parsed leniently: they only add sections to a card that must
    # keep rendering when upstream changes their shape.
    damage_points: tuple[BattleDamagePoint, ...] = ()
    buffs: tuple[BattleBuff, ...] = ()


@dataclass(frozen=True, slots=True)
class BattleCast:
    """One cast from the public export: who cast what, from when to when."""

    character_key: str
    skill_key: str
    skill_name: str
    start_ms: int
    # Missing when the parser never saw the cast end.
    end_ms: int | None = None
    # ``Summon`` marks a summoned entity's cast (sheep, swords, ...).
    source: str | None = None
    recovers_energy: bool = False


@dataclass(frozen=True, slots=True)
class ExportRosterEntry:
    slot: int
    character_name: str
    character_key: str | None = None
    character_level: int | None = None
    character_potential: int | None = None


@dataclass(frozen=True, slots=True)
class BattleExport:
    """``GET /api/v1/battles/{id}/export``: the cast sequence of one battle."""

    battle_id: str
    boss_name: str
    dungeon_name: str
    duration_ms: int
    roster: tuple[ExportRosterEntry, ...]
    casts: tuple[BattleCast, ...]
    boss_slug: str | None = None
    battle_end_at: str | None = None
    parser_version: str | None = None


@dataclass(frozen=True, slots=True)
class EquipSuit:
    """One gear suit as the game data catalog names it."""

    suit_id: str
    name: str


@dataclass(frozen=True, slots=True)
class CharacterType:
    """One character's element and weapon type, from the game data catalog."""

    name: str
    element: str
    weapon_type: str
    # ``professionName`` as the catalog spells it (术师 for the caster); "" when
    # the entry has none. ``professions.normalize_profession`` reads it.
    profession: str = ""


@dataclass(frozen=True, slots=True)
class CharacterStatisticsRow:
    character_key: str
    character_name: str
    character_profession: str
    sample_count: int
    normal_sample_count: int
    outlier_count: int
    insufficient_samples: bool
    rank: int | None = None
    character_avatar_url: str | None = None
    lower_whisker: float | None = None
    p10: float | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    p90: float | None = None
    upper_whisker: float | None = None
    maximum: float | None = None
    outliers: tuple[tuple[float, int], ...] = ()


@dataclass(frozen=True, slots=True)
class CharacterBossStatisticsRow:
    boss_slug: str
    boss_name: str
    dungeon_name: str
    ranked_character_count: int
    sample_count: int
    normal_sample_count: int
    outlier_count: int
    insufficient_samples: bool
    rank: int | None = None
    lower_whisker: float | None = None
    p10: float | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    p90: float | None = None
    upper_whisker: float | None = None
    maximum: float | None = None
    outliers: tuple[tuple[float, int], ...] = ()


@dataclass(frozen=True, slots=True)
class CharacterBossStatistics:
    character_key: str
    character_name: str
    character_profession: str
    range: Literal["7d", "14d", "30d", "all"]
    potential: Literal["0", "1-5", "all"]
    included_boss_count: int
    minimum_sample_count: int
    total_sample_count: int
    total_outlier_count: int
    rows: tuple[CharacterBossStatisticsRow, ...]
    character_avatar_url: str | None = None
    metric: Literal["dps"] = "dps"


@dataclass(frozen=True, slots=True)
class CharacterStatistics:
    scope: Literal["boss", "all"]
    boss_slug: str
    boss_name: str
    dungeon_name: str
    range: Literal["7d", "14d", "30d", "all"]
    potential: Literal["0", "1-5", "all"]
    included_boss_count: int
    minimum_sample_count: int
    eligible_battle_count: int
    total_sample_count: int
    total_outlier_count: int
    rows: tuple[CharacterStatisticsRow, ...]
    metric: Literal["dps"] = "dps"


def parse_equip_catalog(payload: Any) -> tuple[EquipSuit, ...]:
    """Read the suit names out of ``GET /api/game-data/equip``.

    Lenient by design: this is a display label for gear that already
    renders, so an entry the catalog cannot describe is skipped rather than
    failing the page. ``suit_none`` is the game's bucket for pieces that
    belong to no suit and carries its own id as its name, which is not a
    name a reader should see.
    """

    root = _mapping(payload, "equip-catalog")
    suits: list[EquipSuit] = []
    for value in root.get("entries") or ():
        if not isinstance(value, dict):
            continue
        suit_id = value.get("suitID") or value.get("id")
        name = value.get("name") or value.get("displayName")
        if not isinstance(suit_id, str) or not isinstance(name, str):
            continue
        suit_id, name = suit_id.strip(), name.strip()
        if not suit_id or not name or name == suit_id:
            continue
        suits.append(EquipSuit(suit_id=suit_id, name=name))
    return tuple(suits)


def parse_character_types(payload: Any) -> tuple[CharacterType, ...]:
    """Read element and weapon type out of ``GET /api/game-data/character``.

    Lenient like the suit catalog: an entry without a name or a type is
    skipped, never fatal — the rings and the filter simply do not know that
    character. ``charTypeName`` is the element (物理 / 灼热 / 寒冷 / 自然 /
    电磁); the catalog carries every rarity, and the two 管理员 entries agree.
    """

    root = _mapping(payload, "character-catalog")
    types: list[CharacterType] = []
    for value in root.get("entries") or ():
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        element = value.get("charTypeName")
        weapon = value.get("weaponTypeName")
        profession = value.get("professionName")
        if not isinstance(name, str) or not isinstance(element, str):
            continue
        name, element = name.strip(), element.strip()
        if not name or not element:
            continue
        types.append(
            CharacterType(
                name=name,
                element=element,
                weapon_type=weapon.strip() if isinstance(weapon, str) else "",
                profession=profession.strip() if isinstance(profession, str) else "",
            )
        )
    return tuple(types)


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


def parse_character_boss_statistics(payload: Any) -> CharacterBossStatistics:
    """Adapt ``GET /api/characters/{key}/boss-statistics`` (DPS only)."""

    path = "character-boss-statistics"
    item = _mapping(payload, path)
    rows = _list(item.get("rows"), f"{path}.rows")
    return CharacterBossStatistics(
        character_key=_string(item.get("characterKey"), f"{path}.characterKey"),
        character_name=_string(item.get("characterName"), f"{path}.characterName"),
        character_profession=_string(
            item.get("characterProfession"), f"{path}.characterProfession"
        ),
        character_avatar_url=_optional_string(
            item.get("characterAvatarUrl"), f"{path}.characterAvatarUrl"
        ),
        **_parse_statistics_envelope(item, path),
        rows=tuple(
            _parse_character_boss_row(row, f"{path}.rows[{index}]")
            for index, row in enumerate(rows)
        ),
    )


def _parse_statistics_envelope(item: Mapping[str, Any], path: str) -> dict[str, Any]:
    """The fields both statistics responses carry: metric, window, counts."""

    metric = _string(item.get("metric"), f"{path}.metric")
    if metric != "dps":
        raise ModelValidationError(f"{path}.metric must be 'dps'")
    time_range = _string(item.get("range"), f"{path}.range")
    if time_range not in ("7d", "14d", "30d", "all"):
        raise ModelValidationError(f"{path}.range is not a known range")
    potential = _string(item.get("potential"), f"{path}.potential")
    if potential not in ("0", "1-5", "all"):
        raise ModelValidationError(f"{path}.potential is not a known filter")
    return {
        "range": time_range,
        "potential": potential,
        "included_boss_count": _non_negative(
            item.get("includedBossCount"), f"{path}.includedBossCount"
        ),
        "minimum_sample_count": _non_negative(
            item.get("minimumSampleCount"), f"{path}.minimumSampleCount"
        ),
        "total_sample_count": _non_negative(
            item.get("totalSampleCount"), f"{path}.totalSampleCount"
        ),
        "total_outlier_count": _non_negative(
            item.get("totalOutlierCount"), f"{path}.totalOutlierCount"
        ),
    }


def _parse_statistics_row(item: Mapping[str, Any], path: str) -> dict[str, Any]:
    """The distribution fields both statistics rows carry."""

    insufficient = _boolean(
        item.get("insufficientSamples"), f"{path}.insufficientSamples"
    )
    rank = _optional_integer(item.get("rank"), f"{path}.rank")
    if rank is None and not insufficient:
        raise ModelValidationError(f"{path}.rank is required for ranked rows")
    if rank is not None and (insufficient or rank < 1):
        raise ModelValidationError(f"{path}.rank must be a positive ranked index")
    outliers_raw = _list(item.get("outliers", []), f"{path}.outliers")
    outliers = []
    for index, outlier in enumerate(outliers_raw):
        outlier_path = f"{path}.outliers[{index}]"
        outlier_item = _mapping(outlier, outlier_path)
        outliers.append(
            (
                _non_negative_number(
                    outlier_item.get("value"), f"{outlier_path}.value"
                ),
                _non_negative(outlier_item.get("count"), f"{outlier_path}.count"),
            )
        )
    return {
        "sample_count": _non_negative(item.get("sampleCount"), f"{path}.sampleCount"),
        "normal_sample_count": _non_negative(
            item.get("normalSampleCount"), f"{path}.normalSampleCount"
        ),
        "outlier_count": _non_negative(
            item.get("outlierCount"), f"{path}.outlierCount"
        ),
        "insufficient_samples": insufficient,
        "rank": rank,
        "lower_whisker": _optional_stat(
            item.get("lowerWhisker"), f"{path}.lowerWhisker"
        ),
        "p10": _optional_stat(item.get("p10"), f"{path}.p10"),
        "p25": _optional_stat(item.get("p25"), f"{path}.p25"),
        "median": _optional_stat(item.get("median"), f"{path}.median"),
        "p75": _optional_stat(item.get("p75"), f"{path}.p75"),
        "p90": _optional_stat(item.get("p90"), f"{path}.p90"),
        "upper_whisker": _optional_stat(
            item.get("upperWhisker"), f"{path}.upperWhisker"
        ),
        "maximum": _optional_stat(item.get("maximum"), f"{path}.maximum"),
        "outliers": tuple(outliers),
    }


def _parse_character_boss_row(value: Any, path: str) -> CharacterBossStatisticsRow:
    item = _mapping(value, path)
    return CharacterBossStatisticsRow(
        boss_slug=_string(item.get("bossSlug"), f"{path}.bossSlug"),
        boss_name=_string(item.get("bossName"), f"{path}.bossName"),
        dungeon_name=_string(item.get("dungeonName"), f"{path}.dungeonName"),
        ranked_character_count=_non_negative(
            item.get("rankedCharacterCount"), f"{path}.rankedCharacterCount"
        ),
        **_parse_statistics_row(item, path),
    )


def parse_character_statistics(payload: Any) -> CharacterStatistics:
    """Adapt ``GET /api/bosses[/{slug}]/character-statistics`` (DPS only)."""

    item = _mapping(payload, "character-statistics")
    path = "character-statistics"
    scope = _string(item.get("scope"), f"{path}.scope")
    if scope not in ("boss", "all"):
        raise ModelValidationError(f"{path}.scope must be 'boss' or 'all'")
    rows = _list(item.get("rows"), f"{path}.rows")
    return CharacterStatistics(
        scope=scope,
        boss_slug=_string(item.get("bossSlug"), f"{path}.bossSlug"),
        boss_name=_string(item.get("bossName"), f"{path}.bossName"),
        dungeon_name=_string(item.get("dungeonName"), f"{path}.dungeonName"),
        eligible_battle_count=_non_negative(
            item.get("eligibleBattleCount"), f"{path}.eligibleBattleCount"
        ),
        **_parse_statistics_envelope(item, path),
        rows=tuple(
            _parse_character_statistics_row(row, f"{path}.rows[{index}]")
            for index, row in enumerate(rows)
        ),
    )


def _parse_character_statistics_row(value: Any, path: str) -> CharacterStatisticsRow:
    item = _mapping(value, path)
    return CharacterStatisticsRow(
        character_key=_string(item.get("characterKey"), f"{path}.characterKey"),
        character_name=_string(item.get("characterName"), f"{path}.characterName"),
        character_profession=_string(
            item.get("characterProfession"), f"{path}.characterProfession"
        ),
        character_avatar_url=_optional_string(
            item.get("characterAvatarUrl"), f"{path}.characterAvatarUrl"
        ),
        **_parse_statistics_row(item, path),
    )


def _optional_stat(value: Any, path: str) -> float | None:
    if value is None:
        return None
    return _non_negative_number(value, path)


def _non_negative(value: Any, path: str) -> int:
    number = _integer(value, path)
    if number < 0:
        raise ModelValidationError(f"{path} must not be negative")
    return number


def _non_negative_number(value: Any, path: str) -> float:
    number = _number(value, path)
    if number < 0:
        raise ModelValidationError(f"{path} must not be negative")
    return number


def parse_account_search(payload: Any) -> AccountSearch:
    """Adapt ``GET /api/battles/users/search``."""

    item = _mapping(payload, "account-search")
    accounts = _list(item.get("accounts"), "account-search.accounts")
    hits = []
    for index, account in enumerate(accounts):
        path = f"account-search.accounts[{index}]"
        entry = _mapping(account, path)
        hits.append(
            AccountSearchHit(
                account_id=_string(entry.get("accountId"), f"{path}.accountId"),
                account_display_name=_string(
                    entry.get("accountDisplayName"),
                    f"{path}.accountDisplayName",
                ),
            )
        )
    return AccountSearch(
        query=_string(item.get("query"), "account-search.query"),
        has_more=_boolean(item.get("hasMore"), "account-search.hasMore"),
        accounts=tuple(hits),
    )


def parse_public_user_rankings(payload: Any) -> PublicUserRankings:
    """Adapt ``GET /api/battles/users/{account_id}/rankings``."""

    item = _mapping(payload, "public-user-rankings")
    rows = _list(item.get("rankings"), "public-user-rankings.rankings")
    return PublicUserRankings(
        account_id=_string(
            item.get("accountId"),
            "public-user-rankings.accountId",
        ),
        account_display_name=_string(
            item.get("accountDisplayName"),
            "public-user-rankings.accountDisplayName",
        ),
        rankings=tuple(
            _parse_public_user_ranking(
                row,
                f"public-user-rankings.rankings[{index}]",
            )
            for index, row in enumerate(rows)
        ),
    )


def parse_battle_detail(payload: Any) -> BattleDetailSummary:
    """Read only the public summary fields from a full battle response."""

    root = _mapping(payload, "battle-detail")
    battle = _mapping(root.get("battle"), "battle-detail.battle")
    participant_items = _list(
        root.get("participants"),
        "battle-detail.participants",
    )
    participants = tuple(
        _parse_battle_participant(
            value,
            f"battle-detail.participants[{index}]",
        )
        for index, value in enumerate(participant_items)
    )
    roster_items = _list(
        battle.get("roster"),
        "battle-detail.battle.roster",
    )
    # Upstream fills every roster accountDisplayName with the uploader
    # nickname; prefer an explicit uploaderNickname if the API ever adds one.
    uploader_display_name = _first_account_display_name(
        [{"accountDisplayName": battle.get("uploaderNickname")}],
        roster_items,
        participant_items,
    )
    uploader_user_id = _string(
        battle.get("uploaderUserId"),
        "battle-detail.battle.uploaderUserId",
    )
    integrity = _mapping(root.get("integrity"), "battle-detail.integrity")
    skill_items = _list(
        root.get("roleSkillStats", []),
        "battle-detail.roleSkillStats",
    )
    return BattleDetailSummary(
        battle_id=_string(battle.get("id"), "battle-detail.battle.id"),
        uploader_user_id=uploader_user_id,
        uploader_display_name=uploader_display_name or uploader_user_id,
        dungeon_name=_string(
            battle.get("dungeonName"),
            "battle-detail.battle.dungeonName",
        ),
        boss_name=_string(
            battle.get("bossName"),
            "battle-detail.battle.bossName",
        ),
        battle_end_at=_string(
            battle.get("battleEndAt"),
            "battle-detail.battle.battleEndAt",
        ),
        duration_ms=_integer(
            battle.get("durationMs"),
            "battle-detail.battle.durationMs",
        ),
        total_damage=_integer(
            battle.get("totalDamage"),
            "battle-detail.battle.totalDamage",
        ),
        total_dps=_number(
            battle.get("totalDps"),
            "battle-detail.battle.totalDps",
        ),
        participants=participants,
        parser_version=_string(
            battle.get("parserVersion"),
            "battle-detail.battle.parserVersion",
        ),
        rules_version=_string(
            battle.get("rulesVersion"),
            "battle-detail.battle.rulesVersion",
        ),
        time_source=_optional_string(
            battle.get("timeSource"),
            "battle-detail.battle.timeSource",
        ),
        official_timer_start_seen=_optional_boolean(
            battle.get("officialTimerStartSeen"),
            "battle-detail.battle.officialTimerStartSeen",
        ),
        official_timer_end_seen=_optional_boolean(
            battle.get("officialTimerEndSeen"),
            "battle-detail.battle.officialTimerEndSeen",
        ),
        integrity_verified=_boolean(
            integrity.get("verified"),
            "battle-detail.integrity.verified",
        ),
        contract_tag_score=_optional_integer(
            battle.get("contractTagScore"),
            "battle-detail.battle.contractTagScore",
        ),
        contract_tags=_parse_contract_tags(
            battle.get("contractTags", []),
            "battle-detail.battle.contractTags",
        ),
        roster=tuple(
            _parse_battle_roster_entry(
                value, f"battle-detail.battle.roster[{index}]"
            )
            for index, value in enumerate(roster_items)
        ),
        skill_stats=tuple(
            _parse_skill_stat(value, f"battle-detail.roleSkillStats[{index}]")
            for index, value in enumerate(skill_items)
        ),
        damage_points=_parse_damage_points(root.get("timelineEvents")),
        buffs=_parse_character_state_buffs(root.get("characterStates")),
    )


def _parse_damage_points(value: Any) -> tuple[BattleDamagePoint, ...]:
    """Read the damage ticks out of ``timelineEvents``, skipping the rest.

    Deliberately lenient: this feeds the DPS curve, an extra section, so one
    odd event must never cost the whole battle card. Everything the curve
    needs (timestamp, dealer, amount) has to be present and sane, otherwise
    the event is dropped.
    """

    if not isinstance(value, list):
        return ()
    points: list[BattleDamagePoint] = []
    for item in value:
        if not isinstance(item, dict) or item.get("laneType") != "skill":
            continue
        at_ms = item.get("tsMsFromStart")
        amount = item.get("value")
        name = item.get("sourceCharacterName")
        if (
            not isinstance(at_ms, int)
            or isinstance(at_ms, bool)
            or not isinstance(amount, int)
            or isinstance(amount, bool)
            or amount <= 0
            or not isinstance(name, str)
            or not name.strip()
        ):
            continue
        points.append(
            BattleDamagePoint(
                at_ms=at_ms, character_name=name.strip(), value=amount
            )
        )
    return tuple(points)


def _parse_character_state_buffs(value: Any) -> tuple[BattleBuff, ...]:
    """Read the buffs upstream already grouped per character, leniently.

    Both directions are kept: what the character received, and the debuffs
    they put on the enemy (脆弱 / 易伤 / 减抗), which amplify damage just as
    much as a team buff does.
    """

    if not isinstance(value, list):
        return ()
    buffs: list[BattleBuff] = []
    for state in value:
        if not isinstance(state, dict):
            continue
        for field, on_enemy in (("buffsReceived", False), ("debuffsApplied", True)):
            entries = state.get(field)
            if not isinstance(entries, list):
                continue
            for item in entries:
                buff = _parse_buff(item, on_enemy=on_enemy)
                if buff is not None:
                    buffs.append(buff)
    return tuple(buffs)


def _parse_buff(value: Any, *, on_enemy: bool = False) -> BattleBuff | None:
    if not isinstance(value, dict):
        return None
    start = value.get("startTsMsFromStart")
    target = value.get("targetCharacterName")
    name = value.get("eventName")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(target, str)
        or not target.strip()
    ):
        return None
    duration = value.get("durationMs")
    effects = []
    for effect in value.get("effects") or ():
        if not isinstance(effect, dict):
            continue
        rate = effect.get("rate")
        effects.append(
            BattleBuffEffect(
                zone=effect.get("zone")
                if isinstance(effect.get("zone"), str)
                else None,
                element=effect.get("element")
                if isinstance(effect.get("element"), str)
                else None,
                rate=float(rate)
                if isinstance(rate, int | float) and not isinstance(rate, bool)
                else None,
            )
        )
    return BattleBuff(
        name=name.strip() if isinstance(name, str) else "",
        target_name=target.strip(),
        start_ms=start,
        event_key=value.get("eventKey")
        if isinstance(value.get("eventKey"), str)
        else None,
        source_name=value.get("sourceCharacterName")
        if isinstance(value.get("sourceCharacterName"), str)
        else None,
        duration_ms=duration
        if isinstance(duration, int) and not isinstance(duration, bool)
        else None,
        effects=tuple(effects),
        on_enemy=on_enemy,
    )


def parse_battle_export(payload: Any) -> BattleExport:
    """Adapt the public export (schema v1); only what the timeline needs."""

    path = "battle-export"
    item = _mapping(payload, path)
    dungeon = _mapping(item.get("dungeon"), f"{path}.dungeon")
    roster = _list(item.get("roster", []), f"{path}.roster")
    casts = _list(item.get("casts", []), f"{path}.casts")
    return BattleExport(
        battle_id=_string(item.get("battleId"), f"{path}.battleId"),
        boss_name=_string(dungeon.get("bossName"), f"{path}.dungeon.bossName"),
        dungeon_name=_string(
            dungeon.get("dungeonName"), f"{path}.dungeon.dungeonName"
        ),
        duration_ms=_non_negative(item.get("durationMs"), f"{path}.durationMs"),
        roster=tuple(
            _parse_export_roster_entry(entry, f"{path}.roster[{index}]")
            for index, entry in enumerate(roster)
        ),
        casts=tuple(
            _parse_battle_cast(cast, f"{path}.casts[{index}]")
            for index, cast in enumerate(casts)
        ),
        boss_slug=_optional_string(
            dungeon.get("dungeonSlug"), f"{path}.dungeon.dungeonSlug"
        ),
        battle_end_at=_optional_string(
            item.get("battleEndAt"), f"{path}.battleEndAt"
        ),
        parser_version=_optional_string(
            item.get("parserVersion"), f"{path}.parserVersion"
        ),
    )


def _parse_export_roster_entry(value: Any, path: str) -> ExportRosterEntry:
    item = _mapping(value, path)
    return ExportRosterEntry(
        slot=_integer(item.get("slot"), f"{path}.slot"),
        character_name=_string(item.get("characterName"), f"{path}.characterName"),
        character_key=_optional_string(
            item.get("characterKey"), f"{path}.characterKey"
        ),
        character_level=_optional_integer(
            item.get("characterLevel"), f"{path}.characterLevel"
        ),
        character_potential=_optional_integer(
            item.get("characterPotential"), f"{path}.characterPotential"
        ),
    )


def _parse_battle_cast(value: Any, path: str) -> BattleCast:
    item = _mapping(value, path)
    skill_key = _string(item.get("skillKey"), f"{path}.skillKey")
    skill_name = _optional_string(item.get("skillName"), f"{path}.skillName")
    return BattleCast(
        character_key=_string(item.get("characterKey"), f"{path}.characterKey"),
        skill_key=skill_key,
        skill_name=skill_name or skill_key,
        start_ms=_integer(item.get("tsMsFromStart"), f"{path}.tsMsFromStart"),
        end_ms=_optional_integer(item.get("endMsFromStart"), f"{path}.endMsFromStart"),
        source=_optional_string(item.get("skillSource"), f"{path}.skillSource"),
        recovers_energy=bool(
            _optional_boolean(item.get("recoversEnergy"), f"{path}.recoversEnergy")
        ),
    )


def _parse_battle_roster_entry(value: Any, path: str) -> BattleRosterEntry:
    item = _mapping(value, path)
    weapon = item.get("weapon")
    equips = _list(item.get("equips", []), f"{path}.equips")
    skills = _list(item.get("skills", []), f"{path}.skills")
    return BattleRosterEntry(
        slot=_integer(item.get("slot"), f"{path}.slot"),
        character_name=_string(item.get("characterName"), f"{path}.characterName"),
        account_display_name=_string(
            item.get("accountDisplayName"),
            f"{path}.accountDisplayName",
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
        character_element=_optional_string(
            item.get("characterElement"),
            f"{path}.characterElement",
        ),
        character_level=_optional_integer(
            item.get("characterLevel"),
            f"{path}.characterLevel",
        ),
        character_potential=_optional_integer(
            item.get("characterPotential"),
            f"{path}.characterPotential",
        ),
        weapon=(
            None
            if weapon is None
            else _parse_battle_weapon(weapon, f"{path}.weapon")
        ),
        equips=tuple(
            _parse_battle_equip(equip, f"{path}.equips[{index}]")
            for index, equip in enumerate(equips)
        ),
        skills=tuple(
            _parse_roster_skill(skill, f"{path}.skills[{index}]")
            for index, skill in enumerate(skills)
        ),
    )


def _parse_battle_weapon(value: Any, path: str) -> BattleWeapon:
    item = _mapping(value, path)
    skills = _list(item.get("skills", []), f"{path}.skills")
    return BattleWeapon(
        name=_string(item.get("weaponName"), f"{path}.weaponName"),
        template=_optional_string(
            item.get("weaponTemplate"),
            f"{path}.weaponTemplate",
        ),
        level=_optional_integer(item.get("weaponLevel"), f"{path}.weaponLevel"),
        refine=_optional_integer(
            item.get("weaponRefine"),
            f"{path}.weaponRefine",
        ),
        icon_url=_optional_string(item.get("iconUrl"), f"{path}.iconUrl"),
        skills=tuple(
            _parse_weapon_skill(skill, f"{path}.skills[{index}]")
            for index, skill in enumerate(skills)
        ),
    )


def _parse_weapon_skill(value: Any, path: str) -> BattleWeaponSkill:
    item = _mapping(value, path)
    return BattleWeaponSkill(
        skill_key=_string(item.get("skillKey"), f"{path}.skillKey"),
        level=_optional_integer(item.get("level"), f"{path}.level"),
        potential_level=_optional_integer(
            item.get("potentialLevel"),
            f"{path}.potentialLevel",
        ),
    )


def _parse_battle_equip(value: Any, path: str) -> BattleEquip:
    item = _mapping(value, path)
    return BattleEquip(
        slot=_integer(item.get("slot"), f"{path}.slot"),
        piece_name=_string(item.get("pieceName"), f"{path}.pieceName"),
        item_id=_optional_string(item.get("itemId"), f"{path}.itemId"),
        suit_name=_optional_string(item.get("suitName"), f"{path}.suitName"),
        part_name=_optional_string(item.get("partName"), f"{path}.partName"),
        icon_url=_optional_string(item.get("iconUrl"), f"{path}.iconUrl"),
        enhance_levels=_parse_enhance_levels(item.get("enhanceLevels")),
        stats=_parse_equip_stats(item.get("stats")),
    )


def _parse_enhance_levels(value: Any) -> tuple[tuple[int, int], ...]:
    """Keep the well-formed ``{index, level}`` pairs; the list is untyped upstream."""

    if not isinstance(value, list):
        return ()
    levels: dict[int, int] = {}
    for entry in value:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        level = entry.get("level")
        if not _is_plain_int(index) or not _is_plain_int(level):
            continue
        levels[index] = level
    return tuple(sorted(levels.items()))


def _parse_equip_stats(value: Any) -> tuple[BattleEquipStat, ...]:
    """Keep the stat lines that carry a name and a number; skip the rest.

    The upstream schema types these as bare dicts, so a malformed line must
    not fail the whole battle the way a typed field would.
    """

    if not isinstance(value, list):
        return ()
    stats: list[BattleEquipStat] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        raw_value = entry.get("value")
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            continue
        slot = entry.get("slot")
        level = entry.get("level")
        stats.append(
            BattleEquipStat(
                name=name.strip(),
                value=float(raw_value),
                slot=slot if isinstance(slot, str) else None,
                level=level if _is_plain_int(level) else None,
            )
        )
    return tuple(stats)


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_roster_skill(value: Any, path: str) -> BattleRosterSkill:
    item = _mapping(value, path)
    return BattleRosterSkill(
        skill_key=_string(item.get("skillKey"), f"{path}.skillKey"),
        level=_integer(item.get("level"), f"{path}.level"),
    )


def _parse_skill_stat(value: Any, path: str) -> BattleSkillStat:
    item = _mapping(value, path)
    return BattleSkillStat(
        character_name=_string(item.get("characterName"), f"{path}.characterName"),
        skill_name=_string(item.get("skillName"), f"{path}.skillName"),
        cast_count=_non_negative(item.get("castCount"), f"{path}.castCount"),
        total_damage=_integer(item.get("totalDamage"), f"{path}.totalDamage"),
        avg_damage=_number(item.get("avgDamage"), f"{path}.avgDamage"),
        max_damage=_integer(item.get("maxDamage"), f"{path}.maxDamage"),
        skill_key=_optional_string(item.get("skillKey"), f"{path}.skillKey"),
    )


def _parse_public_user_ranking(value: Any, path: str) -> PublicUserRanking:
    item = _mapping(value, path)
    roster = _list(item.get("rosterSummary"), f"{path}.rosterSummary")
    return PublicUserRanking(
        boss_slug=_string(item.get("bossSlug"), f"{path}.bossSlug"),
        boss_name=_string(item.get("bossName"), f"{path}.bossName"),
        dungeon_name=_string(
            item.get("dungeonName"),
            f"{path}.dungeonName",
        ),
        battle_id=_string(item.get("battleId"), f"{path}.battleId"),
        rank=_integer(item.get("rank"), f"{path}.rank"),
        score_percent=_integer(
            item.get("scorePercent"),
            f"{path}.scorePercent",
        ),
        duration_ms=_integer(
            item.get("durationMs"),
            f"{path}.durationMs",
        ),
        total_dps=_number(item.get("totalDps"), f"{path}.totalDps"),
        battle_end_at=_string(
            item.get("battleEndAt"),
            f"{path}.battleEndAt",
        ),
        roster_summary=tuple(
            _string(entry, f"{path}.rosterSummary[{index}]")
            for index, entry in enumerate(roster)
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


def _parse_battle_participant(value: Any, path: str) -> BattleParticipant:
    item = _mapping(value, path)
    return BattleParticipant(
        character_key=_optional_string(
            item.get("characterKey"),
            f"{path}.characterKey",
        ),
        character_name=_string(
            item.get("characterName"),
            f"{path}.characterName",
        ),
        character_profession=_optional_string(
            item.get("characterProfession"),
            f"{path}.characterProfession",
        ),
        character_avatar_url=_optional_string(
            item.get("characterAvatarUrl"),
            f"{path}.characterAvatarUrl",
        ),
        account_display_name=_string(
            item.get("accountDisplayName"),
            f"{path}.accountDisplayName",
        ),
        total_damage=_integer(
            item.get("totalDamage"),
            f"{path}.totalDamage",
        ),
        dps=_number(item.get("dps"), f"{path}.dps"),
        rdps=_number(item.get("rdps"), f"{path}.rdps"),
        max_hit=_optional_integer(item.get("maxHit"), f"{path}.maxHit"),
        crit_rate=_optional_number(
            item.get("critRate"),
            f"{path}.critRate",
        ),
    )


def _first_account_display_name(*collections: list[Any]) -> str | None:
    for collection in collections:
        for index, value in enumerate(collection):
            item = _mapping(value, f"account-display-name[{index}]")
            display_name = item.get("accountDisplayName")
            if isinstance(display_name, str) and display_name.strip():
                return display_name.strip()
    return None


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


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ModelValidationError(f"{path} must be a boolean")
    return value


def _optional_boolean(value: Any, path: str) -> bool | None:
    if value is None:
        return None
    return _boolean(value, path)


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModelValidationError(f"{path} must be a number")
    return float(value)


def _optional_number(value: Any, path: str) -> float | None:
    if value is None:
        return None
    return _number(value, path)
