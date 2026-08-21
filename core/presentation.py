"""Image-template view models for public ZMDLogs ranking data."""

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urljoin, urlsplit

from .matcher import MatchChoice, TargetType
from .models import (
    BattleDetailSummary,
    BossRanking,
    BossRankingRosterEntry,
    BossRankingRow,
    CharacterBossStatistics,
    CharacterStatistics,
    HotBossCard,
    PublicUserRankings,
)
from .routing import (
    DEFAULT_RANKING_TOP,
    MAX_RANKING_TOP,
    MIN_RANKING_TOP,
)

# Temporary presentation compatibility: upstream currently exposes the
# contract board as bossName="破潮之像", while the public site labels the
# activity and ranking page as "危机合约". Remove this override after the
# upstream API provides the same public-facing name.
_CRISIS_CONTRACT_BOSS_SLUG = "indie_group_ccdg"


class PresentationError(ValueError):
    """Raised when data cannot be represented safely in a public template."""


@dataclass(frozen=True, slots=True)
class PageHeader:
    title: str
    subtitle: str
    query: str
    matched_name: str
    target_type: str
    footer_note: str = "公开榜单 · DPS 口径"


@dataclass(frozen=True, slots=True)
class TopRunView:
    rank: int
    character_name: str
    character_initial: str
    character_avatar_url: str | None
    uploader_nickname: str
    duration: str
    contract_score: str | None


@dataclass(frozen=True, slots=True)
class TopCardView:
    dungeon_name: str
    boss_name: str
    runs: tuple[TopRunView, ...]


@dataclass(frozen=True, slots=True)
class TopCardGroupView:
    dungeon_name: str
    cards: tuple[TopCardView, ...]


@dataclass(frozen=True, slots=True)
class Top3Page:
    header: PageHeader
    dungeon_names: tuple[str, ...]
    cards: tuple[TopCardView, ...]
    card_groups: tuple[TopCardGroupView, ...]
    group_by_dungeon: bool


@dataclass(frozen=True, slots=True)
class RosterEntryView:
    character_name: str
    profession: str
    character_initial: str
    avatar_url: str | None


@dataclass(frozen=True, slots=True)
class RankingRowView:
    rank: int
    percentile: str
    account_display_name: str
    character_name: str
    character_profession: str
    character_initial: str
    character_avatar_url: str | None
    roster: tuple[RosterEntryView, ...]
    dps: str
    duration: str
    contract_score: str | None
    # score_percent clamped for the in-row relative-DPS bar (percent of rank 1).
    score_bar_width: float = 100.0


@dataclass(frozen=True, slots=True)
class RankingPage:
    header: PageHeader
    boss_slug: str
    row_count: int
    show_contract_score: bool
    rows: tuple[RankingRowView, ...]
    character_filter: str | None = None
    filtered_count: int | None = None


@dataclass(frozen=True, slots=True)
class CharacterStatRowView:
    rank: int
    character_name: str
    character_profession: str
    character_initial: str
    character_avatar_url: str | None
    sample_count: int
    outlier_count: int
    median: str
    maximum: str
    # Horizontal box-plot geometry, already expressed as percentages of the
    # shared axis so the template performs no arithmetic.
    whisker_left: float
    whisker_width: float
    box_left: float
    box_width: float
    median_left: float
    p10_left: float
    p90_left: float
    maximum_left: float | None


@dataclass(frozen=True, slots=True)
class CharacterStatChipView:
    character_name: str
    sample_count: int


@dataclass(frozen=True, slots=True)
class CharacterStatsPage:
    header: PageHeader
    scope_label: str
    range_label: str
    potential_label: str
    eligible_battle_count: int
    total_sample_count: int
    total_outlier_count: int
    included_boss_count: int
    minimum_sample_count: int
    axis_labels: tuple[str, ...]
    rows: tuple[CharacterStatRowView, ...]
    insufficient: tuple[CharacterStatChipView, ...]


@dataclass(frozen=True, slots=True)
class CharacterBossRowView:
    boss_name: str
    dungeon_name: str
    rank: int
    ranked_character_count: int
    sample_count: int
    outlier_count: int
    median: str
    maximum: str
    whisker_left: float
    whisker_width: float
    box_left: float
    box_width: float
    median_left: float
    p10_left: float
    p90_left: float
    maximum_left: float | None


