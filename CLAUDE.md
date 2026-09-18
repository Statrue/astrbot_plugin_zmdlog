# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Project

AstrBot plugin (`ZmdLogBot`) that queries public ZMDLogs data (Endfield DPS
leaderboards, account best records, battle reports) and replies with a single
1280px-wide PNG rendered by Jinja2 + Playwright.

`main.py` is the only file that touches AstrBot: the handlers, the error ladder,
event field access, the notice sender, the four LLM tool handlers, the
lifecycle. Every other line of logic lives in `core/`, which imports no AstrBot
— that is what keeps the suite runnable as plain unit tests.

Python 3.11+ (`asyncio.timeout`, `datetime.UTC`), dependencies in
`requirements.txt`, Chromium installed separately
(`python -m playwright install chromium`).

**Read the module docstring first.** Every `core/` module opens with one, and
the modules that carry a design worth understanding — `ranking_index`,
`recipes/__init__`, `rank_watch`, `watch`, `queries`, `toolbox`, `facts`,
`contract`, `timeline`, `telemetry`, `bindings`, `metrics`, `cache` — explain
their economics and their reasons there. That is the module map; this file does not
repeat it, and a behaviour question is answered by the docstring beside the
code, not here.

Two other sources of truth this file defers to:

- **[UPSTREAM.md](UPSTREAM.md)** — what the public ZMDLogs API returns, field by
  field, verified in the field. Read it before adding an endpoint, widening a
  model, or trusting a field. Nothing else in the repository records the contract.
- **[README.md](README.md)** — the user-facing surface: every command and its
  syntax, every option's accepted spellings, the config table, what each LLM
  tool answers. Update it whenever any of those change.

## Commands

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -v          # full suite, offline, under 10 s
.venv/Scripts/python.exe -m ruff check main.py core tests tools    # lint (ruff.toml: E/F/I/W, py311)
.venv/Scripts/python.exe -m unittest tests.test_matcher -v         # one module
.venv/Scripts/python.exe -m unittest tests.test_matcher.MatcherTests.test_slug_and_board_aliases_match_one_board
```

Run from the repo root; tests import `core.*` directly. AstrBot itself is
installed in `.venv`, so the commands need that interpreter.

**The suite runs offline and must stay that way.** Importing `tests/` replaces
the httpx transports with one that raises `NetworkAccessInTests` naming the URL
— otherwise the suite measures upstream and goes red during an outage.
`tests/test_main_handlers.py` drives `main.py` as the package
`astrbot_plugin_zmdlog.main` (repo parent on `sys.path`) with fake events,
clients and renderers, and is skipped wherever AstrBot is absent. Two traps when
writing such tests: exceptions must come from `astrbot_plugin_zmdlog.core.*`
(the class identities `main.py` catches), not from `core.*`; and merely
importing `astrbot.api` creates a `./data` directory in the CWD (gitignored).
Playwright is not exercised — render tests cover template output and validation
logic only.

## Request flow

```
main.py  →  core/routing      parse the subcommand and its trailing --options
         →  core/queries      dispatch: resolve the target, or post a pick list
         →  core/recipes      prepare_*: fetch and filter into one page's data
         →  core/presentation build_*_page: view models, formatting, URL safety
         →  core/render        Jinja (StrictUndefined) → Playwright capture
```

Supporting layers: `core/client.py` (httpx; 1 retry, 15 s total budget, 4xx
never retried, every failure a `ZmdLogsClientError` subclass) →
`core/models.py` (field-by-field validation into frozen dataclasses;
`ModelValidationError` → `ZmdLogsProtocolError`). `ZmdLogsDataSource`
(`core/datasource.py`) owns every upstream read and its cache.

`main.py` registers three reply paths — the `zmdlog` command, the quoted
candidate pick, and a `@filter.regex` handler that auto-expands battle links in
group chats — plus the four LLM tools. All of them run through one error guard,
`main._run_guarded`, so they cannot drift apart again.

## Invariants to preserve

- **`core/` imports no AstrBot.** Modules that need to log take a
  `core/logs.LogSink`, because AstrBot's plugin logger is not a `logging.Logger`.
- **Every board ranking is read through `core/ranking_index.RankingIndex`**,
  never from the client directly.
- **DPS by default, rDPS on request, never mixed.** `core/metrics.py` is the one
  spelling table. The client always sends `metric`, DPS included, and the parser
  rejects a response whose metric is not the one asked for — validated against
  the *request*, never against an upstream default that may change, so a DPS
  page can never quietly draw rDPS rows.
- **A page has one recipe.** `core/recipes/` sits between a resolved target and
  the renderer so the command path and the tool path draw the same page; a
  filter or a section can no longer exist on one path only.
- **Image-only output, with two carve-outs.** Any query or help result is one
  PNG; a render failure returns a short error text, never a text fallback of the
  data. The carve-outs: rank-watch notices and the 关注 / 取关 / 别名 / 绑定
  replies are plain text (configuration actions and pushes, not query results),
  and the LLM tools send the picture *and* return text, because the text is what
  the model reasons over.
- **Rendering is gated twice**: concurrent captures capped, and the callers
  waiting for a slot capped too, with only the wait under `render_timeout_ms` —
  wrapping the capture itself would trip on a legitimately slow page. An
  overloaded bot answers 图片生成失败 while the reply is still worth having.
- **Page integrity.** A rendered page must contain `#zmd-page > .main-panel` and
  the local background data-URL, or `_validate_page` fails the render.
  Decorative elements that overflow belong inside `.scene-deco`, or `#zmd-page`
  scrollWidth exceeds 1280 and validation fails.
