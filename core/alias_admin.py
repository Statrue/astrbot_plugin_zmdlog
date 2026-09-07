"""The 别名 commands: list, add and remove hand-written aliases.

Aliases live in one JSON file in the plugin data directory (seeded from
the bundled copy by the host); an edit is written there first and only
then installed, so a failed write leaves the running configuration as it
was. The matcher cache compares alias configurations by identity, which is
why an edit installs a new :class:`AliasConfig` instead of mutating one.
"""

from collections.abc import Callable
from pathlib import Path

from . import messages
from .client import ZmdLogsClientError
from .datasource import ZmdLogsDataSource
from .logs import LogSink
from .matcher import (
    AliasConfig,
    AliasConfigError,
    MatchLevel,
    MatchStatus,
    RankingMatcher,
    TargetType,
    normalize_search_text,
)
from .messages import shorten
from .models import HotBossCard
from .persistence import save_json
from .routing import RouteKind, RouteRequest

_EXACT_TYPES = frozenset({TargetType.BOARD, TargetType.DUNGEON})


class AliasAdmin:
    """Own the editable alias file and answer the 别名 commands in text."""

    def __init__(
        self,
        *,
        path: Path | None,
        data: ZmdLogsDataSource,
        board_matcher: Callable[[tuple[HotBossCard, ...]], RankingMatcher],
        logger: LogSink,
    ) -> None:
        self.path = path
        self._data = data
        self._board_matcher = board_matcher
        self._logger = logger
        self.aliases = self._load()

    def _load(self) -> AliasConfig:
        if self.path is None:
            return AliasConfig.empty()
        try:
            return AliasConfig.load(self.path)
        except AliasConfigError as exc:
            self._logger.error("ZmdLogBot alias configuration failed: %s", exc)
            return AliasConfig.empty()

    async def handle(self, route: RouteRequest, *, is_admin: bool) -> str:
        if route.kind is RouteKind.ALIAS_LIST:
            names: dict[str, str] = {}
            try:
                cards = await self._data.list_hot_bosses()
            except ZmdLogsClientError:
                cards = ()
            for card in cards:
                names[card.boss_slug] = card.boss_name
            return self._format_list(names)
        if not is_admin:
            return messages.ALIAS_ADMIN_ONLY
        if route.kind is RouteKind.ALIAS_REMOVE:
            updated, removed = self.aliases.without_alias(route.query)
            if removed == 0:
                return f"没有找到自定义别名「{shorten(route.query)}」。"
            if not self._save(updated):
                return messages.ALIAS_WRITE_FAILED
            return f"已删除别名「{route.query}」。"
        return await self._add(route.query)

    async def _add(self, argument: str) -> str:
        target_text, _, alias_text = argument.partition(" ")
        aliases = tuple(dict.fromkeys(alias_text.split()))
        blank = [alias for alias in aliases if not normalize_search_text(alias)]
        if blank:
            # A lone "·" folds to nothing and could never match anything.
            return "别名不能只有标点或符号：" + "、".join(blank)
        try:
            cards = await self._data.list_hot_bosses()
        except ZmdLogsClientError:
            return "ZMDLogs 暂时不可用，无法核对目标，请稍后重试。"
        matcher = self._board_matcher(cards)
        match = matcher.match(target_text, allowed_types=_EXACT_TYPES)
        choice = match.selected
        if match.status is not MatchStatus.MATCHED or choice is None:
            return (
                f"没有唯一匹配到「{shorten(target_text)}」，"
                "请用更完整的榜单或副本名。"
            )
        taken = set()
        for alias in aliases:
            probe = matcher.match(alias, allowed_types=_EXACT_TYPES)
            selected = probe.selected
            if (
                probe.status is MatchStatus.MATCHED
                and selected is not None
                and selected.level <= MatchLevel.NORMALIZED_EXACT
                and selected.target.key != choice.target.key
            ):
                taken.add(alias)
        if taken:
            return "以下别名已被其它榜单或副本占用：" + "、".join(sorted(taken))
        updated = self.aliases.with_aliases(
            choice.target.target_type,
            choice.target.key,
            aliases,
        )
        if not self._save(updated):
            return messages.ALIAS_WRITE_FAILED
        label = "榜单" if choice.target.target_type is TargetType.BOARD else "副本"
        return f"已为{label}「{choice.target.name}」添加别名：" + "、".join(aliases)

    def _format_list(self, board_names: dict[str, str]) -> str:
        lines: list[str] = []
        for target_type, key, values in self.aliases.iter_entries():
            if target_type is TargetType.BOARD:
                label, shown = "榜单", board_names.get(key, key)
            else:
                label, shown = "副本", key
            lines.append(f"{label} {shown}：{'、'.join(values)}")
        if not lines:
            return (
                "当前没有自定义别名。"
                "榜单名去掉难度/副本前缀、拼音首字母都已内置支持。"
            )
        return "自定义别名：\n" + "\n".join(lines)

    def _save(self, updated: AliasConfig) -> bool:
        if self.path is None or not save_json(self.path, updated.to_payload()):
            return False
        self.aliases = updated
        return True
