"""Shared model and API payload builders for tests."""

from core.models import HotBossCard, HotBossRun


def make_card(
    slug: str,
    boss_name: str,
    dungeon_name: str,
    *,
    with_run: bool = False,
) -> HotBossCard:
    runs = (
        HotBossRun(
            battle_id=f"battle-{slug}",
            duration_ms=61_234,
            uploader_nickname="公开账户",
            character_name="余烬",
        ),
    ) if with_run else ()
    return HotBossCard(
        boss_slug=slug,
        boss_key=f"key-{slug}",
        boss_name=boss_name,
        dungeon_name=dungeon_name,
        top_speed_runs=runs,
    )


def hot_bosses_payload() -> list[dict]:
    return [
        {
            "bossSlug": "dung01_group_bossrush02",
            "bossKey": "bossrush02",
            "bossName": "危境再现·三位一体",
            "dungeonName": "危境再现 · 测试区",
            "topSpeedRuns": [],
        }
    ]


def ranking_payload(*, metric: str = "dps") -> dict:
    return {
        "bossSlug": "dung01_group_bossrush02",
        "bossName": "危境再现·三位一体",
        "dungeonName": "危境再现 · 测试区",
        "metric": metric,
        "professionGroups": [],
        "rows": [],
    }


def public_user_rankings_payload() -> dict:
    return {
        "accountId": "usr_1234567890abcdef",
        "accountDisplayName": "测试账号",
        "rankings": [
            {
                "bossSlug": "dung01_group_bossrush01",
                "bossName": "“碾骨之拳”罗丹",
                "dungeonName": "危境再现·罗丹",
                "battleId": "btl_upload_abcdef123456",
                "rank": 2,
                "scorePercent": 95,
                "durationMs": 20_833,
                "totalDps": 110_061.2,
                "battleEndAt": "2026-07-13T22:00:32+08:00",
                "rosterSummary": ["洛茜", "卡缪", "洁尔佩塔", "佩丽卡"],
                "contractTagScore": None,
                "contractTags": [],
            }
        ],
    }


def damage_tick(at_ms: int, character: str, value: int) -> dict:
    """One ``timelineEvents`` damage row, trimmed to what the curve reads."""

    return {
        "tsMsFromStart": at_ms,
        "laneType": "skill",
        "sourceCharacterName": character,
        "eventName": "伤害",
        "value": value,
    }


def effect(zone: str, element: str, rate: float) -> dict:
    return {"zone": zone, "element": element, "rate": rate}


def buff(
    key: str,
    name: str,
    source: str,
    target: str,
    start_ms: int,
    duration_ms: int | None,
    effects: list[dict],
) -> dict:
    return {
        "eventKey": key,
        "eventName": name,
        "sourceCharacterName": source,
        "targetCharacterName": target,
        "startTsMsFromStart": start_ms,
        "durationMs": duration_ms,
        "effects": effects,
        "dynamicEffects": [],
    }


