"""Local user-facing command definitions for the ZmdLogBot help page."""

from dataclasses import dataclass

from .presentation import PageHeader


@dataclass(frozen=True, slots=True)
class HelpCommand:
    command: str
    # The question this command answers, in the words a player would use. It is
    # the primary line on the page: several commands look alike from their
    # syntax alone and only differ in what they answer.
    answers: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class HelpSection:
    title: str
    summary: str
    commands: tuple[HelpCommand, ...]


@dataclass(frozen=True, slots=True)
class HelpPage:
    header: PageHeader
    sections: tuple[HelpSection, ...]


def build_help_page(command_prefix: str) -> HelpPage:
    """Build complete help data using the active AstrBot command prefix.

    One row per question, not per spelling: commands that take the same
    argument share a row, and a description is one line of what the syntax
    alone would not tell the reader. The page itself is the answer to
    "which commands exist", so it carries no row for the help command.
    """

    command = f"{command_prefix}zmdlog"
    battle_argument = "<battleId、链接或榜单关键词 [名次]>"
    return HelpPage(
        header=PageHeader(
            title="ZmdLogBot 指令帮助",
            subtitle="查询 ZMDLogs 公开榜单、账号与战报",
            query=command,
            matched_name="指令一览",
            target_type="帮助",
        ),
        sections=(
            HelpSection(
                title="榜单",
                summary="谁打得快、大家在用什么",
                commands=(
                    HelpCommand(
                        command=(
                            f"{command} <榜单关键词> [--top 数量] "
                            "[--角色 角色名…] [--属性 属性]"
                        ),
                        answers="这个榜的前几名是谁？带上某些角色的队伍能排第几？",
                        description=(
                            "关键词认别名和拼音首字母，默认前 10 名；"
                            "--角色 写多个名字时只看同时带上他们的队伍。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} 榜单",
                        answers="现在有哪些榜单，各自前三名是谁？",
                    ),
                    HelpCommand(
                        command=f"{command} 阵容 <榜单关键词> [--top 数量]",
                        answers="这个榜大家都在用什么阵容？",
                        description="职业位出场率按全榜统计，常见组合按前 N 名统计。",
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 角色统计 [榜单关键词或角色名] "
                            "[--范围 7d|14d|30d|all] [--潜能 0|1-5|all]"
                        ),
                        answers="这个榜谁强？这个角色在哪个榜强？",
                        description=(
                            "接榜单看各六星角色的 DPS 分布，接角色名看它在各榜的名次，"
                            "不填看全部榜单。"
                        ),
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 角色排名 [角色名… | --属性 属性 | --职业 职业]"
                        ),
                        answers=(
                            "带这个角色的队伍在各个榜排第几？同时带这几个的呢？"
                            "谁的冠军最多？"
                        ),
                        description=(
                            "接角色名看每个榜带它的最好记录，写几个名字只看同时带上他们的队伍；"
                            "不填看各角色的冠军数。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} 新纪录 [--范围 7d|14d|30d]",
                        answers="最近谁刷新了第一名？新上传了哪些记录？哪个榜最活跃？",
                        description=(
                            "索引每次重读发现的第一名易主和新记录，"
                            "加各榜这段时间打出的记录数；默认近 7 天。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} 玩家排名 [--范围 7d|14d|30d]",
                        answers="哪个玩家的冠军最多？",
                        description=(
                            "各公开账号上传的第一名、前三、前十各几个，"
                            "附常用主C 和常用阵容；写昵称则是那个人的成绩，"
                            "与 角色排名 同形。"
                        ),
                    ),
                ),
            ),
            HelpSection(
                title="战报",
                summary="具体某个人、某一场",
                commands=(
                    HelpCommand(
                        command=f"{command} 账号 <昵称、accountId或主页链接>",
                        answers="这个人各首领的最好成绩是多少？",
                        description="昵称模糊搜索，多个结果时回复序号选择。",
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 战报 | 配装 | 技能 | 技能轴 {battle_argument}"
                        ),
                        answers="这一场怎么打的？用的什么？技能打了多少？什么时候放的？",
                        description=(
                            "四个指令同一种写法，填榜单关键词就看该榜第 N 名"
                            "（默认第 1 名）；战报是总卡，其余三个各放大一面。"
                        ),
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 对比 "
                            "<榜单关键词 [名次 名次] 或 两个battleId>"
                        ),
                        answers="这两场差在哪？",
                        description="默认第 1 名对第 2 名；两场须是同一首领。",
                    ),
                ),
            ),
            HelpSection(
                title="关注",
                summary="名次变了第一时间知道",
                commands=(
                    HelpCommand(
                        command=f"{command} 关注 [<账号> | 榜单 <关键词>]",
                        answers="怎么让机器人盯着一个账号或榜单？",
                        description=(
                            "账号掉名次、榜单前三出新纪录时在本群通报；"
                            "不带参数列出关注列表和序号，"
                            "取关 <序号> 取消，限添加者或管理员。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} 趋势 <账号> [--范围 7d|14d|30d|all]",
                        answers="这个账号最近各榜名次是涨是跌？",
                        description="只有关注过的账号有记录，默认近 30 天。",
                    ),
                ),
            ),
            HelpSection(
                title="绑定",
                summary="把自己和 ZMDLogs 账号对上号",
                commands=(
                    HelpCommand(
                        command=f"{command} 绑定 <绑定码>",
                        answers="怎么让机器人知道我是谁？",
                        description=(
                            "在 ZMDLogs 登录后从 账号菜单 → 机器人绑定 生成绑定码，"
                            "10 分钟内有效；解绑 / 主账号 管理列表。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} 我的 [序号或昵称]",
                        answers="我自己各首领的最好成绩是多少？",
                        description="默认看主账号，绑了几个号就写序号。",
                    ),
                    HelpCommand(
                        command=f"{command} 群榜 <榜单关键词> [--top 数量]",
                        answers="本群谁打这个榜最快？",
                        description=(
                            "本群用过绑定指令的成员在该榜的最好记录，按全榜名次排。"
                        ),
                    ),
                ),
            ),
            HelpSection(
                title="管理",
                summary="仅机器人管理员",
                commands=(
                    HelpCommand(
                        command=(
                            f"{command} 别名 [添加 <榜单或副本> <别名…> | 删除 <别名>]"
                        ),
                        answers="怎么让机器人认我们群的叫法？",
                        description="不带参数列出现有别名，改动立即生效。",
                    ),
                ),
            ),
        ),
    )
