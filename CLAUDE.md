# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Project

AstrBot plugin (`ZmdLogBot`) that queries public ZMDLogs data (Endfield DPS
leaderboards, account best records, battle reports) and replies with a single
PNG rendered by Jinja2 + Playwright: the board ranking a 540 px phone-width
list, every other page 960 px wide, both captured at 2× (ADR 0003).

`main.py` and the platform module `qq_official.py` are the only files that
touch AstrBot. `main.py` holds the handlers, the error ladder, event field
access, the notice sender, the four LLM tool handlers, the lifecycle;
`qq_official.py` holds what the plugin does on the QQ official bot because
AstrBot cannot — its own botpy sends, the notice push, the tap patch — and is
the file to delete once AstrBot can. Every other line of logic lives in
`core/`, which imports no AstrBot — that is what keeps the suite runnable as
plain unit tests. What goes on a button is decided in `core/buttons.py`, not
in the platform module.

Python 3.11+ (`asyncio.timeout`, `datetime.UTC`), dependencies in
`requirements.txt`, Chromium installed separately
(`python -m playwright install chromium`).

**Read the module docstring first.** Every `core/` module opens with one, and
the modules that carry a design worth understanding — `ranking_index`,
`board_changes`, `rank_trend`, `recipes/__init__`, `rank_watch`, `watch`,
`queries`, `toolbox`, `facts`, `contract`, `timeline`, `telemetry`, `crit`,
`bindings`, `origins`, `metrics`, `cache`, `buttons`, `battle_views`,
`render` — explain their economics and their reasons there, and so does `qq_official.py` at the root. That is the module
map; this file does not repeat it, and a behaviour question is answered by the
docstring beside the code, not here.

Two other sources of truth this file defers to:

- **[UPSTREAM.md](UPSTREAM.md)** — what the public ZMDLogs API returns, field by
  field, verified in the field. Read it before adding an endpoint, widening a
  model, or trusting a field. Nothing else in the repository records the contract.
- **[README.md](README.md)** — the user-facing surface: every command and its
  syntax, every option's accepted spellings, the config table, what each LLM
  tool answers. Update it whenever any of those change.

## Commands

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -v          # full suite, offline, about 20 s
.venv/Scripts/python.exe -m ruff check main.py qq_official.py core tests tools    # lint (ruff.toml: E/F/I/W, py311)
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
group chats — plus the four LLM tools, and, on the QQ official bot with
callbacks on, the button tap, hooked through `qq_official.install_callbacks`
rather than a decorator. All of them run through one error guard,
`main._run_guarded`, so they cannot drift apart again.

## Invariants to preserve

- **`core/` imports no AstrBot.** Modules that need to log take a
  `core/logs.LogSink`, because AstrBot's plugin logger is not a `logging.Logger`.
