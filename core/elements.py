"""The five elements a character can have, and how people spell them.

The game data catalog names them 物理 / 灼热 / 寒冷 / 自然 / 电磁; upstream's
battle payloads key them physical / fire / cryst / natural / pulse, which
is what the page CSS uses too. Players type 火 for 灼热 and 雷 for 电磁,
so a small alias table stands between what was typed and the label.
"""

ELEMENT_KEYS: dict[str, str] = {
    "物理": "physical",
    "灼热": "fire",
    "寒冷": "cryst",
    "自然": "natural",
    "电磁": "pulse",
}
ELEMENTS: tuple[str, ...] = tuple(ELEMENT_KEYS)

_ALIASES: dict[str, str] = {
    "物": "物理",
    "physical": "物理",
    "phys": "物理",
    "火": "灼热",
    "炎": "灼热",
    "热": "灼热",
    "fire": "灼热",
    "冰": "寒冷",
    "冷": "寒冷",
    "冻": "寒冷",
    "cryst": "寒冷",
    "ice": "寒冷",
    "草": "自然",
    "木": "自然",
    "natural": "自然",
    "nature": "自然",
    "电": "电磁",
    "雷": "电磁",
    "pulse": "电磁",
    "electric": "电磁",
}
_SUFFIXES = ("属性", "系", "队", "元素")


def normalize_element(text: str) -> str | None:
    """The catalog label for what was typed, or None when it is no element."""

    value = text.strip()
    for suffix in _SUFFIXES:
        if len(value) > len(suffix) and value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    if value in ELEMENT_KEYS:
        return value
    return _ALIASES.get(value.casefold())


def element_key(label: str | None) -> str | None:
    """The CSS / upstream key for a catalog label; None for unknown or none."""

    if label is None:
        return None
    return ELEMENT_KEYS.get(label)