def battle_detail_payload() -> dict:
    def skill(key: str, level: int) -> dict:
        return {"skillKey": key, "level": level}

    def stat(slot: str, name: str, value: float, level: int | None = 3) -> dict:
        return {"slot": slot, "name": name, "value": value, "level": level}

    def equip_icon(item_id: str) -> str:
        return f"/images/equip/iconbig/{item_id}.png"

    def skill_stat(
        character: str,
        key: str | None,
        name: str,
        casts: int,
        total: int,
        maximum: int,
    ) -> dict:
        return {
            "characterName": character,
            "skillKey": key,
            "skillName": name,
            "castCount": casts,
            "totalDamage": total,
            "avgDamage": round(total / casts, 2) if casts else 0.0,
            "maxDamage": maximum,
        }

    return {
        "battle": {
            "id": "btl_upload_abcdef123456",
            "uploaderUserId": "usr_1234567890abcdef",
            "dungeonName": "危境再现·罗丹",
            "bossName": "“碾骨之拳”罗丹",
            "battleEndAt": "2026-07-13T22:00:32+08:00",
            "durationMs": 20_833,
            "totalDamage": 2_292_905,
            "totalDps": 110_061.2,
            "roster": [
                {
                    "slot": 1,
                    "characterKey": "chr_0028_wulfa",
                    "characterName": "洛茜",
                    "characterProfession": "近卫",
                    "characterAvatarUrl": "/images/character/luoxi.png",
                    "characterElement": "fire",
                    "accountDisplayName": "测试账号",
                    "characterLevel": 90,
                    "characterPotential": 5,
                    "weapon": {
                        "weaponTemplate": "wpn_sword_0021",
                        "weaponName": "宏愿",
                        "weaponLevel": 90,
                        "weaponRefine": 3,
                        "iconUrl": "/images/weapon/icon/wpn_sword_0021.png",
                        "skills": [
                            {"skillKey": "sk_wpn_sword_0021", "level": 9,
                             "potentialLevel": 5},
                            {"skillKey": "wpn_attr_str_high", "level": 9,
                             "potentialLevel": 5},
                            {"skillKey": "wpn_sp_attr_atk_high", "level": 7,
                             "potentialLevel": 5},
                        ],
                    },
                    "equips": [
                        {
                            "slot": 0,
                            "itemId": "item_equip_t4_suit_phy01_hand_01",
                            "pieceName": "点剑护手",
                            "suitName": "点剑",
                            "partName": "护手",
                            "iconUrl": equip_icon("item_equip_t4_suit_phy01_hand_01"),
                            "enhanceLevels": [
                                {"index": 1, "level": 3},
                                {"index": 2, "level": 3},
                                {"index": 3, "level": 2},
                            ],
                            "stats": [
                                stat("main", "防御力", 30.0, None),
                                stat("sub1", "力量", 61.0),
                                stat("sub2", "物理伤害提升", 0.1495),
                                stat("sub3", "Main", 0.269123),
                            ],
                        },
                        {
                            # Upstream fell back to the item id: no piece or
                            # suit name, and the untyped lists carry junk.
                            "slot": 1,
                            "itemId": "item_equip_t4_suit_phy01_body_02",
                            "pieceName": "item_equip_t4_suit_phy01_body_02",
                            "suitName": None,
                            "partName": "护甲",
                            "iconUrl": None,
                            "enhanceLevels": [
                                {"index": 1, "level": 3},
                                {"bad": True},
                                "junk",
                            ],
                            "stats": [
                                {"name": "防御力", "value": "56"},
                                {"slot": "sub1", "name": "", "value": 1},
                                "junk",
                            ],
                        },
                        {
                            "slot": 2,
                            "itemId": "item_equip_t4_suit_phy01_edc_03",
                            "pieceName": "点剑火石",
                            "suitName": "点剑",
                            "partName": "配件",
                            "iconUrl": equip_icon("item_equip_t4_suit_phy01_edc_03"),
                            "enhanceLevels": [],
                            "stats": [],
                        },
                        {
                            "slot": 3,
                            "itemId": "item_equip_t4_suit_phy01_edc_03",
                            "pieceName": "点剑火石",
                            "suitName": "点剑",
                            "partName": "配件",
                            "iconUrl": equip_icon("item_equip_t4_suit_phy01_edc_03"),
                            "enhanceLevels": [],
                            "stats": [],
                        },
                    ],
                    "skills": [
                        skill("chr_0028_wulfa_attack1", 12),
                        skill("chr_0028_wulfa_attack2", 12),
                        skill("chr_0028_wulfa_attack3", 12),
                        skill("chr_0028_wulfa_normal_skill", 12),
                        skill("chr_0028_wulfa_normal_skill_combo", 1),
                        skill("chr_0028_wulfa_combo_skill", 9),
                        skill("chr_0028_wulfa_ultimate_skill", 12),
                        skill("chr_0028_wulfa_passive", 1),
                    ],
                },
                {
                    # A support with no gear recorded beyond the weapon.
                    "slot": 2,
                    "characterKey": "chr_0031_kamiu",
                    "characterName": "卡缪",
                    "characterProfession": "术士",
                    "characterAvatarUrl": None,
                    "characterElement": None,
                    "accountDisplayName": "测试账号",
                    "characterLevel": 80,
                    "characterPotential": 0,
                    "weapon": {
                        "weaponTemplate": None,
                        "weaponName": "悼亡诗",
                        "weaponLevel": None,
                        "weaponRefine": 6,
                        "iconUrl": None,
                        "skills": [],
                    },
                    "equips": [],
                    "skills": [],
                },
            ],
            "parserVersion": "raw-log-parser-v34",
            "rulesVersion": "raw-log-parser-v31",
            "timeSource": "game_timer",
            "officialTimerStartSeen": True,
            "officialTimerEndSeen": True,
            "contractTagScore": None,
            "contractTags": [],
        },
        "participants": [
            {
                "characterKey": "chr_0028_wulfa",
                "characterName": "洛茜",
                "characterProfession": "近卫",
                "characterAvatarUrl": "/images/character/luoxi.png",
                "accountDisplayName": "测试账号",
                "totalDamage": 2_027_572,
                "dps": 97_325.01,
                "rdps": 26_428.42,
                "maxHit": 381_016,
                "critRate": 0.8125,
            },
            {
                "characterKey": "chr_0031_kamiu",
                "characterName": "卡缪",
                "characterProfession": "术士",
                "characterAvatarUrl": None,
                "accountDisplayName": "测试账号",
                "totalDamage": 265_333,
                "dps": 12_736.19,
                "rdps": 83_632.78,
                "maxHit": None,
                "critRate": None,
            },
        ],
        # Damage ticks the DPS curve reads, plus rows it must skip: a buff-lane
        # event, a cast with no damage, a malformed one and an unknown shape.
        "timelineEvents": [
            damage_tick(1_000, "洛茜", 900_000),
            damage_tick(4_000, "卡缪", 200_000),
            damage_tick(11_000, "洛茜", 1_127_572),
            damage_tick(20_833, "卡缪", 65_333),
            {**damage_tick(5_000, "洛茜", 10), "laneType": "buff"},
            {**damage_tick(6_000, "洛茜", 0), "value": None},
            {**damage_tick(7_000, "洛茜", 10), "tsMsFromStart": "7000"},
            {"ignored": True},
        ],
        # Buffs as upstream groups them per character; the same buff reaches
        # both characters (one application) and is later refreshed.
        "characterStates": [
            {
                "characterKey": "chr_0028_wulfa",
                "characterName": "洛茜",
                "buffsReceived": [
                    buff("buff_chr_0031_kamiu_atkup", "攻击提升", "卡缪", "洛茜",
                         1_000, 9_000, [effect("atk", "all", 0.16)]),
                    buff("buff_chr_0031_kamiu_atkup", "攻击提升", "卡缪", "洛茜",
                         12_000, 9_000, [effect("atk", "all", 0.16)]),
                    buff("buff_wpn_sword_0021_up", "buff_wpn_sword_0021_up",
                         "洛茜", "洛茜", 2_000, 18_000,
                         [effect("amp", "fire", 0.4)]),
                    # Zones outside the damage set are dropped.
                    buff("buff_speed", "疾行", "洛茜", "洛茜", 0, 5_000,
                         [effect("speedup", "all", 0.2)]),
                    # No duration: nothing to draw.
                    buff("buff_flat", "无时长", "洛茜", "洛茜", 0, None,
                         [effect("atk", "all", 0.1)]),
                    "不是字典",
                ],
                "buffsGiven": [],
                # A debuff on the boss: a different zone set, and the target is
                # sometimes the raw enemy key rather than a display name.
                "debuffsApplied": [
                    buff("buff_chr_0028_wulfa_fragile", "脆弱", "洛茜",
                         "“碾骨之拳”罗丹", 3_000, 8_000,
                         [effect("fragile", "spell", 0.28)]),
                    buff("buff_common_res_down", "腐蚀 / 减抗", "洛茜",
                         "eny_0051_rodin", 5_000, 6_000,
                         [effect("res", "all", 0.036)]),
                    # atk is a team zone, never an enemy one.
                    buff("buff_wrong_zone", "错误区域", "洛茜",
                         "eny_0051_rodin", 1_000, 5_000,
                         [effect("atk", "all", 0.2)]),
                ],
            },
            {
                "characterKey": "chr_0031_kamiu",
                "characterName": "卡缪",
                "buffsReceived": [
                    buff("buff_chr_0031_kamiu_atkup_owner", "攻击提升", "卡缪",
                         "卡缪", 1_040, 9_000, [effect("atk", "all", 0.16)]),
                    # A target outside the roster is ignored.
                    buff("buff_chr_0031_kamiu_atkup", "攻击提升", "卡缪", "旁人",
                         1_000, 9_000, [effect("atk", "all", 0.16)]),
                ],
                "buffsGiven": [],
                "debuffsApplied": [],
            },
            {"characterName": "无增益"},
            "不是字典",
        ],
        "roleSkillStats": [
            skill_stat("洛茜", "chr_0028_wulfa_ultimate_skill", "终结技",
                       2, 1_200_000, 700_000),
            skill_stat("洛茜", "chr_0028_wulfa_attack1", "绯红刃舞",
                       20, 200_000, 15_000),
            skill_stat("洛茜", "chr_0028_wulfa_attack2", "绯红刃舞",
                       18, 180_000, 14_000),
            skill_stat("洛茜", "chr_0028_wulfa_attack3", "绯红刃舞",
                       10, 120_000, 20_000),
            skill_stat("洛茜", "chr_0028_wulfa_normal_skill", "战技",
                       4, 250_000, 90_000),
            skill_stat("洛茜", "chr_0028_wulfa_skill_3090",
                       "chr_0028_wulfa_skill_3090", 1, 60_000, 60_000),
            skill_stat("洛茜", "buff_common_burning_status", "burning status",
                       9, 17_572, 4_000),
            skill_stat("卡缪", "chr_0031_kamiu_combo_skill", "连携·潮汐",
                       6, 200_333, 50_000),
            skill_stat("卡缪", "chr_0031_kamiu_attack1", "A1",
                       30, 65_000, 3_000),
        ],
        "integrity": {"verified": True},
    }