@dataclass(frozen=True, slots=True)
class CharacterBossChipView:
    boss_name: str
    sample_count: int


@dataclass(frozen=True, slots=True)
class CharacterBossPage:
    header: PageHeader
    character_name: str
    character_profession: str
    character_initial: str
    character_avatar_url: str | None
    range_label: str
    potential_label: str
    total_sample_count: int
    total_outlier_count: int
    included_boss_count: int
    minimum_sample_count: int
    axis_labels: tuple[str, ...]
    rows: tuple[CharacterBossRowView, ...]
    insufficient: tuple[CharacterBossChipView, ...]


@dataclass(frozen=True, slots=True)
class UsageEntryView:
    character_name: str
    character_initial: str
    avatar_url: str | None
    percent: str
    bar_width: float


@dataclass(frozen=True, slots=True)
class ProfessionUsageView:
    profession: str
    entries: tuple[UsageEntryView, ...]
    hidden_count: int


@dataclass(frozen=True, slots=True)
class RosterComboView:
    members: tuple[RosterEntryView, ...]
    count: int
    percent: str
    best_rank: int
    best_dps: str


@dataclass(frozen=True, slots=True)
class MainCharacterView:
    character_name: str
    character_initial: str
    avatar_url: str | None
    count: int
    percent: str
    bar_width: float


@dataclass(frozen=True, slots=True)
class RosterPage:
    header: PageHeader
    row_count: int
    sample_size: int
    profession_usage: tuple[ProfessionUsageView, ...]
    combos: tuple[RosterComboView, ...]
    main_characters: tuple[MainCharacterView, ...]


@dataclass(frozen=True, slots=True)
class AccountRankingView:
    boss_name: str
    dungeon_name: str | None
    rank: int
    percentile: str
    duration: str
    total_dps: str
    battle_date: str
    roster: tuple[str, ...]
    contract_score: str | None


@dataclass(frozen=True, slots=True)
class AccountPage:
    header: PageHeader
    account_id: str
    account_url: str
    record_count: int
    best_rank: str
    best_percentile: str
    average_percentile: str
    show_contract_score: bool
    rows: tuple[AccountRankingView, ...]


@dataclass(frozen=True, slots=True)
class BattleParticipantView:
    character_name: str
    character_profession: str
    character_initial: str
    character_avatar_url: str | None
    dps: str
    rdps: str
    total_damage: str
    damage_share: str
    rdps_share: str
    dps_share_percent: float
    rdps_share_percent: float
    max_hit: str
    crit_rate: str


@dataclass(frozen=True, slots=True)
class BattlePage:
    header: PageHeader
    battle_id: str
    report_url: str
    account_id: str
    account_url: str
    uploader_display_name: str
    duration: str
    total_dps: str
    total_damage: str
    battle_date: str
    timer_label: str
    integrity_label: str
    contract_score: str | None
    participants: tuple[BattleParticipantView, ...]


def build_all_top3_page(
    cards: tuple[HotBossCard, ...],
    *,
    query: str,
    web_base_url: str | None = None,
) -> Top3Page:
    """Build the all-board page without exposing ranking-only fields."""

    card_views = tuple(
        _build_top_card(card, web_base_url=web_base_url) for card in cards
    )
    return Top3Page(
        header=PageHeader(
            title="全部榜单前三名",
            subtitle="当前公开榜单与各榜最快记录",
            query=query,
            matched_name="全部公开榜单",
            target_type="全部榜单",
        ),
        dungeon_names=_unique_dungeon_names(cards),
        cards=card_views,
        card_groups=_group_top_cards(card_views),
        group_by_dungeon=False,
    )


