"""Compact text answers about what the public records contain.

This is the layer behind the LLM tools. It reports **what is in the data**
and nothing else: counts, times, distributions, what a battle recorded. It
never says which character or team is better, because the leaderboard is a
self-selected sample of successful speed runs by stronger players, so any
comparison across runs measures the players as much as the subject.

Two rules shape every function here, both learned the hard way:

* Return facts that are already computed, never a raw array. A six-minute
  battle carries 1868 telemetry events; a model handed those restates them
  wrongly. The caller's job is to select and narrate, not to add up.
* Say plainly when the data does not cover the question. A short "没有记录"
  is a better answer than a plausible one assembled from what is nearby.

Every string that reaches these functions from upstream (nicknames, boss
names, skill names) is data written by other people. It is quoted, never
obeyed; the tool docstrings tell the model the same.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .characters import CharacterFilterScope, row_fields
from .events import CHAMPION_CHANGE, NEW_RECORD, BoardActivity, RecordEvent
from .history import AccountHistory, trend_points
from .loadout import (
    group_skill_damage,
    is_raw_item_name,
    skill_level_summary,
    suit_catalog_id,
)
from .models import (
    BattleDetailSummary,
    BattleEquip,
    BattleExport,
    BattleRosterEntry,
    BossRanking,
    BossRankingRow,
    CharacterBossStatistics,
    CharacterStatistics,
    HotBossCard,
    PublicUserRankings,
)
from .professions import normalize_profession
from .standings import (
    AccountTally,
    CharacterStandings,
    CharacterTally,
    ProfessionUsage,
    TeamTally,
    profession_record_counts,
)
from .telemetry import build_buff_coverage
from .timeline import build_timeline
from .timestamps import parse_timestamp

# One line per record is readable; past this a reader (or a model) stops
# taking anything in and the reply crowds out the rest of the context.
DEFAULT_ROW_LIMIT = 10
MAX_ROW_LIMIT = 30
_TOP_DAMAGE_SOURCES = 3
_TOP_BUFFS = 6
_TOP_USAGE = 6
_TOP_TEAMS = 5
_NO_RECORDS_READ = "读过的榜单里没有任何公开记录。"
# Boards named in an overview past this many print their leader only.
_OVERVIEW_RUNS = 3


def format_board_ranking(
    ranking: BossRanking,
    *,
    limit: int = DEFAULT_ROW_LIMIT,
    character: str | None = None,
    character_scope: CharacterFilterScope = CharacterFilterScope.ROSTER,
    element: str | None = None,
    elements: Mapping[str, str] | None = None,
    profession: str | None = None,
    since: datetime | None = None,
    window_label: str = "",
    events: tuple[RecordEvent, ...] = (),
) -> str:
    """The top runs of one board, and which characters that board runs.

    ``character`` keeps only the rows fielding that name (as main C when
    ``character_scope`` says so), which is how "带 X 能排第几" is answered
    without claiming X caused the rank. ``since`` keeps the records fought
    inside a window and adds what the event log saw on this board in it.
    """

    rows = ranking.rows
    filters = []
    if character:
        rows = tuple(
            row for row in rows if row_fields(row, (character,), character_scope)
        )
        filters.append(
            f"主C 为「{character}」"
            if character_scope is CharacterFilterScope.MAIN
            else f"阵容包含「{character}」"
        )
    if element:
        known = elements or {}
        rows = tuple(row for row in rows if known.get(row.character_name) == element)
        filters.append(f"主C 为{element}属性")
    if profession:
        rows = tuple(
            row
            for row in rows
            if normalize_profession(row.character_profession or "") == profession
        )
        filters.append(f"主C 为{profession}")
    if since is not None:
        rows = tuple(
            row
            for row in rows
            if (when := parse_timestamp(row.battle_end_at)) is not None
            and when >= since
        )
        filters.append(f"{window_label}打出的记录（名次仍是全榜名次）")
    lines = [f"榜单：{ranking.dungeon_name} · {ranking.boss_name}（DPS 口径）"]
    if filters:
        lines.append(
            f"筛选：{'，'.join(filters)}，{len(rows)} / {len(ranking.rows)} 条公开记录"
        )
    else:
        lines.append(f"公开记录 {len(ranking.rows)} 条")
    if not rows:
        lines.append(
            "没有符合的公开记录。"
            if filters
            else "这个榜目前没有公开记录。"
        )
        return _joined(lines)
    lines.append(_duration_summary(rows))
    if since is not None:
        lines.extend(_board_window_events(events))
    lines.append("")
    leader = ranking.rows[0]
    for row in rows[: _bounded(limit)]:
        team = "、".join(entry.character_name for entry in row.roster_entries)
        gap = ""
        if row.rank > 1 and row.duration_ms > leader.duration_ms:
            gap = f" · 落后第一 {(row.duration_ms - leader.duration_ms) / 1000:.2f} 秒"
        lines.append(
            f"#{row.rank} {_duration(row.duration_ms)} · DPS {row.dps:,.0f}"
            f" · 主C {row.character_name} · {row.account_display_name}"
            f" · {_date(row.battle_end_at)}{gap}"
        )
        lines.append(f"    阵容 {team} · battleId {row.battle_id}")
    if len(rows) > _bounded(limit):
        lines.append(f"（另有 {len(rows) - _bounded(limit)} 条未列出，图里有）")
    usage = _profession_usage(ranking)
    if usage:
        lines.append("")
        lines.append(
            "各职业位的角色占比（百分比的分母是带这个位的记录数，不是全部记录；"
            "很多队伍不带某些位）："
        )
        lines.extend(usage)
    if character:
        # The filtered rows above are every team fielding the character; this
        # is the same information counted, so the model need not count.
        lines.append("")
        lines.extend(_cooccurrence_lines(_cooccurrence((ranking,), character)))
    else:
        teams = _team_counts(ranking)
        lines.append("")
        lines.append(f"常见阵容（全榜 {len(teams)} 种不同阵容）：")
        for team, count in teams[:_TOP_TEAMS]:
            lines.append(f"    {count} 次 · {team}")
        if len(teams) > _TOP_TEAMS:
            lines.append(f"    （另有 {len(teams) - _TOP_TEAMS} 种各出现较少次数）")
    return _joined(lines)


def format_boards_overview(
    cards: tuple[HotBossCard, ...],
    *,
    title: str,
    runs_per_board: int = _OVERVIEW_RUNS,
) -> str:
    """The leaders of several boards at once, from the board list itself."""

    lines = [f"{title}：{len(cards)} 个榜单（DPS 口径，每榜最快的记录）"]
    if not cards:
        lines.append("目前没有公开榜单。")
        return _joined(lines)
    lines.append("")
    for card in cards:
        if card.dungeon_name == title or card.boss_name.startswith(
            card.dungeon_name.split(" ")[0]
        ):
            # The title already names the dungeon, or the boss name carries it.
            label = card.boss_name
        else:
            label = f"{card.dungeon_name} · {card.boss_name}"
        runs = card.top_speed_runs[: max(1, runs_per_board)]
        if not runs:
            lines.append(f"{label}：暂无公开记录")
            continue
        for position, run in enumerate(runs, start=1):
            prefix = f"{label}：" if position == 1 else "    "
            lines.append(
                f"{prefix}#{position} {_duration(run.duration_ms)}"
                f" · 主C {run.character_name} · {run.uploader_nickname}"
                f" · battleId {run.battle_id}"
            )
    return _joined(lines)


def _duration_summary(rows: tuple[BossRankingRow, ...]) -> str:
    """最快 / 中位 / 平均 of the rows shown, so the model need not add up."""

    durations = sorted(row.duration_ms for row in rows)
    median = durations[len(durations) // 2]
    mean = sum(durations) / len(durations)
    return (
        f"最快 {_duration(durations[0])} · 中位 {_duration(median)}"
        f" · 平均 {_duration(int(mean))}（{len(rows)} 条）"
    )


def _board_window_events(events: tuple[RecordEvent, ...]) -> list[str]:
    """What the event log saw on one board inside a window."""

    changes = [event for event in events if event.kind == CHAMPION_CHANGE]
    fresh = [event for event in events if event.kind == NEW_RECORD]
    lines = [f"这段时间索引发现新上传 {len(fresh)} 条，第一名易主 {len(changes)} 次"]
    for event in changes[:3]:
        lines.append(
            f"    {event.account_display_name}（主C {event.character_name}，"
            f"{_duration(event.duration_ms)}）"
            f"顶掉 {event.previous_account_display_name}"
            f"（{_duration(event.previous_duration_ms)}）· battleId {event.battle_id}"
        )
    return lines


def format_character_partners(
    rankings: Iterable[BossRanking],
    character: str,
    *,
    limit: int = _TOP_USAGE,
) -> str:
    """Who shares a team with ``character`` in the records that were read.

    Counting only. A frequent partner is what people bring, which is not
    evidence that the pair is strong.
    """

    co = _cooccurrence(rankings, character)
    if not co.appearances:
        return with_source(
            f"读过的 {co.total} 条公开记录里没有「{character}」的出场，"
            "可能是名字不对，或这个范围内没人用。"
        )
    lines = [
        f"「{character}」在读过的 {co.total} 条公开记录里出场 {co.appearances} 次，"
        f"分布在 {len(co.boards)} 个榜单。",
        "",
    ]
    lines.extend(_cooccurrence_lines(co, limit=limit))
    return _joined(lines)


@dataclass(frozen=True, slots=True)
class _Cooccurrence:
    """Who one character shared a team with, counted over some rankings."""

    total: int
    appearances: int
    boards: frozenset[str]
    partners: tuple[tuple[str, int], ...]
    teams: tuple[tuple[str, int], ...]


def _cooccurrence(rankings: Iterable[BossRanking], character: str) -> _Cooccurrence:
    partners: dict[str, int] = {}
    teams: dict[str, int] = {}
    appearances = total = 0
    boards: set[str] = set()
    for ranking in rankings:
        for row in ranking.rows:
            total += 1
            names = [entry.character_name for entry in row.roster_entries]
            if character not in names:
                continue
            appearances += 1
            boards.add(ranking.boss_name)
            key = "、".join(sorted(names))
            teams[key] = teams.get(key, 0) + 1
            for name in names:
                if name != character:
                    partners[name] = partners.get(name, 0) + 1
    return _Cooccurrence(
        total=total,
        appearances=appearances,
        boards=frozenset(boards),
        partners=_ranked(partners),
        teams=_ranked(teams),
    )


def _cooccurrence_lines(co: _Cooccurrence, *, limit: int = _TOP_USAGE) -> list[str]:
    lines = ["最常同队："]
    lines.extend(f"    {count} 次 · {name}" for name, count in co.partners[:limit])
    lines.append("")
    lines.append("最常见的完整阵容：")
    lines.extend(f"    {count} 次 · {team}" for team, count in co.teams[:limit])
    lines.append("")
    lines.append("以上只是出场次数，不代表这些角色或组合更强。")
    return lines


def _team_counts(ranking: BossRanking) -> tuple[tuple[str, int], ...]:
    teams: dict[str, int] = {}
    for row in ranking.rows:
        names = sorted(entry.character_name for entry in row.roster_entries)
        if names:
            key = "、".join(names)
            teams[key] = teams.get(key, 0) + 1
    return _ranked(teams)


def _ranked(counts: dict[str, int]) -> tuple[tuple[str, int], ...]:
    """Most frequent first, ties by name so the order is stable."""

    return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def format_battle(
    battle: BattleDetailSummary,
    *,
    export: BattleExport | None = None,
    suits: Mapping[str, str] | None = None,
) -> str:
    """Everything one public battle recorded, already reduced to facts."""

    lines = [
        f"战报 {battle.battle_id}",
        f"{battle.dungeon_name} · {battle.boss_name}",
        f"用时 {_duration(battle.duration_ms)} · 全队 DPS {battle.total_dps:,.0f}"
        f" · 总伤害 {battle.total_damage:,}",
        f"上传者 {battle.uploader_display_name}"
        f" · 战斗时间 {_when(battle.battle_end_at)}",
    ]
    if battle.participants:
        lines.append("")
        lines.append("参战角色（按伤害排序）：")
        total = sum(p.total_damage for p in battle.participants) or 1
        ordered = sorted(
            battle.participants, key=lambda p: p.total_damage, reverse=True
        )
        for participant in ordered:
            share = participant.total_damage / total
            extra = ""
            if participant.crit_rate is not None:
                extra += f" · 暴击率 {participant.crit_rate:.0%}"
            if participant.max_hit is not None:
                extra += f" · 最大单次 {participant.max_hit:,}"
            lines.append(
                f"    {participant.character_name}"
                f" · DPS {participant.dps:,.0f}"
                f" · 伤害占比 {share:.1%}{extra}"
            )
    roster = _roster_lines(battle, suits)
    if roster:
        lines.append("")
        lines.append("配装与养成：")
        lines.extend(roster)
    sources = _damage_sources(battle)
    if sources:
        lines.append("")
        lines.append("主要伤害来源：")
        lines.extend(sources)
    buffs = _buff_lines(battle)
    if buffs:
        lines.append("")
        lines.append("BUFF 覆盖（占全场时长）：")
        lines.extend(buffs)
    if export is not None:
        casts = _cast_lines(export)
        if casts:
            lines.append("")
            lines.append("施放次数：")
            lines.extend(casts)
        opening = _opening_lines(export)
        if opening:
            lines.append("")
            lines.append("开场顺序（每人前几个动作）：")
            lines.extend(opening)
    elif not battle.roster:
        lines.append("")
        lines.append("这份战报由旧版客户端上传，没有记录阵容配装。")
    return _joined(lines)


def format_battle_comparison(
    first: BattleDetailSummary,
    second: BattleDetailSummary,
    *,
    label_a: str = "A",
    label_b: str = "B",
    suits: Mapping[str, str] | None = None,
) -> str:
    """Two battles of the same boss, with the differences named.

    Only differences that are in the records: time, damage, who was on the
    team, what they wore. Why one is faster is not in the data.
    """

    if first.boss_name != second.boss_name:
        return with_source(
            f"「{first.boss_name}」和「{second.boss_name}」不是同一个首领，"
            "每个首领的机制和排轴都不同，两场没有可比性。"
        )
    lines = [
        f"对比 {first.boss_name}",
        f"{label_a} {first.battle_id} · {first.uploader_display_name}",
        f"{label_b} {second.battle_id} · {second.uploader_display_name}",
        "",
        f"用时 {label_a} {_duration(first.duration_ms)}"
        f" / {label_b} {_duration(second.duration_ms)}"
        f"（相差 {abs(first.duration_ms - second.duration_ms) / 1000:.2f} 秒）",
        f"全队 DPS {label_a} {first.total_dps:,.0f}"
        f" / {label_b} {second.total_dps:,.0f}",
        f"总伤害 {label_a} {first.total_damage:,}"
        f" / {label_b} {second.total_damage:,}",
    ]
    names_a = {p.character_name for p in first.participants}
    names_b = {p.character_name for p in second.participants}
    shared = sorted(names_a & names_b)
    if names_a - names_b or names_b - names_a:
        lines.append("")
        lines.append(
            f"阵容差异：{label_a} 独有 {'、'.join(sorted(names_a - names_b)) or '无'}"
            f"；{label_b} 独有 {'、'.join(sorted(names_b - names_a)) or '无'}"
        )
    dps_a = {p.character_name: p.dps for p in first.participants}
    dps_b = {p.character_name: p.dps for p in second.participants}
    if shared:
        lines.append("")
        lines.append("同名角色的 DPS：")
        for name in shared:
            lines.append(
                f"    {name} · {label_a} {dps_a[name]:,.0f}"
                f" / {label_b} {dps_b[name]:,.0f}"
            )
    gear = _gear_differences(
        first, second, label_a=label_a, label_b=label_b, suits=suits
    )
    if gear:
        lines.append("")
        lines.append("配装差异：")
        lines.extend(gear)
    lines.append("")
    lines.append(
        "以上是两份记录的差异本身。哪一处造成了时间差，公开数据无法判定。"
    )
    return _joined(lines)


def format_character_statistics(
    stats: CharacterStatistics,
    *,
    character: str | None = None,
    limit: int = DEFAULT_ROW_LIMIT,
) -> str:
    """The DPS distribution of six-star characters, as upstream computes it."""

    rows = [row for row in stats.rows if row.sample_count]
    if character:
        rows = [row for row in rows if row.character_name == character]
        if not rows:
            return with_source(
                f"「{character}」在这个范围内没有样本，"
                "可能是名字不对，或它还没有公开记录。"
            )
    scope = stats.boss_name or "全部榜单"
    lines = [
        f"角色 DPS 分布（{scope}，范围 {stats.range} · 潜能 {stats.potential}）",
        "名次只看去极值后的正常样本，正常样本不足的角色没有名次"
        "（记录多但分布很散时也会这样）。",
        "",
    ]
    ordered = sorted(
        rows,
        key=lambda row: (row.rank is None, row.rank or 0, -row.median),
    )
    for row in ordered[: _bounded(limit)]:
        rank = f"#{row.rank}" if row.rank is not None else "样本不足"
        lines.append(
            f"{rank} {row.character_name}（{row.character_profession}）"
            f" · 中位 DPS {_stat(row.median)}"
            f" · 四分位 {_stat(row.p25)}–{_stat(row.p75)}"
            f" · {_samples(row.sample_count, row.normal_sample_count)}"
        )
    return _joined(lines)


def format_character_boards(
    stats: CharacterBossStatistics,
    *,
    limit: int = DEFAULT_ROW_LIMIT,
) -> str:
    """One character's standing on every board that has statistics."""

    rows = [row for row in stats.rows if row.sample_count]
    lines = [
        f"「{stats.character_name}」各榜单表现"
        f"（范围 {stats.range} · 潜能 {stats.potential}）",
    ]
    if not rows:
        lines.append("这个角色在任何榜单上都还没有足够的公开样本。")
        return _joined(lines)
    lines.append("")
    ordered = sorted(rows, key=lambda row: (row.rank is None, row.rank or 0))
    shown = ordered[: _bounded(limit)]
    for row in shown:
        rank = (
            f"#{row.rank}/{row.ranked_character_count}"
            if row.rank is not None
            else "样本不足"
        )
        lines.append(
            f"{row.boss_name}"
            f" · {rank} · 中位 DPS {_stat(row.median)}"
            f" · {_samples(row.sample_count, row.normal_sample_count)}"
        )
    if len(ordered) > len(shown):
        lines.append(f"（另有 {len(ordered) - len(shown)} 个榜未列出，图里有）")
    return _joined(lines)


