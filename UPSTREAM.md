# Upstream facts · ZMDLogs public API

What the public ZMDLogs API actually returns, verified in the field: against
live responses from zmdlogs.com and the site's production build (its
server-rendered pages and the JavaScript chunks they load). This file is the
source of truth for the contract: the repository holds no other record of it,
and `core/client.py` / `core/models.py` only implement it.

Upstream's public repository used to be the second witness. It stopped
tracking the live site after 2026-08-03 (see *Sources and dating*), so a fact
here cites it only where it still matches what the site serves, and nothing
learned since cites a commit.

Read this before adding an endpoint, widening a model, or trusting a field.
Dates mark when a fact was verified in the field; a date or a cause that no
source states is marked **inferred**.

## Sources and dating

**The public repository no longer tracks the site** (checked 2026-09-30).
[endfield-suite-open](https://github.com/medps16000/endfield-suite-open) has
one commit on `main`, `4e65587` "Initial open-source release" of 2026-08-03.
Its other branches are dependabot bumps, the latest `fe2715e` of 2026-09-27
under `endfield-pcap/`, and none touches the API or the web app. The live
site runs code that was never published: the snapshot has no
`/api/battles/users/binding-code`, no `/api/battles/users/search` and no
`/api/characters/*` route, and no field added in 2026-09 appears in any public
commit (GitHub code search finds `critDamageBonus` nowhere).

The snapshot is still cited where it matches — `endfield-logs/apps/api/app/services/public_data.py`
for the board seeds and the statistics rules, `apps/web` for the display rules
the plugin mirrors — and those citations are to `4e65587`, not to what the
site runs today.

**How a fact is verified now:**

- *live* — seen in a live response or page, on the date given;
- *bundle* — read in the site's production JavaScript
  (`https://zmdlogs.com/_next/static/chunks/…`). The code is minified, so its
  logic is recorded here in plain form; chunk names are content-hashed, change
  on every deploy, and are given only to date the reading;
- *inferred* — follows from the evidence, but no source states it. Always
  labelled.

Every endpoint below was re-fetched on 2026-09-30 and answers in the shape and
with the error paths recorded; counts and survey figures keep the date they
were taken. The 2026-09 additions are in their sections. Every change to a
response's shape was additive, and `core/models.py` parsed every live payload
unchanged.

**When the 2026-09 changes shipped — inferred.** No commit dates them. The
first-party evidence is the `Last-Modified` header on the site's assets, which
records a deploy's file time rather than a release, and the parser version
stamped on uploads:

| Evidence | `Last-Modified` (UTC) |
| --- | --- |
| Crit worker chunk `72.646043a7bf1bd763.js` | 2026-09-21 17:11 |
| Client download `EndfieldLogsClient_3.0.6_20260923.zip` | 2026-09-22 22:47 |
| Character card art `chr_0016_laevat.f54a4332b9ea.webp` | 2026-09-25 15:55 |
| Current build: home, character page, battle chunk `25-c726b50edd3aa978.js` | 2026-09-25 17:35 |

A parser-v48 upload of 2026-09-14 carries no per-hit crit data; six parser-v57
uploads of 2026-09-25 to 2026-09-30 all do. Inferred from both: the crit data
arrived with parser v57 and client 3.0.6 around 2026-09-21 to 09-23, and the
character directory and the profile endpoint with the build of 2026-09-25.
When the ranking responses gained `characterPotential` / `weaponRefine` is
unknown: this file never listed ranking-row fields, the test fixtures are
hand-written, the bot's hot-bosses snapshot is overwritten on every fetch,
and the Wayback Machine holds no copy of the site.

## Site pages

The paths the plugin links to answer 200 as of 2026-09-30: `/battle/{id}`,
`/axis/{id}`, `/boss/{slug}` (also `?metric=rdps`), `/boss/{slug}/statistics`
and `/records/{accountId}`.

Since the build of 2026-09-25 (inferred, above) the home page is a 角色档案 directory — every
character, filtered by profession and element — plus four quick links to
boards; the top-3 cards it used to draw from `hot-bosses` are gone. Its data
is embedded in the server-rendered page, and no public endpoint for it was
found: `/api/characters`, `/api/characters/catalog`, `/api/characters/{key}`,
`/api/home` and `/api/home/characters` all answer 404. The embedded entries
carry `englishName`, `rarity`, `sourceCharacterId` and art paths the game-data
catalog lacks; `/api/game-data/character` stays the plugin's catalog source,
and its professions and elements agree with the directory's.

A directory entry opens `/character/{characterKey}?range=&boss=`, titled
`{name} · 配装与竞速统计`, backed by the profile endpoint below.

## Response times

Seen 2026-09-30: the rankings of `indie_battletower007_ex` took 7.8 s once and
2.6 s on repeat, and the 1.6 MB detail of the v31 contract battle took 10.2 s.
Both fit the client's 15 s budget, but not by a wide margin.

## Asset URLs

`characterAvatarUrl` / `avatarUrl` in hot-bosses, rankings and battles are
usually **site-relative paths** (`/images/...`), occasionally absolute CDN
URLs — resolve them against `web_base_url` before rendering.

The export carries no avatar URLs at all; portraits there use the observed
`/images/character/charremoteicon/icon_{key}.png` convention, and an
`<img onerror>` drops a wrong guess. Same for a weapon or equip piece whose
`iconUrl` upstream omits (its catalog does not know the piece yet, while the
file exists): the conventional paths are
`/images/equip/iconbig/{itemId}.png` and `/images/weapon/icon/{weaponTemplate}.png`.
All four conventional paths — `charremoteicon`, `weapon/icon`, `equip/iconbig`
and `contract-tag` — still answer 200 (2026-09-30).

The home page's new art lives under `/images/character/game-art/{cards,full}/…`
and `/images/character/game-avatars/…`, with content-hashed file names that
cannot be guessed without the page's embedded data.

## Boards

Board slugs come from static `BOSS_SEEDS` (snapshot); the crisis-contract
board is `indie_group_ccdg` (`bossName` 破潮之像, `dungeonName` 危机合约).
Unknown slug → 404 `boss_not_found`. `hot-bosses` is the only board/dungeon
index there is — there is no catalog endpoint.

### `hot-bosses` has lost its visible consumer

`GET /api/home/hot-bosses` is live on 2026-09-30 and unchanged in shape but for
two run fields (see the next section): 49 cards
`{bossSlug, bossKey, bossName, dungeonName, topSpeedRuns[]}`, 48 with three
runs and `indie_battletower011_ex` (重伤之围·残酷) with none. But the home page
that drew its top-3 cards no longer does, and whether any site page still
calls it cannot be seen, since every page that could is server-rendered. An
endpoint with no visible consumer is a retirement risk.

Two responses list the statistics boards and could stand in for it, both with
gaps:

- the profile endpoint's `bossOptions[]` — 48 boards, each with `bossSlug`,
  `bossName`, `dungeonName`;
- the rows of `/api/characters/{key}/boss-statistics` — the same 48.

Neither carries `bossKey`, and neither lists the crisis-contract board, which
statistics exclude. Nothing uses them as an index; this records the risk only.

## `GET /api/bosses/{slug}/rankings`

One response is the whole ranking, every public record: 215 KB for the busiest
board on 2026-10-01. Measured that day, so that nobody tries again:

- the response carries no `ETag`, `Last-Modified` or `Cache-Control`, so there
  is no conditional request to make;
- `limit`, `pageSize`, `page`, `top` and `since` are all ignored: the response
  bytes are identical with and without each;
- `HEAD` answers 405.

Every re-read therefore downloads the full ranking, which is why
`core/ranking_index` re-reads a ranking less often the longer it has gone
unchanged.

## `metric=dps|rdps`

Verified live 2026-09-16; anything else → 422 `literal_error`.

Accepted by `/api/bosses/{slug}/rankings`,
`/api/bosses[/{slug}]/character-statistics` and
`/api/characters/{key}/boss-statistics`. Ignored by
`/api/battles/users/{id}/rankings` (identical ranks with and without it on 14
boards). `hot-bosses` carries only `topSpeedRuns`.

The rDPS ranking lists the records whose `battle.rdpsRankingEligible` is true —
sampled: `raw-log-parser-v51` uploads yes, v46 / v48 no, every eligible record
on the sampled board fought after 2026-09-14 17:16 (+08) — **in the same
clear-time order**, ranks dense from 1. 31 of 2077 records (1.5%) on 11 of 49
boards, at most 12 on one board, and 3 of the 31 were not on the DPS board, so
it is not a strict subset. Team `dps` equalled `rdps` on every sampled row;
what changes is the main C, which becomes the top-rDPS character on 17 of 28
shared records (usually a support). `rdpsStrictOk` / `rdpsPreflightOk` were
false even on eligible records (blocker counts 300–800): meaning unknown, not used.

rDPS character statistics are thin: 43 battles / 133 samples, 4 of 17
characters ranked (DPS: 7630 / 23365 / 17).

A rankings request without `metric` is still byte-identical to `metric=dps`
(2026-09-30).

## `characterPotential` / `weaponRefine` on ranking-style responses

Verified 2026-09-30. This is the pair the site prints as `5+6` under an
avatar; its own markup reads `aria-label="潜能 5，武器精炼 6"`. The first number
is the character's potential (0–5), the second its weapon's refinement (1–6).

- **Board rankings** (`/api/bosses/{slug}/rankings`, DPS, rDPS and the
  contract board alike): each row, and each entry of the row's
  `rosterEntries[]` (`characterKey, characterName, profession, avatarUrl`),
  now carries `characterPotential` (int 0–5; present on all 280 rows and all
  1120 entries of `indie_battletower007_ex`) and `weaponRefine` (int 1–6, or
  `null` for an unrecorded weapon: 7 rows, 26 entries). The row-level pair is
  the main C's: it equals the main C's own roster entry on every row.
- **`hot-bosses` runs** (`topSpeedRuns[]`): the same two fields, both
  nullable; present on all 144 runs, one run null in each.
- **`/api/battles/users/{id}/rankings`**: each best record gained a whole
  `rosterEntries[]`, in the board rows' shape.

The board-ranking pairs equal the battle detail's
`roster[].characterPotential` / `roster[].weapon.weaponRefine` for the same
record (all four roster pairs of `btl_upload_338dd470b6ac`); the `hot-bosses`
and account-ranking copies were not checked against a battle. **Inferred:**
all of them are read from the stored roster, not measured anew.

## Battle detail

In battle detail every `roster[].accountDisplayName` is the uploader nickname,
so `roster[0]` is a valid uploader name source;
`GET /api/battles/{id}/share-summary` also returns `uploaderNickname` directly.

### `battle` — four timer fields, meaning unknown

`officialTimerObservedElapsedMs` (int|null), `packetTimerElapsedMs` (only
`null` seen), `timerIntegrityOk` (bool|null), `timerIntegrityReasons` (list).
On the v48 battle and the six v57 battles sampled 2026-09-30,
`timerIntegrityOk` is true and the reasons are empty.
`officialTimerObservedElapsedMs` sits within 6 ms of `durationMs` on six of
those seven (69333 against 69339), but `btl_upload_f239236b857f` reads 136419
against 130488 — 5.9 s apart with integrity still true. On the v31 contract
battle all three scalars are `null` and the reasons `[]`. No source says what
they mean, and nothing reads them.

### `participants[]` — `critRate` is realised, not a stat

`critRate`, the site's 暴击率 column, is the share of the character's hits in
this battle that crit — not the character's crit-rate attribute. On three v57
battles (2026-09-30) it equals the observed `isCritical` share (`hitContext.critical`,
below) for every character to 3–4 decimals: 莱万汀 0.0547 against 0.055.

### `roster[]` — the loadout recorded at upload

`slot` (1-based), `characterElement`
(`physical|fire|cryst|natural|pulse` = 物理/灼热/寒冷/自然/电磁),
`characterLevel`, `characterPotential`,
`weapon{weaponName, weaponLevel (often null/0), weaponRefine, iconUrl, skills[{skillKey, level, potentialLevel}]}`
where `sk_wpn_*` is the weapon's own skill and `wpn_attr_*` / `wpn_sp_attr_*`
are affixes (词条, below), `equips[]` (4 pieces: 护手 / 护甲 / 配件 ×2) and
`skills[{skillKey, level}]` (`_attack1..5`, `_normal_skill`, `_combo_skill`,
`_ultimate_skill`, talents at level 1).

`equips[]` detail: `pieceName` falls back to the raw `item_equip_…` id with
`suitName` null when the catalog has no name;
`enhanceLevels[{index, level}]`; `stats[{slot: main|sub1..3, name, value, level}]`
with ratio stats as fractions (e.g. `0.1495`) and occasionally untranslated
names `Main` / `副能力`.

**A stat `name` can be the game's attribute enum by number,
`attribute_type_N`, instead of the Chinese label** (live, 2026-10-05). Over the
gear of 180 board battles it appears on parsers v48–v57 and on none older,
mixed with Chinese names inside one battle, one character and even one piece
(`btl_upload_c5cf4810aa09`). Numbers seen: 0, 3, 40, 41, 42, 44, 87. They are
the ids of `/api/game-data/attribute_type` (below), which names 3 `Def`,
39–42 `Str` / `Agi` / `Wisd` / `Will`, 44 `AttributeType44` with icon
`icon_ultimate_sp_gain_scalar` and 87 `PhySpellUp` with `icon_originium_arts`.
The values agree with the Chinese spellings of the same survey: every
`_3` value is a main-slot 防御力 value (21 / 42 / 56), `_44` values
(0.160179, 0.266964, …) are 终结技充能效率 values, `_87`'s 53.82 / 44.85 /
26.91 are 源石技艺强度 values, and 40–42 take the value table the four
primary attributes (力量 / 敏捷 / 智识 / 意志) share, so the values alone cannot
tell them apart — the catalog name does (**inferred** that the battle's
numbers are the catalog's ids; nothing states it). 39 (`Str`, 力量) was not
seen and is mapped from the catalog alone. `attribute_type_0` (`AttributeType0`, no icon) carries
values of several different stats (0.299, 0.269123, 35, 23) and names none.
`core/loadout.stat_label` maps the numbers above and prints any other
non-Chinese name as 名称未收录, the one term for any raw id.

**`suitName` is not a property of the item and must never be used as an
identity or as the truth.** Surveyed over 270 battles: 17 of 129 item ids come
back under more than one suit, `item_equip_t4_suit_usp02_body_03` alone under
eight, and 26 times the same item id disagreed with itself on two characters of
the *same* battle. `item_equip_t4_suit_spellburst_hand_01` was once labelled
长息, which is the name of `suit_usp02`. Compare and group gear by `itemId`,
take the suit name from the game data catalog, and treat `suitName` as a
fallback label only. `独立装备` is what upstream calls a `_parts_` piece, which
belongs to no suit.

Older uploads (crisis-contract era, parser < v34) have empty weapon skills /
stats / enhance lists.

**Weapon 词条 keys carry no name; the key spells it** (2026-10-05). A
weapon's `skills[]` hold three entries in no fixed order — `wpn_sp_attr_*`,
`sk_wpn_<template>`, `wpn_attr_*` is how `btl_upload_c5cf4810aa09` sends them.
`wpn_attr_{stat}_{tier}` is the base attribute (词条 1) and
`wpn_sp_attr_{kind}_{tier}` the second (词条 2); the tier is `low` / `mid` /
`high` = 小 / 中 / 大. The names are the game data catalog's: each weapon's
`skilllist` in `/api/game-data/weapon/{id}` lists the two 词条 by name with
the stat they raise as the blackboard key, which the skill catalog's
`binaryProbe` hints tie to the key. Stats: `str` 力量, `agi` 敏捷, `wisd`
智识, `will` 意志, `main` 主能力; kinds: `atk` 攻击, `hp` 生命, `heal` 治疗效率,
`crirate` 暴击率, `usgs` 终结技充能效率 (not 源石技艺), `phy_spell` 源石技艺强度,
`phydam` / `firedam` / `crystdam` / `electrondam` / `naturaldam` 物理 / 灼热 /
寒冷 / 电磁 / 自然伤害, and `magicdam` 法术 — the catalog's name is 法术提升
though it raises 法术伤害. Each name is `<stat>提升·<tier>`. On 120 board
battles every key seen named the same catalog name every time. `cridmg`
(`wpn_sp_attr_cridmg_high`) is in the skill catalog but on no battle and on no
weapon whose detail answers, so 暴击伤害 is **inferred**. The skill catalog
also lists `wpn_sk_atk` / `wpn_sk_cscd` / `wpn_sk_ussp` and
`wpn_sp_normalattack_high`, never seen on a battle and named nowhere; the
plugin prints any key it cannot name as 名称未收录. The weapon skill's level
is capped by the 精炼: 精炼 r allows r + 3 (the boards: 精炼 1 at 4, 6 at 9,
2–5 at 5–8), though 3 of 399 board weapons at 精炼 1 carried a level of 5 or
8. The weapon skill's own name (`skilllist`'s last entry) is the catalog's
only; the battle has none.

### `roleSkillStats[]`

`characterName, skillKey, skillName, castCount, hitCount, totalDamage, avgDamage, maxDamage, maxHit, displayGroupKey, displayGroupName`,
one row per damage source: the normal attack chain is one row per segment,
`skillName` may be a raw key or an English placeholder (`burning status`,
`poise can be breaking attacked`), and `buff_common_*` rows are engine-side
damage (element reactions, statuses).

`hitCount`, `maxHit`, `displayGroupKey` and `displayGroupName` are absent from
the `4e65587` snapshot's schema and were first recorded here on 2026-09-30.
They are served on v31, v48 and v57 records alike, so they are not gated on the
parser version.

- **Casts and hits.** `castCount` counts casts and `hitCount` hits (never
  fewer). `avgDamage` is per cast, `totalDamage / castCount` on every row
  sampled. `maxHit` is the largest single hit, and `maxDamage` stays the
  larger per-cast figure (above `maxHit` on 37 of 48 rows). On two battles
  (v48, v57) every row's `hitCount`, `maxHit` and `totalDamage` equal the
  count, maximum and sum of the `laneType: skill` timeline events with that
  `eventKey` and `value > 0`. The site's tooltip prints `· {hitCount} hit`.
- **The game's own skill name.** `displayGroupKey`
  (`official:{char}:{char}_{NormalAttack|NormalSkill|ComboSkill|UltimateSkill}`)
  and `displayGroupName` group the rows of one in-game skill under the game's
  name for it: `chr_0016_laevat_normal_skill` and
  `…_normal_skill_during_ult` are both 焚灭, `chr_0013_aglina_attack3` is
  秘杖·束能技艺. Both are `null` on the buff and status rows and on many
  projectile and mechanism rows (`…_projhit`, `…_abilityentity`,
  `…_abilityrange`): 22 of 48 rows on a v57 battle, 18 of 36 on a v48 one.
  Where it is set it usually equals `skillName`. It differs where
  `skillName` is a token join or a segment label (梨诺's
  `ultimate / skill / 派生 / l` is 晨星的协奏曲, her `A1` 怦然星动, on v46–v48
  uploads) and on 诀's `power_attack` (重击, in 重火力截击). The plugin reads
  `displayGroupName` only, as a row's name (`core/loadout.py`); it does not
  regroup rows by `displayGroupKey`, and it does not read `hitCount` or
  `maxHit`.

### `timelineEvents[]` and `characterStates[]`

`timelineEvents[]` (`tsMsFromStart`, `laneType: skill|buff`,
`sourceCharacterName`, `value`, `durationMs`, `effects[{zone, element, rate}]`,
`poiseDamage{value, current_value}`, …) is the per-hit log: 171 events / 0.2 MB
for a 22 s run, 1868 / 1.9 MB for a six-minute one, parsed in ~12 ms. Its
damage values sum **exactly** to `battle.totalDamage`, and per
`sourceCharacterName` to each `participants[].totalDamage` — which is what
makes the DPS curve reconcile with the table above it.

`characterStates[]` (`characterName`, `loadout`, `initialBaseline`,
`buffsReceived|buffsGiven|debuffsApplied[{eventKey, eventName, sourceCharacterName, targetCharacterName, startTsMsFromStart, durationMs, effects}]`)
is the same buff data pre-grouped per character, and is the source the buff band uses.

`poiseDamage` (失衡) is present but deliberately **not** drawn: speed runs
sample it a handful of times and never break it, and there is no cap field
(upstream's 上限 is the max observed), so the lane would be a flat line on
exactly the fights people look at.

### `timelineEvents[].hitContext.critical` — the input of 暴击期望

The snapshot's schema already declares `hitContext` (`dict | None`), and it is
`null` on every event of older uploads (a v48 battle of 2026-09-14, the v31
contract battle). From parser v57 on — v57 is the newest version seen; the
earliest v57 upload seen is of 2026-09-25, and the earliest there is was not
located — every skill hit with `value > 0` carries `hitContext.critical`: 3937
of 3937 hits over six battles, 2026-09-30. It has one of two shapes:

```text
verified     (3782 hits)
{"version": 1, "status": "verified", "isCritical": true, "critRate": 0.05,
 "critDamageBonus": 0.5, "uncappedDamage": 581, "damageCap": 1477030,
 "source": "bound_entity_attributes_and_exact_damage_unit"}

unavailable  (155 hits)
{"version": 1, "status": "unavailable",
 "reasons": ["part_damage_roll_relation_unavailable"]}
```

`damageCap` is int|null (null on 175 hits). `uncappedDamage` was never null,
but the site treats it as optional. All 155 `unavailable` hits are on one
battle, `btl_upload_f6db0e45723d`. `critRate` was 0.05 on 3607 hits and 0.0 on
175 — **inferred** to be the game's base rate; whether it ever includes crit
buffs is unknown, since every sampled team fielded 莱万汀. `source` had the one value shown.
Old records cannot gain it: the site's empty state says they did not save the
per-hit crit parameters, and that newer clients upload them with new battles.

**The site's formula** (bundle, build of 2026-09-25: hit selection in the
battle chunk `25-c726b50edd3aa978.js`, the maths in the Web Worker
`72.646043a7bf1bd763.js`), computed in the browser:

1. **Select the hits.** Timeline events with `laneType == "skill"` and
   `value > 0` — of one character, when one is selected. A hit is analysed
   when `critical.version == 1`, `status == "verified"`, and `isCritical`,
   `critRate` and `critDamageBonus` are well-typed. Coverage is analysed
   damage over all selected damage; the panel prints 已覆盖 x% 伤害 when it is
   under 100%.
2. **Per hit**, with `d = value`, `p = critRate`, `B = critDamageBonus`,
   `crit = isCritical`:

   ```text
   n    = uncappedDamage ?? d
   cap  = damageCap ?? ∞
   base = crit ? n / (1 + B) : n          # the non-crit roll
   lo   = min(base, cap)                  # damage without a crit
   m    = min(base · (1 + B), cap) − lo   # what a crit adds
   if p == 0 or m == 0:  fixed += lo
   elif p == 1:          fixed += lo + m
   else:                 fixed += lo;  mean += m·p;  var += m²·p·(1 − p)
   actual += d
   ```

3. **Totals.**

   ```text
   expectedDamage     = fixed + mean
   expectedDps        = 1000 · expectedDamage / battle.durationMs
   damageDifference   = actual − expectedDamage
   relativeDifference = damageDifference / expectedDamage     # 实际偏差
   standardDeviation  = sqrt(var)
   ```

The worker refuses the whole computation, with an error, on any of five
inconsistent inputs:

1. `critRate` outside [0, 1];
2. `critRate == 0` on a hit that crit;
3. `critRate == 1` on a hit that did not;
4. `uncappedDamage < value`;
5. `damageCap < value`.

Check figures, reproduced in Python on 2026-09-30 (all hits analysed):

| Battle | Actual | Expected | 实际偏差 | Expected DPS |
| --- | --- | --- | --- | --- |
| `btl_upload_960a800ae840` | 3,375,148 | 3,300,712 | +2.26% | 43,941 |
| `btl_upload_9e9fe2ffaf34` | 9,725,405 | 9,743,537 | −0.19% | 140,520 |
| `btl_upload_f239236b857f` | 4,939,659 | 4,824,702 | +2.38% | 36,974 |

The panel also prints a luck percentile, 暴击运气位于 前 N%. It is
`P(X < actual − fixed)` for `X = Σ Bernoulli(pᵢ)·mᵢ` over the stochastic hits,
ties counted half, shown as 前 {100·(1 − upper)}% (前 1% 以内 below 1%). The
distribution is exact for ≤ 16 stochastic hits or equal weights, otherwise a
histogram convolution on floor/ceil grids of 128–65536 bins, refined until the
bracket is under 0.002; more than 20000 stochastic hits is an error asking for
one character. This half was read in the bundle, not reproduced.

### `contractTagScore` and `contractTags[]` — 危机合约

Both ride on `battle`, on ranking rows, on `hot-bosses` runs and on user
rankings; off the contract board (`indie_group_ccdg`) they are `null` / `[]`.
Surveyed over the whole board, 15 records, 2026-09-19, and re-surveyed
2026-09-30 with the same result except for the null icons:

- Each tag: `tagId`, `score` (1 | 2 | 3), `name`, `description`, `iconId`,
  `iconUrl` (site-relative, `/images/contract-tag/icon_activity_contract_tag_NNN.png`,
  one sprite per tier, white on transparent bar one yellow; **`null` on four
  ids** as of 2026-09-30 — 101603 改写：热量汲取, 103401 队列：重负 and
  102202 环境：再构成 on all 7 records using each, 102103 环境：时限 on both of
  its 2; on 2026-09-19 only 101603 was), `buffId`, `groupId`, `conflictId`, `terms`,
  `values` — **the last five are `null` / empty on every tag of every record.**
- `contractTagScore` equals the sum of the tags' `score` on every record;
  14–25 tags per record, totals 46–52 at the top of the board.
- `tagId // 10` is the tag and `tagId % 10` its tier (1–3). The tier
  usually equals the score (33 of 38: `环境：时限` 102101 / 102102 / 102103
  score 1 / 2 / 3) **but not always** — 101402 队列：衰竭 is tier 2 at score
  3, 103102 改写：裹附 tier 2 at score 1, three more — so the score is read
  from `score` and never derived from the id. Names carry a family prefix,
  队列 / 改写 / 环境 (12 / 11 / 15 of the 38 distinct ids seen). A name may
  change between tiers — base 10130 is 环境：厌氧 at tier 1 and 环境：禁锢 at
  tier 3 — so tags are keyed by `tagId`, never merged by base.
- `description` is the raw game template: on the #1 record 19 of 25 carry
  unexpanded `{key:0%}` placeholders and 20 carry `<color=#…>` markup, and
  `values` (the substitution table) is empty. It cannot be printed.
- There is no catalog endpoint: the 38 ids above are what the board's
  records happen to use, a lower bound, not the pool.

## `GET /api/battles/users/{id}/rankings`

Lists the account's best record on **every** board it ever uploaded to,
including boards absent from hot-bosses (协议空间, 支线任务, finished events,
earlier challenge rotations: `indie_hard014`, `dung01-takestwo004`,
`dung01-group-gold01`, `dung-group-ss01`, `unknown-dungeon` …). Nothing says
whether such a board is retired: the 25 accounts with the most records touch
26 of them, every one still answered its ranking on 2026-10-01, and 6 of them
(协议空间 among them) took new records in September. The plugin calls them
未收录榜单 and never 已下线.

On the boards both carry, its `rank` equals the rank of the account's best row
in that board's ranking — verified on 8 accounts on 2026-09-06 with zero
differences, and again on 2026-10-01 for one account, rank and `battleId`
36 of 36 — which is what lets the rank watch and the account page read ranks
off the index instead of requesting them (#27). The index covers the
hot-bosses boards only, the same universe every other feature lives in.

It is computed per request and slow: ten calls in a row on 2026-10-01 took
0.57–5.0 s, median 1.0 s, with single calls up to 12 s.

Each entry of `rankings[]` now also carries `rosterEntries[]` (first recorded
2026-09-30; when it appeared is unknown), the record's four members with their
潜能 and 精炼 — see
*`characterPotential` / `weaponRefine` on ranking-style responses*.
`?metric=rdps` is still ignored (byte-identical, 2026-09-30).

## `GET /api/characters/{character_key}/boss-statistics`

Same `range` / `potential` / `metric` params. Returns one character's row for
**every** statistics board (zero-sample rows included; crisis contract and
inactive season towers excluded); each row equals the character's row in the
matching per-board response, plus `rankedCharacterCount`. Unknown key → 404
`character_not_found`. The admin operator is unified as `chr_9000_endmin`.

## `GET /api/characters/{character_key}/profile?range=&boss=`

Absent from the snapshot; **inferred** to have shipped with the build of
2026-09-25. Verified 2026-09-30 (live and bundle). It backs the site's
角色档案 page `/character/{key}`: server-rendered on first load, and re-fetched
by the page (`cache: "no-store"`) when the reader switches board.

- `range=7d|14d|30d|all`, default `all`; anything else → 422 `literal_error`.
- `boss={slug}` narrows the response to one board: `bossSlug` echoes it,
  `bosses[]` holds that board only and `sampleCount` counts its records,
  which are one per account, so `accountCount` equals it; the shares are
  out of it too. Its `rows[]` still stops at 20: 莱万汀 on
  `indie_battletower001_ex` answered 28 records and 20 rows (2026-10-01). An
  unknown slug and the crisis contract `indie_group_ccdg` both → 404
  `boss_not_found`.
- Unknown key → 404 `character_not_found`.
- **Every rarity has one** (2026-10-01): 狼卫 (5★, `chr_0006_wolfgd`) and
  秋栗 (4★, `chr_0019_karin`) answer 200 with samples, unlike the six-star
  statistics. The plugin therefore resolves a 角色档案 name against the
  game-data catalog (below), not the statistics catalog.
- **The admin is one character here too**: `chr_0002_endminm` answers with
  `characterKey: chr_9000_endmin`, so the key in the answer is not always
  the key asked for.
- A `range` outside the four is FastAPI's own 422 `{detail: [...]}` with no
  `error` object, which the client reads as `http_422`.
- `metric` and `potential` are ignored: `?metric=rdps` and `?potential=5`
  return bytes identical to `range=all`.
- No `Cache-Control`; `cf-cache-status: DYNAMIC`. For 莱万汀 (270 records):
  262 KB in 2–3 s at `range=all`, 30 KB at `7d`, 25 KB in up to 5 s for one
  board.

**Sample.** The public valid clears that contain the character, **one per
account per board — the account's fastest**. The page states it:
"同一账号在同一副本难度下保留最快一场".

```text
characterKey: str            "chr_0016_laevat"
range: str                   "all"
bossSlug: str|null           null unless ?boss=
generatedAt: str             "2026-09-30T02:42:02.846885+00:00"
latestBattleAt: str          "2026-09-30T01:28:05+08:00"
sampleCount: int             270     records containing the character
accountCount: int            98
bossCount: int               45      boards with samples
potentials[]    {key "0".."5"|"unknown", name "0 潜"|"未知", count, percent, iconUrl null}
refinements[]   {key "1".."6"|"unknown", name "精炼 1"|"未知", count, percent, iconUrl null}
combinations[]  {key "(5, 6)"|"(0, None)"|"(None, None)", name "5 + 6"|"0 + 未知"|"未知 + 未知", count, percent, iconUrl null}
teammates[]     {key chr_*, name, count, percent, iconUrl null}
weapons[]       {key wpn_*|"unknown", name, count, percent, iconUrl "/images/weapon/icon/…"|null}
equipment[]     {key item_equip_*, name, count, percent, iconUrl "/images/equip/iconbig/…"}
equipmentUnknownSamples: int
bosses[]        boards with samples, most samples first
  bossSlug, bossName, dungeonName, sampleCount,
  characterRank: int, bestDurationMs: int, rankedCharacterCount: int,
  characterRows[] {rank, characterKey, characterName, sampleCount, bestDurationMs, battleId, accountDisplayName}
  rows[]          {rank, battleId, accountId, accountDisplayName, durationMs, battleEndAt,
                   potential: int|null, refinement: int|null}
bossOptions[]   the bosses[] shape for every statistics board (48); a board
                without samples has characterRank and bestDurationMs null
```

- **Shares.** `percent` is out of `sampleCount` (teammate 卡缪 241 / 270 =
  89.26). The `potentials` counts, and the boards' `sampleCount`s, each sum to
  `sampleCount`. The `unknown` key (name 未知) counts records whose value is
  unrecorded, and a `combinations` key is a Python tuple repr.
- **`characterRank` is a clear-time rank**, not the DPS-distribution rank of
  `boss-statistics`; the two answer different questions. `characterRows` lists
  every character on the board, ordered by the fastest team clear it appears
  in, and `characterRank` is this character's row. Ties share a rank and the
  next rank skips — the four members of the #1 battle are all 1 and the next
  character is 5 — on all 45 boards. On every board `characterRank` equals the
  character's own row, `bestDurationMs` equals `rows[0].durationMs`, and
  `rankedCharacterCount` equals `len(characterRows)`. (The page heads the
  section 在各副本中的排名 · 按队伍最快通关时间排名.)
- **`rows[]`** are the character's records on the board by clear time, at most
  20, one per account, ranked 1..n without ties. `potential` / `refinement`
  equal the battle roster's `characterPotential` / `weapon.weaponRefine`
  (six battles checked).
- **`bossOptions[]`** is the same 48 boards whatever `range` or `boss` is,
  zero-sample boards included with `characterRank` and `bestDurationMs` null
  and empty lists; the crisis contract is not among them.

## `GET /api/bosses[/{slug}]/character-statistics`

`range=7d|14d|30d|all`, `potential=0|1-5|all`, `metric` defaults to dps.

Returns every six-star character (`SIX_STAR_STATISTICS_CATALOG`, snapshot), zero-sample
rows included; `rank` is null when `normalSampleCount < minimumSampleCount` (5);
whiskers are min/max of IQR-filtered normal samples, `maximum` includes
outliers. Crisis contract → 404 `character_statistics_not_available`.
`professionGroups[].entries[].usagePercent` is the share within that profession
slot over **all** rows.

The global (slug-less) response takes upstream ~5 s to compute, and its names
and keys are what the character catalog is built from.

## `GET /api/v1/battles/{battle_id}/export`

Public, no token, per-IP 60 requests/min → 429 `rate_limited`,
`Cache-Control: max-age=60` + weak ETag, CORS `*`. The snapshot calls this the
read-only contract upstream keeps for external axis tools.

Returns `{schemaVersion: 1, battleId, parserVersion, rulesVersion, dungeon{dungeonSlug, dungeonName, bossKey, bossName}, durationMs, battleStartAt, battleEndAt, roster[…], casts[…]}`.
`roster` is the detail roster minus profession / avatar / element /
accountDisplayName. `casts[]` =
`{tsMsFromStart, endMsFromStart|null, characterKey, skillKey, skillName, skillSource: "unknown"|"Summon", recoversEnergy}`
and includes normal-attack segments and summoned entities' casts; a
summon's `skillName` can be its raw key
(`chr_0011_seraph_normal_skill_abentity_onfield`, `Summon`, v57 — `abentity`
is the keys' short spelling of `abilityentity`, **inferred**);
`tsMsFromStart` can be negative (before the timer), `endMsFromStart` null or
past `durationMs`, 10–110 casts per speed run.

404 for a non-public battle, 422 `battle_export_unsupported` for uploads older
than parser v33 (no casts; the crisis-contract era).

The web's per-hit `timelineEvents` in the detail response (1.6 MB for a
six-minute fight) is *not* used for the timeline.

## `GET /api/game-data/*`

Public, no auth, static game data.

`equip` returns
`{kind, count, entries[{suitID, name, displayName, rarity, pieceCount, icon, passiveSkillId, …}]}`
— roughly two dozen suits, and the authority on which suit an item id belongs
to. `suit_none` carries its own id as its `name` and is skipped.

`character` carries `charTypeName` (物理 / 灼热 / 寒冷 / 自然 / 电磁),
`weaponTypeName` and `professionName` — ranking rows carry no element, and
battle rosters carry it per battle only, so this is the only source of a
character's element and profession. Its 术师 disagrees with the rosters' 术士.

Its `id` (equal to `charId`) is the key the character endpoints take,
`chr_0016_laevat`, for all 33 entries of every rarity, and `rarity` is an
int 4–6 (2026-10-01); the plugin reads both, for 角色档案's names and for
whether a character has 角色统计. Three entries are named 管理员
(`chr_0002_endminm`, `chr_0003_endminf`, `chr_9000_endmin`).

Sibling modules: `character` (33), `weapon` (79), `enemy` (92), `dungeon` (94),
`buff`, `skill`, `attribute_type`. The detail routes
(`/api/game-data/{kind}/{id}`) answered 503 `catalog_data_missing` when first
tried; on 2026-10-05 `weapon/{id}` answered for 72 of the 80 weapons it then
listed (the other 8 still 503), with `detail.skilllist[{skillName,
description, blackboard}]` — the 词条 names above. Python urllib's default
User-Agent got 403 there; curl's did not.

`GET /api/game-semantics/*` and `/api/game-semantics/hints/*` are live too but
**not worth using**: their ids are internal `abilityentity_*` keys with mostly
empty `decodedName`, they do not match the `roleSkillStats` keys the plugin
prints, and `attribute_type` is English enum names (`MaxHp`,
`AttributeType11`) rather than the Chinese labels the battle payload usually carries.
`/api/game-data/attribute_type` lists the same 94 entries keyed by number,
`{id: "3", name: "Def", slug, icon, …}`: the numbers newer uploads write as
`attribute_type_N` in gear stats (`roster[]` above).

## `GET /api/battles/users/search?query=&limit=`

Public, no auth. NFKC-normalized case-insensitive substring match over public
nicknames; `query` must be 2–64 chars after strip (else 422), `limit` 1–20
(default 10). Returns `{query, hasMore, accounts[{accountId, accountDisplayName}]}`,
only accounts with valid public ranking records. Server caches ≤60 s, HTTP
`max-age=30`.

## `GET /api/battles/users/binding-code?code=ZMD-XXXX-XXXX`

Public, no auth. Spec written 2026-09-09; **live on zmdlogs.com as of
2026-09-15** — a made-up well-formed code answers 404 `binding_code_invalid`,
where a deployment without the route answers FastAPI's plain 404 (read as
`http_404`).

Returns the account a code was issued to, `{accountId, accountDisplayName}`
and nothing else. The code is generated by the logged-in user on
`/account/binding` (`POST /api/auth/binding-code`, 10-minute life, one current
code per account, 3 per minute), format
`^ZMD-[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$`, uppercase only (422 otherwise).

**The lookup neither consumes nor extends the code and writes nothing, so
replay defence is the bot's job.** 404 `binding_code_invalid` for unknown /
expired / replaced / disabled account; 429 `too_many_requests` with
`Retry-After` past 10 lookups per IP per minute (malformed and invalid queries
count); `Cache-Control: no-store` on every answer.

## Upstream web code this plugin mirrors

Paths are in the `4e65587` snapshot; the live bundle may have moved on.

- **Skill display names**: `apps/web/lib/format/skill-display.ts`. Upstream
  names a damage row from the game's text table when it can and otherwise
  joins the key's segments with ` / `, translating some tokens — which is how
  `…_normal_skill_projhit_hit` becomes `普攻 / skill / 派生 / hit`, wrong
  because `normal_skill` is one phrase, the 战技. `core/loadout.py` therefore
  re-humanises any name that is still upstream's token join. The battle
  response now carries the game's own name for half or more of the rows,
  `roleSkillStats[].displayGroupName` (above), and the plugin prints that
  first; the re-humanising is the fallback for the rows without it.
- **Cast classification**: `apps/web/lib/endaxis-project.ts` — end-anchored key
  rules. A mechanism entity such as `…_combo_skill_water_gene` must not be
  drawn as the player's 连携技 (upstream's own "汤汤 连携×3" incident).
- **Token labels**: parser_core's `_humanize_skill_suffix_text` and reaction
  names (自然爆发, 燃烧 …).
- **Element colours**: `apps/web/features/battle-detail` `DAMAGE_ELEMENT_COLORS`
  — physical `#9aa3ad`, fire `#ff5f5f`, cryst `#63a9ff`, natural `#74d66b`,
  pulse `#f5cf4e`. These are the ring colours in `base.css`; a hand-picked set
  made 电磁 purple when the game and the site both make it yellow.
