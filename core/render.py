"""Strict HTML templates and single-image Playwright capture for ZmdLogBot."""

import asyncio
import base64
import re
import struct
import tempfile
import time
import uuid
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
from .matcher import MatchChoice
from .models import (
    BattleDetailSummary,
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
    build_dungeon_top3_page,
    build_ranking_page,
    build_roster_page,
)
from .routing import DEFAULT_RANKING_TOP

_VERSION_LINE = re.compile(
    r"^\s*version\s*:\s*(?P<value>[^#]+?)\s*(?:#.*)?$"
)
_OUTPUT_FILE_GLOB = "zmd-*.png"
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
        "warmup",
    }
)
_MAX_CAPTURE_HEIGHT_PX = 15_000
DEFAULT_MAX_CONCURRENT_RENDERS = 2
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
        self.font_face_css = _load_font_face_css(fonts_path)
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

    def render_help(self, *, command_prefix: str) -> str:
        page = build_help_page(command_prefix)
        return self._render("help/help.html", page, "help")

    def render_all_top3(
        self,
        cards: tuple[HotBossCard, ...],
        *,
        query: str,
        web_base_url: str | None = None,
    ) -> str:
        page = build_all_top3_page(
            cards,
            query=query,
            web_base_url=web_base_url,
        )
        return self._render("all-top3/all-top3.html", page, "all-top3")

    def render_dungeon_top3(
        self,
        choice: MatchChoice,
        cards: tuple[HotBossCard, ...],
        *,
        query: str,
        web_base_url: str | None = None,
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
        )

    def render_ranking(
        self,
        ranking: BossRanking,
        *,
        query: str,
        ranking_limit: int = DEFAULT_RANKING_TOP,
        web_base_url: str | None = None,
        character_filter: str | None = None,
        character_filter_scope: CharacterFilterScope = CharacterFilterScope.MAIN,
    ) -> str:
        page = build_ranking_page(
            ranking,
            query=query,
            display_limit=ranking_limit,
            web_base_url=web_base_url,
            character_filter=character_filter,
            character_filter_scope=character_filter_scope,
        )
        return self._render("ranking/ranking.html", page, "ranking")

    def render_character_stats(
        self,
        stats: CharacterStatistics,
        *,
        query: str,
        web_base_url: str | None = None,
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
        )

    def render_character_boss(
        self,
        stats: CharacterBossStatistics,
        *,
        query: str,
        web_base_url: str | None = None,
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
        )

    def render_roster(
        self,
        ranking: BossRanking,
        *,
        query: str,
        ranking_limit: int = DEFAULT_RANKING_TOP,
        web_base_url: str | None = None,
    ) -> str:
        page = build_roster_page(
            ranking,
            query=query,
            display_limit=ranking_limit,
            web_base_url=web_base_url,
        )
        return self._render("roster/roster.html", page, "roster")

    def render_account(
        self,
        account: PublicUserRankings,
        *,
        query: str,
        web_base_url: str,
    ) -> str:
        page = build_account_page(
            account,
            query=query,
            web_base_url=web_base_url,
        )
        return self._render("account/account.html", page, "account")

    def render_battle(
        self,
        battle: BattleDetailSummary,
        *,
        query: str,
        web_base_url: str,
    ) -> str:
        page = build_battle_page(
            battle,
            query=query,
            web_base_url=web_base_url,
        )
        return self._render("battle/battle.html", page, "battle")

    def _render(self, template_name: str, page, page_kind: str) -> str:
        template = self.environment.get_template(template_name)
        return template.render(
            page=page,
            page_kind=page_kind,
            plugin={"name": "ZmdLogBot", "version": self.version},
            background_data_url=self.background_data_url,
            font_face_css=self.font_face_css,
        )


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
        self._output_lock = asyncio.Lock()
        self._created_files: set[Path] = set()
        self._active_outputs: set[Path] = set()
        self._cleanup_task: asyncio.Task[None] | None = None

    async def render_help(self, *, command_prefix: str) -> str:
        try:
            html = self.templates.render_help(command_prefix=command_prefix)
        except Exception as exc:
            raise RenderError("help template rendering failed") from exc
        return await self._capture(html, "help")

    async def render_all_top3(
        self,
        cards: tuple[HotBossCard, ...],
        *,
        query: str,
        web_base_url: str | None = None,
    ) -> str:
        try:
            html = self.templates.render_all_top3(
                cards,
                query=query,
                web_base_url=web_base_url,
            )
        except Exception as exc:
            raise RenderError("all-board template rendering failed") from exc
        return await self._capture(html, "all-top3")

    async def render_dungeon_top3(
        self,
        choice: MatchChoice,
        cards: tuple[HotBossCard, ...],
        *,
        query: str,
        web_base_url: str | None = None,
    ) -> str:
        try:
            html = self.templates.render_dungeon_top3(
                choice,
                cards,
                query=query,
                web_base_url=web_base_url,
            )
        except Exception as exc:
            raise RenderError("dungeon template rendering failed") from exc
        return await self._capture(html, "dungeon-top3")

    async def render_ranking(
        self,
        ranking: BossRanking,
        *,
        query: str,
        ranking_limit: int = DEFAULT_RANKING_TOP,
        web_base_url: str | None = None,
        character_filter: str | None = None,
        character_filter_scope: CharacterFilterScope = CharacterFilterScope.MAIN,
    ) -> str:
        try:
            html = self.templates.render_ranking(
                ranking,
                query=query,
                ranking_limit=ranking_limit,
                web_base_url=web_base_url,
                character_filter=character_filter,
                character_filter_scope=character_filter_scope,
            )
        except Exception as exc:
            raise RenderError("ranking template rendering failed") from exc
        return await self._capture(html, "ranking")

    async def render_character_stats(
        self,
        stats: CharacterStatistics,
        *,
        query: str,
        web_base_url: str | None = None,
    ) -> str:
        try:
            html = self.templates.render_character_stats(
                stats,
                query=query,
                web_base_url=web_base_url,
            )
        except Exception as exc:
            raise RenderError("character stats template rendering failed") from exc
        return await self._capture(html, "character-stats")

    async def render_character_boss(
        self,
        stats: CharacterBossStatistics,
        *,
        query: str,
        web_base_url: str | None = None,
    ) -> str:
        try:
            html = self.templates.render_character_boss(
                stats,
                query=query,
                web_base_url=web_base_url,
            )
        except Exception as exc:
            raise RenderError(
                "character boss template rendering failed"
            ) from exc
        return await self._capture(html, "character-boss")

    async def render_roster(
        self,
        ranking: BossRanking,
        *,
        query: str,
        ranking_limit: int = DEFAULT_RANKING_TOP,
        web_base_url: str | None = None,
    ) -> str:
        try:
            html = self.templates.render_roster(
                ranking,
                query=query,
                ranking_limit=ranking_limit,
                web_base_url=web_base_url,
            )
        except Exception as exc:
            raise RenderError("roster template rendering failed") from exc
        return await self._capture(html, "roster")

    async def render_account(
        self,
        account: PublicUserRankings,
        *,
        query: str,
        web_base_url: str,
    ) -> str:
        try:
            html = self.templates.render_account(
                account,
                query=query,
                web_base_url=web_base_url,
            )
        except Exception as exc:
            raise RenderError("account template rendering failed") from exc
        return await self._capture(html, "account")

    async def render_battle(
        self,
        battle: BattleDetailSummary,
        *,
        query: str,
        web_base_url: str,
    ) -> str:
        try:
            html = self.templates.render_battle(
                battle,
                query=query,
                web_base_url=web_base_url,
            )
        except Exception as exc:
            raise RenderError("battle template rendering failed") from exc
        return await self._capture(html, "battle")

    async def warm_up(self) -> None:
        """Launch Chromium and render one page so the first query is fast."""

        html = self.templates.render_help(command_prefix="/")
        output_path = await self._capture(html, "warmup")
        await self._discard_output(Path(output_path))

    async def _capture(self, html: str, page_kind: str) -> str:
        async with self._render_semaphore:
            try:
                return await self._capture_once(html, page_kind)
            except RenderError as exc:
                exc.html = html
                raise

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
        if request.resource_type != "image" or origin not in self.allowed_image_origins:
            await route.abort()
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
        await route.fulfill(response=response)

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


def _load_font_face_css(fonts_path: Path | None) -> Markup:
    if fonts_path is None:
        return Markup("")
    rules: list[str] = []
    for file_name, family, weight in _FONT_FACES:
        try:
            payload = (fonts_path / file_name).read_bytes()
        except OSError:
            continue
        encoded = base64.b64encode(payload).decode("ascii")
        rules.append(
            "@font-face{"
            f'font-family:"{family}";font-weight:{weight};font-style:normal;'
            f"font-display:block;src:url(data:font/woff2;base64,{encoded})"
            'format("woff2")}'
        )
    return Markup("\n".join(rules))


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
