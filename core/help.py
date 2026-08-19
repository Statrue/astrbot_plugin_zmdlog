"""Local user-facing command definitions for the ZmdLogBot help page."""

from dataclasses import dataclass

from .presentation import PageHeader


@dataclass(frozen=True, slots=True)
class HelpCommand:
    command: str
    description: str


@dataclass(frozen=True, slots=True)
class HelpSection:
    title: str
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
            matched_name="常用查询",
            target_type="帮助",
        ),
        sections=(
            HelpSection(
                title="帮助",
                commands=(
                    HelpCommand(
                        command=command,
                        description="显示当前版本的完整帮助页。",
                    ),
                    HelpCommand(
                        command=f"{command} help",
                        description="与无参数指令显示相同的帮助页。",
                    ),
                ),
            ),
            HelpSection(
                title="常用查询",
                commands=(
                    HelpCommand(
                        command=f"{command} 榜单",
                        description="显示全部公开榜单及各榜前三名。",
                    ),
                    HelpCommand(
                        command=f"{command} 榜单 <关键词>",
                        description="查询具体榜单或副本。",
                    ),
                    HelpCommand(
                        command=f"{command} 关键词 [--top 数量] [--角色 角色名]",
                        description=(
                            "查询榜单或副本，支持别名与拼音首字母；"
                            "具体榜单可指定 1–30 名，或只看某个主C的记录。"
                        ),
                    ),
                ),
            ),
            HelpSection(
                title="角色与阵容",
                commands=(
                    HelpCommand(
                        command=(
                            f"{command} 角色统计 [榜单关键词] "
                            "[--范围 7d|14d|30d|all] [--潜能 0|1-5|all]"
                        ),
                        description=(
                            "查看全部副本或某个榜单的六星角色 DPS 分布，"
                            "可限定时间范围与潜能。"
                        ),
                    ),
                    HelpCommand(
                        command=f"{command} 阵容 <榜单关键词> [--top 数量]",
                        description="查看某个榜单的职业位出场率与前 N 名常见阵容。",
                    ),
                ),
            ),
            HelpSection(
                title="账号与战报",
                commands=(
                    HelpCommand(
                        command=f"{command} 账号 <accountId或账号主页链接>",
                        description="按公开账号 ID 查询各首领最佳记录。",
                    ),
                    HelpCommand(
                        command=f"{command} 战报 <battleId或战报链接>",
                        description="生成一场公开战斗的摘要卡。",
                    ),
                ),
            ),
            HelpSection(
                title="别名管理",
                commands=(
                    HelpCommand(
                        command=f"{command} 别名",
                        description="查看本群机器人自定义的榜单 / 副本别名。",
                    ),
                    HelpCommand(
                        command=f"{command} 别名 添加 <榜单或副本> <别名...>",
                        description="管理员为榜单或副本增加别名，立即生效。",
                    ),
                    HelpCommand(
                        command=f"{command} 别名 删除 <别名>",
                        description="管理员删除一个自定义别名。",
                    ),
                ),
            ),
        ),
    )
