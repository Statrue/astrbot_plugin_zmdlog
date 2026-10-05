# The pages are budgeted for a phone's width, and Design Skill V2 owns their look

Every result picture was laid out as a 1280 px desktop page captured at 1×.
A phone shows a picture at its own width — about 390 pt — so the page reached
the reader at ≈0.3×: body text set at 11–13 px read at ≈3.7 pt, the 52 px
hero numeral at ≈16 pt, and reading any value took a pinch. PC QQ was no
better, plausibly because its viewer fits a tall picture to the window's
height.

We decided (2026-10-03):

- **The phone is the reading target.** A page passes when its hero figures
  and its main columns read in phone full-screen view without zooming. The
  budget is one phone width per row: information text at least 12 pt at
  viewing size, about four to five numeric columns across. Side-by-side
  panels and multi-column tables are allowed within that budget; a row
  carrying more is not.
- **Endfield Design Skill V2 owns the look**, including its readability floor
  (12 px at viewing size, 4.5:1 body contrast). Every visual and content
  ruling in CLAUDE.md's _Decisions recorded only here_ may be reopened by the
  refactor — the cast rail encoding, the chart order, the element rings, the
  font scale — and an explanatory `//` subtitle or a field the reader does
  not need may be cut. Rulings about what data *means* (冠军, Top-3 not
  leaking ranking fields, DPS/rDPS never mixed) are not visual and stand.
- **A battle draws a 摘要 by default**; the rest moves to four 详细视图
  (数据 / 曲线 / 施法 / 配装), buttons on the QQ official bot and a printed
  command hint on every platform, so the "buttons are an addition" rule
  holds.
- **A board ranking is paged**, ten rows a page, `--page` / `-p` / `--页`;
  `--top` is removed outright from 榜单, 阵容 and 群榜.
- **No dark theme** in this refactor. The Skill's catalog leans light and has
  no dark-mode rule; a picture cannot follow the viewer's system theme.

Settled only by the prototype (`prototype/frontend-v2`, battle card and
ranking page first): the CSS width and capture scale that express the budget
(540 px at 2× is the candidate), whether the 摘要 carries a DPS curve, and
the ranking row's layout. Those land in this ADR's successor, not here.

Considered and rejected: keeping 1280 px and only enlarging the type — the
same picture on a phone, with every size on the scale tripled; one full
report per query — the battle card was seven phone screens long.