def build_dungeon_top3_page(
    choice: MatchChoice,
    cards: tuple[HotBossCard, ...],
    *,
    query: str,
    web_base_url: str | None = None,
) -> Top3Page:
    """Build a dungeon or dungeon-scope page using the shared top-three card."""

    target_type = choice.target.target_type
    if target_type not in {TargetType.DUNGEON, TargetType.DUNGEON_SCOPE}:
        raise PresentationError("dungeon page requires a dungeon target")

    label = "副本范围" if target_type is TargetType.DUNGEON_SCOPE else "副本"
    subtitle = (
        "范围内全部标准副本与榜单前三名"
        if target_type is TargetType.DUNGEON_SCOPE
        else "该标准副本下全部榜单前三名"
    )
    card_views = tuple(
        _build_top_card(card, web_base_url=web_base_url) for card in cards
    )
    return Top3Page(
        header=PageHeader(
            title=f"{label}榜单前三名",
            subtitle=subtitle,
            query=query,
            matched_name=choice.target.name,
            target_type=label,
        ),
        dungeon_names=choice.target.dungeon_names,
        cards=card_views,
        card_groups=_group_top_cards(card_views),
        group_by_dungeon=target_type is TargetType.DUNGEON_SCOPE,
    )


def build_ranking_page(
    ranking: BossRanking,
    *,
    query: str,
    display_limit: int = DEFAULT_RANKING_TOP,
    web_base_url: str | None = None,
    character_filter: str | None = None,
) -> RankingPage:
    """Build the first public DPS rows in their upstream order.

    ``character_filter`` is an already-resolved main-character name; rows keep
    their upstream global rank after filtering.
    """

    if isinstance(display_limit, bool) or not isinstance(display_limit, int):
        raise PresentationError("ranking display limit must be an integer")
    if not MIN_RANKING_TOP <= display_limit <= MAX_RANKING_TOP:
        raise PresentationError("ranking display limit must be between 1 and 30")

    source_rows = ranking.rows
    filtered_count: int | None = None
    if character_filter is not None:
        source_rows = tuple(
            row for row in ranking.rows if row.character_name == character_filter
        )
        filtered_count = len(source_rows)
    displayed_rows = source_rows[:display_limit]
    is_crisis_contract = ranking.boss_slug == _CRISIS_CONTRACT_BOSS_SLUG
    title = "危机合约" if is_crisis_contract else ranking.boss_name
    subtitle = "活动竞速" if is_crisis_contract else ranking.dungeon_name
    matched_name = (
        "危机合约"
        if is_crisis_contract
        else f"{ranking.dungeon_name} · {ranking.boss_name}"
    )
    return RankingPage(
        header=PageHeader(
            title=title,
            subtitle=subtitle,
            query=query,
            matched_name=matched_name,
            target_type="具体榜单",
        ),
        boss_slug=ranking.boss_slug,
        row_count=len(ranking.rows),
        show_contract_score=any(
            row.contract_tag_score is not None for row in displayed_rows
        ),
        rows=tuple(
            RankingRowView(
                rank=row.rank,
                percentile=f"{format_number(row.score_percent)}%",
                account_display_name=row.account_display_name,
                character_name=row.character_name,
                character_profession=row.character_profession,
                character_initial=_initial(row.character_name),
                character_avatar_url=_safe_asset_url(
                    row.character_avatar_url,
                    base_url=web_base_url,
                ),
                roster=_build_roster(
                    row.roster_entries,
                    row.roster_summary,
                    web_base_url=web_base_url,
                ),
                dps=format_number(row.dps),
                duration=format_duration(row.duration_ms),
                contract_score=(
                    format_number(row.contract_tag_score)
                    if row.contract_tag_score is not None
                    else None
                ),
                score_bar_width=max(2.0, min(100.0, float(row.score_percent))),
            )
            for row in displayed_rows
        ),
        character_filter=character_filter,
        filtered_count=filtered_count,
    )


_RANGE_LABELS = {
    "7d": "近 7 天",
    "14d": "近 14 天",
    "30d": "近 30 天",
    "all": "全部时间",
}
_POTENTIAL_LABELS = {"0": "0 潜能", "1-5": "1–5 潜能", "all": "全部潜能"}
_MAX_USAGE_ENTRIES = 6
_MAX_COMBOS = 5
_MAX_MAIN_CHARACTERS = 8
_PROFESSION_ORDER = ("近卫", "重装", "辅助", "突击", "术士", "先锋")


