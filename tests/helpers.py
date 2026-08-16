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
