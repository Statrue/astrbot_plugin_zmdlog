"""The six professions, as the records spell them, and what people type.

Rosters say 先锋 / 近卫 / 重装 / 术士 / 突击 / 辅助 and the game data
catalog spells the caster 术师, as the game's own UI does; players shorten
辅助 to 奶 and 重装 to 坦. A small alias table stands between any of that
and one label — the records' spelling, which every page already prints —
the same shape as ``elements``.
"""

from .labels import normalize_label

PROFESSIONS: tuple[str, ...] = ("先锋", "近卫", "重装", "术士", "突击", "辅助")

_ALIASES: dict[str, str] = {
    "术师": "术士",
    "法师": "术士",
    "术": "术士",
    "坦克": "重装",
    "坦": "重装",
    "盾": "重装",
    "奶妈": "辅助",
    "奶": "辅助",
    "辅": "辅助",
    "突": "突击",
    "近": "近卫",
    "先": "先锋",
    "vanguard": "先锋",
    "guard": "近卫",
    "defender": "重装",
    "caster": "术士",
    "striker": "突击",
    "supporter": "辅助",
    "support": "辅助",
}
_SUFFIXES = ("职业", "干员", "角色", "位", "队")


def normalize_profession(text: str) -> str | None:
    """The records' label for what was typed, or None when it is no profession."""

    return normalize_label(
        text, labels=PROFESSIONS, aliases=_ALIASES, suffixes=_SUFFIXES
    )