def build_character_stats_page(
    stats: CharacterStatistics,
    *,
    query: str,
    web_base_url: str | None = None,
) -> CharacterStatsPage:
    """Turn upstream percentile rows into box-plot geometry for the template."""

    is_global = stats.scope == "all"
    title = "全部副本" if is_global else stats.boss_name
    subtitle = "角色统计总榜" if is_global else stats.dungeon_name
    matched_name = (
        "角色统计总榜" if is_global else f"{stats.dungeon_name} · {stats.boss_name}"
    )
    ranked = tuple(
        row
        for row in stats.rows
        if row.rank is not None and not row.insufficient_samples
    )
    axis_max = _stats_axis_max(ranked)
    step = axis_max / 4 if axis_max else 0
    axis_labels = tuple(_format_axis_value(step * index) for index in range(5))

    def percent(value: float | None) -> float:
        if value is None or axis_max <= 0:
            return 0.0
        return round(max(0.0, min(100.0, value / axis_max * 100)), 2)

    rows: list[CharacterStatRowView] = []
    for row in ranked:
        low = row.lower_whisker if row.lower_whisker is not None else row.p25
        high = row.upper_whisker if row.upper_whisker is not None else row.p75
        p25 = row.p25 if row.p25 is not None else row.median
        p75 = row.p75 if row.p75 is not None else row.median
        whisker_left = percent(low)
        whisker_right = percent(high)
        box_left = percent(p25)
        box_right = percent(p75)
        maximum_left = None
        if row.maximum is not None and high is not None and row.maximum > high:
            # Outlier maxima can dwarf the whisker scale; pin them to the edge.
            maximum_left = min(percent(row.maximum), 100.0)
        rows.append(
            CharacterStatRowView(
                rank=row.rank or 0,
                character_name=row.character_name,
                character_profession=row.character_profession,
                character_initial=_initial(row.character_name),
                character_avatar_url=_safe_asset_url(
                    row.character_avatar_url, base_url=web_base_url
                ),
                sample_count=row.normal_sample_count,
                outlier_count=row.outlier_count,
                median=format_number(row.median) if row.median is not None else "—",
                maximum=(
                    format_number(row.maximum) if row.maximum is not None else "—"
                ),
                whisker_left=whisker_left,
                whisker_width=round(max(0.0, whisker_right - whisker_left), 2),
                box_left=box_left,
                box_width=round(max(0.0, box_right - box_left), 2),
                median_left=percent(row.median),
                p10_left=percent(row.p10 if row.p10 is not None else p25),
                p90_left=percent(row.p90 if row.p90 is not None else p75),
                maximum_left=maximum_left,
            )
        )

    insufficient = tuple(
        CharacterStatChipView(
            character_name=row.character_name,
            sample_count=row.sample_count,
        )
        for row in stats.rows
        if row.insufficient_samples and row.sample_count > 0
    )
    return CharacterStatsPage(
        header=PageHeader(
            title=title,
            subtitle=subtitle,
            query=query,
            matched_name=matched_name,
            target_type="角色统计",
            footer_note="公开战斗 · 六星角色 DPS 分布",
        ),
        scope_label="全部副本" if is_global else "单个榜单",
        range_label=_RANGE_LABELS.get(stats.range, stats.range),
        potential_label=_POTENTIAL_LABELS.get(stats.potential, stats.potential),
        eligible_battle_count=stats.eligible_battle_count,
        total_sample_count=stats.total_sample_count,
        total_outlier_count=stats.total_outlier_count,
        included_boss_count=stats.included_boss_count,
        minimum_sample_count=stats.minimum_sample_count,
        axis_labels=axis_labels,
        rows=tuple(rows),
        insufficient=insufficient,
    )


