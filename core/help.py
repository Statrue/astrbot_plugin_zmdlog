"""Local user-facing command definitions for the ZmdLogBot help page.

The page is one comic picture (``resources/help``): every command as the
shortest way to write it beside one statement of what it does, and the
``--options`` explained once, in their own card. ``HelpCommand.command``
keeps the full syntax, options and all, so the options card can be checked
against what the commands actually take; the page draws ``short``.
"""

import re
from dataclasses import dataclass

from .presentation import PageHeader

# A bracketed group that holds an --option, innermost first, so a group
# nested in one (``[名次]`` in ``[榜单 [名次] --x]``) goes with it.
_OPTION_GROUP = re.compile(r"\s*\[[^\[\]]*--[^\[\]]*\]")


@dataclass(frozen=True, slots=True)
class HelpCommand:
    command: str
    # What this command does, as one statement that opens with its verb and
    # ends without punctuation. It is the only line under the command: several
    # commands look alike from their syntax alone and differ only in this.
    answers: str

    @property
    def short(self) -> str:
        """The command without its --options, the form the page shows."""

        text = self.command
        while (trimmed := _OPTION_GROUP.sub("", text)) != text:
            text = trimmed
        return text


@dataclass(frozen=True, slots=True)
class HelpSection:
    title: str
    summary: str
    commands: tuple[HelpCommand, ...]


@dataclass(frozen=True, slots=True)
class HelpOption:
    """One line of the options card: how it is written, and what it does."""

    option: str
    does: str


@dataclass(frozen=True, slots=True)
class HelpPage:
    header: PageHeader
    sections: tuple[HelpSection, ...]
    # Every --option the commands take, explained once rather than on each
    # row, plus the two usages the pictures never hint at (paging, 对比 … 我).
    options: tuple[HelpOption, ...] = ()
    # Small print on what the chat itself must allow; empty off the QQ
    # official bot, where nothing needs allowing.
    notes: tuple[str, ...] = ()

    @property
    def command_count(self) -> int:
        return sum(len(section.commands) for section in self.sections)


# The official bot hears a group only when @-ed, unless the group lets it read
# every message, and pushes nothing into a group that has not let it speak
# unasked. Both are switches a group owner or admin sets, not the plugin.
OFFICIAL_NOTES = (
    "没开「获取群内全部消息」的群，指令前要先 @机器人。",
    "榜单通报要群主或管理员先开「机器人主动在群聊内发言」。",
)


# The options card, in the order a reader meets them. ``对比 … 我`` is not an
# --option but is written like one, after the command it changes.
OPTIONS = (
    HelpOption("--页 N|全部", "翻页，全部看前 30 条"),
    HelpOption("--角色 角色名…", "只看带上他们的队伍"),
    HelpOption("--属性 / --职业", "按属性、职业筛"),
    HelpOption("--范围 7d|14d|30d|all", "时间范围"),
    HelpOption("--口径 rdps", "看团队贡献"),
    HelpOption("--潜能 0|1-5|all", "角色统计按潜能"),
    HelpOption("--榜单 关键词", "角色档案只看一个榜"),
    HelpOption("对比 … 我", "拿自己的主账号来比，要先绑定"),
)


