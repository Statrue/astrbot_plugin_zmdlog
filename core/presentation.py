"""Image-template view models for public ZMDLogs ranking data."""

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urljoin, urlsplit

from .matcher import MatchChoice, TargetType
from .models import (
    BattleDetailSummary,
    BossRanking,
    BossRankingRosterEntry,
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


@dataclass(frozen=True, slots=True)
class RankingPage:
    header: PageHeader
    boss_slug: str
    row_count: int
    show_contract_score: bool
    rows: tuple[RankingRowView, ...]


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
) -> RankingPage:
    """Build the first public DPS rows in their upstream order."""

    if isinstance(display_limit, bool) or not isinstance(display_limit, int):
        raise PresentationError("ranking display limit must be an integer")
    if not MIN_RANKING_TOP <= display_limit <= MAX_RANKING_TOP:
        raise PresentationError("ranking display limit must be between 1 and 30")

    displayed_rows = ranking.rows[:display_limit]
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
            )
            for row in displayed_rows
        ),
    )


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