def battle_export_payload() -> dict:
    """Public export (schema v1): two characters, a summon, noise, edge cases."""

    def cast(
        key: str,
        name: str,
        start: int,
        end: int | None,
        *,
        character: str = "chr_0028_wulfa",
        source: str | None = "unknown",
        energy: bool = False,
    ) -> dict:
        return {
            "tsMsFromStart": start,
            "endMsFromStart": end,
            "characterKey": character,
            "skillKey": key,
            "skillName": name,
            "skillSource": source,
            "recoversEnergy": energy,
        }

    kamiu = "chr_0031_kamiu"
    return {
        "schemaVersion": 1,
        "battleId": "btl_upload_abcdef123456",
        "parserVersion": "raw-log-parser-v46",
        "rulesVersion": "raw-log-parser-v40",
        "dungeon": {
            "dungeonSlug": "dung01_group_bossrush01",
            "dungeonName": "危境再现·罗丹",
            "bossKey": "eny_0051_rodin",
            "bossName": "“碾骨之拳”罗丹",
        },
        "durationMs": 25_000,
        "battleStartAt": "2026-07-13T22:00:11+08:00",
        "battleEndAt": "2026-07-13T22:00:36+08:00",
        "roster": [
            {
                "slot": 2,
                "characterKey": kamiu,
                "characterName": "卡缪",
                "characterLevel": 80,
                "characterPotential": 0,
            },
            {
                "slot": 1,
                "characterKey": "chr_0028_wulfa",
                "characterName": "洛茜",
                "characterLevel": 90,
                "characterPotential": 5,
            },
        ],
        "casts": [
            cast("chr_0028_wulfa_ultimate_skill", "终结技", 1_000, 4_000),
            cast("chr_0028_wulfa_attack1", "A1", 4_000, 4_500),
            cast("chr_0028_wulfa_attack2", "A2", 4_500, 5_100),
            cast("chr_0028_wulfa_power_attack", "重击", 5_100, 6_000, energy=True),
            cast("chr_0028_wulfa_normal_skill", "战技", 6_000, 8_000),
            cast("chr_0028_wulfa_combo_skill", "连携技", 8_000, 9_000),
            # Movement noise, hidden.
            cast("chr_0028_wulfa_dash", "通用 / character / ai / 冲刺", 9_000, 9_300),
            # End never seen: an instant mark.
            cast("chr_0028_wulfa_attack3", "A3", 12_000, None),
            # Runs past the recorded end of the fight: clipped.
            cast("chr_0028_wulfa_skill_9001", "血红之影", 18_000, 27_000),
            # Stamped before the timer: dropped.
            cast("chr_0028_wulfa_attack1", "A1", -500, -100),
            # 卡缪: an own skill, a long summon, and a mechanism entity whose
            # key merely contains combo_skill.
            cast(f"{kamiu}_normal_skill", "战技", 3_000, 4_000, character=kamiu),
            cast(
                f"{kamiu}_normal_skill_pet",
                "召唤 / pet",
                3_000,
                15_000,
                character=kamiu,
                source="Summon",
            ),
            cast(
                f"{kamiu}_combo_skill_water_gene",
                "河水 / water / gene",
                2_000,
                22_000,
                character=kamiu,
            ),
            cast(f"{kamiu}_ultimate_skill", "终结技", 21_000, 23_000, character=kamiu),
        ],
    }


