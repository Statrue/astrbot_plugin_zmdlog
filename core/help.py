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
class HelpPage:
    header: PageHeader
    sections: tuple[HelpSection, ...]


def build_help_page(command_prefix: str) -> HelpPage:
    """Build complete help data using the active AstrBot command prefix."""

    command = f"{command_prefix}zmdlog"
    return HelpPage(
        header=PageHeader(
            title="ZmdBot 指令帮助",
            subtitle="查询 ZMDLogs 公开 DPS 榜单",
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
                        command=f"{command} 关键词 [--top 数量]",
                        description="查询榜单或副本；具体榜单可指定 1–30 名。",
                    ),
                ),
            ),
        ),
    )
