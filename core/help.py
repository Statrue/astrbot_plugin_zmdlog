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
                        command=f"{command} 关键词",
                        description="直接输入关键词查询榜单或副本。",
                    ),
                ),
            ),
        ),
    )