def character_statistics_payload(*, scope: str = "boss", metric: str = "dps") -> dict:
    def row(
        name: str,
        profession: str,
        key: str,
        *,
        rank: int | None,
        samples: int,
        median: float | None,
        maximum: float | None = None,
        outliers: int = 0,
    ) -> dict:
        insufficient = rank is None
        spread = (median or 0) * 0.2
        return {
            "rank": rank,
            "characterKey": key,
            "characterName": name,
            "characterProfession": profession,
            "characterAvatarUrl": f"/images/character/{key}.png",
            "sampleCount": samples + outliers,
            "normalSampleCount": samples,
            "outlierCount": outliers,
            "insufficientSamples": insufficient,
            "lowerWhisker": None if median is None else median - spread * 2,
            "p10": None if median is None else median - spread * 1.5,
            "p25": None if median is None else median - spread,
            "median": median,
            "p75": None if median is None else median + spread,
            "p90": None if median is None else median + spread * 1.5,
            "upperWhisker": None if median is None else median + spread * 2,
            "maximum": maximum if maximum is not None else (
                None if median is None else median + spread * 2
            ),
            "outliers": (
                [{"value": (median or 0) * 3, "count": outliers}] if outliers else []
            ),
        }

    is_global = scope == "all"
    return {
        "scope": scope,
        "bossSlug": "all" if is_global else "dung01_group_bossrush02",
        "bossName": "全部副本" if is_global else "危境再现·三位一体",
        "dungeonName": "角色统计总榜" if is_global else "危境再现 · 测试区",
        "metric": metric,
        "range": "all",
        "potential": "all",
        "includedBossCount": 12 if is_global else 1,
        "minimumSampleCount": 5,
        "eligibleBattleCount": 40,
        "totalSampleCount": 57,
        "totalOutlierCount": 2,
        "rows": [
            row("黎风", "近卫", "chr_lifeng", rank=1, samples=20, median=120_000,
                maximum=260_000, outliers=2),
            row("洛茜", "近卫", "chr_luoxi", rank=2, samples=15, median=90_000),
            row("卡缪", "术士", "chr_kamiu", rank=3, samples=8, median=60_500.5),
            row("佩丽卡", "辅助", "chr_peilika", rank=None, samples=3, median=30_000),
            row("无样本", "先锋", "chr_none", rank=None, samples=0, median=None),
        ],
    }