- **Buttons are an addition on one platform, never a replacement.** Only the
  built-in WebSocket `qq_official` gets them (`qq_official.is_official`, asked
  per message); `qq_official_webhook`, `qq_official_v2`, the wild bot and every
  other platform get byte for byte the messages they got before, and so does
  the official bot with `disable_qq_official_buttons` on (its notices are still
  pushed by the plugin, as native pictures, see `qq_official`). Every button
  path falls back to that same output when its send fails — the plain pick
  list, the native picture — and reports a failure only when the fallback
  fails too.
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
  PNG, and so is a board notice (the 顶屁股通告, one per chat per interval;
  on the QQ official bot a markdown picture with no keyboard); a render
  failure returns a short error text, never a text fallback of the data, and
  a notice that cannot be drawn is not sent and waits for the next interval.
  The carve-outs: the 关注 / 取关 / 别名 / 绑定 replies are text
  (configuration actions, not query results), and the LLM tools send the
  picture *and* return text, because the text is what the model reasons
  over — except the board tool's 全部, which has no page since 榜单 became a
  pick list of dungeons (#17) and answers in text alone.
- **Rendering is gated twice**: concurrent captures capped, and the callers
  waiting for a slot capped too, with only the wait under `render_timeout_ms` —
  wrapping the capture itself would trip on a legitimately slow page. An
  overloaded bot answers 图片生成失败 while the reply is still worth having.
- **Every page kind declares its frame.** `render.PAGE_FRAMES` maps each
  kind to the 540 list frame or the 960 wide frame (ADR 0003), and the frame
  is the one source of the width: the template sets the root to it, the
  capture opens a viewport of it, and `_validate_page` checks the drawn page
  against it — `#zmd-root > .zmd-main` present, `--zmd-frame-width` declared
  equal to the frame, the root exactly the frame wide, the main panel ending
  inside it. A decoration that overflows widens the root and fails the
  render; a new page kind is added to `PAGE_FRAMES` or it cannot be drawn.
- **Template discipline.** Everything shared lives under `resources/shell/`.
  `shell/base.html` is the root: font links, colour and size tokens, the
  validation anchors. A list page extends `shell/list-base.html` (540), every
  other page `shell/wide-base.html` (960), and includes its own stylesheet
  into `styles`; the frame's shared CSS comes with the base, and wide pages
  share the macros in `shell/wide-parts.html` and the table core in
  `shell/wide-table.css`. The comic pages (帮助 and the 顶屁股通告) extend
  `shell/base.html` directly and share `shell/comic.css` (included by the
  page itself) and the macros in `shell/comic.html`. Anything two pages need
  goes into the shell, never into one page's stylesheet. CSS files render
  under autoescape like the templates.
- **The approved prototype owns the look** (ADR 0002): the pictures on the
  branch `prototype/frontend-v2` are the reference a page is checked against,
  and a change to one is a design change for the user to approve. Every
  colour and every font size is a token in `shell/base.css` (the `--fs-*`
  scale; a size off it is a design change, and
  `test_render.test_every_font_size_is_a_scale_token` pins the rule). Each
  concept has one word on every page, recorded in [CONTEXT.md](CONTEXT.md).
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
  to render, and the curve, buff band and 暴击期望 are sections the card must
  render without. A hit's crit roll that is unreadable or self-contradictory
  lowers 暴击期望's coverage; it never withholds the section, which is where
  `crit` departs from the site on purpose. Raw item ids are never printed as
  names (`is_raw_item_name` → 名称未收录), nor is a weapon 词条's key
  (`weapon_affix_name`); a raw buff key prints its effect
  instead of the key.
- **Top-3 pages must not leak ranking-only fields** (slug, percentile, DPS,
  roster, links) — enforced by
  `test_render.test_top_three_template_does_not_leak_ranking_fields`.
- **User-facing errors are short and generic**; log only the exception type or
  API code, never response bodies or stacks.
- **Help data (`core/help.py`) is updated whenever a user-facing command
  changes.** `test_routing_help` asserts the exact command list, and every
  `HelpCommand` carries a one-line `answers` statement (opens with its verb,
  no closing punctuation), because 阵容 / 角色统计 / `--角色` are
  indistinguishable from their syntax alone. The page shows each command
  without its `--options` (`HelpCommand.short`); every option a command
  takes is explained once in `OPTIONS`, and a test holds the two in step.
  Version comes from `metadata.yaml`; don't hardcode it.
- **Capture fetches images only from the configured origins.** The Playwright
  route handler only ever sees the first hop of a redirect chain, so allowed
  requests are fetched *inside* the handler with `max_redirects=0` and any 3xx
  is aborted — otherwise a redirect served by an allowed origin would pull an
  image from anywhere. **This has no unit test** (needs Chromium): verify with a
  two-local-origins script when touching `_route_asset_request`.
- **Every JSON store distinguishes missing from corrupt**
  (`core/persistence.py`) — a watch list is recoverable from nowhere else.
- **A chat is its group.** Everything kept per chat — watch lists, candidate
  lists, the auto-expand cooldown — keys on `main._event_origin`, never on
  `event.unified_msg_origin`, which AstrBot's 隔离对话 rewrites per member.
  `core/origins` explains the restore, and the one-time move of records
  written before it.
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
  common ← charts ← rail ← battle ← build ← compare, never back**;
  `battle_data` (数据) hangs off `battle` and nothing imports it, and the
  pages outside a battle import only `common` (and `boards`, for its rows
  and faces).
- **The LLM tool surface is four tools, one per subject** (榜单 / 战报 / 角色 /
  账号), never one per feature — README's 大模型工具 section states the four
  design rules and their reasons. What follows from them when extending:
  a new view of a subject becomes a parameter or extra lines in that tool's
  text, never a fifth tool; `core/facts.py` returns **computed** facts, never a
  raw array; and 循环 DPS or 专武收益 need a damage calculator and belong to
  `astrbot_plugin_zmdlore`, not here.
- **A notice claims only what two states of the board prove — and with two
  states it may say who pushed whom.** The old account notice could only say
  期间上方新增纪录: one read of an account's rank cannot tell who overtook
  it, and the row now above it is usually a bystander. The 顶屁股通告
  (#53) compares a board read with the board just before it — the index's
  held copy, or after a restart the top-N snapshot on disk — so
  `core/board_changes` knows which battle ids are new uploads and exactly
  which accounts each one moved down, and the page says 被顶下去的 and 新冠军
  outright. Keep the precondition, not the old wording: causal claims only
  from a diff of two states of one board, never from one read, and off a
  partial baseline (a snapshot holds the top N) only for a record that
  provably came above a record it holds — the module docstring says why.
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
  before it and must not come back: horizontal 20-second strips (the old
  desktop page's fixed width could not fit a name into a half-second cast), full-width
  vertical blocks ("一整块砸下来"), a hairline rail with dots (too thin to read
  without zooming), and a uniform 44 px track (the light-grey normal-attack runs
  read as columns of filler). The graded bar widths settled it.
- **Chart section order.** The 摘要 is the record band, then 伤害构成 →
  暴击期望 → DPS 曲线 (2026-10-05), and no per-character table: the numbers
  are 数据's. 排轴 (#43) is BUFF 覆盖, then the cast rail: the band's time
  runs left to right and the rail's top to bottom, so the one switch between
  them falls on the rail's heading. The order is older than V2 — the buff
  band was first placed inside the old card's 施法节奏 section, the user asked
  for it to be moved out, and the 技能轴 page that 排轴 replaced kept it. On
  排轴 the band's rows that differ only in their element (one source, target,
  value and set of spans) are one row, 寒冷/自然增幅 +47%, merged in the
  presentation layer, and the rail aims at 1100 px, down from the 技能轴
  page's 1600.
- **A battle's pages name one another from one table** (2026-10-05):
  `core/battle_views` lists 摘要 and the 详细视图 in the order the foot's strip
  prints them, and the QQ official bot's buttons under the picture are exactly
  the strip's other entries, by the same names. A view a battle lacks the
  data for (no skill statistics, no casts, no roster) is left out of both.
- **Elements get a filter, nothing else** (2026-09-06): no element chip, no
  element statistics page. The old pages also ringed each face in its
  element's colour; the V2 pages draw no element rings (the approved
  prototype has none, and the one ring left is the main C's yellow), so
  `--属性` and the element named after the profession on 养成 are all of
  elements the bot shows.
- **冠军 means a board's #1 record, credited to all four members** of that team
  (2026-09-06) — the 冠军 column and the sort key of the 角色冠军榜. First
  places as main C are a side column (当主 C).
- **One tool per subject** (2026-09-05), set when a seven-tool surface was cut:
  seven schemas cost ≈1445 tokens on every message in every group. The
  `zmdlogs_` prefix keeps these apart from the sibling plugin zmdlore's
  `*_endfield_*` wiki tools — as `query_endfield_character` the character tool
  was one verb away from `get_endfield_operator`, and the model reached for the
  wiki profile first on a 冠军 question.
- **Only a binding code binds** (shipped 2026-09-15). The unverified bookmark of
  the 2026-08 design is gone for good; unverified binding was judged an
  unacceptable impersonation surface and that judgement stands.
- **Binding commands answer in private chats too** (2026-10-05, #36),
  reversing "every binding command is group-only" (2026-09-15). That rule's
  reason — the bot adds nobody as a friend, so a private chat is not a place
  any of this is used from — was judged no reason to restrict: a binding is
  the person's, keyed on the platform user key and never on the chat, so
  绑定 / 解绑 / 主账号 / 我的 / `对比 … 我` answer wherever the person asks.
  Whether that key is one person's in both places on the official bot is a
  pending field test (below). 关注 is not a binding command and stayed open
  in private chats throughout (2026-09-30, #11 withdrawn). The reason has
  changed and the conclusion has not: a watch names boards, never a person
  (account watches went with #51), and a notice reads the same whoever
  asked for it, so a private chat is a chat like any other and gets the
  same 顶屁股通告 picture (#53).
- **The per-chat board of bound members is gone** (2026-10-05, #37), shipped
  2026-09-15 (`d5736c5`) and too little used to keep. With it went the only
  reader of which chats a person used a binding command in, so `bindings.json`
  no longer records that; a file that still has the list loads without it.
- **Cross-boss battle comparison is meaningless** and answers in text: every
  boss has its own rotation.
- **In a comparison, A keeps the order its own 养成 page shows and B is matched
  to it.** Sorting both sides aligned them but left neither matching the page a
  reader cross-checks against.
- **玩家排名** was named 玩家冠军榜 on 2026-09-06 (榜霸榜 rejected), then renamed
  on 2026-09-08 so it reads as the sibling of 角色排名.
- **rDPS got the full scope** on 2026-09-16, chosen knowing the rDPS boards hold
  1.5% of the records.
- **危机合约 词条 stay off the ranking rows and go on the 数据 view**
  (2026-09-19). `eb907ff` (2026-08-09) cut the tag chips from the ranking page
  — five columns plus chips read as noise — and kept only 合约分数; that ruling
  is about the *rows* and stands (`test_contract_ranking_only_shows_score`
  pins it). The battle page never had a ruling: its contract fields arrived a
  week later with the battle lookup and were parsed but never drawn, which is
  why this read as a deferred feature for six weeks. The record's own tags
  are drawn as one section — icon, name and score, grouped 队列 / 改写 /
  环境, each family's header carrying that family's tag count and subtotal —
  and nothing beyond that: no `description` (an unexpandable template, see
  UPSTREAM.md), no tier badge (the id's last digit usually matches the score
  but five tags disagree, and the score is what counts), no merging by tag
  base (names change between tiers), no "N of the catalog" denominator and
  no board-best reference (the catalog has no upstream endpoint, and a
  second upstream for a decorative figure was rejected). Neutral colour: 合约
  is data, not action. The section went on the old battle card after its
  identity; since the 摘要 replaced the card (#39) the 摘要 carries 合约分数
  alone, as its lead hero figure, and the tags section is the 数据 view's
  last.
- **The Chinese name is the market name; the code identity stays ZmdLogBot**
  (2026-09-19). `display_name` is 终末地·藕粉铺子 (藕粉 puns on 凹分) and the help
  page title follows it; the repository name, the `zmdlog` command, the
  `ZmdLogBotPlugin` class and every log prefix do not move. The V2 pages
  carry no wordmark: the foot says 数据来源 ZMDLogs and the version, and
  `render.py`'s `plugin.name`, the Latin `ZmdLog`, reaches only the
  document title.
- **Market metadata lives in `metadata.yaml`, and AstrBot itself ignores most of
  it** (2026-09-19). The cloud market reads `category`, `tags`, `social_link`
  and `support_platforms`; `StarMetadata` only knows the last two. Omitting
  `category` gets you `其他`, as it did for 1032 of the market's 2189 entries.
  It is set to the English key `entertainment`, not `娱乐`, because the
  dashboard's i18n table is keyed
  `ai_tools/entertainment/productivity/integrations/utilities/other` and the
  plugin detail page prints `[MISSING: …]` verbatim on a miss (the market list
  has the fallback the detail page lacks). The cost is a filter bucket of our
  own instead of joining the 394 plugins under `娱乐` — the category never
  appears on a market card, only in that dropdown, which is not worth the red
  text. The detail page's 更新日志 is `CHANGELOG.md` at the plugin root, read
  from the installed directory (none before 1.1.0, so it said 暂无更新日志); a
  release moves it together with the `metadata.yaml` version and the README
  badge.
- **The QQ official bot answers a private chat as it answers a group**
  (2026-09-28, reversing "no buttons in private chats"): pick-list buttons,
  the markdown picture and its buttons, callbacks and the notice picture all
  go to `post_c2c_message` as they go to `post_group_message`. The prototype tested
  groups only; each private path was field-tested as its ticket shipped.
- **Buttons stop at the plugin's own replies** (2026-09-28). The four LLM
  tools' pictures stay native pictures with no keyboard, and the auto-expand
  handler does not read links inside QQ cards. A button never uses `enter`
  (send on tap): in a group the platform only fills the box (measured), and
  using it in private chats alone would split the two; a one-tap page is what
  the callback switch is for.
- **Callbacks are the plugin's own minimal patch, not a ride on another
  plugin** (2026-09-28). The first plan used qqoffice_expand's taps when it
  was installed; it was dropped for the conflict `qq_official` describes, so
  qqoffice_expand is only detected and yielded to, and README says the two are
  incompatible. The `qq_official_v2` adapter gets no buttons. No AstrBot
  source change and no AstrBot PR from this repository.
- **Bindings on the wild bot and the official bot never merge** (2026-09-28),
  and `union_openid` is not used. On the official bot one person's openid is
  the same in every group (measured), so a binding works across groups and
  the bind reply carries no "this group only" caveat.
- **Field results the official-bot code relies on without a test**
  (2026-09-27/28): zmdlogs.com links in the bot's text go out and are
  clickable despite the platform's URL whitelist; a markdown picture keeps
  showing after its `raw_url` expires
  (the platform re-stores it). The button path costs one chunked upload,
  1.5–3 s; a slow page is slow in fetch and render (#14), not in the buttons.
  The prototype that measured all of this is the branch
  `prototype/qq-official-buttons` on GitHub — a record, never to be merged.
- **待实测: is one person's c2c `user_openid` their group `member_openid`?**
  (#36, 2026-10-05). AstrBot's `qq_official` adapter takes a private
  message's sender from `author.user_openid` and a group message's from
  `author.member_openid`, and the binding key is `qq_official:<that id>`. To
  check: bind in a group, then send the bot `/zmdlog 我的` in a private chat
  (and the reverse). 还没有绑定账号 means the two ids differ, and a binding made
  in one place is not known in the other — README tells users to bind again
  where it is not known. Record the result here; build no compatibility code
  for a mismatch.
- **When AstrBot ships keyboards** (#9809, #7868 or #9355), check whether it
  sends command buttons (action type 2) as well as callbacks; only then can
  `qq_official`'s own sends go, since the pick list and the sibling views
  depend on command buttons.

## Agent skills

### Issue tracker

GitHub Issues on `Statrue/astrbot_plugin_zmdlog` (public), via `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, each label named after its role (`wontfix` already exists). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root, created lazily. See `docs/agents/domain.md`.

## References

- AstrBot plugin dev docs: https://docs.astrbot.app/dev/star/plugin-new.html
  (and `plugin.html` for the event/config/html_render API)
- Upstream ZMDLogs source, as of its 2026-08-03 snapshot only:
  https://github.com/medps16000/endfield-suite-open (`endfield-logs/apps/api`).
  The live site has run unpublished code since; UPSTREAM.md says what still
  matches.
- Reference plugin for structure only:
  https://github.com/Entropy-Increase-Team/astrbot_plugin_endfield (AGPL-3.0;
  the first pages borrowed its background image and style, and neither is
  left)
- Planning notes under `docs/` are local and gitignored, except `docs/agents/`
  and `docs/adr/`.
  [UPSTREAM.md](UPSTREAM.md) is the source of truth for the API contract.