def build_character_boss_page(
    stats: CharacterBossStatistics,
    *,
    query: str,
    web_base_url: str | None = None,
) -> CharacterBossPage:
    """One character across every board: rank badge first, box plot second."""

    ranked = tuple(
        row
        for row in stats.rows
        if row.rank is not None and not row.insufficient_samples
    )
    ranked = tuple(
        sorted(
            ranked,
            key=lambda row: (
                -(row.median if row.median is not None else -1.0),
                row.boss_name,
            ),
        )
    )
    axis_max = _stats_axis_max(ranked)
    step = axis_max / 4 if axis_max else 0
    axis_labels = tuple(_format_axis_value(step * index) for index in range(5))

    def percent(value: float | None) -> float:
        if value is None or axis_max <= 0:
            return 0.0
        return round(max(0.0, min(100.0, value / axis_max * 100)), 2)

    rows: list[CharacterBossRowView] = []
    for row in ranked:
        low = row.lower_whisker if row.lower_whisker is not None else row.p25
        high = row.upper_whisker if row.upper_whisker is not None else row.p75
        p25 = row.p25 if row.p25 is not None else row.median
        p75 = row.p75 if row.p75 is not None else row.median
        whisker_left = percent(low)
        whisker_right = percent(high)
        box_left = percent(p25)
        box_right = percent(p75)
        maximum_left = None
        if row.maximum is not None and high is not None and row.maximum > high:
            maximum_left = min(percent(row.maximum), 100.0)
        rows.append(
            CharacterBossRowView(
                boss_name=row.boss_name,
                dungeon_name=row.dungeon_name,
                rank=row.rank or 0,
                ranked_character_count=row.ranked_character_count,
                sample_count=row.normal_sample_count,
                outlier_count=row.outlier_count,
                median=format_number(row.median) if row.median is not None else "—",
                maximum=(
                    format_number(row.maximum) if row.maximum is not None else "—"
                ),
                whisker_left=whisker_left,
                whisker_width=round(max(0.0, whisker_right - whisker_left), 2),
                box_left=box_left,
                box_width=round(max(0.0, box_right - box_left), 2),
                median_left=percent(row.median),
                p10_left=percent(row.p10 if row.p10 is not None else p25),
                p90_left=percent(row.p90 if row.p90 is not None else p75),
                maximum_left=maximum_left,
            )
        )

    insufficient = tuple(
        CharacterBossChipView(
            boss_name=row.boss_name,
            sample_count=row.sample_count,
        )
        for row in stats.rows
        if row.insufficient_samples and row.sample_count > 0
    )
    return CharacterBossPage(
        header=PageHeader(
            title=stats.character_name,
            subtitle=stats.character_profession,
            query=query,
            matched_name=f"{stats.character_name} · 全榜单角色统计",
            target_type="角色统计",
            footer_note="公开战斗 · 单角色全榜单 DPS 分布",
        ),
        character_name=stats.character_name,
        character_profession=stats.character_profession,
        character_initial=_initial(stats.character_name),
        character_avatar_url=_safe_asset_url(
            stats.character_avatar_url, base_url=web_base_url
        ),
        range_label=_RANGE_LABELS.get(stats.range, stats.range),
        potential_label=_POTENTIAL_LABELS.get(stats.potential, stats.potential),
        total_sample_count=stats.total_sample_count,
        total_outlier_count=stats.total_outlier_count,
        included_boss_count=stats.included_boss_count,
        minimum_sample_count=stats.minimum_sample_count,
        axis_labels=axis_labels,
        rows=tuple(rows),
        insufficient=insufficient,
    )


