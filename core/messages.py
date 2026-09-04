"""User-facing wording shared by more than one reply path.

Every sentence here used to be spelled out as a literal wherever it was
needed, and the copies drifted; the reply paths that quote the same fact
must quote the same words. Route-specific sentences stay next to the
route that produces them.
"""

UPSTREAM_UNAVAILABLE = "ZMDLogs 暂时不可用，请稍后重试。"
UNEXPECTED_FAILURE = "ZmdLogBot 暂时无法完成查询，请稍后重试。"
RENDER_FAILURE = "图片生成失败，请稍后重试。"
ACCOUNT_NOT_FOUND = "没有找到这个公开账号，或该账号暂无公开榜单记录。"
ACCOUNT_REFERENCE_NEEDED = (
    "请提供公开昵称（至少 2 个字符）、accountId 或 ZMDLogs 账号主页链接。"
)
BOARD_NOT_FOUND = "没有找到这个榜单，可能已下线或暂未公开。"
BATTLE_NOT_FOUND = "战报不存在、未公开或已删除。"
BATTLE_LINK_NOT_FOUND = "链接对应的公开战报不存在、未公开或已删除。"
CRISIS_CONTRACT_NO_STATISTICS = "危机合约不提供角色统计。"

# Battle pages that an older upload cannot fill.
NO_LOADOUT = "这份战报没有记录阵容配装。"
NO_SKILL_STATS = "这份战报没有技能统计数据。"
NO_TIMELINE = "这条战斗由旧版客户端上传，没有完整施法序列，画不了技能轴。"
TIMELINE_RATE_LIMITED = "技能轴接口请求过于频繁，请稍后再试。"

# 对比
COMPARE_REFERENCE_NEEDED = "对比两场战报时，两个参数都要是 battleId 或战报链接。"
COMPARE_SAME_BATTLE = "两边是同一场战报，没有可比的。"
COMPARE_CROSS_BOSS = (
    "两场不是同一个首领（{first} / {second}），每个首领的排轴都不同，不做跨榜单对比。"
)

# 关注 / 趋势
WATCH_DISABLED = "本机器人未开启名次通报功能。"
NO_ORIGIN = "无法确定当前会话，关注功能在这里不可用。"
WATCHLIST_WRITE_FAILED = "关注列表写入失败，请检查数据目录权限。"
WATCH_REMOVE_FORBIDDEN = "只有添加这条关注的人或机器人管理员可以取消它。"
TREND_NO_DATA = (
    "这个账号不在任何关注列表里，还没有名次记录；"
    "用 关注 <昵称或accountId> 关注后会从下一轮检查开始记录。"
)

# 别名
ALIAS_WRITE_FAILED = "别名文件写入失败，请检查数据目录权限。"
ALIAS_ADMIN_ONLY = "只有机器人管理员可以修改别名。"

ECHO_LIMIT = 40


def shorten(text: str, limit: int = ECHO_LIMIT) -> str:
    """Cut user text echoed in a reply so a huge message is never repeated."""

    return text if len(text) <= limit else text[: limit - 1] + "…"