- **Template discipline.** Pages extend `resources/common/base.html`;
  page-specific CSS is `{% include %}`d into `extra_styles`; a page includes its
  own stylesheet plus the shared ones under `resources/common/`. Anything two
  pages need lives in `base.css`. CSS files render under autoescape like the
  templates.
- **Visual language is Endfield-style**, every colour and every font size a
  token in `base.css` (the `--fs-*` scale; a size off it is a design change,
  and `test_render.test_every_font_size_is_a_scale_token` pins the rule), and
  one term per concept on every page: 主 C, 名次, 通关时间 (hero) / 用时
  (column), DPS (column) / 总 DPS (hero), 公开账号, 全部榜单, 总伤害, 阵容,
  武器未记录. A single-series bar is always yellow.
- **Fonts stay subset and OFL**, and the bundled faces are not swapped without
  re-reading the licence reasoning in
  [ASSETS.md](resources/common/ASSETS.md), which owns the font story. The one
  fact it omits: fonts are *linked* on `render.FONT_ORIGIN` rather than
  embedded because embedding made every document 3.7 MB and cost a third of
  each capture.
- **Strict for typed fields, lenient for untyped and for optional telemetry.**
  `models.py` validates every field upstream types; the lists it leaves as bare
  dicts, and `timelineEvents` / `characterStates`, are read entry by entry with
  unreadable ones dropped — one odd gear line can never fail a battle that used
  to render, and the curve and buff band are sections the card must render
  without. Raw item ids are never printed as names (`is_raw_item_name` →
  名称未收录); a raw buff key prints its effect instead of the key.
- **Top-3 pages must not leak ranking-only fields** (slug, percentile, DPS,
  roster, links) — enforced by
  `test_render.test_top_three_template_does_not_leak_ranking_fields`.
- **User-facing errors are short and generic**; log only the exception type or
  API code, never response bodies or stacks.
- **Help data (`core/help.py`) is updated whenever a user-facing command
  changes.** `test_routing_help` asserts the exact command list, and every
  `HelpCommand` carries an `answers` line ending in `？`, because 阵容 /
  角色统计 / `--角色` are indistinguishable from their syntax alone. Version
  comes from `metadata.yaml`; don't hardcode it.
- **Capture fetches images only from the configured origins.** The Playwright
  route handler only ever sees the first hop of a redirect chain, so allowed
  requests are fetched *inside* the handler with `max_redirects=0` and any 3xx
  is aborted — otherwise a redirect served by an allowed origin would pull an
  image from anywhere. **This has no unit test** (needs Chromium): verify with a
  two-local-origins script when touching `_route_asset_request`.
- **Every JSON store distinguishes missing from corrupt**
  (`core/persistence.py`) — a watch list is recoverable from nowhere else.
- **The binding file holds public data plus one platform user key, and never a
  code.** `bindings.json` is the one file tying a person to an account (README
  lists its fields); no QQ nickname, no site credential, no plaintext code —
  `client.install_log_redaction` keeps the code out of httpx's request log,
  which prints every URL at INFO. A binding exists only because the site handed
  a code to whoever was logged into that account, and nothing may create one
  any other way.
- **`hot-bosses` is the sole source of the board and dungeon index** — no
  hardcoded catalog, with a JSON snapshot for when upstream is unreachable.
  Hand-written aliases in `aliases.json` outscore the derived ones, so an admin
  can pin an ambiguous name to one difficulty. A similarity below
  `matcher.MIN_SIMILARITY` is a miss, not "the closest board", and queries
  longer than `MAX_QUERY_LENGTH` are rejected outright: the fuzzy fallback runs
  synchronously on the event loop, so an unbounded keyword was a one-message
  denial of service.
- **`core/presentation/` dependency direction is
  common ← charts ← rail ← battle ← compare, never back.**
- **The LLM tool surface is four tools, one per subject** (榜单 / 战报 / 角色 /
  账号), never one per feature — README's 大模型工具 section states the four
  design rules and their reasons. What follows from them when extending:
  a new view of a subject becomes a parameter or extra lines in that tool's
  text, never a fifth tool; `core/facts.py` returns **computed** facts, never a
  raw array; and 循环 DPS or 专武收益 need a damage calculator and belong to
  `astrbot_plugin_zmdlore`, not here.