def build_roster_page(
    ranking: BossRanking,
    *,
    query: str,
    display_limit: int = DEFAULT_RANKING_TOP,
    web_base_url: str | None = None,
) -> RosterPage:
    """Profession usage (whole board) plus top-N roster / main-character stats."""

    if isinstance(display_limit, bool) or not isinstance(display_limit, int):
        raise PresentationError("ranking display limit must be an integer")
    if not MIN_RANKING_TOP <= display_limit <= MAX_RANKING_TOP:
        raise PresentationError("ranking display limit must be between 1 and 30")

    is_crisis_contract = ranking.boss_slug == _CRISIS_CONTRACT_BOSS_SLUG
    title = "危机合约" if is_crisis_contract else ranking.boss_name
    subtitle = "活动竞速" if is_crisis_contract else ranking.dungeon_name
    matched_name = (
        "危机合约"
        if is_crisis_contract
        else f"{ranking.dungeon_name} · {ranking.boss_name}"
    )
    sample_rows = ranking.rows[:display_limit]
    sample_size = len(sample_rows)

    profession_usage = []
    for group in ranking.profession_groups:
        entries = group.entries[:_MAX_USAGE_ENTRIES]
        peak = max((entry.usage_percent for entry in entries), default=0.0)
        profession_usage.append(
            ProfessionUsageView(
                profession=group.profession,
                entries=tuple(
                    UsageEntryView(
                        character_name=entry.character_name,
                        character_initial=_initial(entry.character_name),
                        avatar_url=_safe_asset_url(
                            entry.avatar_url, base_url=web_base_url
                        ),
                        percent=f"{format_number(entry.usage_percent)}%",
                        bar_width=_bar_width(entry.usage_percent, peak),
                    )
                    for entry in entries
                ),
                hidden_count=max(0, len(group.entries) - len(entries)),
            )
        )

    combos = _build_roster_combos(sample_rows, web_base_url=web_base_url)

    main_counter: Counter[str] = Counter(row.character_name for row in sample_rows)
    main_avatars: dict[str, str | None] = {}
    for row in sample_rows:
        main_avatars.setdefault(row.character_name, row.character_avatar_url)
    main_peak = max(main_counter.values(), default=0)
    main_characters = tuple(
        MainCharacterView(
            character_name=name,
            character_initial=_initial(name),
            avatar_url=_safe_asset_url(
                main_avatars.get(name), base_url=web_base_url
            ),
            count=count,
            percent=_share(count, sample_size),
            bar_width=_bar_width(count, main_peak),
        )
        for name, count in sorted(
            main_counter.items(), key=lambda item: (-item[1], item[0])
        )[:_MAX_MAIN_CHARACTERS]
    )

    return RosterPage(
        header=PageHeader(
            title=title,
            subtitle=subtitle,
            query=query,
            matched_name=matched_name,
            target_type="榜单阵容",
        ),
        row_count=len(ranking.rows),
        sample_size=sample_size,
        profession_usage=tuple(profession_usage),
        combos=combos,
        main_characters=main_characters,
    )


def _build_roster_combos(
    rows: tuple[BossRankingRow, ...],
    *,
    web_base_url: str | None,
) -> tuple[RosterComboView, ...]:
    groups: dict[tuple[str, ...], list[BossRankingRow]] = {}
    for row in rows:
        groups.setdefault(_roster_key(row), []).append(row)
    ordered = sorted(
        groups.items(),
        key=lambda item: (-len(item[1]), min(row.rank for row in item[1])),
    )[:_MAX_COMBOS]
    combos: list[RosterComboView] = []
    for _, members in ordered:
        best = min(members, key=lambda row: row.rank)
        sorted_entries = _sorted_roster_entries(best)
        combos.append(
            RosterComboView(
                members=_build_roster(
                    sorted_entries,
                    best.roster_summary,
                    web_base_url=web_base_url,
                ),
                count=len(members),
                percent=_share(len(members), len(rows)),
                best_rank=best.rank,
                best_dps=format_number(best.dps),
            )
        )
    return tuple(combos)


def _sorted_roster_entries(
    row: BossRankingRow,
) -> tuple[BossRankingRosterEntry, ...]:
    def order(entry: BossRankingRosterEntry) -> tuple[int, str]:
        try:
            index = _PROFESSION_ORDER.index(entry.profession)
        except ValueError:
            index = len(_PROFESSION_ORDER)
        return index, entry.character_name

    return tuple(sorted(row.roster_entries, key=order))


def _roster_key(row: BossRankingRow) -> tuple[str, ...]:
    names = (
        tuple(entry.character_name for entry in row.roster_entries)
        or row.roster_summary
    )
    return tuple(sorted(names))


def _share(count: int, total: int) -> str:
    if total <= 0:
        return "0%"
    return f"{format_number(round(count / total * 100, 1))}%"


def _bar_width(value: float, peak: float) -> float:
    if peak <= 0 or value <= 0:
        return 0.0
    return round(min(100.0, value / peak * 100), 2)


def _stats_axis_max(rows) -> float:
    """Axis spans the normal-sample whiskers, not the (often extreme) maxima."""

    peak = 0.0
    for row in rows:
        for value in (row.upper_whisker, row.p90, row.p75, row.median):
            if value is not None and value > peak:
                peak = value
    if peak <= 0:
        return 0.0
    # Round up to a "nice" step so the axis labels read cleanly.
    magnitude = 10 ** (len(str(int(peak))) - 1)
    for unit in (1, 2, 2.5, 5, 10):
        candidate = magnitude * unit
        if peak <= candidate:
            return float(candidate)
    return float(math.ceil(peak / magnitude) * magnitude)


