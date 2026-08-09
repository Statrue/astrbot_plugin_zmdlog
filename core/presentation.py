"""Image-template view models for public ZMDLogs ranking data."""

from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from .matcher import MatchChoice, TargetType
from .models import (
    BossRanking,
    BossRankingRosterEntry,
    ContractTag,
    HotBossCard,
)

RANKING_DISPLAY_LIMIT = 30


class PresentationError(ValueError):
    """Raised when data cannot be represented safely in a public template."""


@dataclass(frozen=True, slots=True)
class PageHeader:
    title: str
    subtitle: str
    query: str
    matched_name: str
    target_type: str


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
class Top3Page:
    header: PageHeader
    dungeon_names: tuple[str, ...]
    cards: tuple[TopCardView, ...]


@dataclass(frozen=True, slots=True)
class ContractTagView:
    name: str
    score: str


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
    contract_tags: tuple[ContractTagView, ...]
    battle_id: str
    battle_url: str


@dataclass(frozen=True, slots=True)
class RankingPage:
    header: PageHeader
    boss_slug: str
    row_count: int
    rows: tuple[RankingRowView, ...]


def build_all_top3_page(
    cards: tuple[HotBossCard, ...],
    *,
    query: str,
) -> Top3Page:
    """Build the all-board page without exposing ranking-only fields."""

    return Top3Page(
        header=PageHeader(
            title="全部榜单前三名",
            subtitle="当前公开榜单与各榜最快记录",
            query=query,
            matched_name="全部公开榜单",
            target_type="全部榜单",
        ),
        dungeon_names=_unique_dungeon_names(cards),
        cards=tuple(_build_top_card(card) for card in cards),
    )


def build_dungeon_top3_page(
    choice: MatchChoice,
    cards: tuple[HotBossCard, ...],
    *,
    query: str,
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
    return Top3Page(
        header=PageHeader(
            title=f"{label}榜单前三名",
            subtitle=subtitle,
            query=query,
            matched_name=choice.target.name,
            target_type=label,
        ),
        dungeon_names=choice.target.dungeon_names,
        cards=tuple(_build_top_card(card) for card in cards),
    )


def build_ranking_page(
    ranking: BossRanking,
    *,
    query: str,
    web_base_url: str,
) -> RankingPage:
    """Build the first public DPS rows in their upstream order."""

    battle_base_url = _normalise_web_base_url(web_base_url)
    displayed_rows = ranking.rows[:RANKING_DISPLAY_LIMIT]
    return RankingPage(
        header=PageHeader(
            title=ranking.boss_name,
            subtitle=ranking.dungeon_name,
            query=query,
            matched_name=f"{ranking.dungeon_name} · {ranking.boss_name}",
            target_type="具体榜单",
        ),
        boss_slug=ranking.boss_slug,
        row_count=len(ranking.rows),
        rows=tuple(
            RankingRowView(
                rank=row.rank,
                percentile=f"{format_number(row.score_percent)}%",
                account_display_name=row.account_display_name,
                character_name=row.character_name,
                character_profession=row.character_profession,
                character_initial=_initial(row.character_name),
                character_avatar_url=_safe_http_url(
                    row.character_avatar_url,
                ),
                roster=_build_roster(row.roster_entries, row.roster_summary),
                dps=format_number(row.dps),
                duration=format_duration(row.duration_ms),
                contract_score=(
                    format_number(row.contract_tag_score)
                    if row.contract_tag_score is not None
                    else None
                ),
                contract_tags=tuple(
                    _build_contract_tag(tag) for tag in row.contract_tags
                ),
                battle_id=row.battle_id,
                battle_url=(
                    f"{battle_base_url}/battle/"
                    f"{quote(row.battle_id, safe='')}"
                ),
            )
            for row in displayed_rows
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


def _build_top_card(card: HotBossCard) -> TopCardView:
    return TopCardView(
        dungeon_name=card.dungeon_name,
        boss_name=card.boss_name,
        runs=tuple(
            TopRunView(
                rank=index,
                character_name=run.character_name,
                character_initial=_initial(run.character_name),
                character_avatar_url=_safe_http_url(
                    run.character_avatar_url,
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


def _build_contract_tag(tag: ContractTag) -> ContractTagView:
    return ContractTagView(
        name=tag.name or f"合约标签 {tag.tag_id}",
        score=format_number(tag.score),
    )


def _build_roster(
    entries: tuple[BossRankingRosterEntry, ...],
    summary: tuple[str, ...],
) -> tuple[RosterEntryView, ...]:
    if entries:
        return tuple(
            RosterEntryView(
                character_name=entry.character_name,
                profession=entry.profession,
                character_initial=_initial(entry.character_name),
                avatar_url=_safe_http_url(entry.avatar_url),
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


def _initial(value: str) -> str:
    return value[:1] or "?"


def _safe_http_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _normalise_web_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PresentationError("web_base_url must be an absolute HTTP(S) URL")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
