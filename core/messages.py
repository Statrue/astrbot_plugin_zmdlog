"""User-facing wording shared by more than one reply path.

Every sentence here used to be spelled out as a literal wherever it was
needed, and the copies drifted; the reply paths that quote the same fact
must quote the same words. Route-specific sentences stay next to the
route that produces them.
"""

UPSTREAM_UNAVAILABLE = "ZMDLogs 暂时不可用，请稍后重试。"
INDEX_FILLING = "榜单索引还在建立（启动后约需十几秒），请稍后再试。"
UNEXPECTED_FAILURE = "ZmdLogBot 暂时无法完成查询，请稍后重试。"
RENDER_FAILURE = "图片生成失败，请稍后重试。"
ACCOUNT_NOT_FOUND = "没有找到这个公开账号，或该账号暂无公开榜单记录。"
MORE_NICKNAME_HITS = "还有更多同名结果未列出，可输入更完整的昵称。"
ACCOUNT_REFERENCE_NEEDED = (
    "请提供公开昵称（至少 2 个字符）、accountId 或 ZMDLogs 账号主页链接。"
)
BOARD_NOT_FOUND = "没有找到这个榜单，可能已下线或暂未公开。"
PUBLIC_DATA_NOT_FOUND = "没有找到对应的公开数据。"
RATE_LIMITED = "ZMDLogs 请求过于频繁，请稍后再试。"
BATTLE_NOT_FOUND = "战报不存在、未公开或已删除。"
BATTLE_LINK_NOT_FOUND = "链接对应的公开战报不存在、未公开或已删除。"
CRISIS_CONTRACT_NO_STATISTICS = "危机合约不提供角色统计。"
CHARACTER_NOT_IN_RECORDS = "公开记录里没有这个角色出场，可能是名字不对。"
NO_TEAM_FIELDING = "公开记录里没有同时带「{names}」的队伍。"

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

# 绑定 / 我的 / 群榜. A binding is only ever made with a code the site
# issued to whoever was logged into the account; there is no other way in.
BINDINGS_DISABLED = "本机器人未开启账号绑定功能。"
NO_SENDER = "无法识别发送者，绑定功能在这里不可用。"
HOW_TO_GET_A_CODE = (
    "登录 ZMDLogs，在 账号菜单 → 机器人绑定（/account/binding）生成绑定码，"
    "10 分钟内发送 {command} 绑定 ZMD-XXXX-XXXX。"
)
NOT_BOUND = "你还没有绑定账号。" + HOW_TO_GET_A_CODE
BIND_CODE_NEEDED = "绑定只认绑定码，不认昵称或 accountId。" + HOW_TO_GET_A_CODE
BIND_CODE_INVALID = "绑定码无效或已过期，请到 ZMDLogs 重新生成。"
BIND_CODE_USED = "这个绑定码已经用过了，请重新生成一个。"
BIND_UNSUPPORTED = "当前配置的 ZMDLogs 地址没有绑定码接口，暂时无法绑定。"
BIND_LIMIT_REACHED = "每人最多绑定 {limit} 个账号，请先解绑一个。"
BINDINGS_WRITE_FAILED = "绑定信息写入失败，请检查数据目录权限。"
GROUP_BOARD_PRIVATE = "群榜只能在群聊里用。"
GROUP_BOARD_EMPTY = (
    "本群还没有人绑定账号。绑定过账号的成员在本群用过 绑定、我的 或 群榜 后才会上群榜；"
    + HOW_TO_GET_A_CODE
)

# Appended to a tool result when the same turn already drew another picture:
# the model must not send the reader to an image that will not come.
TOOL_PICTURE_WITHHELD = "（这次回答查了多个对象，不附图，不要让用户看图。）"

ECHO_LIMIT = 40


def shorten(text: str, limit: int = ECHO_LIMIT) -> str:
    """Cut user text echoed in a reply so a huge message is never repeated."""

    return text if len(text) <= limit else text[: limit - 1] + "…"


def ambiguous_character(query: str, candidates) -> str:
    """Several characters fit what was typed: name them, ask for the full one."""

    return f"「{shorten(query)}」可能是：{' / '.join(candidates)}，请写全名。"