def _format_axis_value(value: float) -> str:
    if value >= 10_000:
        return f"{format_number(round(value / 10_000, 1))}万"
    return format_number(round(value))


def build_account_page(
    account: PublicUserRankings,
    *,
    query: str,
    web_base_url: str,
) -> AccountPage:
    """Build one exact public account's best-record overview."""

    rows = account.rankings
    record_count = len(rows)
    best_rank = f"#{min(row.rank for row in rows)}" if rows else "—"
    best_percentile = (
        f"{max(row.score_percent for row in rows)}%" if rows else "—"
    )
    average_percentile = (
        f"{round(sum(row.score_percent for row in rows) / record_count)}%"
        if rows
        else "—"
    )
    return AccountPage(
        header=PageHeader(
            title=account.account_display_name,
            subtitle="公开账号各首领最佳记录",
            query=query,
            matched_name=account.account_id,
            target_type="公开账号",
            footer_note="公开账号 · 当前最佳记录",
        ),
        account_id=account.account_id,
        account_url=_public_url(
            web_base_url,
            "records",
            account.account_id,
        ),
        record_count=record_count,
        best_rank=best_rank,
        best_percentile=best_percentile,
        average_percentile=average_percentile,
        show_contract_score=any(
            row.contract_tag_score is not None for row in rows
        ),
        rows=tuple(
            AccountRankingView(
                boss_name=row.boss_name,
                dungeon_name=(
                    row.dungeon_name
                    if row.dungeon_name != row.boss_name
                    else None
                ),
                rank=row.rank,
                percentile=f"{format_number(row.score_percent)}%",
                duration=format_duration(row.duration_ms),
                total_dps=format_number(row.total_dps),
                battle_date=_format_date(row.battle_end_at),
                roster=row.roster_summary,
                contract_score=(
                    format_number(row.contract_tag_score)
                    if row.contract_tag_score is not None
                    else None
                ),
            )
            for row in rows
        ),
    )


def build_battle_page(
    battle: BattleDetailSummary,
    *,
    query: str,
    web_base_url: str,
) -> BattlePage:
    """Build a compact card without exposing the full battle timeline."""

    timer_is_official = (
        battle.time_source == "game_timer"
        and battle.official_timer_start_seen is True
        and battle.official_timer_end_seen is True
    )
    total_damage = battle.total_damage
    # Highest DPS first, mirroring the site's contribution breakdown.
    participants = sorted(
        battle.participants,
        key=lambda participant: participant.dps,
        reverse=True,
    )
    total_rdps = sum(max(participant.rdps, 0.0) for participant in participants)
    return BattlePage(
        header=PageHeader(
            title=battle.boss_name,
            subtitle=battle.dungeon_name,
            query=query,
            matched_name=battle.battle_id,
            target_type="公开战报",
            footer_note="公开战报 · DPS / rDPS",
        ),
        battle_id=battle.battle_id,
        report_url=_public_url(
            web_base_url,
            "battle",
            battle.battle_id,
        ),
        account_id=battle.uploader_user_id,
        account_url=_public_url(
            web_base_url,
            "records",
            battle.uploader_user_id,
        ),
        uploader_display_name=battle.uploader_display_name,
        duration=format_duration(battle.duration_ms),
        total_dps=format_number(battle.total_dps),
        total_damage=format_number(total_damage),
        battle_date=_format_datetime(battle.battle_end_at),
        timer_label="官方计时" if timer_is_official else "计时待核验",
        integrity_label=(
            "结构校验通过" if battle.integrity_verified else "结构校验未通过"
        ),
        contract_score=(
            format_number(battle.contract_tag_score)
            if battle.contract_tag_score is not None
            else None
        ),
        participants=tuple(
            BattleParticipantView(
                character_name=participant.character_name,
                character_profession=participant.character_profession or "",
                character_initial=_initial(participant.character_name),
                character_avatar_url=_safe_asset_url(
                    participant.character_avatar_url,
                    base_url=web_base_url,
                ),
                dps=format_number(participant.dps),
                rdps=format_number(participant.rdps),
                total_damage=format_number(participant.total_damage),
                damage_share=(
                    f"{participant.total_damage / total_damage * 100:.1f}%"
                    if total_damage > 0
                    else "—"
                ),
                rdps_share=(
                    f"{participant.rdps / total_rdps * 100:.1f}%"
                    if total_rdps > 0
                    else "—"
                ),
                dps_share_percent=(
                    round(participant.total_damage / total_damage * 100, 2)
                    if total_damage > 0
                    else 0.0
                ),
                rdps_share_percent=(
                    round(max(participant.rdps, 0.0) / total_rdps * 100, 2)
                    if total_rdps > 0
                    else 0.0
                ),
                max_hit=(
                    format_number(participant.max_hit)
                    if participant.max_hit is not None
                    else "—"
                ),
                crit_rate=(
                    f"{participant.crit_rate * 100:.1f}%"
                    if participant.crit_rate is not None
                    else "—"
                ),
            )
            for participant in participants
        ),
    )
