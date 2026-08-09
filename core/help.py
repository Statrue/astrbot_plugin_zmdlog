"""Local user-facing command definitions for the ZmdBot help page."""

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
class HelpTip:
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class HelpPage:
    header: PageHeader
    tips: tuple[HelpTip, ...]
    sections: tuple[HelpSection, ...]


def build_help_page(command_prefix: str) -> HelpPage:
    """Build complete help data using the active AstrBot command prefix."""

    command = f"{command_prefix}zmdlog"
    return HelpPage(
        header=PageHeader(
            title="ZmdBot 指令帮助",
            subtitle="查询 ZMDLogs 公开 DPS 榜单",
            query=command,
            matched_name="完整指令说明",
            target_type="帮助",
        ),
        tips=(
            HelpTip(
                title="固定口径",
                description="首期榜单固定使用 DPS，不提供指标切换。",
            ),
            HelpTip(
                title="智能匹配",
                description=(
                    "支持标准名称、榜单 slug、本地别名和少量错漏字；"
                    "无法唯一确认时会列出候选。"
                ),
            ),
            HelpTip(
                title="副本范围",
                description=(
                    "“影拓”聚合影拓丰碑 1—4 期；“影拓4”只查询第四期。"
                ),
            ),
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
                title="榜单",
                commands=(
                    HelpCommand(
                        command=f"{command} 榜单",
                        description="显示全部公开榜单及各榜前三名。",
                    ),
                    HelpCommand(
                        command=f"{command} 榜单 <关键词>",
                        description="查询具体榜单、副本或同系列副本范围。",
                    ),
                    HelpCommand(
                        command=f"{command} <关键词>",
                        description="使用快捷入口智能匹配榜单与副本。",
                    ),
                    HelpCommand(
                        command=f"{command} 榜单 罗丹",
                        description="查询“危境再现·罗丹”完整 DPS 排名。",
                    ),
                    HelpCommand(
                        command=f"{command} 三位",
                        description="通过别名查询“危境再现·三位一体”。",
                    ),
                    HelpCommand(
                        command=f"{command} 影拓",
                        description="聚合影拓丰碑 1—4 期全部榜单前三名。",
                    ),
                    HelpCommand(
                        command=f"{command} 影拓4",
                        description="只显示影拓丰碑第四期全部榜单前三名。",
                    ),
                    HelpCommand(
                        command=f"{command} 榜单 dung01_group_bossrush02",
                        description="也可使用完整榜单 slug 精确查询。",
                    ),
                ),
            ),
        ),
    )
