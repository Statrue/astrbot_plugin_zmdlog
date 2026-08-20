# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

AstrBot plugin (`ZmdLogBot`) that queries public ZMDLogs data (Endfield DPS leaderboards, account best records, battle reports) and replies with a single 1280px-wide PNG rendered by Jinja2 + Playwright. Entry point is `main.py` (`ZmdLogBotPlugin(Star)`); all logic lives in `core/`. Python 3.10+, deps in `requirements.txt` (httpx, jinja2, playwright, pypinyin). Chromium must be installed separately (`python -m playwright install chromium`).

## Commands

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -v          # full suite (~85 tests, ~2s)
.venv/Scripts/python.exe -m ruff check main.py core tests tools    # lint (ruff.toml: E/F/I/W, py310)
.venv/Scripts/python.exe -m unittest tests.test_matcher -v         # one module
.venv/Scripts/python.exe -m unittest tests.test_matcher.MatcherTests.test_slug_and_board_aliases_match_one_board
```

Tests import `core.*` directly (run from repo root). Playwright is *not* exercised by tests — render tests only cover template output and validation logic. Lint with ruff (`ruff.toml`); `ruff format` is not enforced on the existing files. Runtime is inside AstrBot (`data/plugins/astrbot_plugin_zmdlog`), so `astrbot.api` imports in `main.py` are unavailable outside AstrBot; keep `core/` free of AstrBot imports so it stays unit-testable.

## Architecture (request flow)

`main.py` → `core/routing.parse_zmdlog_payload` (subcommand + trailing `--top` / `--角色` / `--范围` / `--潜能` options, parsed by `_extract_options`; each route rejects options it does not accept) → `_dispatch`:
- HELP → `core/help.build_help_page` (local only, no API)
- ALL_RANKINGS → `hot-bosses` cache → all-top3 template
- ACCOUNT_QUERY / BATTLE_QUERY → `core/identifiers` (exact ID or trusted `web_base_url` URL) → client → account/battle template. A non-ID 账号 argument (≥2 chars) falls back to `GET /api/battles/users/search` (`main._account_search_outcome`): 0 hits ⇒ text, 1 hit ⇒ account page, several ⇒ `TargetType.ACCOUNT` candidates (search `limit=MAX_CANDIDATES`, upstream `hasMore` note appended). The smart route also searches accounts whenever the best board hit is weaker than the exact tiers (`> MatchLevel.PINYIN_EXACT`) and no options were given: an exact folded-nickname hit beats the fuzzy boards (1 exact ⇒ account page, several ⇒ account-only candidates), otherwise fuzzy account hits are appended to the board pick list; on a full board miss the quiet search runs before the character-name hint. Account-page rows with rank ≤3 get `tr.rank-N` medal styling (yellow/ink/muted)
- RANKING_QUERY / SMART_QUERY → `hot-bosses` cache → `core/matcher.RankingMatcher` → BOARD ⇒ `/api/bosses/{slug}/rankings` (ranking template; `--角色 X` filters rows by main character after `core/characters.resolve_character_name` resolves X against the names in that ranking — exact → folded → pinyin → unique prefix/substring; rows keep their global rank); DUNGEON / DUNGEON_SCOPE ⇒ filter hot-bosses cards (dungeon-top3 template); AMBIGUOUS ⇒ compact text list stored in `core/candidates.CandidateStore` (ends with `候选编号 XXXX · N 分钟内有效`); a reply that *quotes* that message with a number (bare, or `/zmdlog 2`) resolves it via `main._quoted_candidate_code`
- CHARACTER_STATS (`/zmdlog 角色统计 [关键词] [--范围] [--潜能]`; bare `角色` is still accepted as an alias) → no keyword ⇒ `GET /api/bosses/character-statistics`; keyword ⇒ same matcher, but **board-only**: dungeon/scope hits are flattened with `RankingMatcher.expand_to_boards` into a candidate list (single board ⇒ rendered directly) → `GET /api/bosses/{slug}/character-statistics?range=&potential=` (never `metric`) → character-stats template (CSS box plots; axis spans whiskers, off-scale maxima pinned at 100%; crisis contract returns upstream 404 `character_statistics_not_available` → `危机合约不提供角色统计。`)
- ROSTER_QUERY (`/zmdlog 阵容 <关键词> [--top N]`) → same board-only pipeline → reuses the ranking cache → roster template (upstream `professionGroups` for the whole board + local top-N roster combos / main-character counts)
- `PendingCandidates.view` (`CandidateView.RANKING|CHARACTER_STATS|ROSTER`) plus the parsed options travel with the candidate list so a quoted pick renders the right page (`main._render_board` is the single place that renders one slug in any view)
- ALIAS_LIST / ALIAS_ADD / ALIAS_REMOVE (`/zmdlog 别名 …`) → admin-only (`event.is_admin()`), edits the alias JSON in the data dir and swaps `self.aliases` in place

Supporting layers: `core/client.py` (httpx; 1 retry, 15s total budget, 4xx never retried, all failures → `ZmdLogsClientError` subclasses) → `core/models.py` (strict field-by-field validation into frozen dataclasses; `ModelValidationError` → `ZmdLogsProtocolError`) → `core/presentation.py` (view models, number/duration formatting, URL safety) → `core/render.py` (`TemplateRenderer` = Jinja with `StrictUndefined`; `LongImageRenderer` = Playwright capture with page-integrity validation, render semaphore, output pruning). `core/cache.py` `AsyncTTLCache` merges concurrent loads per key; only the hot-bosses cache allows stale-on-error.

`main.py` also registers a `@filter.regex` handler that auto-expands ZMDLogs battle links in group chats (off by default, per-group/battle cooldown).

## Invariants to preserve

- **DPS only.** Never send `metric`, never show RDPS rankings; `parse_boss_ranking` rejects `metric != "dps"`.
- **Image-only output.** Any query/help result is one PNG; render failure returns a short error text, never a text fallback of the data. `_require_renderer()` raises `RenderError` if Playwright setup failed. When only the Chromium capture fails, `RenderError.html` carries the rendered page and `main._render_with_astrbot` retries through AstrBot's `html_render` (config `fallback_to_astrbot_renderer`, on by default) after neutralising Jinja delimiters.
- **Top-3 pages must not leak ranking-only fields** (slug, percentile, DPS, roster, links) — enforced by `test_top_three_template_does_not_leak_ranking_fields`.
- **User-facing errors are short and generic**; log only exception type / API code, never response bodies or stacks.
- **Help data (`core/help.py`) must be updated whenever a user-facing command changes**; `test_routing_help` asserts the exact command list. Version comes from `metadata.yaml`, don't hardcode.
- Rendered pages must contain `#zmd-page > .main-panel` and the local background data-URL (`resources/common/scene-background.svg`, inlined as `data:image/svg+xml`), or `_validate_page` fails the render. Templates extend `resources/common/base.html`; page-specific CSS is `{% include %}`d into `extra_styles`. Decorative elements that overflow must live inside `.scene-deco` (overflow hidden) or `#zmd-page` scrollWidth exceeds 1280 and validation fails.
- **Visual language is Endfield-style** (light paper `#f1f1ee`, ink `#1c1c1c`, accent yellow `#f8d34d`, cut corners via `clip-path`, skewed badges, Barlow for Latin/digits, MiSans for CJK). Fonts are subset woff2 files (CJK = full GB2312, 6763 chars) in `resources/common/fonts/` embedded as data URLs by `core/render._load_font_face_css`; regenerate with `tools/build_fonts.py <dir-with-source-ttfs>` (needs fonttools+brotli, see `resources/common/ASSETS.md`). No AGPL assets remain.
- Avatar URLs from upstream are site-relative; every page builder takes `web_base_url` and resolves via `_safe_asset_url` (unsafe schemes → `None`).
- `hot-bosses` is the sole source of the board/dungeon index — no hardcoded board catalog; a JSON snapshot of the last good payload is kept in the data dir (`hot-bosses.json`) and used when upstream is unreachable. Matching derives aliases automatically (`derived_board_aliases`: strip `·苦难/·残酷` suffix and `{dungeon}·` prefix; `derived_dungeon_aliases`: split `影拓丰碑4期 · 山中见犼` into parts + `丰碑4/影拓4`) and pinyin keys (`pypinyin`, full + initials, `MatchLevel.PINYIN_EXACT`; a CJK query is also compared by its own full pinyin so homophone typos like 躁雷→噪雷 hit as PINYIN_EXACT 0.98 / PREFIX_SUFFIX 0.85). A single below-threshold similarity hit renders directly; only ≥2 weak hits become a pick list. An exact hit on several dungeons of one phase becomes a DUNGEON_SCOPE instead of AMBIGUOUS; an exact hit on a one-board dungeon is promoted to that board. Hand-written aliases live in `aliases.json` (`boards` keyed by `bossSlug`, `dungeons` keyed by full `dungeonName`); the bundled file only seeds `data/plugin_data/astrbot_plugin_zmdlog/aliases.json`, which is the editable copy.
- Only image requests to `api_base_url` / `web_base_url` origins are allowed during capture; everything else is aborted.

