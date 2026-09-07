"""One way to turn what a person typed into a fixed label.

Elements and professions both keep a small alias table (火 → 灼热, 奶 →
辅助) and tolerate a trailing word (火属性, 辅助位); this is the lookup
they share.
"""

from collections.abc import Iterable, Mapping


def normalize_label(
    text: str,
    *,
    labels: Iterable[str],
    aliases: Mapping[str, str],
    suffixes: Iterable[str],
) -> str | None:
    """The label for ``text``, or None when it names none of ``labels``."""

    value = text.strip()
    for suffix in suffixes:
        if len(value) > len(suffix) and value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    if value in labels:
        return value
    return aliases.get(value.casefold())
