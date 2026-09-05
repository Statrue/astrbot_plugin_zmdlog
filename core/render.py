"""Strict HTML templates and single-image Playwright capture for ZmdLogBot."""

import asyncio
import base64
import re
import struct
import tempfile
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    select_autoescape,
)
from markupsafe import Markup

from .characters import CharacterFilterScope
from .help import build_help_page
from .history import AccountHistory
from .matcher import MatchChoice
from .models import (
    BattleDetailSummary,
    BattleExport,
    BossRanking,
    CharacterBossStatistics,
    CharacterStatistics,
    HotBossCard,
    PublicUserRankings,
)
from .presentation import (
    build_account_page,
    build_all_top3_page,
    build_battle_page,
    build_character_boss_page,
    build_character_stats_page,
    build_compare_page,
    build_dungeon_top3_page,
    build_loadout_page,
    build_ranking_page,
    build_roster_page,
    build_skill_page,
    build_timeline_page,
    build_trend_page,
)
from .routing import DEFAULT_RANKING_TOP

_VERSION_LINE = re.compile(
    r"^\s*version\s*:\s*(?P<value>[^#]+?)\s*(?:#.*)?$"
)
_OUTPUT_FILE_GLOB = "zmd-*.png"
# The bundled fonts are served to Chromium from memory under this origin
# (a reserved, unresolvable TLD) instead of being embedded as data URLs:
# they weigh 3.7 MB base64, and pushing that through set_content plus
# decoding it cost a third of every capture.
FONT_ORIGIN = "https://fonts.zmdlog.invalid"
# Short pages are captured at 2x for legibility on phones; the potentially very
# long top-3 pages stay at 1x to remain well below Chromium's 16384px limit.
_HIGH_DPI_PAGE_KINDS = frozenset(
    {
        "help",
        "ranking",
        "account",
        "battle",
        "character-stats",
        "character-boss",
        "roster",
        "loadout",
        "skills",
        "trend",
        "timeline",
        "compare",
        "warmup",
    }
)
_MAX_CAPTURE_HEIGHT_PX = 15_000
DEFAULT_MAX_CONCURRENT_RENDERS = 2
# The semaphore bounds how many captures run at once, not how many callers
# wait for one. A burst from several chats would otherwise queue without
# limit, and the wait counted against no timeout at all, so the last caller
# could sit there for minutes before Chromium even opened its page.
DEFAULT_MAX_QUEUED_RENDERS = 8
DEFAULT_OUTPUT_TTL_SECONDS = 10 * 60
DEFAULT_MAX_OUTPUT_FILES = 50


class TemplateConfigurationError(ValueError):
    """Raised when local template metadata is missing or malformed."""


class RenderError(RuntimeError):
    """Raised when a complete ZmdLogBot image cannot be produced.

    ``html`` carries the fully rendered page when only the browser capture
    failed, so callers can hand it to another renderer.
    """

    def __init__(self, message: str, *, html: str | None = None) -> None:
        super().__init__(message)
        self.html = html


