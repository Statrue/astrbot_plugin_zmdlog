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