def format_duration(duration_ms: int) -> str:
    """Format milliseconds as ``minutes:seconds.milliseconds``."""

    if duration_ms < 0:
        raise PresentationError("duration cannot be negative")
    minutes, remainder = divmod(duration_ms, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{minutes}:{seconds:02d}.{milliseconds:03d}"


def format_number(value: int | float) -> str:
    """Use grouping separators without inventing insignificant decimals."""

    number = float(value)
    if number.is_integer():
        return f"{number:,.0f}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _build_top_card(
    card: HotBossCard,
    *,
    web_base_url: str | None,
) -> TopCardView:
    return TopCardView(
        dungeon_name=card.dungeon_name,
        boss_name=card.boss_name,
        runs=tuple(
            TopRunView(
                rank=index,
                character_name=run.character_name,
                character_initial=_initial(run.character_name),
                character_avatar_url=_safe_asset_url(
                    run.character_avatar_url,
                    base_url=web_base_url,
                ),
                uploader_nickname=run.uploader_nickname,
                duration=format_duration(run.duration_ms),
                contract_score=(
                    format_number(run.contract_tag_score)
                    if run.contract_tag_score is not None
                    else None
                ),
            )
            for index, run in enumerate(card.top_speed_runs, start=1)
        ),
    )


def _build_roster(
    entries: tuple[BossRankingRosterEntry, ...],
    summary: tuple[str, ...],
    *,
    web_base_url: str | None,
) -> tuple[RosterEntryView, ...]:
    if entries:
        return tuple(
            RosterEntryView(
                character_name=entry.character_name,
                profession=entry.profession,
                character_initial=_initial(entry.character_name),
                avatar_url=_safe_asset_url(
                    entry.avatar_url,
                    base_url=web_base_url,
                ),
            )
            for entry in entries
        )
    return tuple(
        RosterEntryView(
            character_name=name,
            profession="",
            character_initial=_initial(name),
            avatar_url=None,
        )
        for name in summary
    )


def _unique_dungeon_names(cards: tuple[HotBossCard, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(card.dungeon_name for card in cards))


def _group_top_cards(
    cards: tuple[TopCardView, ...],
) -> tuple[TopCardGroupView, ...]:
    """Group cards by dungeon while preserving the upstream result order."""

    grouped: dict[str, list[TopCardView]] = {}
    for card in cards:
        grouped.setdefault(card.dungeon_name, []).append(card)
    return tuple(
        TopCardGroupView(dungeon_name=name, cards=tuple(group_cards))
        for name, group_cards in grouped.items()
    )


def _initial(value: str) -> str:
    return value[:1] or "?"


def _safe_http_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _safe_asset_url(
    value: str | None,
    *,
    base_url: str | None,
) -> str | None:
    """Resolve upstream asset paths (usually site-relative) to safe URLs."""

    if value is None:
        return None
    if base_url is None:
        return _safe_http_url(value)
    resolved = urljoin(f"{base_url.rstrip('/')}/", value)
    return _safe_http_url(resolved)


def _public_url(base_url: str, resource: str, identifier: str) -> str:
    path = f"{resource}/{quote(identifier, safe='')}"
    return urljoin(f"{base_url.rstrip('/')}/", path)


def _format_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d"
        )
    except ValueError:
        return value[:10]


def _format_datetime(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return value
