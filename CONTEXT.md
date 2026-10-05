# ZmdLogBot

The vocabulary of the plugin's pages, replies and LLM tool text: what a
player-facing word means here, and which of its synonyms the pages never use.
CLAUDE.md's "one term per concept" list is the older seed of this file; a
term recorded there is not repeated here unless its meaning needed sharpening.

## Language

### Boards

**全部榜单**:
The boards the ZMDLogs site lists as its boards — 49 on 2026-10-01. Every
page and tool of the bot lives inside them: "all boards" anywhere in the
bot means these, and a board outside them is never read or counted.
_Avoid_: 热门榜, 所有榜单

**未收录榜单**:
A board the site still serves a ranking for but does not list: 协议空间,
支线任务, finished events, earlier challenge rotations. Named only where an
account's own records on one must be drawn. Whether such a board is retired
cannot be told from upstream, so the bot never says it is.
_Avoid_: 已下线榜单, 下线的榜

### Loadout

**潜能**:
A character's potential rank, 0–5, as recorded on the battle roster or the
ranking row. Printed as `潜能 N` on the battle card.
_Avoid_: 潜力, 星级

**精炼**:
A weapon's refinement rank, 1–6. Printed as `精炼 N` on the battle card.
_Avoid_: 精炼等级, 武器等级, 突破

**词条**:
One of a weapon's two attribute lines, named as the game names it
(`敏捷提升·大`): 词条 1 the base attribute, 词条 2 the second. Beside them
the 养成 page draws the weapon's own skill as **武器技能**, whose level the
精炼 caps (精炼 + 3). A line the bot cannot name is 名称未收录.
_Avoid_: 副词条, 武器属性

**养成**:
How far one fielded character is built: its level, 潜能, 精炼, skill
levels, weapon and gear — everything the 养成 view of a battle draws. Its
shorthand is the pair 潜能 + 精炼, written `5+6` under an avatar on
ranking-style pages (not on the V2 board ranking, 账号 or 角色排名 <角色>,
whose rows show faces, time and DPS only), in the 角色档案 records column
and as the 养成组合
shares on 角色档案 (`0+1`, `5+6`, …), `5+?` when the weapon is
unrecorded — the site's own shorthand; a player reads it there first.
_Avoid_: 练度, 培养, 配装

### Character pages

**角色档案**:
The page answering how a character is built and how fast it runs: its
养成 combinations, weapons, equipment and teammates as shares of its
public records, and its 通关名次 on every board. Cut to one board
(`--榜单`), the shares are that board's and its records replace the list of
boards. Backed by the site's character page, which uses the same word.
_Avoid_: 角色配装, 角色养成, 角色资料

**通关名次**:
A character's position on one board when every character is ordered by the
fastest team clear it appears in; ties share a rank. Distinct from 名次,
which is a record's DPS position on a board.
_Avoid_: 竞速名次, 速通排名, 角色名次

### Battle statistics

**暴击率**:
The share of a character's hits in this battle that actually crit — a
realised share, not the crit-rate attribute on the character panel. The
column keeps the site's own name so a player can match it to the web page.
_Avoid_: 暴击占比, 暴击概率

**暴击期望**:
What a battle's damage would have been at the recorded crit rates, hit by
hit, with the skills and hits fixed as they happened; the site's 暴击分析
panel. Available only on battles uploaded by clients that record per-hit
crit parameters (from 2026-09-25). The luck percentile the site adds is not
part of this concept here.
_Avoid_: 期望伤害, 暴击运气

### Page structure

**摘要**:
The page a battle query draws by default: the battle's identity, its hero
figures (用时, 总 DPS, 总伤害, 阵容), 伤害构成, 暴击期望 and the DPS 曲线.
It answers "how did this run go"; everything else is in a 详细视图.
_Avoid_: 概览, 简版, 首页

**详细视图**:
One of the three pages a battle opens beside its 摘要, each its own
command — **数据** (the full character table, team contribution, 暴击期望,
the DPS 曲线, 技能伤害统计, the 合约 tags), **排轴** (BUFF 覆盖, then the
cast rail) and **养成** (each character's level, 潜能, skills, weapon and
gear). The foot of every battle page lists 摘要 and the three, the current
one lit; on the QQ official bot they are buttons as well. A view the
battle lacks the data for is left out of both. ADR 0003.
_Avoid_: 子页, 详情页, 二级页, 曲线, 施法, 配装 (the views before 1.3.0)

**页**:
Ten consecutive rows of a board ranking, page 1 being 名次 1–10; asked for
with `--页 N` (`--page N`, `-p N`). The rows are the ones the filters keep,
so page 2 of `--角色 黎风` is that filter's rows 11–20, each still showing
its board rank. `--页 全部` draws the first thirty on one picture, with a
strip sending the rest to the site; the picture never says a next page
exists. Only the board ranking has pages; 阵容 counts its first ten.
_Avoid_: 前 N 名, top