## Config

`_conf_schema.json` is the AstrBot config schema; defaults are duplicated in `main.py.__init__` `.get(...)` calls — keep both in sync. `README.md` documents commands/config for end users and must be updated alongside. Rendered PNGs go to `StarTools.get_data_dir("astrbot_plugin_zmdlog")/render` (falls back to system temp when StarTools is unavailable); `@filter.on_astrbot_loaded` warms up Chromium. `_HIGH_DPI_PAGE_KINDS` in `render.py` (help, ranking, account, battle, character-stats, roster) are captured at `device_scale_factor=2` (PNG 2560px wide); the long top-3 pages stay 1x. `metadata.yaml` carries `short_desc`; `logo.png` (256×256) is the market icon.

## Upstream facts (verified against endfield-suite-open `apps/api/app/services/public_data.py`)

- `characterAvatarUrl` / `avatarUrl` in hot-bosses, rankings and battles are usually **site-relative paths** (`/images/...`), occasionally absolute CDN URLs — resolve them against `web_base_url` before rendering.
- In battle detail every `roster[].accountDisplayName` is the uploader nickname, so `roster[0]` is a valid uploader name source; `GET /api/battles/{id}/share-summary` also returns `uploaderNickname` directly.
- Board slugs come from static `BOSS_SEEDS`; the crisis-contract board is `indie_group_ccdg` (`bossName` 破潮之像, `dungeonName` 危机合约). Unknown slug → 404 `boss_not_found`.
- `GET /api/bosses[/{slug}]/character-statistics` (`range=7d|14d|30d|all`, `potential=0|1-5|all`, `metric` defaults to dps) returns every six-star character (`SIX_STAR_STATISTICS_CATALOG`), zero-sample rows included; `rank` is null when `normalSampleCount < minimumSampleCount` (5); whiskers are min/max of IQR-filtered normal samples, `maximum` includes outliers. Crisis contract → 404 `character_statistics_not_available`. `professionGroups[].entries[].usagePercent` is the share within that profession slot over **all** rows.
- `GET /api/battles/users/search?query=&limit=` (public, no auth): NFKC-normalized case-insensitive substring match over public nicknames; `query` must be 2–64 chars after strip (else 422), `limit` 1–20 (default 10); returns `{query, hasMore, accounts[{accountId, accountDisplayName}]}`, only accounts with valid public ranking records. Server caches ≤60s, HTTP `max-age=30`. Full contract in local `PUBLIC_ACCOUNT_SEARCH_API.md`-style doc (Downloads).

## References

- AstrBot plugin dev docs: https://docs.astrbot.app/dev/star/plugin-new.html (and `plugin.html` for the event/config/html_render API)
- Upstream ZMDLogs source: https://github.com/medps16000/endfield-suite-open (`endfield-logs/apps/api`)
- Reference plugin for structure/visual style only: https://github.com/Entropy-Increase-Team/astrbot_plugin_endfield (AGPL-3.0; its background image was used until the Endfield restyle and is no longer present)
- `MVP.md`, `UPSTREAM_API.md`, `UPSTREAM_MISSING_API.md` are gitignored local planning docs. `MVP.md` is partly stale (predates `--top`, account, battle features).
- `docs/style-refs/` holds official Endfield UI/poster screenshots used as visual references (gitignored, © HYPERGRYPH — do not commit or redistribute).
