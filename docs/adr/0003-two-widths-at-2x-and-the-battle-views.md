# Two widths at 2×, three battle views, and only the ranking is paged

Settles what ADRs 0001 and 0002 left to the prototype (2026-10-05, after
every V2 page family was approved on `prototype/frontend-v2`, `38726f7` and
`289b2c9`; release 1.3.0, spec #32).

- **Two widths.** The board ranking is a 540 px phone list, read without
  zooming; every other page — battle, comparison, account, trend, character
  pages, the other lists, help, notices — is 960 px wide, the battle card's
  width, opened and zoomed on a phone. A page kind declares its width; the
  capture opens a viewport of it and validation checks the drawn root
  against it (`core/render.PAGE_FRAMES`).
- **Both captured at 2×.** Measured on the longest wide page, 角色排名 洛茜
  (960 × ≈3850 CSS px, 192 avatars), with the production capture's own
  steps (linked fonts served from memory, images prefetched, the same
  settle), five runs each after a warm-up:

  | | 2× | 1.5× |
  |---|---|---|
  | production host (Intel Core 3 N355, 2 cores, Chromium in the `astrbot` container) | 2.62 s median (2.54–2.65) | 1.81 s (1.80–1.84) |
  | dev box (Windows 11) | 1.06 s (1.04–1.08) | 0.74 s (0.73–0.76) |
  | PNG | 1920 × 7710, 4.26 MB | 1440 × 5783, 2.78 MB |

  The render budget, `render_timeout_ms`, is 30 000 ms by default; it bounds
  the wait for a capture slot and each Playwright call inside the capture,
  and the wait for fonts and images is capped at 5 s. 2.6 s is under a
  tenth of the budget, so 2× stands and 1.5× is not needed. What 2× costs
  instead is the file: 4.3 MB for the longest page, against 2.8 MB at
  1.5×, paid in the QQ official bot's chunked upload. If uploads of the long wide pages turn
  out slow, 1.5× for `WIDE_FRAME` is the one-line fallback, measured above.
- **A battle draws its 摘要 with the DPS 曲线 in it**, and three 详细视图
  beside it, each its own command: **数据** (the full table, team
  contribution, 暴击期望, DPS 曲线, skill damage, 合约 tags), **排轴**
  (BUFF 覆盖, then the cast rail) and **养成** (levels, 潜能, skills,
  weapon and gear). The 曲线 view of ADR 0001 is gone — its DPS curve went
  to 摘要 and 数据, its BUFF band to 排轴 — 施法 is renamed 排轴 and 配装
  养成. The old commands (配装, 装备, 技能, 技能统计, 技能轴, 时间轴) are
  dropped without aliases.
- **Only the board ranking is paged.** Ten rows a page, `--页 N` /
  `--page N` / `-p N`; `--页 全部` (`all`) draws the first thirty on one
  picture. Filters and the metric hold on every page, and the header counts
  the rows they keep. A page past the last answers "共 N 页" in text.
  `--top` is gone from every command and answers with a pointer to `--页`;
  阵容 counts a fixed first ten. Account, trend and 角色排名 <角色> are one
  wide table each, never paged.
- **The picture never says there is a next page** (ADR 0002 stands), with
  one exception: the 全部 view of a board holding more than thirty rows ends
  in a strip, "其余 N 条记录请到 ZMDLogs 官网查看", since the reader has asked
  for everything and the picture stops short of it.

How the shell carries this: `resources/shell/base.html` holds only what
every V2 page shares (the root `#zmd-root` at the frame's width, declared as
`--zmd-frame-width`, the fonts, the palette, the type scale, and the
`.zmd-main` panel validation requires); `list-base.html` (540) extends it,
and the wide shell and help extend it in their own tickets. Old pages keep
`resources/common/base.html`, the 1280 px 1× frame and its checks until
their family migrates.

Considered and rejected: 1.5× for the wide pages from the start — a third
faster and a third smaller, but the measured 2× is well inside the budget,
and the wide pages are the ones a phone reader zooms into.
