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
    """Build complete help data using the active AstrBot command prefix."""

    command = f"{command_prefix}zmdlog"
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
                title="帮助",
                summary="不记得指令时先来这里",
                commands=(
                    HelpCommand(
                        command=f"{command} [help]",
                        answers="有哪些指令可以用？",
                        description="无参数或加 help 都显示当前版本的这张帮助页。",
                    ),
                ),
            ),
            HelpSection(
                title="榜单排名",
                summary="想知道谁打得快，用这一组",
                commands=(
                    HelpCommand(
                        command=f"{command} 榜单",
                        answers="现在有哪些榜单，各自前三名是谁？",
                        description="一页看完当前全部公开榜单。",
                    ),
                    HelpCommand(
                        command=f"{command} <榜单关键词> [--top 数量]",
                        answers="这个榜单的前几名是谁？",
                        description=(
                            "关键词支持别名与拼音首字母；默认前 10 名，"
                            "可指定 1–30；写成 "
                            f"{command} 榜单 <关键词> 完全等价。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} <榜单关键词> --角色 <角色名> [角色名…]",
                        answers="带上这个角色（或这几个角色）的队伍，能排到第几？",
                        description=(
                            "优先只看以它为主 C 的记录；辅助、重装这类"
                            "从不当主 C 的角色没有主 C 记录，"
                            "会自动改为匹配整个四人阵容。"
                            "写两个以上角色名时，只看同时带上他们的队伍。"
                        ),
                    ),
                ),
            ),
            HelpSection(
                title="榜单分析",
                summary="想知道大家都在用什么、某个角色强不强，用这一组",
                commands=(
                    HelpCommand(
                        command=f"{command} 阵容 <榜单关键词> [--top 数量]",
                        answers="这个榜单大家都在用什么阵容？",
                        description=(
                            "各职业位的出场率按全榜统计，"
                            "常见队伍组合按前 N 名统计（默认 10）。"
                        ),
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 角色统计 [榜单关键词] "
                            "[--范围 7d|14d|30d|all] [--潜能 0|1-5|all]"
                        ),
                        answers="这个榜单里，各个六星角色分别能打多少？",
                        description="不填榜单则统计全部副本；箱线图为 DPS 分布。",
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 角色统计 <角色名> "
                            "[--范围 7d|14d|30d|all] [--潜能 0|1-5|all]"
                        ),
                        answers="这一个角色，在各个榜单分别能打多少？",
                        description="每行一个榜单，徽章为该角色在那个榜的名次。",
                    ),
                ),
            ),
            HelpSection(
                title="账号与战报",
                summary="想看具体某个人、某一场，用这一组",
                commands=(
                    HelpCommand(
                        command=f"{command} 账号 <昵称、accountId或主页链接>",
                        answers="这个人各个首领的最好成绩是多少？",
                        description="昵称支持模糊搜索，多个结果时回复序号选择。",
                    ),
                    HelpCommand(
                        command=f"{command} 战报 <battleId、链接或榜单关键词 [名次]>",
                        answers="这一场具体是怎么打的？",
                        description=(
                            "填榜单关键词直接看该榜第 N 名的战报，默认第 1 名；"
                            "卡片依次是战斗贡献、DPS 曲线、BUFF 覆盖与施法节奏，"
                            "底部附每个角色的配装概览和主要伤害来源。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} 配装 <battleId、链接或榜单关键词 [名次]>",
                        answers="这一场每个人用的什么武器、装备和词条？",
                        description=(
                            "逐角色列出等级、潜能、武器精炼与技能等级，"
                            "四件装备的套装、强化和词条数值。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} 技能 <battleId、链接或榜单关键词 [名次]>",
                        answers="这一场每个技能各打了多少伤害？",
                        description=(
                            "逐角色列出各技能的次数、总伤、占比、均伤与最高伤害，"
                            "普攻各段合并显示。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} 技能轴 <battleId、链接或榜单关键词 [名次]>",
                        answers="这一场每个人什么时候放了什么技能？",
                        description=(
                            "把战报卡里的施法节奏放大成整页：每个角色一条色轨，"
                            "招式按时长占一段，普攻按连段折叠。别名 排轴 / 时间轴；"
                            "旧版客户端上传的战斗没有施法序列。"
                        ),
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 对比 <榜单关键词 [名次A 名次B] "
                            "或 两个battleId/链接>"
                        ),
                        answers="这两场差在哪？",
                        description=(
                            "同一榜单默认比第 1 名和第 2 名，只给一个名次就是榜首对它；"
                            "并排看通关时间、DPS、阵容、两条 DPS 曲线，"
                            "以及同一角色的配装差异和主要伤害来源。别名 比较。"
                        ),
                    ),
                ),
            ),
            HelpSection(
                title="名次通报",
                summary="被人顶下去了、榜首换人了想第一时间知道，用这一组",
                commands=(
                    HelpCommand(
                        command=f"{command} 关注 <昵称、accountId或主页链接>",
                        answers="怎么让机器人盯着一个账号？",
                        description=(
                            "把公开账号加进关注列表；它的榜单名次掉下来时在这里"
                            "通报，并列出这段时间内新出现在它上方的纪录。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} 关注 榜单 <榜单关键词>",
                        answers="怎么让机器人盯着一个榜单的新纪录？",
                        description=(
                            "该榜前三名出现新纪录时在这里通报，"
                            "并说明谁跌出了前三。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} 关注",
                        answers="本群现在盯着哪些账号和榜单？",
                        description="分别列出关注的账号与榜单及序号，取关时用这个序号。",
                    ),
                    HelpCommand(
                        command=f"{command} 取关 <序号或昵称>",
                        answers="怎么不盯这个账号了？",
                        description="添加这条关注的人和机器人管理员可以取消它。",
                    ),
                    HelpCommand(
                        command=f"{command} 取关 榜单 <序号或榜单关键词>",
                        answers="怎么不盯这个榜单了？",
                        description=(
                            "序号见 关注 列表的榜单一栏；同样限添加者或管理员。"
                        ),
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 趋势 <昵称、accountId或主页链接> "
                            "[--范围 7d|14d|30d|all]"
                        ),
                        answers="这个账号最近各榜名次是涨是跌？",
                        description=(
                            "只有关注过的账号才有记录：名次通报每轮记一次，"
                            "只在名次变化时留点；默认看近 30 天。"
                        ),
                    ),
                ),
            ),
            HelpSection(
                title="别名管理",
                summary="榜单叫不出名字时，教机器人认你们的叫法（仅管理员）",
                commands=(
                    HelpCommand(
                        command=f"{command} 别名",
                        answers="机器人现在认哪些自定义叫法？",
                        description="列出本机器人已配置的榜单 / 副本别名。",
                    ),
                    HelpCommand(
                        command=f"{command} 别名 添加 <榜单或副本> <别名...>",
                        answers="怎么让机器人认我们群的叫法？",
                        description="管理员为榜单或副本增加别名，立即生效。",
                    ),
                    HelpCommand(
                        command=f"{command} 别名 删除 <别名>",
                        answers="怎么去掉一个加错的叫法？",
                        description="管理员删除一个自定义别名。",
                    ),
                ),
            ),
        ),
    )
