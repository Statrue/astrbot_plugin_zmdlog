import dataclasses
import json
import unittest
from pathlib import Path

from core.client import ZmdLogsClient
from core.render import _normalise_http_origin
from core.settings import (
    MIN_RANK_WATCH_INTERVAL_SECONDS,
    PluginSettings,
    load_settings,
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"


class SettingsTests(unittest.TestCase):
    def test_defaults_match_the_astrbot_config_schema(self) -> None:
        # CLAUDE.md asks for the schema and the code defaults to stay in sync;
        # this is what makes that a checked fact rather than a reminder.
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        defaults = PluginSettings()

        self.assertEqual(
            set(schema), {field.name for field in dataclasses.fields(PluginSettings)}
        )
        for name, spec in schema.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(defaults, name), spec["default"])
                # True == 1, so a flag also has to be checked by type.
                self.assertEqual(
                    isinstance(getattr(defaults, name), bool), spec["type"] == "bool"
                )

    def test_missing_or_empty_config_gives_the_defaults(self) -> None:
        warnings: list[str] = []
        self.assertEqual(load_settings(None, warn=warnings.append), PluginSettings())
        self.assertEqual(load_settings({}, warn=warnings.append), PluginSettings())
        self.assertEqual(warnings, [])

    def test_valid_values_are_kept_and_coerced(self) -> None:
        warnings: list[str] = []
        settings = load_settings(
            {
                "api_base_url": " https://mirror.example/ ",
                "web_base_url": "http://localhost:8080",
                "request_timeout_ms": 5_000,
                "render_timeout_ms": 1_000,
                "fallback_to_astrbot_renderer": 0,
                "alias_file_path": "custom.json",
                "ranking_cache_ttl_seconds": 30,
                "battle_link_dedupe_seconds": 12.5,
                "fuzzy_match_threshold": 1,
                "ambiguity_score_gap": 0,
                "rank_watch_enabled": False,
                "rank_watch_interval_seconds": 600,
                "rank_watch_rank_threshold": 3.9,
            },
            warn=warnings.append,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(settings.api_base_url, "https://mirror.example/")
        self.assertEqual(settings.web_base_url, "http://localhost:8080")
        self.assertEqual(settings.request_timeout_ms, 5_000)
        self.assertEqual(settings.render_timeout_ms, 1_000)
        self.assertFalse(settings.fallback_to_astrbot_renderer)
        self.assertEqual(settings.alias_file_path, "custom.json")
        self.assertEqual(settings.ranking_cache_ttl_seconds, 30.0)
        self.assertIs(type(settings.ranking_cache_ttl_seconds), float)
        self.assertEqual(settings.battle_link_dedupe_seconds, 12.5)
        self.assertEqual(settings.fuzzy_match_threshold, 1.0)
        self.assertEqual(settings.ambiguity_score_gap, 0.0)
        self.assertFalse(settings.rank_watch_enabled)
        self.assertEqual(settings.rank_watch_interval_seconds, 600.0)
        self.assertEqual(settings.rank_watch_rank_threshold, 3)

    def test_unusable_values_warn_once_and_keep_the_default(self) -> None:
        bad_values = {
            "api_base_url": "zmdlogs.com",
            "web_base_url": "https://zmdlogs.com/?x=1",
            "request_timeout_ms": "10000",
            "render_timeout_ms": 500,
            "alias_file_path": "",
            "ranking_cache_ttl_seconds": 0,
            "character_stats_cache_ttl_seconds": True,
            "account_cache_ttl_seconds": -1,
            "battle_cache_ttl_seconds": None,
            "battle_link_dedupe_seconds": "300",
            "fuzzy_match_threshold": 65,
            "ambiguity_score_gap": -0.1,
            "rank_watch_rank_threshold": 0,
        }
        defaults = PluginSettings()
        for name, value in bad_values.items():
            with self.subTest(name=name):
                warnings: list[str] = []
                settings = load_settings({name: value}, warn=warnings.append)
                self.assertEqual(getattr(settings, name), getattr(defaults, name))
                self.assertEqual(len(warnings), 1)
                self.assertIn(name, warnings[0])

    def test_rank_watch_interval_is_raised_to_the_minimum(self) -> None:
        warnings: list[str] = []
        settings = load_settings(
            {"rank_watch_interval_seconds": 5}, warn=warnings.append
        )

        self.assertEqual(
            settings.rank_watch_interval_seconds, MIN_RANK_WATCH_INTERVAL_SECONDS
        )
        self.assertEqual(len(warnings), 1)
        # Three intervals, but never less than an hour.
        self.assertEqual(settings.rank_snapshot_max_age_seconds, 3600.0)
        long_interval = load_settings(
            {"rank_watch_interval_seconds": 7200}, warn=warnings.append
        )
        self.assertEqual(long_interval.rank_snapshot_max_age_seconds, 21600.0)

    def test_booleans_are_parsed_rather_than_coerced(self) -> None:
        # A config panel hands over real booleans, but a hand-edited file
        # says "false", and bool("false") is True.
        warnings: list[str] = []
        off = load_settings(
            {"rank_watch_enabled": "false", "auto_expand_battle_links": "0"},
            warn=warnings.append,
        )
        on = load_settings(
            {"auto_expand_battle_links": "Yes", "rank_watch_enabled": 1},
            warn=warnings.append,
        )

        self.assertFalse(off.rank_watch_enabled)
        self.assertFalse(off.auto_expand_battle_links)
        self.assertTrue(on.auto_expand_battle_links)
        self.assertTrue(on.rank_watch_enabled)
        self.assertEqual(warnings, [])

        nonsense: list[str] = []
        settings = load_settings(
            {"rank_watch_enabled": "maybe"}, warn=nonsense.append
        )
        self.assertTrue(settings.rank_watch_enabled)
        self.assertEqual(len(nonsense), 1)

    def test_a_malformed_base_url_warns_instead_of_raising(self) -> None:
        # The point of this module is that a bad value never reaches the
        # plugin constructor: urlsplit raises on a bad bracket and .port
        # raises on a non-numeric or out-of-range port.
        defaults = PluginSettings()
        for raw in (
            "http://[bad",
            "http://host:abc",
            "http://host:99999",
            "zmdlogs.com",
            "https://zmdlogs.com/?x=1",
            "ftp://zmdlogs.com",
        ):
            with self.subTest(raw=raw):
                warnings: list[str] = []
                settings = load_settings({"api_base_url": raw}, warn=warnings.append)
                self.assertEqual(settings.api_base_url, defaults.api_base_url)
                self.assertEqual(len(warnings), 1)

    def test_a_usable_base_url_survives_intact(self) -> None:
        warnings: list[str] = []
        settings = load_settings(
            {"api_base_url": " https://mirror.example:8443/base/ "},
            warn=warnings.append,
        )

        self.assertEqual(settings.api_base_url, "https://mirror.example:8443/base/")
        self.assertEqual(warnings, [])
        # What settings accepts, the client and the renderer must accept too.
        ZmdLogsClient(api_base_url=settings.api_base_url)
        self.assertEqual(
            _normalise_http_origin(settings.api_base_url),
            "https://mirror.example:8443",
        )

    def test_render_timeout_stays_inside_the_renderer_bounds(self) -> None:
        warnings: list[str] = []
        too_long = load_settings({"render_timeout_ms": 120_001}, warn=warnings.append)

        self.assertEqual(too_long.render_timeout_ms, 30_000)
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
