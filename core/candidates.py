"""Short-lived candidate lists that users pick from by quoting the message."""

import re
import secrets
import time
from dataclasses import dataclass

from .matcher import MatchChoice, TargetType

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(r"#Q([A-Z2-9]{4})\b")
_SELECTION_RE = re.compile(r"^\s*(?:选|选择|第)?\s*(\d{1,2})\s*(?:号|个)?\s*$")
DEFAULT_CANDIDATE_TTL_SECONDS = 10 * 60
MAX_CANDIDATES = 5


@dataclass(frozen=True, slots=True)
class PendingCandidates:
    code: str
    query: str
    choices: tuple[MatchChoice, ...]
    ranking_top: int | None
    created_at: float


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
        )
        self._entries[code] = entry
        return entry

    def resolve(
        self,
        code: str,
        selection: str,
        *,
        now: float | None = None,
    ) -> tuple[PendingCandidates, MatchChoice] | None:
        timestamp = time.monotonic() if now is None else now
        self._prune(timestamp)
        entry = self._entries.get(code.upper())
        index = parse_selection(selection)
        if entry is None or index is None or index > len(entry.choices):
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
    """Find the ``#QXXXX`` marker inside a quoted candidate message."""

    if not text:
        return None
    match = _CODE_RE.search(text)
    return match.group(1) if match else None


def parse_selection(text: str) -> int | None:
    match = _SELECTION_RE.match(text or "")
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= MAX_CANDIDATES else None


def format_candidates(entry: PendingCandidates) -> str:
    """Compact, phone-friendly candidate list ending with the pick marker."""

    lines = [
        f"「{entry.query}」匹配到 {len(entry.choices)} 个目标，"
        "引用本条消息回复序号即可："
    ]
    for index, choice in enumerate(entry.choices, start=1):
        lines.append(f"{index}. {describe_choice(choice)}")
    lines.append(f"#Q{entry.code}")
    return "\n".join(lines)


def describe_choice(choice: MatchChoice) -> str:
    target = choice.target
    if target.target_type is TargetType.BOARD:
        dungeon = target.dungeon_names[0] if target.dungeon_names else ""
        label = f"{target.name} · 榜单"
        if dungeon and dungeon not in target.name:
            label += f" · {dungeon}"
        return label
    kind = "副本" if target.target_type is TargetType.DUNGEON else "副本系列"
    return f"{target.name} · {kind}（{len(target.boss_slugs)} 个榜单）"