# Absent boards are named up to this many; past it the page carries the list.
MAX_ABSENT_NAMED = 12


def format_character_standings(
    standings: CharacterStandings,
    *,
    limit: int = DEFAULT_ROW_LIMIT,
    age_seconds: float | None = None,
) -> str:
    """The best record fielding one character on each board, best rank first.

    These are a team's results, not the character's: the rank is the
    record's rank among every record on that board.
    """

    name = standings.character
    as_of = ""
    if age_seconds is not None:
        minutes = int(age_seconds // 60)
        as_of = "（数据截至刚才）" if minutes < 1 else f"（数据截至 {minutes} 分钟前）"
    lines = [f"带「{name}」的队伍在各榜单的最好名次{as_of}"]
    if not standings.boards:
        lines.append(f"读过的 {len(standings.absent)} 个榜里没有「{name}」出场。")
        return _joined(lines)
    lines.append(
        f"出场 {standings.appearances} 次，分布在 {len(standings.boards)} 个榜；"
        f"{len(standings.absent)} 个榜没有出场。"
    )
    # "哪几个榜没上" is a real question (2026-09-06: the model had to answer
    # 「具体哪 5 个我这边没列出来」); a short list is named, a long one is
    # on the page.
    if 0 < len(standings.absent) <= MAX_ABSENT_NAMED:
        lines.append(
            "没有出场的榜：" + "、".join(board.boss_name for board in standings.absent)
        )
    elif standings.absent:
        lines.append("没有出场的榜太多，名单见图。")
    # "有多少冠军" is answered here, not by counting the lines below, which
    # stop at ``limit``.
    lines.append(
        f"冠军（第一名）{standings.first_places} 个榜"
        f"（其中 {name} 当主C {standings.first_places_as_main} 个）"
        f" · 前三 {len(standings.boards_within(3))} 个榜"
        f" · 前十 {len(standings.boards_within(10))} 个榜。"
    )
    # "有哪些冠军" needs every board named, and the detailed rows below stop
    # at ``limit``; a compact list is bounded by the number of boards.
    firsts = standings.boards_within(1)
    if firsts:
        lines.append(
            "第一名的榜："
            + "、".join(
                board.boss_name + ("" if board.best_as_main else "（队员）")
                for board in firsts
            )
        )
    runners_up = [
        board for board in standings.boards_within(3) if board.best.rank > 1
    ]
    if runners_up:
        lines.append(
            "第二、三名的榜："
            + "、".join(
                f"{board.boss_name} #{board.best.rank}" for board in runners_up
            )
        )
    lines.append("")
    for board in standings.boards[: _bounded(limit)]:
        row = board.best
        team = "、".join(entry.character_name for entry in row.roster_entries)
        lead = (
            f"主C {name}"
            if board.best_as_main
            else f"主C {row.character_name}（{name} 为队员）"
        )
        lines.append(
            f"#{row.rank}/{board.total_rows} {board.boss_name}"
            f" · {_duration(row.duration_ms)} · DPS {row.dps:,.0f}"
            f" · {lead} · {row.account_display_name}"
        )
        lines.append(
            f"    阵容 {team} · battleId {row.battle_id}"
            f" · 该榜带它的记录 {board.appearances} 条"
        )
    if len(standings.boards) > _bounded(limit):
        lines.append(f"（另有 {len(standings.boards) - _bounded(limit)} 个榜未列出）")
    lines.append("")
    lines.append("以上是带该角色的队伍的成绩，不是角色本身的强度；名次受玩家水平和配装影响。")
    return _joined(lines)


def format_character_tallies(
    tallies: tuple[CharacterTally, ...],
    *,
    board_count: int,
    limit: int = DEFAULT_ROW_LIMIT,
    age_seconds: float | None = None,
    element: str | None = None,
    profession: str | None = None,
    teams: tuple[TeamTally, ...] = (),
    usage: tuple[ProfessionUsage, ...] = (),
    window_label: str = "",
    unseen: tuple[str, ...] = (),
) -> str:
    """Every character's first places, podiums and top tens over all boards.

    With ``profession`` the board is one class: every member is listed,
    zeros included, and the fewest are named. ``unseen`` are the catalog's
    characters (of that class) no public record fields.
    """

    as_of = ""
    if age_seconds is not None:
        minutes = int(age_seconds // 60)
        as_of = "（数据截至刚才）" if minutes < 1 else f"（数据截至 {minutes} 分钟前）"
    who = _who(element, profession)
    noun = "角色" if element is None and profession is None else who
    scope = (
        f"全部 {board_count} 个榜的第一名记录"
        if not window_label
        else f"{window_label}各榜最快记录（{board_count} 个榜）"
    )
    lines = [
        f"{scope}里{who}各占几个{as_of}",
        "「冠军」= 该榜第一名记录的队伍里带这个角色，四名角色各算一个；"
        "「当主C」= 其中该角色是主C的。",
        "",
    ]
    if not tallies:
        if element is None and profession is None:
            lines.append(_NO_RECORDS_READ)
        else:
            lines.append(f"公开记录里没有{who}出场。")
        if unseen:
            lines.append(f"从未出现在公开记录里的{noun}：{'、'.join(unseen)}")
        return _joined(lines)
    top_team = max(tallies, key=lambda t: t.first_places)
    top_main = max(tallies, key=lambda t: t.first_places_as_main)
    lines.append(
        f"冠军最多：{top_team.name} {top_team.first_places} 个榜；"
        f"当主C的冠军最多：{top_main.name} {top_main.first_places_as_main} 个榜。"
    )
    lines.append(
        f"上榜{noun} {len(tallies)} 个，其中有冠军的"
        f" {sum(1 for t in tallies if t.first_places)} 个。"
    )
    lines.append("")
    if profession is not None:
        # One class is a handful: list every member, zeros included.
        shown, rest_note = tallies, ""
    else:
        shown, rest_note = _champions_cut(tallies, limit, "角色", name_zeros=True)
    for tally in shown:
        lines.append(
            f"{tally.name} · 冠军 {tally.first_places}"
            f"（当主C {tally.first_places_as_main}）"
            f" · 前三 {tally.podiums} · 前十 {tally.top_tens}"
            f" · 上榜 {tally.boards} 个榜"
        )
    if rest_note:
        lines.append(rest_note)
    if profession is not None:
        # The question a class board gets asked (2026-09-07: 谁是冠军最少的
        # 突击, answered 没法拍板 while 大潘 and 艾维文娜 sat at zero).
        fewest = min(tally.first_places for tally in tallies)
        least = [tally.name for tally in tallies if tally.first_places == fewest]
        if unseen and fewest > 0:
            fewest, least = 0, list(unseen)
        elif unseen:
            least.extend(unseen)
        lines.append(f"冠军最少：{'、'.join(least)}（{fewest} 个）")
    if unseen:
        lines.append(f"从未出现在公开记录里的{noun}：{'、'.join(unseen)}")
    if teams:
        lines.append("")
        lines.append("最常见的第一名阵容：")
        for team in teams[:_TOP_TEAMS]:
            boards = "、".join(team.boards[:4]) + ("…" if len(team.boards) > 4 else "")
            lines.append(f"    {team.count} 个榜 · {'、'.join(team.names)}（{boards}）")
    if usage:
        lines.append("")
        lines.append("各职业位出场率（带该角色的记录占全部记录的比例）：")
        for group in usage:
            if profession and normalize_profession(group.profession) != profession:
                continue
            chips = ", ".join(
                f"{entry.name} {entry.share:g}%" for entry in group.entries[:4]
            )
            lines.append(f"    {group.profession}：{chips}")
    lines.append("")
    lines.append("以上是队伍成绩的计数，不代表哪个角色更强。")
    return _joined(lines)


def format_records(
    events: tuple[RecordEvent, ...],
    activity: tuple[BoardActivity, ...],
    *,
    window_label: str,
    age_seconds: float | None = None,
    log_since: str | None = None,
    limit: int = DEFAULT_ROW_LIMIT,
) -> str:
    """Champion changes, new records and per-board activity in a window."""

    as_of = ""
    if age_seconds is not None:
        minutes = int(age_seconds // 60)
        as_of = "（数据截至刚才）" if minutes < 1 else f"（数据截至 {minutes} 分钟前）"
    changes = [event for event in events if event.kind == CHAMPION_CHANGE]
    records = [event for event in events if event.kind == NEW_RECORD]
    lines = [f"{window_label}的新纪录{as_of}"]
    if log_since:
        lines.append(f"新纪录流从 {_when(log_since)} 起记录，之前的变化没有。")
    else:
        lines.append("新纪录流刚开始记录，还没有发现任何变化。")
    lines.append("")
    lines.append(f"第一名易主 {len(changes)} 次：")
    for event in changes[: _bounded(limit)]:
        lines.append(
            f"    {event.boss_name}：{event.account_display_name}"
            f"（主C {event.character_name}，{_duration(event.duration_ms)}）"
            f" 顶掉 {event.previous_account_display_name}"
            f"（{_duration(event.previous_duration_ms)}）"
            f" · battleId {event.battle_id}"
        )
    if not changes:
        lines.append("    没有")
    lines.append("")
    per_board: dict[str, int] = {}
    for event in records:
        per_board[event.boss_name] = per_board.get(event.boss_name, 0) + 1
    lines.append(f"新上传的记录 {len(records)} 条，按榜：")
    for name, count in sorted(per_board.items(), key=lambda kv: (-kv[1], kv[0]))[
        : _bounded(limit)
    ]:
        lines.append(f"    {name} {count} 条")
    if not records:
        lines.append("    没有")
    lines.append("")
    lines.append(f"各榜{window_label}打出的记录数（按战斗时间，与日志无关）：")
    for item in [row for row in activity if row.count][: _bounded(limit)]:
        lines.append(f"    {item.boss_name} {item.count} 条（共 {item.total} 条）")
    return _joined(lines)


def format_account_tallies(
    tallies: tuple[AccountTally, ...],
    *,
    board_count: int,
    limit: int = DEFAULT_ROW_LIMIT,
    age_seconds: float | None = None,
    window_label: str = "",
) -> str:
    """Every uploading account's first places, podiums and top tens."""

    as_of = ""
    if age_seconds is not None:
        minutes = int(age_seconds // 60)
        as_of = "（数据截至刚才）" if minutes < 1 else f"（数据截至 {minutes} 分钟前）"
    scope = (
        f"全部 {board_count} 个榜的第一名记录"
        if not window_label
        else f"{window_label}各榜最快记录（{board_count} 个榜）"
    )
    lines = [
        f"{scope}里各玩家各占几个{as_of}",
        "「冠军」= 该榜第一名记录的上传者；前三、前十按该账号在该榜的最好名次算；"
        "只统计设为公开的账号。",
        "",
    ]
    if not tallies:
        lines.append(_NO_RECORDS_READ)
        return _joined(lines)
    top = tallies[0]
    most_records = max(tallies, key=lambda t: t.records)
    lines.append(f"冠军最多：{top.display_name} {top.first_places} 个榜。")
    lines.append(
        f"有公开记录的账号 {len(tallies)} 个，其中有冠军的"
        f" {sum(1 for t in tallies if t.first_places)} 个；"
        f"记录最多：{most_records.display_name} {most_records.records} 条。"
    )
    lines.append("")
    shown, rest_note = _champions_cut(tallies, limit, "账号")
    for tally in shown:
        habits = ""
        if tally.main_c:
            habits = f" · 常用主C {tally.main_c}（{tally.main_c_count} 次）"
        lines.append(
            f"{tally.display_name} · 冠军 {tally.first_places}"
            f" · 前三 {tally.podiums} · 前十 {tally.top_tens}"
            f" · 上榜 {tally.boards} 个榜 · 记录 {tally.records} 条{habits}"
        )
    if rest_note:
        lines.append(rest_note)
    lines.append("")
    lines.append("以上是上传记录的计数，上传得多、打得快的人靠前，不代表别的。")
    return _joined(lines)


# The account tool's rows: past this the page carries the rest.
ACCOUNT_ROW_LIMIT = 20
NO_RANK_HISTORY = (
    "名次变化：这个账号没被任何群关注，没有名次记录；"
    "用 /zmdlog 关注 <昵称或accountId> 可以从下一轮检查起记录。"
)


def format_account(
    account: PublicUserRankings,
    *,
    limit: int = ACCOUNT_ROW_LIMIT,
    habits: AccountTally | None = None,
    since: datetime | None = None,
    window_label: str = "",
) -> str:
    """One public account's best record on each board.

    ``since`` keeps only the best records fought inside a window — the
    endpoint carries one record per board, so this is "which of its bests
    are recent", not every fight of the window.
    """

    lines = [f"公开账号 {account.account_display_name}（{account.account_id}）"]
    if not account.rankings:
        lines.append("这个账号目前没有公开的榜单记录。")
        return _joined(lines)
    lines.append(f"上榜 {len(account.rankings)} 个副本")
    if habits is not None:
        lines.append(
            f"公开记录 {habits.records} 条 · 冠军 {habits.first_places} 个榜"
            f" · 前三 {habits.podiums} · 前十 {habits.top_tens}"
        )
        if habits.main_c:
            lines.append(
                f"常用主C {habits.main_c}（{habits.main_c_count} 次）"
                + (
                    f" · 常用阵容 {'、'.join(habits.team)}（{habits.team_count} 次）"
                    if habits.team
                    else ""
                )
            )
    lines.append("")
    ordered = sorted(account.rankings, key=lambda row: row.rank)
    if since is not None:
        ordered = [
            row
            for row in ordered
            if (when := parse_timestamp(row.battle_end_at)) is not None
            and when >= since
        ]
        lines.append(
            f"{window_label}打出的最好记录 {len(ordered)} 条"
            "（每个榜只有一条最好记录，更早的最好记录不在其中）："
        )
        if not ordered:
            lines.append("    没有")
            return _joined(lines)
    for row in ordered[: _bounded(limit)]:
        team = "、".join(row.roster_summary) if row.roster_summary else "未记录"
        lines.append(
            f"#{row.rank} {row.dungeon_name} · {row.boss_name}"
            f" · {_duration(row.duration_ms)} · DPS {row.total_dps:,.0f}"
            f" · {_date(row.battle_end_at)}"
        )
        lines.append(f"    阵容 {team} · battleId {row.battle_id}")
    if len(ordered) > _bounded(limit):
        lines.append(f"（另有 {len(ordered) - _bounded(limit)} 个榜未列出，图里有）")
    return _joined(lines)


def format_account_trend(
    history: AccountHistory,
    *,
    since: datetime | None = None,
    window_label: str = "",
    last_checked: str | None = None,
    limit: int = DEFAULT_ROW_LIMIT,
) -> str:
    """How a watched account's rank moved, per board, from the watch's trace.

    Only accounts someone 关注'd have a trace, and a point is recorded only
    when the rank moved, so "no change" is a real observation.
    """

    scope = window_label or "有记录以来"
    checked = f"（最近检查 {_when(last_checked)}）" if last_checked else ""
    lines = [f"名次变化（{scope}，来自名次通报的记录）{checked}"]
    boards = []
    for board in history.boards:
        points = trend_points(board, start=since)
        if not points:
            continue
        boards.append((points[-1].rank, board, points))
    if not boards:
        lines.append("    这段时间没有名次记录。")
        return _joined(lines)
    boards.sort(key=lambda item: (item[0], item[1].boss_name))
    for current, board, points in boards[: _bounded(limit)]:
        ranks = [point.rank for point in points]
        moves = sum(1 for a, b in zip(ranks, ranks[1:], strict=False) if a != b)
        if moves == 0:
            lines.append(f"    {board.boss_name}：#{current}，没有变动")
            continue
        lines.append(
            f"    {board.boss_name}：#{ranks[0]} → #{current}"
            f"（最好 #{min(ranks)} · 最差 #{max(ranks)} · 变动 {moves} 次，"
            f"最近一次 {_when(points[-1].checked_at)}）"
        )
    if len(boards) > _bounded(limit):
        lines.append(f"    （另有 {len(boards) - _bounded(limit)} 个榜未列出）")
    return _joined(lines)


# --- pieces -------------------------------------------------------------------


# Every text a tool hands the model ends with where the numbers come from:
# a model that is not told will call a leaderboard count a server-wide one.
SOURCE_NOTE = "数据来源：ZMDLogs 上玩家自愿上传的公开记录，不是全服统计。"


def _joined(lines: list[str]) -> str:
    return with_source("\n".join(lines))


def join_sections(*sections: str) -> str:
    """Several formatter outputs as one answer, the source line once at the end."""

    stripped = [
        section.replace(SOURCE_NOTE, "").rstrip() for section in sections if section
    ]
    return with_source((chr(10) * 2).join(stripped))


def with_source(text: str) -> str:
    """Append the source line once, whatever shape the text has."""

    if SOURCE_NOTE in text:
        return text
    return text.rstrip() + "\n\n" + SOURCE_NOTE


def _champions_cut(tallies, limit: int, noun: str, *, name_zeros: bool = False):
    """Cut a champions list so that every champion is still on it.

    A model reads absence from the list as zero — 伊冯, seventeenth with one
    first place, was reported as having none — so the cut never drops a
    tally with a first place while the cap allows, and the trailer says
    what the cut characters have. ``name_zeros`` names them: a roster is
    some thirty characters, so "who has none" is answerable in one line,
    while the accounts with none are hundreds and stay a count.
    """

    with_first = sum(1 for tally in tallies if tally.first_places > 0)
    keep = max(_bounded(limit), min(with_first, MAX_ROW_LIMIT))
    shown = tallies[:keep]
    rest = tallies[keep:]
    if not rest:
        return shown, ""
    if all(tally.first_places == 0 for tally in rest):
        if name_zeros:
            names = "、".join(tally.name for tally in rest)
            return shown, f"（其余 {len(rest)} 个{noun}冠军 0 个：{names}）"
        return shown, f"（其余 {len(rest)} 个{noun}冠军 0 个，未列出）"
    return shown, f"（另有 {len(rest)} 个{noun}未列出，其中仍有冠军的见图）"


def _who(element: str | None, profession: str | None) -> str:
    """各角色, 自然属性角色, 突击角色, 自然属性突击角色."""

    if element is None and profession is None:
        return "各角色"
    return (f"{element}属性" if element else "") + (profession or "") + "角色"


def _when(value: str) -> str:
    """A stamp in the game's server time (UTC+8), like the pages show it."""

    parsed = parse_timestamp(value)
    if parsed is None:
        return value[:16].replace("T", " ")
    return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def _date(value: str) -> str:
    """The day of a stamp in the game's server time."""

    return _when(value)[:10]


def _stat(value: float | None) -> str:
    """Upstream leaves a quartile null when too few samples survive."""

    return f"{value:,.0f}" if value is not None else "—"


def _samples(sample_count: int, normal_sample_count: int) -> str:
    if normal_sample_count != sample_count:
        return f"样本 {sample_count}（去极值后 {normal_sample_count}）"
    return f"样本 {sample_count}"


def _bounded(limit: int) -> int:
    return max(1, min(int(limit), MAX_ROW_LIMIT))


def _duration(duration_ms: int) -> str:
    seconds = duration_ms / 1000
    if seconds < 60:
        return f"{seconds:.3f} 秒"
    return f"{int(seconds // 60)} 分 {seconds % 60:.3f} 秒"


def _profession_usage(ranking: BossRanking) -> list[str]:
    """Who fills each profession slot, and how many records field it at all.

    Upstream's ``usagePercent`` is a share *within* one slot, so a slot only
    one team in forty used still reads 100% — which a reader (or a model)
    takes for "every team runs this character". The count in front is the
    denominator that makes the percentage safe to quote.
    """

    fielding = profession_record_counts(ranking)
    total = len(ranking.rows)
    lines = []
    for group in ranking.profession_groups:
        entries = [entry for entry in group.entries if entry.usage_percent > 0]
        if not entries:
            continue
        shown = ", ".join(
            f"{entry.character_name} {entry.usage_percent:.0f}%"
            for entry in entries[:_TOP_USAGE]
        )
        count = fielding.get(group.profession, 0)
        lines.append(f"    {group.profession}（{count}/{total} 条记录带）：{shown}")
    return lines



def _roster_lines(
    battle: BattleDetailSummary, suits: Mapping[str, str] | None = None
) -> list[str]:
    lines = []
    for entry in sorted(battle.roster, key=lambda item: item.slot):
        parts = [entry.character_name]
        if entry.character_level:
            parts.append(f"Lv.{entry.character_level}")
        if entry.character_potential is not None:
            parts.append(f"潜能{entry.character_potential}")
        if entry.weapon is not None:
            weapon = _weapon_name(entry)
            if entry.weapon.refine:
                weapon += f"·精炼{entry.weapon.refine}"
            parts.append(weapon)
        suit_summary = _suit_summary(entry, suits)
        if suit_summary:
            parts.append(f"套装 {suit_summary}")
        levels = skill_level_summary(entry)
        if levels:
            parts.append(
                " ".join(f"{level.label}{level.level}" for level in levels)
            )
        lines.append("    " + " · ".join(parts))
    return lines


def _weapon_name(entry: BattleRosterEntry) -> str:
    name = entry.weapon.name.strip() if entry.weapon is not None else ""
    return name or "未知武器"


def _suit_label(equip: BattleEquip, suits: Mapping[str, str] | None) -> str:
    """The suit a piece belongs to, from the catalog first, upstream second."""

    catalog_id = suit_catalog_id(equip.item_id)
    name = (suits or {}).get(catalog_id, "") if catalog_id else ""
    if not name and equip.suit_name:
        name = equip.suit_name.strip()
    if name:
        return name
    return "未收录" if is_raw_item_name(equip.piece_name, equip.item_id) else "散件"


def _suit_summary(
    entry: BattleRosterEntry, suits: Mapping[str, str] | None
) -> str:
    """险关×2 长息×2 — the four pieces counted by suit, most first."""

    if not entry.equips:
        return ""
    counts: dict[str, int] = {}
    for equip in entry.equips:
        label = _suit_label(equip, suits)
        counts[label] = counts.get(label, 0) + 1
    return " ".join(f"{label}×{count}" for label, count in _ranked(counts))


def _opening_lines(export: BattleExport, *, moves: int = 6) -> list[str]:
    """Each character's first few moves in order, from the cast list."""

    timeline = build_timeline(export)
    lines = []
    for lane in timeline.lanes:
        steps = []
        for event in lane.events[:moves]:
            label = event.name if event.count == 1 else f"{event.name}×{event.count}"
            steps.append(f"{event.start_ms / 1000:.1f}s {label}")
        if steps:
            lines.append(f"    {lane.character_name}：{' → '.join(steps)}")
    return lines


def _damage_sources(battle: BattleDetailSummary) -> list[str]:
    lines = []
    for group in group_skill_damage(battle.skill_stats):
        total = group.total_damage or 1
        shown = ", ".join(
            f"{row.name} {row.total_damage / total:.0%}"
            for row in group.rows[:_TOP_DAMAGE_SOURCES]
        )
        if shown:
            lines.append(f"    {group.character_name}：{shown}")
    return lines


def _buff_lines(battle: BattleDetailSummary) -> list[str]:
    coverage = build_buff_coverage(
        battle.buffs,
        duration_ms=battle.duration_ms,
        roster_names=tuple(entry.character_name for entry in battle.roster),
    )
    if coverage is None:
        return []
    lines = []
    duration = coverage.duration_ms or 1
    for row in coverage.rows[:_TOP_BUFFS]:
        target = "敌方减益" if row.on_enemy else "己方增益"
        lines.append(
            f"    {row.name}（{row.effect_label}，{target}）"
            f"{row.covered_ms / duration:.0%}"
        )
    if coverage.hidden_rows:
        lines.append(f"    （另有 {coverage.hidden_rows} 条覆盖较低的增益未列出）")
    return lines


def _cast_lines(export: BattleExport) -> list[str]:
    timeline = build_timeline(export)
    lines = []
    for lane in timeline.lanes:
        counts: dict[str, int] = {}
        for event in lane.events:
            counts[event.category.value] = (
                counts.get(event.category.value, 0) + event.count
            )
        if not counts:
            continue
        shown = ", ".join(
            f"{name}×{count}"
            for name, count in sorted(counts.items(), key=lambda i: -i[1])
        )
        lines.append(f"    {lane.character_name}：{shown}")
    return lines


def _gear_differences(
    first: BattleDetailSummary,
    second: BattleDetailSummary,
    *,
    label_a: str,
    label_b: str,
    suits: Mapping[str, str] | None = None,
) -> list[str]:
    by_a = {entry.character_name: entry for entry in first.roster}
    by_b = {entry.character_name: entry for entry in second.roster}
    lines = []
    for name in sorted(set(by_a) & set(by_b)):
        left, right = by_a[name], by_b[name]
        differences = []
        if left.character_potential != right.character_potential:
            differences.append(
                f"潜能 {label_a}{left.character_potential}"
                f"/{label_b}{right.character_potential}"
            )
        weapon_a = _weapon_name(left) if left.weapon else None
        weapon_b = _weapon_name(right) if right.weapon else None
        if weapon_a != weapon_b:
            differences.append(f"武器 {label_a}{weapon_a}/{label_b}{weapon_b}")
        elif left.weapon and right.weapon and left.weapon.refine != right.weapon.refine:
            differences.append(
                f"精炼 {label_a}{left.weapon.refine}/{label_b}{right.weapon.refine}"
            )
        items_a = sorted(item.item_id or "" for item in left.equips)
        items_b = sorted(item.item_id or "" for item in right.equips)
        if items_a != items_b:
            summary_a = _suit_summary(left, suits) or "未记录"
            summary_b = _suit_summary(right, suits) or "未记录"
            differences.append(f"装备 {label_a} {summary_a} / {label_b} {summary_b}")
        if differences:
            lines.append(f"    {name}：{'；'.join(differences)}")
    return lines or ["    两边同名角色的养成与装备一致。"]
