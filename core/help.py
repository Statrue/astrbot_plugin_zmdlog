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
                        command=f"{command} <榜单关键词> --角色 <角色名>",
                        answers="带上这个角色的队伍，能排到第几？",
                        description=(
                            "优先只看以它为主 C 的记录；辅助、重装这类"
                            "从不当主 C 的角色没有主 C 记录，"
                            "会自动改为匹配整个四人阵容。"
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
                            "填榜单关键词直接看该榜第 N 名的战报，默认第 1 名。"
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