def ranking_payload_with_rows() -> dict:
    def entry(name: str, profession: str) -> dict:
        return {
            "characterKey": f"chr_{name}",
            "characterName": name,
            "profession": profession,
            "avatarUrl": f"/images/character/{name}.png",
        }

    def row(rank: int, main: str, roster: list[tuple[str, str]], dps: float) -> dict:
        return {
            "rank": rank,
            "scorePercent": 100 - rank,
            "battleId": f"btl_upload_{rank:012d}",
            "battleEndAt": "2026-07-13T22:00:32+08:00",
            "characterKey": f"chr_{main}",
            "characterName": main,
            "characterProfession": dict(roster)[main],
            "characterAvatarUrl": f"/images/character/{main}.png",
            "accountId": f"usr_{rank:032d}",
            "accountDisplayName": f"公开账号{rank}",
            "dps": dps,
            "rdps": dps / 2,
            "durationMs": 60_000 + rank * 1000,
            "rosterSummary": [name for name, _ in roster],
            "rosterEntries": [entry(name, profession) for name, profession in roster],
            "contractTagScore": None,
            "contractTags": [],
        }

    support = [("卡缪", "术士"), ("佩丽卡", "辅助"), ("洁尔佩塔", "重装")]
    team_a = [("黎风", "近卫"), *support]
    team_b = [("洛茜", "近卫"), *support]
    return {
        "bossSlug": "dung01_group_bossrush02",
        "bossName": "危境再现·三位一体",
        "dungeonName": "危境再现 · 测试区",
        "metric": "dps",
        "professionGroups": [
            {
                "profession": "近卫",
                "entries": [
                    {"characterKey": "chr_黎风", "characterName": "黎风",
                     "avatarUrl": "/images/character/黎风.png", "usagePercent": 60.0},
                    {"characterKey": "chr_洛茜", "characterName": "洛茜",
                     "avatarUrl": None, "usagePercent": 40.0},
                ],
            },
            {"profession": "重装", "entries": [
                {"characterKey": None, "characterName": "洁尔佩塔",
                 "avatarUrl": None, "usagePercent": 100.0}]},
            {"profession": "辅助", "entries": [
                {"characterKey": None, "characterName": "佩丽卡",
                 "avatarUrl": None, "usagePercent": 100.0}]},
            {"profession": "突击", "entries": []},
            {"profession": "术士", "entries": [
                {"characterKey": None, "characterName": "卡缪",
                 "avatarUrl": None, "usagePercent": 100.0}]},
            {"profession": "先锋", "entries": []},
        ],
        "rows": [
            row(1, "黎风", team_a, 130_000.5),
            row(2, "黎风", list(reversed(team_a)), 125_000),
            row(3, "洛茜", team_b, 110_000),
            row(4, "黎风", team_a, 100_000),
            row(5, "卡缪", team_b, 90_000),
        ],
    }