class TemplateRenderer:
    """Render self-contained HTML for one of the public page types."""

    def __init__(
        self,
        resources_path: Path,
        metadata_path: Path,
        background_path: Path,
        fonts_path: Path | None = None,
    ) -> None:
        self.resources_path = resources_path.resolve()
        self.version = read_plugin_version(metadata_path)
        self.background_data_url = _load_background_data_url(background_path)
        self.font_files, self.font_face_css, self.linked_font_face_css = (
            _load_fonts(fonts_path)
        )
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
        resources_path = plugin_root / "resources"
        return cls(
            resources_path,
            plugin_root / "metadata.yaml",
            resources_path / "common" / "scene-background.svg",
            resources_path / "common" / "fonts",
        )

    def render_help(
        self,
        *,
        command_prefix: str,
        embed_fonts: bool = True,
    ) -> str:
        page = build_help_page(command_prefix)
        return self._render(
            "help/help.html",
            page,
            "help",
            embed_fonts=embed_fonts,
        )

    def render_all_top3(
        self,
        cards: tuple[HotBossCard, ...],
        *,
        query: str,
        web_base_url: str | None = None,
        embed_fonts: bool = True,
    ) -> str:
        page = build_all_top3_page(
            cards,
            query=query,
            web_base_url=web_base_url,
        )
        return self._render(
            "all-top3/all-top3.html",
            page,
            "all-top3",
            embed_fonts=embed_fonts,
        )

    def render_dungeon_top3(
        self,
        choice: MatchChoice,
        cards: tuple[HotBossCard, ...],
        *,
        query: str,
        web_base_url: str | None = None,
        embed_fonts: bool = True,
    ) -> str:
        page = build_dungeon_top3_page(
            choice,
            cards,
            query=query,
            web_base_url=web_base_url,
        )
        return self._render(
            "dungeon-top3/dungeon-top3.html",
            page,
            "dungeon-top3",
            embed_fonts=embed_fonts,
        )

    def render_ranking(
        self,
        ranking: BossRanking,
        *,
        query: str,
        ranking_limit: int = DEFAULT_RANKING_TOP,
        web_base_url: str | None = None,
        character_filter: str | tuple[str, ...] | None = None,
        character_filter_scope: CharacterFilterScope = CharacterFilterScope.MAIN,
        embed_fonts: bool = True,
    ) -> str:
        page = build_ranking_page(
            ranking,
            query=query,
            display_limit=ranking_limit,
            web_base_url=web_base_url,
            character_filter=character_filter,
            character_filter_scope=character_filter_scope,
        )
        return self._render(
            "ranking/ranking.html",
            page,
            "ranking",
            embed_fonts=embed_fonts,
        )

    def render_character_stats(
        self,
        stats: CharacterStatistics,
        *,
        query: str,
        web_base_url: str | None = None,
        embed_fonts: bool = True,
    ) -> str:
        page = build_character_stats_page(
            stats,
            query=query,
            web_base_url=web_base_url,
        )
        return self._render(
            "character-stats/character-stats.html",
            page,
            "character-stats",
            embed_fonts=embed_fonts,
        )

    def render_character_boss(
        self,
        stats: CharacterBossStatistics,
        *,
        query: str,
        web_base_url: str | None = None,
        embed_fonts: bool = True,
    ) -> str:
        page = build_character_boss_page(
            stats,
            query=query,
            web_base_url=web_base_url,
        )
        return self._render(
            "character-boss/character-boss.html",
            page,
            "character-boss",
            embed_fonts=embed_fonts,
        )

    def render_roster(
        self,
        ranking: BossRanking,
        *,
        query: str,
        ranking_limit: int = DEFAULT_RANKING_TOP,
        web_base_url: str | None = None,
        embed_fonts: bool = True,
    ) -> str:
        page = build_roster_page(
            ranking,
            query=query,
            display_limit=ranking_limit,
            web_base_url=web_base_url,
        )
        return self._render(
            "roster/roster.html",
            page,
            "roster",
            embed_fonts=embed_fonts,
        )

    def render_account(
        self,
        account: PublicUserRankings,
        *,
        query: str,
        web_base_url: str,
        embed_fonts: bool = True,
    ) -> str:
        page = build_account_page(
            account,
            query=query,
            web_base_url=web_base_url,
        )
        return self._render(
            "account/account.html",
            page,
            "account",
            embed_fonts=embed_fonts,
        )

    def render_battle(
        self,
        battle: BattleDetailSummary,
        *,
        query: str,
        web_base_url: str,
        export: BattleExport | None = None,
        export_note: str | None = None,
        suits: dict[str, str] | None = None,
        embed_fonts: bool = True,
    ) -> str:
        page = build_battle_page(
            battle,
            query=query,
            web_base_url=web_base_url,
            export=export,
            export_note=export_note,
            suits=suits,
        )
        return self._render(
            "battle/battle.html",
            page,
            "battle",
            embed_fonts=embed_fonts,
        )

    def render_loadout(
        self,
        battle: BattleDetailSummary,
        *,
        query: str,
        web_base_url: str,
        suits: dict[str, str] | None = None,
        embed_fonts: bool = True,
    ) -> str:
        page = build_loadout_page(
            battle,
            query=query,
            web_base_url=web_base_url,
            suits=suits,
        )
        return self._render(
            "loadout/loadout.html",
            page,
            "loadout",
            embed_fonts=embed_fonts,
        )

    def render_skills(
        self,
        battle: BattleDetailSummary,
        *,
        query: str,
        web_base_url: str,
        embed_fonts: bool = True,
    ) -> str:
        page = build_skill_page(
            battle,
            query=query,
            web_base_url=web_base_url,
        )
        return self._render(
            "skills/skills.html",
            page,
            "skills",
            embed_fonts=embed_fonts,
        )

    def render_trend(
        self,
        history: AccountHistory,
        *,
        query: str,
        web_base_url: str,
        time_range: str = "30d",
        last_checked: str | None = None,
        embed_fonts: bool = True,
    ) -> str:
        page = build_trend_page(
            history,
            query=query,
            web_base_url=web_base_url,
            time_range=time_range,
            last_checked=last_checked,
        )
        return self._render(
            "trend/trend.html",
            page,
            "trend",
            embed_fonts=embed_fonts,
        )

    def render_timeline(
        self,
        export: BattleExport,
        *,
        query: str,
        web_base_url: str,
        battle: BattleDetailSummary | None = None,
        embed_fonts: bool = True,
    ) -> str:
        page = build_timeline_page(
            export,
            query=query,
            web_base_url=web_base_url,
            battle=battle,
        )
        return self._render(
            "timeline/timeline.html",
            page,
            "timeline",
            embed_fonts=embed_fonts,
        )

    def render_compare(
        self,
        first: BattleDetailSummary,
        second: BattleDetailSummary,
        *,
        query: str,
        web_base_url: str,
        rank_a: int | None = None,
        rank_b: int | None = None,
        suits: dict[str, str] | None = None,
        embed_fonts: bool = True,
    ) -> str:
        page = build_compare_page(
            first,
            second,
            query=query,
            web_base_url=web_base_url,
            rank_a=rank_a,
            rank_b=rank_b,
            suits=suits,
        )
        return self._render(
            "compare/compare.html",
            page,
            "compare",
            embed_fonts=embed_fonts,
        )

    def _render(
        self,
        template_name: str,
        page,
        page_kind: str,
        *,
        embed_fonts: bool = True,
    ) -> str:
        """Render one page; ``embed_fonts`` picks how the fonts are referenced.

        Embedded data URLs make the document self-contained for a renderer
        that cannot reach this process (AstrBot's fallback); linked fonts on
        :data:`FONT_ORIGIN` keep it small for the bundled Chromium, whose
        route handler serves them from memory.
        """

        template = self.environment.get_template(template_name)
        return template.render(
            page=page,
            page_kind=page_kind,
            plugin={"name": "ZmdLogBot", "version": self.version},
            background_data_url=self.background_data_url,
            font_face_css=(
                self.font_face_css if embed_fonts else self.linked_font_face_css
            ),
        )


