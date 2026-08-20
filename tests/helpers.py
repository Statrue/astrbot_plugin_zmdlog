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


def battle_detail_payload() -> dict:
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
                    "characterName": "洛茜",
                    "accountDisplayName": "测试账号",
                }
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
            }
        ],
        "timelineEvents": [{"ignored": True}],
        "roleSkillStats": [{"ignored": True}],
        "integrity": {"verified": True},
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
