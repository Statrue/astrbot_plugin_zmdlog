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

from collections.abc import Iterable
from dataclasses import dataclass

from .loadout import group_skill_damage, skill_level_summary
from .models import (
    BattleDetailSummary,
    BattleExport,
    BossRanking,
    CharacterBossStatistics,
    CharacterStatistics,
    PublicUserRankings,
)
from .standings import CharacterStandings, CharacterTally
from .telemetry import build_buff_coverage
from .timeline import build_timeline

# One line per record is readable; past this a reader (or a model) stops
# taking anything in and the reply crowds out the rest of the context.
DEFAULT_ROW_LIMIT = 10
MAX_ROW_LIMIT = 30
_TOP_DAMAGE_SOURCES = 3
_TOP_BUFFS = 6
_TOP_USAGE = 6
_TOP_TEAMS = 5


@dataclass(frozen=True, slots=True)
class TeamUsage:
    """How often one team, or one companion, appears in a set of records."""

    label: str
    count: int


def format_board_ranking(
    ranking: BossRanking,
    *,
    limit: int = DEFAULT_ROW_LIMIT,
    character: str | None = None,
) -> str:
    """The top runs of one board, and which characters that board runs.

    ``character`` keeps only the rows fielding that name, which is how the
    "带 X 能排第几" question is answered without claiming X caused the rank.
    """

    rows = ranking.rows
    if character:
        rows = tuple(
            row
            for row in rows
            if any(
                entry.character_name == character for entry in row.roster_entries
            )
        )
    lines = [f"榜单：{ranking.dungeon_name} · {ranking.boss_name}（DPS 口径）"]
    if character:
        lines.append(
            f"筛选：阵容包含「{character}」，"
            f"{len(rows)} / {len(ranking.rows)} 条公开记录"
        )
    else:
        lines.append(f"公开记录 {len(ranking.rows)} 条")
    if not rows:
        lines.append(
            "没有符合的公开记录。"
            if character
            else "这个榜目前没有公开记录。"
        )
        return _joined(lines)
    lines.append("")
    for row in rows[: _bounded(limit)]:
        team = "、".join(entry.character_name for entry in row.roster_entries)
        lines.append(
            f"#{row.rank} {_duration(row.duration_ms)} · DPS {row.dps:,.0f}"
            f" · 主C {row.character_name} · {row.account_display_name}"
        )
        lines.append(f"    阵容 {team} · battleId {row.battle_id}")
    usage = _profession_usage(ranking)
    if usage:
        lines.append("")
        lines.append("全榜出场率（按职业位，上游统计）：")
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