def character_boss_statistics_payload(*, metric: str = "dps") -> dict:
    def row(slug: str, boss: str, dungeon: str, *, rank: int | None,
            ranked_total: int, samples: int, median: float | None,
            maximum: float | None = None, outliers: int = 0) -> dict:
        spread = (median or 0) * 0.2
        return {
            "bossSlug": slug,
            "bossName": boss,
            "dungeonName": dungeon,
            "rank": rank,
            "rankedCharacterCount": ranked_total,
            "sampleCount": samples + outliers,
            "normalSampleCount": samples,
            "outlierCount": outliers,
            "insufficientSamples": rank is None,
            "lowerWhisker": None if median is None else median - spread * 2,
            "p10": None if median is None else median - spread * 1.5,
            "p25": None if median is None else median - spread,
            "median": median,
            "p75": None if median is None else median + spread,
            "p90": None if median is None else median + spread * 1.5,
            "upperWhisker": None if median is None else median + spread * 2,
            "maximum": maximum if maximum is not None else (
                None if median is None else median + spread * 2
            ),
            "outliers": (
                [{"value": (median or 0) * 3, "count": outliers}]
                if outliers
                else []
            ),
        }

    return {
        "characterKey": "chr_0028_wulfa",
        "characterName": "洛茜",
        "characterProfession": "近卫",
        "characterAvatarUrl": "/images/character/icon_chr_0028_wulfa.png",
        "metric": metric,
        "range": "30d",
        "potential": "all",
        "minimumSampleCount": 5,
        "includedBossCount": 4,
        "totalSampleCount": 120,
        "totalOutlierCount": 3,
        "rows": [
            row("slug_a", "危境再现·罗丹", "危境再现",
                rank=2, ranked_total=15, samples=93, median=57000,
                maximum=150000, outliers=2),
            row("slug_b", "蚀影噪雷", "危境碎片",
                rank=1, ranked_total=12, samples=20, median=90000, outliers=1),
            row("slug_c", "白垩界卫·苦难", "危境再现·白垩界卫",
                rank=None, ranked_total=9, samples=3, median=30000),
            row("slug_d", "清波访客·苦难", "影拓丰碑4期 · 山中见犼",
                rank=None, ranked_total=0, samples=0, median=None),
        ],
    }
