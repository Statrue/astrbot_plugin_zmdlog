"""Top-3 cards, one board's ranking and the roster page."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from ..characters import CharacterFilterScope, filter_ranking_rows
from ..elements import element_key
from ..matcher import MatchChoice, TargetType
from ..metrics import is_rdps, metric_label
from ..models import (
    BossRanking,
    BossRankingRosterEntry,
    BossRankingRow,
    HotBossCard,
)
from ..routing import (
    ALL_PAGES,
    MAX_RANKING_ROWS,
    RANKING_PAGE_SIZE,
    ranking_page_count,
)
from ..standings import profession_record_counts
from .common import (
    _CRISIS_CONTRACT_BOSS_SLUG,
    InvestmentView,
    PageHeader,
    PresentationError,
    _bar_width,
    _initial,
    _safe_asset_url,
    _share,
    format_duration,
    format_number,
    investment_view,
    metric_footer,
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
    # 养成, when the record names it; a roster of names only never does.
    investment: InvestmentView | None = None


@dataclass(frozen=True, slots=True)
class RankingRowView:
    rank: int
    account_display_name: str
    character_name: str
    # The four faces, the main C's first; the rest keep the record's order.
    roster: tuple[RosterEntryView, ...]
    # The board's own figure, DPS or rDPS, as a whole number.
    dps: str
    duration: str
    contract_score: str | None


@dataclass(frozen=True, slots=True)
class RankingPage:
    """One page of one board's ranking, or its 全部 view.

    ``record_count`` is what the header says the board holds: the rows the
    filters keep, the whole board without filters, the rDPS board's own
    rows on the rDPS board. A numbered page holds ten of them; the 全部
    view (``page_number`` None) the first thirty, and ``site_only_count``
    is how many more the site lists, for the strip that sends the reader
    there. The page never says whether a next page exists.
    """

    header: PageHeader
    boss_slug: str
    record_count: int
    rows: tuple[RankingRowView, ...]
    # A 危机合约 board is ordered by 合约分数, which takes the time's place.
    show_contract_score: bool
    page_number: int | None
    page_count: int
    site_only_count: int = 0
    # ``dps`` or ``rdps``, and its label beside each row's figure.
    metric: str = "dps"
    metric_label: str = "DPS"
    # The dungeon, unless the board's name already says it.
    dungeon_label: str = ""
    # The resolved filters the rows passed; the page itself names none.
    character_filters: tuple[str, ...] = ()
    character_filter_scope: CharacterFilterScope = CharacterFilterScope.MAIN
    element_filter: str | None = None
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
    # Records fielding this slot at all: the denominator behind the percents,
    # which are shares within the slot, not shares of the board.
    record_count: int = 0


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
    metric_label: str = "DPS"


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
    page: int | str = 1,
    web_base_url: str | None = None,
    character_filter: str | tuple[str, ...] | None = None,
    character_filter_scope: CharacterFilterScope = CharacterFilterScope.MAIN,
    element_filter: str | None = None,
    elements: Mapping[str, str] | None = None,
    profession_filter: str | None = None,
) -> RankingPage:
    """One page of the rows the filters keep, in their upstream order.

    ``page`` is a page number from 1 or ``ALL_PAGES``; a page past the last
    is the caller's to refuse (the recipe answers "共 N 页"), so it is an
    error here. ``character_filter`` is already-resolved names matched as
    ``character_filter_scope`` says; rows keep their board rank after
    filtering.
    """

    if character_filter is None:
        filters: tuple[str, ...] = ()
    elif isinstance(character_filter, str):
        filters = (character_filter,)
    else:
        filters = tuple(character_filter)
    kept = filter_ranking_rows(
        ranking,
        names=filters,
        scope=character_filter_scope,
        element=element_filter,
        elements=elements,
        profession=profession_filter,
    )
    page_count = ranking_page_count(len(kept))
    if page == ALL_PAGES:
        page_number = None
        shown = kept[:MAX_RANKING_ROWS]
    elif (
        isinstance(page, int)
        and not isinstance(page, bool)
        and 1 <= page <= page_count
    ):
        page_number = page
        first = (page - 1) * RANKING_PAGE_SIZE
        shown = kept[first : first + RANKING_PAGE_SIZE]
    else:
        raise PresentationError("ranking page is out of range")
    rdps = is_rdps(ranking.metric)

    def value(row: BossRankingRow) -> float:
        # The rDPS board's number is the row's rDPS; an older row without
        # one falls back to its DPS rather than showing nothing.
        return row.rdps if rdps and row.rdps is not None else row.dps

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
            footer_note=metric_footer(ranking.metric),
            metric=ranking.metric,
        ),
        boss_slug=ranking.boss_slug,
        record_count=len(kept),
        rows=tuple(
            RankingRowView(
                rank=row.rank,
                account_display_name=row.account_display_name,
                character_name=row.character_name,
                roster=_lead_first(row, web_base_url=web_base_url),
                dps=format_number(round(value(row))),
                duration=format_duration(row.duration_ms),
                contract_score=(
                    format_number(row.contract_tag_score)
                    if row.contract_tag_score is not None
                    else None
                ),
            )
            for row in shown
        ),
        show_contract_score=any(
            row.contract_tag_score is not None for row in shown
        ),
        page_number=page_number,
        page_count=page_count,
        site_only_count=(
            len(kept) - len(shown) if page_number is None else 0
        ),
        metric=ranking.metric,
        metric_label=metric_label(ranking.metric),
        dungeon_label="" if _normalised(subtitle) in _normalised(title) else subtitle,
        character_filters=filters,
        character_filter_scope=character_filter_scope,
        element_filter=element_filter,
        profession_filter=profession_filter,
    )


def _lead_first(
    row: BossRankingRow, *, web_base_url: str | None
) -> tuple[RosterEntryView, ...]:
    """The record's four faces with its main C moved to the front.

    A record whose roster does not name its main C still shows that face
    first, drawn from the row's own portrait.
    """

    faces = _build_roster(
        row.roster_entries, row.roster_summary, web_base_url=web_base_url
    )
    lead = [face for face in faces if face.character_name == row.character_name]
    rest = [face for face in faces if face.character_name != row.character_name]
    if not lead and row.character_name:
        lead = [
            RosterEntryView(
                character_name=row.character_name,
                profession=row.character_profession or "",
                character_initial=_initial(row.character_name),
                avatar_url=_safe_asset_url(
                    row.character_avatar_url, base_url=web_base_url
                ),
            )
        ]
    return tuple(lead[:1] + rest)


def _normalised(text: str) -> str:
    return "".join(text.split()).replace("·", "").replace("・", "")


_MAX_USAGE_ENTRIES = 6


_MAX_COMBOS = 5


_MAX_MAIN_CHARACTERS = 8


_PROFESSION_ORDER = ("近卫", "重装", "辅助", "突击", "术士", "先锋")


def build_roster_page(
    ranking: BossRanking,
    *,
    query: str,
    web_base_url: str | None = None,
) -> RosterPage:
    """Profession usage (whole board) plus the first ten records' teams and
    main Cs; the count is fixed since 阵容 lost ``--top``."""

    is_crisis_contract = ranking.boss_slug == _CRISIS_CONTRACT_BOSS_SLUG
    title = "危机合约" if is_crisis_contract else ranking.boss_name
    subtitle = "活动竞速" if is_crisis_contract else ranking.dungeon_name
    matched_name = (
        "危机合约"
        if is_crisis_contract
        else f"{ranking.dungeon_name} · {ranking.boss_name}"
    )
    sample_rows = ranking.rows[:RANKING_PAGE_SIZE]
    sample_size = len(sample_rows)

    fielding = profession_record_counts(ranking)
    profession_usage = []
    for group in ranking.profession_groups:
        entries = group.entries[:_MAX_USAGE_ENTRIES]
        peak = max((entry.usage_percent for entry in entries), default=0.0)
        profession_usage.append(
            ProfessionUsageView(
                record_count=fielding.get(group.profession, 0),
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
            footer_note=metric_footer(ranking.metric),
            metric=ranking.metric,
        ),
        row_count=len(ranking.rows),
        sample_size=sample_size,
        profession_usage=tuple(profession_usage),
        combos=combos,
        main_characters=main_characters,
        metric_label=metric_label(ranking.metric),
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
    icons: Mapping[str, str] | None = None,
    investment_of: str | None = None,
) -> tuple[RosterEntryView, ...]:
    """The four faces of one record.

    A ranking row names its roster and carries a portrait for each; a
    record on a board the index does not hold arrives as names only, and
    ``icons`` (the game-data catalog) is where those faces come from.
    Every face carries its 养成, or only the one named ``investment_of``.
    """

    known = elements or {}
    portraits = icons or {}

    def portrait(name: str, given: str | None) -> str | None:
        return _safe_asset_url(given or portraits.get(name), base_url=web_base_url)

    if entries:
        return tuple(
            RosterEntryView(
                character_name=entry.character_name,
                profession=entry.profession,
                character_initial=_initial(entry.character_name),
                avatar_url=portrait(entry.character_name, entry.avatar_url),
                element_key=element_key(known.get(entry.character_name)),
                investment=(
                    investment_view(entry.character_potential, entry.weapon_refine)
                    if investment_of in (None, entry.character_name)
                    else None
                ),
            )
            for entry in entries
        )
    return tuple(
        RosterEntryView(
            character_name=name,
            profession="",
            character_initial=_initial(name),
            avatar_url=portrait(name, None),
            element_key=element_key(known.get(name)),
        )
        for name in summary
    )


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