_ASSET_CACHE_TTL_SECONDS = 6 * 3600.0
_ASSET_CACHE_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_ASSET_CACHE_MAX_ITEM_BYTES = 2 * 1024 * 1024


@dataclass(slots=True)
class CachedAsset:
    status: int
    content_type: str
    body: bytes
    expires_at: float


class AssetCache:
    """In-memory byte cache for upstream avatars fetched during capture.

    Every capture runs in a fresh incognito browser context, so Chromium's own
    cache never helps: without this, each rendered page re-downloads its whole
    avatar set from upstream. The avatar catalog is small and static, hence
    the long TTL. Insertion-ordered eviction under a total-byte cap; anything
    over the per-item cap is simply not stored.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = _ASSET_CACHE_TTL_SECONDS,
        max_total_bytes: int = _ASSET_CACHE_MAX_TOTAL_BYTES,
        max_item_bytes: int = _ASSET_CACHE_MAX_ITEM_BYTES,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_total_bytes = int(max_total_bytes)
        self.max_item_bytes = int(max_item_bytes)
        self._entries: OrderedDict[str, CachedAsset] = OrderedDict()
        self._total_bytes = 0

    def get(self, url: str, *, now: float | None = None) -> CachedAsset | None:
        timestamp = time.monotonic() if now is None else now
        entry = self._entries.get(url)
        if entry is None:
            return None
        if entry.expires_at <= timestamp:
            self._drop(url)
            return None
        return entry

    def put(
        self,
        url: str,
        *,
        status: int,
        content_type: str,
        body: bytes,
        now: float | None = None,
    ) -> None:
        if len(body) > self.max_item_bytes:
            return
        timestamp = time.monotonic() if now is None else now
        self._drop(url)
        self._entries[url] = CachedAsset(
            status=status,
            content_type=content_type,
            body=body,
            expires_at=timestamp + self.ttl_seconds,
        )
        self._total_bytes += len(body)
        while self._total_bytes > self.max_total_bytes and self._entries:
            oldest = next(iter(self._entries))
            self._drop(oldest)

    def _drop(self, url: str) -> None:
        entry = self._entries.pop(url, None)
        if entry is not None:
            self._total_bytes -= len(entry.body)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def __len__(self) -> int:
        return len(self._entries)


def _captured(
    page_kind: str,
    build_html: Callable[..., str],
) -> Callable[..., Awaitable[str]]:
    """Build the :class:`LongImageRenderer` method that captures one page.

    Every caller passes keyword arguments, so the wrapper forwards them to the
    :class:`TemplateRenderer` method unchanged and adds only the page kind.
    """

    async def render(self, *args: Any, **kwargs: Any) -> str:
        return await self._render(
            page_kind,
            partial(build_html, self.templates, *args, **kwargs),
        )

    render.__name__ = build_html.__name__
    render.__qualname__ = f"LongImageRenderer.{build_html.__name__}"
    render.__doc__ = build_html.__doc__
    return render


class LongImageRenderer:
    """Capture every result as one complete 1280px-wide PNG."""

    def __init__(
        self,
        plugin_root: Path,
        *,
        render_timeout_ms: int = 30_000,
        output_dir: Path | None = None,
        allowed_image_origins: tuple[str, ...] = (),
        max_concurrent_renders: int = DEFAULT_MAX_CONCURRENT_RENDERS,
        max_queued_renders: int = DEFAULT_MAX_QUEUED_RENDERS,
        output_ttl_seconds: float = DEFAULT_OUTPUT_TTL_SECONDS,
        max_output_files: int = DEFAULT_MAX_OUTPUT_FILES,
    ) -> None:
        if isinstance(render_timeout_ms, bool) or not isinstance(
            render_timeout_ms,
            int,
        ):
            raise TemplateConfigurationError("render timeout must be an integer")
        if not 1_000 <= render_timeout_ms <= 120_000:
            raise TemplateConfigurationError(
                "render timeout must be between 1000 and 120000 milliseconds"
            )

        self.templates = TemplateRenderer.from_plugin_root(plugin_root)
        self.render_timeout_ms = render_timeout_ms
        self.output_dir = output_dir or (
            Path(tempfile.gettempdir()) / "astrbot_plugin_zmdlog"
        )
        self.output_ttl_seconds = _positive_number(
            output_ttl_seconds,
            "output_ttl_seconds",
        )
        self.max_output_files = _positive_integer(
            max_output_files,
            "max_output_files",
        )
        self.allowed_image_origins = frozenset(
            _normalise_http_origin(value) for value in allowed_image_origins
        )
        self._playwright: Any = None
        self._browser: Any = None
        self._launch_lock = asyncio.Lock()
        self._render_semaphore = asyncio.Semaphore(
            _positive_integer(
                max_concurrent_renders,
                "max_concurrent_renders",
            )
        )
        self.max_queued_renders = _positive_integer(
            max_queued_renders,
            "max_queued_renders",
        )
        self._queued_renders = 0
        self._output_lock = asyncio.Lock()
        self._asset_cache = AssetCache()
        self._created_files: set[Path] = set()
        self._active_outputs: set[Path] = set()
        self._cleanup_task: asyncio.Task[None] | None = None

    # One capturing wrapper per page, with the name and signature of the
    # TemplateRenderer method it runs; the page kind picks the capture scale
    # and names the output file.
    render_help = _captured("help", TemplateRenderer.render_help)
    render_all_top3 = _captured("all-top3", TemplateRenderer.render_all_top3)
    render_dungeon_top3 = _captured(
        "dungeon-top3", TemplateRenderer.render_dungeon_top3
    )
    render_ranking = _captured("ranking", TemplateRenderer.render_ranking)
    render_character_stats = _captured(
        "character-stats", TemplateRenderer.render_character_stats
    )
    render_character_boss = _captured(
        "character-boss", TemplateRenderer.render_character_boss
    )
    render_roster = _captured("roster", TemplateRenderer.render_roster)
    render_account = _captured("account", TemplateRenderer.render_account)
    render_battle = _captured("battle", TemplateRenderer.render_battle)
    render_loadout = _captured("loadout", TemplateRenderer.render_loadout)
    render_skills = _captured("skills", TemplateRenderer.render_skills)
    render_trend = _captured("trend", TemplateRenderer.render_trend)
    render_timeline = _captured("timeline", TemplateRenderer.render_timeline)
    render_compare = _captured("compare", TemplateRenderer.render_compare)

    async def warm_up(self) -> None:
        """Launch Chromium and render one page so the first query is fast."""

        output_path = await self._render(
            "warmup",
            partial(self.templates.render_help, command_prefix="/"),
        )
        await self._discard_output(Path(output_path))

    async def _render(
        self,
        page_kind: str,
        build: Callable[..., str],
    ) -> str:
        """Render one page with ``build(embed_fonts=...)`` and capture it.

        Chromium gets the small document with linked fonts. When only the
        capture fails, the error carries a self-contained copy with the
        fonts embedded, because AstrBot's fallback renderer cannot reach the
        route handler that serves them.
        """

        try:
            html = build(embed_fonts=False)
        except Exception as exc:
            raise RenderError(f"{page_kind} template rendering failed") from exc
        try:
            return await self._capture(html, page_kind)
        except RenderError as exc:
            try:
                exc.html = build(embed_fonts=True)
            except Exception:
                pass  # The linked copy is still a complete page, in system fonts.
            raise

    async def _capture(self, html: str, page_kind: str) -> str:
        """Wait for a capture slot, then capture; both waits are bounded.

        Only the queue wait is under the timeout. Wrapping the capture as
        well would trip on a page that is legitimately slow, and the render
        timeout already bounds every Playwright call inside it.
        """

        if self._queued_renders >= self.max_queued_renders:
            # Answering "try again" now beats a reply that arrives after the
            # reader has stopped waiting for it.
            raise RenderError("too many renders are already queued")
        self._queued_renders += 1
        try:
            async with asyncio.timeout(self.render_timeout_ms / 1000):
                await self._render_semaphore.acquire()
        except TimeoutError:
            raise RenderError("timed out waiting for a render slot") from None
        finally:
            self._queued_renders -= 1
        try:
            return await self._capture_once(html, page_kind)
        except RenderError as exc:
            exc.html = html
            raise
        finally:
            self._render_semaphore.release()

    async def _capture_once(self, html: str, page_kind: str) -> str:
        browser = await self._ensure_browser()
        output_path = await self._reserve_output_path(page_kind)
        scale = 2 if page_kind in _HIGH_DPI_PAGE_KINDS else 1
        context = None
        page = None
        try:
            context, page, metrics = await self._load_page(browser, html, scale)
            if scale > 1 and metrics["height"] * scale > _MAX_CAPTURE_HEIGHT_PX:
                # Very long pages fall back to 1x to stay inside Chromium's
                # texture limit instead of failing.
                await page.close()
                await context.close()
                scale = 1
                context, page, metrics = await self._load_page(browser, html, scale)
            await page.screenshot(
                path=str(output_path),
                full_page=True,
                animations="disabled",
                type="png",
                timeout=self.render_timeout_ms,
            )
            width, height = _read_png_dimensions(output_path)
            if width != 1280 * scale or height < metrics["height"] * scale:
                raise RenderError("captured image does not contain the full page")
            await self._complete_output(output_path)
            return str(output_path)
        except RenderError:
            await self._discard_output(output_path)
            raise
        except Exception as exc:
            await self._discard_output(output_path)
            raise RenderError("browser capture failed") from exc
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

    async def _load_page(self, browser, html: str, scale: int):
        """Open the page in a fresh context and validate its frame."""

        # Viewport height must not exceed the page's min-height, otherwise
        # short pages get a blank strip below the footer in full-page shots.
        context = await browser.new_context(
            viewport={"width": 1280, "height": 600},
            device_scale_factor=scale,
            color_scheme="light",
        )
        page = None
        try:
            await context.route("**/*", self._route_asset_request)
            page = await context.new_page()
            page.set_default_timeout(self.render_timeout_ms)
            await page.emulate_media(reduced_motion="reduce")
            await page.set_content(
                html,
                wait_until="domcontentloaded",
                timeout=self.render_timeout_ms,
            )
            await self._settle_page(page)
            metrics = await self._validate_page(page)
        except BaseException:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            try:
                await context.close()
            except Exception:
                pass
            raise
        return context, page, metrics

    async def _reserve_output_path(self, page_kind: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        async with self._output_lock:
            self._prune_output_files_locked()
            output_path = self.output_dir / (
                f"zmd-{page_kind}-{uuid.uuid4().hex}.png"
            )
            self._active_outputs.add(output_path)
            return output_path

    async def _complete_output(self, output_path: Path) -> None:
        async with self._output_lock:
            self._active_outputs.discard(output_path)
            self._created_files.add(output_path)
            self._prune_output_files_locked()
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(
                    self._cleanup_output_loop()
                )

    async def _discard_output(self, output_path: Path) -> None:
        async with self._output_lock:
            self._delete_output_locked(output_path)

    async def _prune_output_files(self) -> None:
        """Delete expired and excess images left by this or older runs."""

        async with self._output_lock:
            self._prune_output_files_locked()

    def _prune_output_files_locked(self) -> None:
        try:
            output_paths = tuple(self.output_dir.glob(_OUTPUT_FILE_GLOB))
        except OSError:
            return

        now = time.time()
        retained: list[tuple[float, Path]] = []
        for output_path in output_paths:
            if output_path in self._active_outputs:
                continue
            try:
                modified_at = output_path.stat().st_mtime
            except OSError:
                continue
            if now - modified_at >= self.output_ttl_seconds:
                self._delete_output_locked(output_path)
                continue
            retained.append((modified_at, output_path))

        excess_count = len(retained) - self.max_output_files
        if excess_count <= 0:
            return
        for _, output_path in sorted(retained)[:excess_count]:
            self._delete_output_locked(output_path)

    async def _cleanup_output_loop(self) -> None:
        interval = min(60.0, self.output_ttl_seconds)
        while True:
            await asyncio.sleep(interval)
            await self._prune_output_files()

    def _delete_output_locked(self, output_path: Path) -> None:
        self._created_files.discard(output_path)
        self._active_outputs.discard(output_path)
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass

    async def _ensure_browser(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            try:
                from playwright.async_api import async_playwright

                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                )
                return self._browser
            except Exception as exc:
                if self._playwright is not None:
                    await self._playwright.stop()
                self._playwright = None
                self._browser = None
                raise RenderError("Playwright Chromium is unavailable") from exc

    async def _route_asset_request(self, route) -> None:
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.scheme not in {"http", "https"}:
            await route.continue_()
            return
        origin = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
        if origin == FONT_ORIGIN:
            body = self.templates.font_files.get(parsed.path.rpartition("/")[2])
            if body is None:
                await route.abort()
                return
            await route.fulfill(status=200, content_type="font/woff2", body=body)
            return
        if request.resource_type != "image" or origin not in self.allowed_image_origins:
            await route.abort()
            return
        cached = self._asset_cache.get(request.url)
        if cached is not None:
            await route.fulfill(
                status=cached.status,
                content_type=cached.content_type,
                body=cached.body,
            )
            return
        # Fetch here instead of letting Chromium follow the request: this
        # handler only ever sees the first hop, so a redirect served by an
        # allowed origin would otherwise pull the image from anywhere.
        try:
            response = await route.fetch(max_redirects=0)
        except Exception:
            await route.abort()
            return
        if 300 <= response.status < 400:
            await route.abort()
            return
        try:
            body = await response.body()
        except Exception:
            await route.abort()
            return
        content_type = response.headers.get(
            "content-type", "application/octet-stream"
        )
        if response.status == 200:
            self._asset_cache.put(
                request.url,
                status=response.status,
                content_type=content_type,
                body=body,
            )
        await route.fulfill(
            status=response.status,
            content_type=content_type,
            body=body,
        )

    async def _settle_page(self, page) -> None:
        await page.evaluate(
            """
            async (timeoutMs) => {
              if (document.fonts && document.fonts.ready) {
                await Promise.race([
                  document.fonts.ready,
                  new Promise(resolve => setTimeout(resolve, timeoutMs)),
                ]);
              }
              const pending = Array.from(document.images)
                .filter(image => !image.complete)
                .map(image => new Promise(resolve => {
                  image.addEventListener("load", resolve, { once: true });
                  image.addEventListener("error", resolve, { once: true });
                }));
              await Promise.race([
                Promise.all(pending),
                new Promise(resolve => setTimeout(resolve, timeoutMs)),
              ]);
              for (const image of document.images) {
                if (!image.complete || image.naturalWidth === 0) image.remove();
              }
            }
            """,
            min(5_000, self.render_timeout_ms // 3),
        )

    async def _validate_page(self, page) -> dict[str, int]:
        metrics = await page.evaluate(
            """
            () => {
              const root = document.querySelector("#zmd-page");
              const panel = root && root.querySelector(".main-panel");
              if (!root || !panel) return null;
              const rootRect = root.getBoundingClientRect();
              const panelRect = panel.getBoundingClientRect();
              return {
                width: Math.ceil(root.scrollWidth),
                height: Math.ceil(root.scrollHeight),
                panelBottom: Math.round(panelRect.bottom - rootRect.top),
                hasBackground:
                  getComputedStyle(root).backgroundImage !== "none" &&
                  getComputedStyle(root)
                    .getPropertyValue("--zmd-scene-background")
                    .includes("data:image/"),
              };
            }
            """
        )
        if metrics is None:
            raise RenderError("page frame is missing")
        if metrics["width"] != 1280 or metrics["height"] <= 0:
            raise RenderError("page frame has invalid dimensions")
        if not metrics["hasBackground"]:
            raise RenderError("local scene background is missing")
        if metrics["panelBottom"] > metrics["height"]:
            raise RenderError("main panel does not contain the complete result")
        return metrics

    async def close(self) -> None:
        cleanup_task = self._cleanup_task
        self._cleanup_task = None
        if cleanup_task is not None:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            try:
                if self._playwright is not None:
                    await self._playwright.stop()
            finally:
                self._browser = None
                self._playwright = None
                async with self._output_lock:
                    output_paths = self._created_files | self._active_outputs
                    for output_path in tuple(output_paths):
                        try:
                            output_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    self._created_files.clear()
                    self._active_outputs.clear()


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


def _load_background_data_url(background_path: Path) -> str:
    try:
        payload = background_path.read_bytes()
    except OSError as exc:
        raise TemplateConfigurationError(
            "local scene background is unavailable"
        ) from exc
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
    elif payload.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif payload.lstrip().startswith((b"<svg", b"<?xml")):
        media_type = "image/svg+xml"
    else:
        raise TemplateConfigurationError(
            "local scene background has an unsupported format"
        )
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


# Subset web fonts built by tools/build_fonts.py; missing files simply fall
# back to the system font stack declared in base.css.
_FONT_FACES = (
    ("MiSans-Heavy.woff2", "MiSans", 900),
    ("MiSans-Bold.woff2", "MiSans", 700),
    ("MiSans-Regular.woff2", "MiSans", 400),
    ("Barlow-Bold.woff2", "Barlow", 700),
    ("Barlow-SemiBold.woff2", "Barlow", 600),
    ("Barlow-Medium.woff2", "Barlow", 500),
    ("BarlowSemiCondensed-ExtraBold.woff2", "Barlow Semi Condensed", 800),
)


def _load_fonts(
    fonts_path: Path | None,
) -> tuple[dict[str, bytes], Markup, Markup]:
    """Read the bundled subset fonts once.

    Returns the raw files (served to Chromium by the route handler), the
    ``@font-face`` CSS with the files embedded as data URLs, and the same
    CSS linking them on :data:`FONT_ORIGIN`.
    """

    files: dict[str, bytes] = {}
    embedded: list[str] = []
    linked: list[str] = []
    if fonts_path is None:
        return files, Markup(""), Markup("")
    for file_name, family, weight in _FONT_FACES:
        try:
            payload = (fonts_path / file_name).read_bytes()
        except OSError:
            continue
        files[file_name] = payload
        face = (
            f'@font-face{{font-family:"{family}";font-weight:{weight};'
            "font-style:normal;font-display:block;src:url("
        )
        encoded = base64.b64encode(payload).decode("ascii")
        embedded.append(
            f'{face}data:font/woff2;base64,{encoded})format("woff2")}}'
        )
        linked.append(f'{face}{FONT_ORIGIN}/{file_name})format("woff2")}}')
    return files, Markup("\n".join(embedded)), Markup("\n".join(linked))


def _read_png_dimensions(path: Path) -> tuple[int, int]:
    try:
        with path.open("rb") as stream:
            header = stream.read(24)
    except OSError as exc:
        raise RenderError("captured image cannot be read") from exc
    if len(header) != 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RenderError("captured image is not a PNG")
    return struct.unpack(">II", header[16:24])


def _normalise_http_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TemplateConfigurationError(
            "allowed image origin must be an absolute HTTP(S) URL"
        )
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"


def _positive_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TemplateConfigurationError(f"{name} must be a positive integer")
    return value


def _positive_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise TemplateConfigurationError(f"{name} must be a positive number")
    return float(value)