def build_help_page(command_prefix: str, *, official: bool = False) -> HelpPage:
    """Build complete help data using the active AstrBot command prefix.

    One row per use, not per spelling: commands that take the same argument
    share a row, and its statement is what the syntax alone would not tell
    the reader. The page itself is the answer to "which commands exist", so
    it carries no row for the help command.
    ``official`` adds the QQ official bot's two preconditions as small print.
    """

    command = f"{command_prefix}zmdlog"
    battle_argument = "<battleId、链接或榜单关键词 [名次]>"
    return HelpPage(
        header=PageHeader(
            title="终末地·藕粉铺子 指令帮助",
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
                            f"{command} <榜单关键词> [--页 N|全部] "
                            "[--角色 角色名…] [--属性 属性] [--口径 rdps]"
                        ),
                        answers="查询某个榜单",
                    ),
                    HelpCommand(
                        command=f"{command} 榜单",
                        answers="列出全部副本和榜单",
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 阵容 <榜单关键词> [--口径 rdps]"
                        ),
                        answers="统计某个榜单的常用阵容",
                    ),
                    HelpCommand(
                        command=f"{command} 新纪录 [--范围 7d|14d|30d] [--口径 rdps]",
                        answers="列出最近易主的第一名、新纪录和最活跃的榜",
                    ),
                    HelpCommand(
                        command=f"{command} 玩家排名 [--范围 7d|14d|30d] [--口径 rdps]",
                        answers="统计哪个玩家的冠军最多",
                    ),
                ),
            ),
            # Three commands that take a character name: the statements beside
            # them are what tells them apart.
            HelpSection(
                title="角色",
                summary="一个角色强不强、怎么养、跑得多快",
                commands=(
                    HelpCommand(
                        command=(
                            f"{command} 角色统计 [榜单关键词或角色名] "
                            "[--范围 7d|14d|30d|all] [--潜能 0|1-5|all] [--口径 rdps]"
                        ),
                        answers="比较一个榜里谁强，或一个角色在哪个榜强",
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 角色排名 [角色名… | --属性 属性 | --职业 职业] "
                            "[--口径 rdps]"
                        ),
                        answers=(
                            "统计各角色的冠军数，写角色名查带它的队伍在各榜排第几"
                        ),
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 角色档案 <角色名> [--榜单 榜单关键词] "
                            "[--范围 7d|14d|30d|all]"
                        ),
                        answers="查询一个角色大家怎么养、带谁、在哪些榜跑得快",
                    ),
                ),
            ),
            HelpSection(
                title="战报",
                summary="具体某个人、某一场",
                commands=(
                    HelpCommand(
                        command=f"{command} 账号 <昵称、accountId或主页链接>",
                        answers="查询一个账号各首领的最好成绩",
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 战报 | 配装 | 技能 | 技能轴 {battle_argument}"
                            " [--口径 rdps]"
                        ),
                        answers="查看一场战报的摘要，配装、技能、技能轴各放大一面",
                    ),
                    HelpCommand(
                        command=(
                            f"{command} 对比 "
                            "<榜单关键词 [名次 名次] 或 两个battleId> [--口径 rdps]"
                        ),
                        answers="对比同一首领的两场战斗",
                    ),
                    HelpCommand(
                        command=f"{command} 对比 <榜单关键词> 我 [名次] [--口径 rdps]",
                        answers="拿自己在该榜的最好记录对比第 N 名",
                    ),
                ),
            ),
            HelpSection(
                title="关注",
                summary="有新纪录第一时间知道",
                commands=(
                    HelpCommand(
                        command=f"{command} 关注 [<榜单关键词> | 全部榜单]",
                        answers="关注榜单，有新纪录在这里通报；取关 撤销",
                    ),
                    HelpCommand(
                        command=f"{command} 趋势 <账号> [--范围 7d|14d|30d|all]",
                        answers="查看一个账号最近各榜名次的涨跌",
                    ),
                ),
            ),
            HelpSection(
                title="绑定",
                summary="把自己和 ZMDLogs 账号对上号，群聊私聊都行",
                commands=(
                    HelpCommand(
                        command=f"{command} 绑定 <绑定码>",
                        answers="用绑定码绑上账号，解绑 / 主账号 管理列表",
                    ),
                    HelpCommand(
                        command=f"{command} 我的 [序号或昵称]",
                        answers="查看自己各首领的最好成绩",
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
                        answers="教机器人认新的榜单叫法",
                    ),
                ),
            ),
        ),
        options=OPTIONS,
        notes=OFFICIAL_NOTES if official else (),
    )
