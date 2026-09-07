"""Top-3 cards, one board's ranking and the roster page."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from ..characters import CharacterFilterScope
from ..elements import element_key
from ..matcher import MatchChoice, TargetType
from ..models import (
    BossRanking,
    BossRankingRosterEntry,
    BossRankingRow,
    HotBossCard,
)
from ..professions import normalize_profession
from ..routing import (
    DEFAULT_RANKING_TOP,
    MAX_RANKING_TOP,
    MIN_RANKING_TOP,
)
from .common import (
    _CRISIS_CONTRACT_BOSS_SLUG,
    PageHeader,
    PresentationError,
    _bar_width,
    _dps_share,
    _initial,
    _safe_asset_url,
    _share,
    format_duration,
    format_number,
)


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
    # The CSS key of the catalog element; None when the catalog lacks the name.
    element_key: str | None = None


@dataclass(frozen=True, slots=True)
class RankingRowView:
    rank: int
    percentile: str
    account_display_name: str
    character_name: str
    character_initial: str
    character_avatar_url: str | None
    roster: tuple[RosterEntryView, ...]
    dps: str
    duration: str
    contract_score: str | None
    # Real DPS share of the board leader, for the in-row bar. Upstream
    # ``scorePercent`` is a rank percentile and would only restate the rank.
    score_bar_width: float = 100.0


@dataclass(frozen=True, slots=True)
class RankingPage:
    header: PageHeader
    boss_slug: str
    row_count: int
    show_contract_score: bool
    rows: tuple[RankingRowView, ...]
    # Formatted DPS of the board leader; the 100% mark of the in-row bars.
    top_dps: str = ""
    character_filter: str | None = None
    # MAIN filters on the row's main C; ROSTER is the automatic fallback for
    # characters that never carry, matching anywhere in the four-man team.
    character_filter_scope: CharacterFilterScope = CharacterFilterScope.MAIN
    filtered_count: int | None = None
    # The resolved names behind ``character_filter`` (their display join);
    # several names mean every one of them must be in the team.
    character_filters: tuple[str, ...] = ()
    # Rows whose main C has this element, on top of the character filter.
    element_filter: str | None = None
    # Rows whose main C has this profession, on top of the other filters.
    profession_filter: str | None = None


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
    character_filter: str | tuple[str, ...] | None = None,
    character_filter_scope: CharacterFilterScope = CharacterFilterScope.MAIN,
    element_filter: str | None = None,
    elements: Mapping[str, str] | None = None,
    profession_filter: str | None = None,
) -> RankingPage:
    """Build the first public DPS rows in their upstream order.

    ``character_filter`` is an already-resolved character name matched against
    the row's main C, or against the whole roster when the scope says so; rows
    keep their upstream global rank after filtering.
    """

    if isinstance(display_limit, bool) or not isinstance(display_limit, int):
        raise PresentationError("ranking display limit must be an integer")
    if not MIN_RANKING_TOP <= display_limit <= MAX_RANKING_TOP:
        raise PresentationError("ranking display limit must be between 1 and 30")

    if character_filter is None:
        filters: tuple[str, ...] = ()
    elif isinstance(character_filter, str):
        filters = (character_filter,)
    else:
        filters = tuple(character_filter)
    source_rows = ranking.rows
    filtered_count: int | None = None
    if filters:
        if character_filter_scope is CharacterFilterScope.ROSTER:
            # Every named character has to be in the four-man team.
            source_rows = tuple(
                row
                for row in ranking.rows
                if all(
                    any(entry.character_name == name for entry in row.roster_entries)
                    for name in filters
                )
            )
        else:
            source_rows = tuple(
                row for row in ranking.rows if row.character_name in filters
            )
        filtered_count = len(source_rows)
    if element_filter is not None:
        known = elements or {}
        source_rows = tuple(
            row
            for row in source_rows
            if known.get(row.character_name) == element_filter
        )
        filtered_count = len(source_rows)
    if profession_filter is not None:
        source_rows = tuple(
            row
            for row in source_rows
            if normalize_profession(row.character_profession or "")
            == profession_filter
        )
        filtered_count = len(source_rows)
    displayed_rows = source_rows[:display_limit]
    top_dps = ranking.rows[0].dps if ranking.rows else 0.0
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
                character_initial=_initial(row.character_name),
                character_avatar_url=_safe_asset_url(
                    row.character_avatar_url,
                    base_url=web_base_url,
                ),
                roster=_build_roster(
                    row.roster_entries,
                    row.roster_summary,
                    web_base_url=web_base_url,
                    elements=elements,
                ),
                dps=format_number(row.dps),
                duration=format_duration(row.duration_ms),
                contract_score=(
                    format_number(row.contract_tag_score)
                    if row.contract_tag_score is not None
                    else None
                ),
                score_bar_width=_dps_share(row.dps, top_dps),
            )
            for row in displayed_rows
        ),
        top_dps=format_number(top_dps) if ranking.rows else "",
        character_filter=" · ".join(filters) if filters else None,
        character_filter_scope=character_filter_scope,
        filtered_count=filtered_count,
        character_filters=filters,
        element_filter=element_filter,
        profession_filter=profession_filter,
    )


_MAX_USAGE_ENTRIES = 6


_MAX_COMBOS = 5


_MAX_MAIN_CHARACTERS = 8


_PROFESSION_ORDER = ("近卫", "重装", "辅助", "突击", "术士", "先锋")


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
    elements: Mapping[str, str] | None = None,
) -> tuple[RosterEntryView, ...]:
    known = elements or {}
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
                element_key=element_key(known.get(entry.character_name)),
            )
            for entry in entries
        )
    return tuple(
        RosterEntryView(
            character_name=name,
            profession="",
            character_initial=_initial(name),
            avatar_url=None,
            element_key=element_key(known.get(name)),
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