def format_board_teams(ranking: BossRanking, *, limit: int = _TOP_TEAMS) -> str:
    """The team combinations this board's public records actually used."""

    teams = _team_counts(ranking)
    lines = [
        f"榜单：{ranking.dungeon_name} · {ranking.boss_name}",
        f"公开记录 {len(ranking.rows)} 条，不同阵容 {len(teams)} 种",
    ]
    if not teams:
        lines.append("这个榜目前没有公开记录。")
        return _joined(lines)
    lines.append("")
    for team, count in teams[:limit]:
        lines.append(f"{count} 次 · {team}")
    if len(teams) > limit:
        lines.append(f"（另有 {len(teams) - limit} 种阵容各出现较少次数）")
    return _joined(lines)


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
) -> str:
    """Everything one public battle recorded, already reduced to facts."""

    lines = [
        f"战报 {battle.battle_id}",
        f"{battle.dungeon_name} · {battle.boss_name}",
        f"用时 {_duration(battle.duration_ms)} · 全队 DPS {battle.total_dps:,.0f}"
        f" · 总伤害 {battle.total_damage:,}",
        f"上传者 {battle.uploader_display_name} · 战斗时间 {battle.battle_end_at}",
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
            lines.append(
                f"    {participant.character_name}"
                f" · DPS {participant.dps:,.0f}"
                f" · 伤害占比 {share:.1%}"
            )
    roster = _roster_lines(battle)
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
    gear = _gear_differences(first, second, label_a=label_a, label_b=label_b)
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
    scope = stats.boss_name or "全部副本"
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
) -> str:
    """Every character's first places, podiums and top tens over all boards."""

    as_of = ""
    if age_seconds is not None:
        minutes = int(age_seconds // 60)
        as_of = "（数据截至刚才）" if minutes < 1 else f"（数据截至 {minutes} 分钟前）"
    lines = [
        f"全部 {board_count} 个榜的第一名记录里各角色各占几个{as_of}",
        "「冠军」= 该榜第一名记录的队伍里带这个角色，四名角色各算一个；"
        "「当主C」= 其中该角色是主C的。",
        "",
    ]
    if not tallies:
        lines.append("读过的榜单里没有任何公开记录。")
        return _joined(lines)
    top_team = max(tallies, key=lambda t: t.first_places)
    top_main = max(tallies, key=lambda t: t.first_places_as_main)
    lines.append(
        f"冠军最多：{top_team.name} {top_team.first_places} 个榜；"
        f"当主C的冠军最多：{top_main.name} {top_main.first_places_as_main} 个榜。"
    )
    lines.append("")
    shown = tallies[: _bounded(limit)]
    for tally in shown:
        lines.append(
            f"{tally.name} · 冠军 {tally.first_places}"
            f"（当主C {tally.first_places_as_main}）"
            f" · 前三 {tally.podiums} · 前十 {tally.top_tens}"
            f" · 上榜 {tally.boards} 个榜"
        )
    if len(tallies) > len(shown):
        lines.append(f"（另有 {len(tallies) - len(shown)} 个角色未列出，图里有）")
    lines.append("")
    lines.append("以上是队伍成绩的计数，不代表哪个角色更强。")
    return _joined(lines)


def format_account(account: PublicUserRankings, *, limit: int = MAX_ROW_LIMIT) -> str:
    """One public account's best record on each board."""

    lines = [f"公开账号 {account.account_display_name}（{account.account_id}）"]
    if not account.rankings:
        lines.append("这个账号目前没有公开的榜单记录。")
        return _joined(lines)
    lines.append(f"上榜 {len(account.rankings)} 个副本")
    lines.append("")
    ordered = sorted(account.rankings, key=lambda row: row.rank)
    for row in ordered[: _bounded(limit)]:
        team = "、".join(row.roster_summary) if row.roster_summary else "未记录"
        lines.append(
            f"#{row.rank} {row.dungeon_name} · {row.boss_name}"
            f" · {_duration(row.duration_ms)} · DPS {row.total_dps:,.0f}"
        )
        lines.append(f"    阵容 {team} · battleId {row.battle_id}")
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
    lines = []
    for group in ranking.profession_groups:
        entries = [entry for entry in group.entries if entry.usage_percent > 0]
        if not entries:
            continue
        shown = ", ".join(
            f"{entry.character_name} {entry.usage_percent:.0f}%"
            for entry in entries[:_TOP_USAGE]
        )
        lines.append(f"    {group.profession}：{shown}")
    return lines


def _roster_lines(battle: BattleDetailSummary) -> list[str]:
    lines = []
    for entry in sorted(battle.roster, key=lambda item: item.slot):
        parts = [entry.character_name]
        if entry.character_level:
            parts.append(f"Lv.{entry.character_level}")
        if entry.character_potential is not None:
            parts.append(f"潜能{entry.character_potential}")
        if entry.weapon is not None:
            weapon = entry.weapon.name
            if entry.weapon.refine:
                weapon += f"·精炼{entry.weapon.refine}"
            parts.append(weapon)
        levels = skill_level_summary(entry)
        if levels:
            parts.append(
                " ".join(f"{level.label}{level.level}" for level in levels)
            )
        lines.append("    " + " · ".join(parts))
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
        weapon_a = left.weapon.name if left.weapon else None
        weapon_b = right.weapon.name if right.weapon else None
        if weapon_a != weapon_b:
            differences.append(f"武器 {label_a}{weapon_a}/{label_b}{weapon_b}")
        elif left.weapon and right.weapon and left.weapon.refine != right.weapon.refine:
            differences.append(
                f"精炼 {label_a}{left.weapon.refine}/{label_b}{right.weapon.refine}"
            )
        items_a = sorted(item.item_id or "" for item in left.equips)
        items_b = sorted(item.item_id or "" for item in right.equips)
        if items_a != items_b:
            differences.append("装备不同")
        if differences:
            lines.append(f"    {name}：{'；'.join(differences)}")
    return lines or ["    两边同名角色的养成与装备一致。"]
