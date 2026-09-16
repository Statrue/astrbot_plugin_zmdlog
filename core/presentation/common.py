"""Shared page header, number / time formatting and URL safety."""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlsplit

from ..metrics import metric_label

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
    # ``rdps`` puts the rDPS badge next to the title; every page that can be
    # drawn from either ranking sets it, the rest stay DPS.
    metric: str = "dps"


def metric_footer(metric: str, what: str = "公开榜单") -> str:
    """The footer note naming the metric a page was drawn from."""

    return f"{what} · {metric_label(metric)} 口径"


def _dps_share(dps: float, top_dps: float) -> float:
    """Percent of the board leader's DPS, clamped to a visible minimum."""

    if top_dps <= 0:
        return 2.0
    return round(max(2.0, min(100.0, dps / top_dps * 100.0)), 2)


_RANGE_LABELS = {
    "7d": "近 7 天",
    "14d": "近 14 天",
    "30d": "近 30 天",
    "all": "全部时间",
}


def _share(count: int, total: int) -> str:
    if total <= 0:
        return "0%"
    return f"{format_number(round(count / total * 100, 1))}%"


def _bar_width(value: float, peak: float) -> float:
    if peak <= 0 or value <= 0:
        return 0.0
    return round(min(100.0, value / peak * 100), 2)


def _format_axis_value(value: float) -> str:
    if value >= 10_000:
        return f"{format_number(round(value / 10_000, 1))}万"
    return format_number(round(value))


def _nice_ceiling(value: float) -> float:
    """Round a peak up to a readable axis top (1 / 2 / 2.5 / 5 / 10 × 10ⁿ)."""

    if value <= 0:
        return 0.0
    magnitude = 10 ** (len(str(int(value))) - 1)
    for unit in (1, 1.5, 2, 2.5, 5, 10):
        candidate = magnitude * unit
        if value <= candidate:
            return float(candidate)
    return float(math.ceil(value / magnitude) * magnitude)


# The export carries no portrait URLs; this is the path every upstream
# response uses for character portraits. The template drops the image when it
# does not load, so a wrong guess only costs the picture.
_CHARACTER_AVATAR_PATH = "/images/character/charremoteicon/icon_{key}.png"


# Gear icons follow the same convention, keyed by item id / weapon template.
# Upstream omits ``iconUrl`` for pieces its catalog does not know yet, while
# the files themselves exist, so the path is derived when it is missing.
_EQUIP_ICON_PATH = "/images/equip/iconbig/{item_id}.png"


_WEAPON_ICON_PATH = "/images/weapon/icon/{template}.png"


_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


def _derived_asset_url(
    explicit: str | None,
    pattern: str,
    key: str | None,
    *,
    web_base_url: str | None,
) -> str | None:
    """The upstream URL when given, else the conventional path for ``key``."""

    if explicit:
        return _safe_asset_url(explicit, base_url=web_base_url)
    if not key or not _ASSET_ID_RE.match(key):
        return None
    path = pattern.format_map({"item_id": key, "template": key})
    return _safe_asset_url(path, base_url=web_base_url)


def _format_stat_value(value: float) -> str:
    """Ratios arrive as fractions (0.1495), flat stats as plain numbers."""

    if 0 < abs(value) < 1:
        return f"{format_number(round(value * 100, 1))}%"
    return format_number(round(value, 1))


def _clean_text(value: str | None) -> str:
    """Collapse whitespace; upstream piece names can carry a stray newline."""

    return " ".join(value.split()) if value else ""


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


def public_url(base_url: str, resource: str, identifier: str) -> str:
    path = f"{resource}/{quote(identifier, safe='')}"
    return urljoin(f"{base_url.rstrip('/')}/", path)


# Every stamp is shown in the game's server time (UTC+8), which is also the
# offset upstream's battle stamps carry; the plugin's own stamps are UTC and
# would otherwise sit eight hours off the battle times next to them.
DISPLAY_TZ = timezone(timedelta(hours=8))


def _displayed(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(DISPLAY_TZ) if parsed.tzinfo is not None else parsed


def _format_month_day(value: str) -> str:
    """月-日：窗口最长 30 天的页面上，年份只是噪音。"""

    return _format_date(value)[5:] or _format_date(value)


def _format_date(value: str) -> str:
    try:
        return _displayed(value).strftime("%Y-%m-%d")
    except ValueError:
        return value[:10]


def _format_datetime(value: str) -> str:
    try:
        return _displayed(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _as_of_label(age_seconds: float | None) -> str:
    if age_seconds is None:
        return ""
    minutes = int(age_seconds // 60)
    if minutes < 1:
        return "数据刚刚更新"
    if minutes < 60:
        return f"数据截至 {minutes} 分钟前"
    hours, minutes = divmod(minutes, 60)
    return f"数据截至 {hours} 小时 {minutes} 分钟前"
