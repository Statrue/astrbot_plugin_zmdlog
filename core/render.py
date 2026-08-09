"""Strict Jinja rendering for the four ZmdBot HTML page templates."""

import re
from pathlib import Path

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    select_autoescape,
)

from .help import build_help_page
from .matcher import MatchChoice
from .models import BossRanking, HotBossCard
from .presentation import (
    build_all_top3_page,
    build_dungeon_top3_page,
    build_ranking_page,
)

_VERSION_LINE = re.compile(
    r"^\s*version\s*:\s*(?P<value>[^#]+?)\s*(?:#.*)?$"
)


class TemplateConfigurationError(ValueError):
    """Raised when local template metadata is missing or malformed."""


class TemplateRenderer:
    """Render complete HTML; browser capture is added in the next step."""

    def __init__(self, resources_path: Path, metadata_path: Path) -> None:
        self.resources_path = resources_path.resolve()
        self.version = read_plugin_version(metadata_path)
        self.environment = Environment(
            loader=FileSystemLoader(str(self.resources_path)),
            autoescape=select_autoescape(
                enabled_extensions=("html", "xml"),
                default_for_string=True,
            ),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    @classmethod
    def from_plugin_root(cls, plugin_root: Path) -> "TemplateRenderer":
        return cls(plugin_root / "resources", plugin_root / "metadata.yaml")

    def render_help(self, *, command_prefix: str) -> str:
        page = build_help_page(command_prefix)
        return self._render("help/help.html", page, "help")

    def render_all_top3(
        self,
        cards: tuple[HotBossCard, ...],
        *,
        query: str,
    ) -> str:
        page = build_all_top3_page(cards, query=query)
        return self._render("all-top3/all-top3.html", page, "all-top3")

    def render_dungeon_top3(
        self,
        choice: MatchChoice,
        cards: tuple[HotBossCard, ...],
        *,
        query: str,
    ) -> str:
        page = build_dungeon_top3_page(choice, cards, query=query)
        return self._render(
            "dungeon-top3/dungeon-top3.html",
            page,
            "dungeon-top3",
        )

    def render_ranking(
        self,
        ranking: BossRanking,
        *,
        query: str,
        web_base_url: str,
    ) -> str:
        page = build_ranking_page(
            ranking,
            query=query,
            web_base_url=web_base_url,
        )
        return self._render("ranking/ranking.html", page, "ranking")

    def _render(self, template_name: str, page, page_kind: str) -> str:
        template = self.environment.get_template(template_name)
        return template.render(
            page=page,
            page_kind=page_kind,
            plugin={"name": "ZmdBot", "version": self.version},
        )


def read_plugin_version(metadata_path: Path) -> str:
    """Read the manifest version without duplicating it in help definitions."""

    try:
        lines = metadata_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TemplateConfigurationError(
            f"cannot read plugin metadata: {metadata_path.name}"
        ) from exc

    for line in lines:
        match = _VERSION_LINE.match(line)
        if match is None:
            continue
        version = match.group("value").strip().strip("'\"").strip()
        if version:
            return version
        break
    raise TemplateConfigurationError("plugin metadata has no valid version")
