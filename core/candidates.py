"""Short-lived candidate lists that users pick from by quoting the message."""

import re
import secrets
import time
from dataclasses import dataclass
from enum import Enum

from .matcher import MatchChoice, MatchLevel, MatchTarget, TargetType

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(r"候选编号 ?([A-Z2-9]{4})(?![A-Z2-9])")
_SELECTION_RE = re.compile(r"^\s*(?:选|选择|第)?\s*(\d{1,2})\s*(?:号|个)?\s*$")
DEFAULT_CANDIDATE_TTL_SECONDS = 10 * 60
MAX_CANDIDATES = 5


class CandidateView(str, Enum):
    """Which page a resolved candidate should render."""

    RANKING = "ranking"
    CHARACTER_STATS = "character_stats"
    ROSTER = "roster"
    BATTLE = "battle"
    LOADOUT = "loadout"
    SKILLS = "skills"
    TIMELINE = "timeline"
    COMPARE = "compare"
    WATCH = "watch"
    WATCH_BOARD = "watch_board"
    TREND = "trend"


_VIEW_TITLES = {
    CandidateView.RANKING: "匹配到 {count} 个目标",
    CandidateView.CHARACTER_STATS: "的角色统计匹配到 {count} 个榜单",
    CandidateView.ROSTER: "的阵容查询匹配到 {count} 个榜单",
    CandidateView.BATTLE: "的战报查询匹配到 {count} 个榜单",
    CandidateView.LOADOUT: "的配装查询匹配到 {count} 个榜单",
    CandidateView.SKILLS: "的技能统计查询匹配到 {count} 个榜单",
    CandidateView.TIMELINE: "的技能轴查询匹配到 {count} 个榜单",
    CandidateView.COMPARE: "的战报对比匹配到 {count} 个榜单",
    CandidateView.WATCH: "匹配到 {count} 个公开账号，选一个关注",
    CandidateView.WATCH_BOARD: "匹配到 {count} 个榜单，选一个关注",
    CandidateView.TREND: "匹配到 {count} 个公开账号，选一个看名次趋势",
}


@dataclass(frozen=True, slots=True)
class PendingCandidates:
    code: str
    query: str
    choices: tuple[MatchChoice, ...]
    ranking_top: int | None
    created_at: float
    view: CandidateView = CandidateView.RANKING
    # The chat the list was posted in. A code is only honoured from the same
    # chat, so a code learned elsewhere cannot select on someone else's behalf.
    origin: str = ""
    character_filter: str | None = None
    element_filter: str | None = None
    stats_range: str = "all"
    stats_potential: str = "all"
    battle_rank: int = 1
    compare_rank: int = 2


class CandidateStore:
    """Remember ambiguous results for a while so a quoted reply can pick one."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_CANDIDATE_TTL_SECONDS,
        max_entries: int = 200,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._entries: dict[str, PendingCandidates] = {}

    def remember(
        self,
        query: str,
        choices: tuple[MatchChoice, ...],
        *,
        ranking_top: int | None = None,
        view: CandidateView = CandidateView.RANKING,
        character_filter: str | None = None,
        element_filter: str | None = None,
        stats_range: str = "all",
        stats_potential: str = "all",
        battle_rank: int = 1,
        compare_rank: int = 2,
        origin: str = "",
        now: float | None = None,
    ) -> PendingCandidates:
        timestamp = time.monotonic() if now is None else now
        self._prune(timestamp)
        code = self._new_code()
        entry = PendingCandidates(
            code=code,
            query=query,
            choices=tuple(choices[:MAX_CANDIDATES]),
            ranking_top=ranking_top,
            created_at=timestamp,
            view=view,
            character_filter=character_filter,
            element_filter=element_filter,
            stats_range=stats_range,
            stats_potential=stats_potential,
            battle_rank=battle_rank,
            compare_rank=compare_rank,
            origin=origin,
        )
        self._entries[code] = entry
        return entry

    def resolve(
        self,
        code: str,
        selection: str,
        *,
        origin: str | None = None,
        now: float | None = None,
    ) -> tuple[PendingCandidates, MatchChoice] | None:
        """Pick ``selection`` from the list ``code`` names.

        With ``origin`` given, the list must have been posted in that chat.
        """

        timestamp = time.monotonic() if now is None else now
        self._prune(timestamp)
        entry = self._entries.get(code.upper())
        index = parse_selection(selection)
        if entry is None or index is None or index > len(entry.choices):
            return None
        if origin is not None and entry.origin != origin:
            return None
        return entry, entry.choices[index - 1]

    def _new_code(self) -> str:
        while True:
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
            if code not in self._entries:
                return code

    def _prune(self, now: float) -> None:
        expired = [
            code
            for code, entry in self._entries.items()
            if now - entry.created_at >= self.ttl_seconds
        ]
        for code in expired:
            del self._entries[code]
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries.values(), key=lambda item: item.created_at)
            del self._entries[oldest.code]


def extract_code(text: str | None) -> str | None:
    """Find the ``候选编号 XXXX`` marker inside a quoted candidate message.

    The real marker is always the last line the bot wrote, after the choice
    lines. Those lines carry upstream nicknames, which could themselves spell
    a marker, so the last match wins rather than the first.
    """

    if not text:
        return None
    matches = _CODE_RE.findall(text)
    return matches[-1] if matches else None


def parse_selection(text: str) -> int | None:
    match = _SELECTION_RE.match(text or "")
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= MAX_CANDIDATES else None


def format_candidates(
    entry: PendingCandidates,
    *,
    ttl_seconds: float = DEFAULT_CANDIDATE_TTL_SECONDS,
    note: str | None = None,
) -> str:
    """Compact, phone-friendly candidate list ending with the pick marker."""

    title = _VIEW_TITLES[entry.view].format(count=len(entry.choices))
    lines = [f"「{entry.query}」{title}，引用本条消息回复序号即可："]
    for index, choice in enumerate(entry.choices, start=1):
        lines.append(f"{index}. {describe_choice(choice)}")
    if note:
        lines.append(note)
    minutes = max(1, int(ttl_seconds // 60))
    lines.append(f"候选编号 {entry.code} · {minutes} 分钟内有效")
    return "\n".join(lines)


def describe_choice(choice: MatchChoice) -> str:
    target = choice.target
    if target.target_type is TargetType.ACCOUNT:
        # Upstream nicknames are arbitrary text; keep them on one line so they
        # cannot imitate the list structure.
        return f"{' '.join(target.name.split())} · 公开账号"
    if target.target_type is TargetType.BOARD:
        dungeon = target.dungeon_names[0] if target.dungeon_names else ""
        label = f"{target.name} · 榜单"
        if dungeon and dungeon not in target.name:
            label += f" · {dungeon}"
        return label
    kind = "副本" if target.target_type is TargetType.DUNGEON else "副本系列"
    return f"{target.name} · {kind}（{len(target.boss_slugs)} 个榜单）"


def account_choice(account_id: str, display_name: str, *, query: str) -> MatchChoice:
    """Wrap one public account in the candidate shape the pick list understands."""

    return MatchChoice(
        target=MatchTarget(
            target_type=TargetType.ACCOUNT,
            key=account_id,
            name=display_name,
            dungeon_names=(),
            boss_slugs=(),
            query_text=query,
        ),
        level=MatchLevel.STANDARD_EXACT,
        score=1.0,
        matched_text=display_name,
    )
