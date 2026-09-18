# Upstream facts · ZMDLogs public API

What the public ZMDLogs API actually returns, verified against
[endfield-suite-open](https://github.com/medps16000/endfield-suite-open)
(`endfield-logs/apps/api/app/services/public_data.py`) and by probing the live
site. This file is the source of truth for the contract: the repository holds
no other record of it, and `core/client.py` / `core/models.py` only implement it.

Read this before adding an endpoint, widening a model, or trusting a field.
Dates mark when a fact was verified in the field.

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

## Boards

Board slugs come from static `BOSS_SEEDS`; the crisis-contract board is
`indie_group_ccdg` (`bossName` 破潮之像, `dungeonName` 危机合约). Unknown slug
→ 404 `boss_not_found`. `hot-bosses` is the only board/dungeon index there is —
there is no catalog endpoint.

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

## Battle detail

In battle detail every `roster[].accountDisplayName` is the uploader nickname,
so `roster[0]` is a valid uploader name source;
`GET /api/battles/{id}/share-summary` also returns `uploaderNickname` directly.

### `roster[]` — the loadout recorded at upload

`slot` (1-based), `characterElement`
(`physical|fire|cryst|natural|pulse` = 物理/灼热/寒冷/自然/电磁),
`characterLevel`, `characterPotential`,
`weapon{weaponName, weaponLevel (often null/0), weaponRefine, iconUrl, skills[{skillKey, level, potentialLevel}]}`
where `sk_wpn_*` is the weapon's own skill and `wpn_attr_*` / `wpn_sp_attr_*`
are affixes, `equips[]` (4 pieces: 护手 / 护甲 / 配件 ×2) and
`skills[{skillKey, level}]` (`_attack1..5`, `_normal_skill`, `_combo_skill`,
`_ultimate_skill`, talents at level 1).

`equips[]` detail: `pieceName` falls back to the raw `item_equip_…` id with
`suitName` null when the catalog has no name;
`enhanceLevels[{index, level}]`; `stats[{slot: main|sub1..3, name, value, level}]`
with ratio stats as fractions (e.g. `0.1495`) and occasionally untranslated
names `Main` / `副能力`.

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

### `roleSkillStats[]`

`characterName, skillKey, skillName, castCount, totalDamage, avgDamage, maxDamage`,
one row per damage source: the normal attack chain is one row per segment,
`skillName` may be a raw key or an English placeholder (`burning status`,
`poise can be breaking attacked`), and `buff_common_*` rows are engine-side
damage (element reactions, statuses).

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

## `GET /api/battles/users/{id}/rankings`

Lists the account's best record on **every** board it ever uploaded to,
including boards absent from hot-bosses (retired or non-standard content:
`indie_hard014`, `dung01-takestwo004`, `dung01-group-gold01`,
`dung-group-ss01`, `unknown-dungeon` …).

On the boards both carry, its `rank` equals the rank of the account's best row
in that board's ranking — verified on 8 accounts on 2026-09-06 with zero
differences — which is what lets the rank watch read ranks off the index
instead of requesting them. The index covers the hot-bosses boards only, the
same universe every other feature lives in.

## `GET /api/characters/{character_key}/boss-statistics`

Same `range` / `potential` / `metric` params. Returns one character's row for
**every** statistics board (zero-sample rows included; crisis contract and
inactive season towers excluded); each row equals the character's row in the
matching per-board response, plus `rankedCharacterCount`. Unknown key → 404
`character_not_found`. The admin operator is unified as `chr_9000_endmin`.

## `GET /api/bosses[/{slug}]/character-statistics`

`range=7d|14d|30d|all`, `potential=0|1-5|all`, `metric` defaults to dps.

Returns every six-star character (`SIX_STAR_STATISTICS_CATALOG`), zero-sample
rows included; `rank` is null when `normalSampleCount < minimumSampleCount` (5);
whiskers are min/max of IQR-filtered normal samples, `maximum` includes
outliers. Crisis contract → 404 `character_statistics_not_available`.
`professionGroups[].entries[].usagePercent` is the share within that profession
slot over **all** rows.

The global (slug-less) response takes upstream ~5 s to compute, and its names
and keys are what the character catalog is built from.

## `GET /api/v1/battles/{battle_id}/export`

Public, no token, per-IP 60 requests/min → 429 `rate_limited`,
`Cache-Control: max-age=60` + weak ETag, CORS `*`. This is the read-only
contract upstream keeps for external axis tools.

Returns `{schemaVersion: 1, battleId, parserVersion, rulesVersion, dungeon{dungeonSlug, dungeonName, bossKey, bossName}, durationMs, battleStartAt, battleEndAt, roster[…], casts[…]}`.
`roster` is the detail roster minus profession / avatar / element /
accountDisplayName. `casts[]` =
`{tsMsFromStart, endMsFromStart|null, characterKey, skillKey, skillName, skillSource: "unknown"|"Summon", recoversEnergy}`
and includes normal-attack segments and summoned entities' casts;
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

Sibling modules: `character` (33), `weapon` (79), `enemy` (92), `dungeon` (94),
`buff`, `skill`, `attribute_type`. The detail routes
(`/api/game-data/{kind}/{id}`) answer 503 `catalog_data_missing`.

`GET /api/game-semantics/*` and `/api/game-semantics/hints/*` are live too but
**not worth using**: their ids are internal `abilityentity_*` keys with mostly
empty `decodedName`, they do not match the `roleSkillStats` keys the plugin
prints, and `attribute_type` is English enum names (`MaxHp`,
`AttributeType11`) rather than the Chinese labels the battle payload already carries.

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

- **Skill display names**: `apps/web/lib/format/skill-display.ts`. Upstream
  names a damage row from the game's text table when it can and otherwise
  joins the key's segments with ` / `, translating some tokens — which is how
  `…_normal_skill_projhit_hit` becomes `普攻 / skill / 派生 / hit`, wrong
  because `normal_skill` is one phrase, the 战技. `core/loadout.py` therefore
  re-humanises any name that is still upstream's token join.
- **Cast classification**: `apps/web/lib/endaxis-project.ts` — end-anchored key
  rules. A mechanism entity such as `…_combo_skill_water_gene` must not be
  drawn as the player's 连携技 (upstream's own "汤汤 连携×3" incident).
- **Token labels**: parser_core's `_humanize_skill_suffix_text` and reaction
  names (自然爆发, 燃烧 …).
- **Element colours**: `apps/web/features/battle-detail` `DAMAGE_ELEMENT_COLORS`
  — physical `#9aa3ad`, fire `#ff5f5f`, cryst `#63a9ff`, natural `#74d66b`,
  pulse `#f5cf4e`. These are the ring colours in `base.css`; a hand-picked set
  made 电磁 purple when the game and the site both make it yellow.