- **Rank-watch attribution is deliberately weak, and the wording must stay
  weak.** `watch.find_new_record_above`'s docstring explains why one board read
  cannot prove who overtook an account. The notice says 期间上方新增纪录, never
  超过 TA 的是. Do not "improve" this into a causal claim without a real
  previous board state.
- **Config is validated once at load** by `core/settings.load_settings`, which
  replaces every unusable value with its default and warns once, so a typo
  cannot fail every keyword query and a bad TTL or base URL cannot fail the
  plugin load. `tests/test_settings.py` asserts the dataclass defaults equal
  `_conf_schema.json` key for key.

## Decisions recorded only here

Rulings and rejected alternatives that no file or docstring carries. Dates are
when the user settled the question.

- **The cast rail** follows one rule from the user's design memo (2026-09-03):
  线管连续、长度管时长、宽度与颜色管招式层级、节点管瞬时. Four layouts came
  before it and must not come back: horizontal 20-second strips (the fixed
  1280 px width could not fit a name into a half-second cast), full-width
  vertical blocks ("一整块砸下来"), a hairline rail with dots (too thin to read
  without zooming), and a uniform 44 px track (the light-grey normal-attack runs
  read as columns of filler). The graded bar widths settled it.
- **Chart section order** on the battle card is DPS 曲线 → BUFF 覆盖 → 施法节奏,
  so the one switch from left-to-right to top-to-bottom time coincides with a
  heading. The buff band was first placed inside the 施法节奏 section and the
  user asked for it to be moved out.
- **Elements get rings and a filter, nothing else** (2026-09-06): no element
  chip, no element text, no element statistics page. The main C wears no ring of
  its own — the row prints 主 C by name — so a ring there only cost the element
  its colour.
- **冠军 means a board's #1 record, credited to all four members** of that team
  (2026-09-06) — the headline and the sort key. First places as main C are a
  side note.
- **One tool per subject** (2026-09-05), set when a seven-tool surface was cut:
  seven schemas cost ≈1445 tokens on every message in every group. The
  `zmdlogs_` prefix keeps these apart from the sibling plugin zmdlore's
  `*_endfield_*` wiki tools — as `query_endfield_character` the character tool
  was one verb away from `get_endfield_operator`, and the model reached for the
  wiki profile first on a 冠军 question.
- **Only a binding code binds** (shipped 2026-09-15). The unverified bookmark of
  the 2026-08 design is gone for good; unverified binding was judged an
  unacceptable impersonation surface and that judgement stands.
- **Every binding command is group-only** (2026-09-15): the bot adds nobody as a
  friend, so a private chat is not a place any of this is used from. A
  关注-list-based 群榜 was judged not worth building, and was not; the 群榜 that
  shipped the same day (`d5736c5`) is drawn from the chat's *bindings* —
  `queries._group_board_gate` enrols whoever asks, and the page is one board
  read filtered to the members' account ids.
- **Cross-boss battle comparison is meaningless** and answers in text: every
  boss has its own rotation.
- **In a comparison, A keeps the order its own 配装 page shows and B is matched
  to it.** Sorting both sides aligned them but left neither matching the page a
  reader cross-checks against.
- **玩家排名** was named 玩家冠军榜 on 2026-09-06 (榜霸榜 rejected), then renamed
  on 2026-09-08 so it reads as the sibling of 角色排名.
- **rDPS got the full scope** on 2026-09-16, chosen knowing the rDPS boards hold
  1.5% of the records.
- **危机合约 词条 stay off the ranking rows and go on the battle card**
  (2026-09-19). `eb907ff` (2026-08-09) cut the tag chips from the ranking page
  — five columns plus chips read as noise — and kept only 合约分数; that ruling
  is about the *rows* and stands (`test_contract_ranking_only_shows_score`
  pins it). The battle card never had a ruling: its contract fields arrived a
  week later with the battle lookup and were parsed but never drawn, which is
  why this read as a deferred feature for six weeks. The card draws the
  record's own tags as one section after `battle-identity` — icon, name and
  score, grouped 队列 / 改写 / 环境, each family's header carrying that
  family's tag count and subtotal — and nothing beyond that: no
  `description` (an unexpandable template, see UPSTREAM.md), no tier badge
  (the id's last digit usually matches the score but five tags disagree, and
  the score is what counts), no merging by tag base (names change between
  tiers), no "N of the catalog" denominator and no board-best reference (the
  catalog has no upstream endpoint, and a second upstream for a decorative
  figure was rejected). Neutral colour: 合约 is data, not action.

## References

- AstrBot plugin dev docs: https://docs.astrbot.app/dev/star/plugin-new.html
  (and `plugin.html` for the event/config/html_render API)
- Upstream ZMDLogs source:
  https://github.com/medps16000/endfield-suite-open (`endfield-logs/apps/api`)
- Reference plugin for structure/visual style only:
  https://github.com/Entropy-Increase-Team/astrbot_plugin_endfield (AGPL-3.0;
  its background image was used until the Endfield restyle and is no longer present)
- Planning notes under `docs/` are local and gitignored.
  [UPSTREAM.md](UPSTREAM.md) is the source of truth for the API contract.
