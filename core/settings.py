"""Plugin configuration, validated once at load.

Every value AstrBot hands over is checked here and replaced by its default
with one warning when it is unusable, so a typo in the config panel can
never fail every query at request time (the matcher, the client and the
renderer validate the same bounds again, but they raise). The defaults are
the ones ``_conf_schema.json`` declares; a test keeps the two in step.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .client import DEFAULT_API_BASE_URL, DEFAULT_REQUEST_TIMEOUT_MS
from .watch import DEFAULT_RANK_THRESHOLD

MIN_RANK_WATCH_INTERVAL_SECONDS = 120.0
# One board re-read per pace; faster than this is a crawl of a public site.
MIN_RANKING_INDEX_PACE_SECONDS = 5.0
# A rank baseline older than this many polling intervals (never less than an
# hour) is re-seeded silently instead of replaying everything that moved.
SNAPSHOT_MAX_AGE_INTERVALS = 3
MIN_SNAPSHOT_MAX_AGE_SECONDS = 3600.0
_MIN_RENDER_TIMEOUT_MS = 1_000
_MAX_RENDER_TIMEOUT_MS = 120_000
# A config panel hands over real booleans, but a hand-edited YAML or JSON
# file says "false", and ``bool("false")`` is True.
_TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
_FALSE_WORDS = frozenset({"false", "no", "off", "0"})


@dataclass(frozen=True, slots=True)
class PluginSettings:
    api_base_url: str = DEFAULT_API_BASE_URL
    web_base_url: str = DEFAULT_API_BASE_URL
    request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS
    render_timeout_ms: int = 30_000
    fallback_to_astrbot_renderer: bool = True
    alias_file_path: str = "aliases.json"
    ranking_cache_ttl_seconds: float = 60.0
    character_stats_cache_ttl_seconds: float = 120.0
    account_cache_ttl_seconds: float = 60.0
    battle_cache_ttl_seconds: float = 300.0
    auto_expand_battle_links: bool = False
    battle_link_dedupe_seconds: float = 300.0
    fuzzy_match_threshold: float = 0.65
    ambiguity_score_gap: float = 0.08
    rank_watch_enabled: bool = True
    rank_watch_interval_seconds: float = 900.0
    rank_watch_rank_threshold: int = DEFAULT_RANK_THRESHOLD
    ranking_index_enabled: bool = True
    ranking_index_pace_seconds: float = 30.0

    @property
    def rank_snapshot_max_age_seconds(self) -> float:
        return max(
            self.rank_watch_interval_seconds * SNAPSHOT_MAX_AGE_INTERVALS,
            MIN_SNAPSHOT_MAX_AGE_SECONDS,
        )


def load_settings(
    config: Mapping[str, Any] | None,
    *,
    warn: Callable[[str], None],
) -> PluginSettings:
    """Read the AstrBot config mapping; unusable values warn and use defaults."""

    source = config if config is not None else {}
    defaults = PluginSettings()
    reader = _Reader(source, defaults, warn)
    interval = reader.positive_number("rank_watch_interval_seconds")
    if interval < MIN_RANK_WATCH_INTERVAL_SECONDS:
        warn(
            "ZmdLogBot config rank_watch_interval_seconds must be at least "
            f"{MIN_RANK_WATCH_INTERVAL_SECONDS:g}; using that."
        )
        interval = MIN_RANK_WATCH_INTERVAL_SECONDS
    pace = reader.positive_number("ranking_index_pace_seconds")
    if pace < MIN_RANKING_INDEX_PACE_SECONDS:
        warn(
            "ZmdLogBot config ranking_index_pace_seconds must be at least "
            f"{MIN_RANKING_INDEX_PACE_SECONDS:g}; using that."
        )
        pace = MIN_RANKING_INDEX_PACE_SECONDS
    return PluginSettings(
        api_base_url=reader.http_url("api_base_url"),
        web_base_url=reader.http_url("web_base_url"),
        request_timeout_ms=reader.positive_integer("request_timeout_ms"),
        render_timeout_ms=reader.positive_integer(
            "render_timeout_ms",
            minimum=_MIN_RENDER_TIMEOUT_MS,
            maximum=_MAX_RENDER_TIMEOUT_MS,
        ),
        fallback_to_astrbot_renderer=reader.flag("fallback_to_astrbot_renderer"),
        alias_file_path=reader.text("alias_file_path"),
        ranking_cache_ttl_seconds=reader.positive_number("ranking_cache_ttl_seconds"),
        character_stats_cache_ttl_seconds=reader.positive_number(
            "character_stats_cache_ttl_seconds"
        ),
        account_cache_ttl_seconds=reader.positive_number("account_cache_ttl_seconds"),
        battle_cache_ttl_seconds=reader.positive_number("battle_cache_ttl_seconds"),
        auto_expand_battle_links=reader.flag("auto_expand_battle_links"),
        battle_link_dedupe_seconds=reader.positive_number(
            "battle_link_dedupe_seconds"
        ),
        fuzzy_match_threshold=reader.ratio("fuzzy_match_threshold"),
        ambiguity_score_gap=reader.ratio("ambiguity_score_gap"),
        rank_watch_enabled=reader.flag("rank_watch_enabled"),
        ranking_index_enabled=reader.flag("ranking_index_enabled"),
        ranking_index_pace_seconds=pace,
        rank_watch_interval_seconds=interval,
        rank_watch_rank_threshold=max(
            1, int(reader.positive_number("rank_watch_rank_threshold"))
        ),
    )


def _is_usable_base_url(value: str) -> bool:
    """Whether the client and the renderer will both accept this base URL.

    Everything they check is checked here, inside one ``try``: ``urlsplit``
    raises on a bad bracket and ``.port`` raises on ``:abc`` or ``:99999``.
    A reader that let either through would move the failure into the plugin
    constructor, which is the one place this module exists to protect.
    """

    try:
        parsed = urlsplit(value)
        parsed.port  # noqa: B018 - raises for a malformed or out-of-range port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and not parsed.query
        and not parsed.fragment
    )


class _Reader:
    """Typed accessors over the raw mapping; each falls back with one warning."""

    def __init__(
        self,
        source: Mapping[str, Any],
        defaults: PluginSettings,
        warn: Callable[[str], None],
    ) -> None:
        self._source = source
        self._defaults = defaults
        self._warn = warn

    def _fallback(self, name: str, requirement: str) -> Any:
        default = getattr(self._defaults, name)
        self._warn(f"ZmdLogBot config {name} must be {requirement}; using {default}.")
        return default

    def _raw(self, name: str) -> Any:
        return self._source.get(name, getattr(self._defaults, name))

    def flag(self, name: str) -> bool:
        value = self._raw(name)
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            if value in (0, 1):
                return bool(value)
        elif isinstance(value, str):
            word = value.strip().casefold()
            if word in _TRUE_WORDS:
                return True
            if word in _FALSE_WORDS:
                return False
        return bool(self._fallback(name, "true or false"))

    def text(self, name: str) -> str:
        value = self._raw(name)
        if not isinstance(value, str) or not value.strip():
            return self._fallback(name, "a file name")
        return value

    def http_url(self, name: str) -> str:
        value = self._raw(name)
        if isinstance(value, str) and _is_usable_base_url(value.strip()):
            return value.strip()
        return self._fallback(name, "an absolute HTTP(S) URL")

    def positive_integer(
        self,
        name: str,
        *,
        minimum: int = 1,
        maximum: int | None = None,
    ) -> int:
        value = self._raw(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
            or (maximum is not None and value > maximum)
        ):
            bound = (
                f"an integer between {minimum} and {maximum}"
                if maximum is not None
                else f"an integer of at least {minimum}"
            )
            return self._fallback(name, bound)
        return value

    def positive_number(self, name: str) -> float:
        value = self._raw(name)
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            return float(self._fallback(name, "a positive number"))
        return float(value)

    def ratio(self, name: str) -> float:
        value = self._raw(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return float(self._fallback(name, "between 0 and 1"))
        if not 0 <= value <= 1:
            return float(self._fallback(name, "between 0 and 1"))
        return float(value)
