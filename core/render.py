"""Strict HTML templates and single-image Playwright capture for ZmdBot."""

import asyncio
import base64
import re
import struct
import tempfile
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


class RenderError(RuntimeError):
    """Raised when a complete ZmdBot image cannot be produced."""


class TemplateRenderer:
    """Render self-contained HTML for one of the four public page types."""

    def __init__(
        self,
        resources_path: Path,
        metadata_path: Path,
        background_path: Path,
    ) -> None:
        self.resources_path = resources_path.resolve()
        self.version = read_plugin_version(metadata_path)
        self.background_data_url = _load_background_data_url(background_path)
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
            resources_path / "common" / "endfield-crane-grid.jpg",
        )

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
    ) -> str:
        page = build_ranking_page(
            ranking,
            query=query,
        )
        return self._render("ranking/ranking.html", page, "ranking")

    def _render(self, template_name: str, page, page_kind: str) -> str:
        template = self.environment.get_template(template_name)
        return template.render(
            page=page,
            page_kind=page_kind,
            plugin={"name": "ZmdBot", "version": self.version},
            background_data_url=self.background_data_url,
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
            Path(tempfile.gettempdir()) / "astrbot_plugin_zmdbot"
        )
        self.allowed_image_origins = frozenset(
            _normalise_http_origin(value) for value in allowed_image_origins
        )
        self._playwright: Any = None
        self._browser: Any = None
        self._launch_lock = asyncio.Lock()
        self._created_files: set[Path] = set()

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
    ) -> str:
        try:
            html = self.templates.render_all_top3(cards, query=query)
        except Exception as exc:
            raise RenderError("all-board template rendering failed") from exc
        return await self._capture(html, "all-top3")

    async def render_dungeon_top3(
        self,
        choice: MatchChoice,
        cards: tuple[HotBossCard, ...],
        *,
        query: str,
    ) -> str:
        try:
            html = self.templates.render_dungeon_top3(
                choice,
                cards,
                query=query,
            )
        except Exception as exc:
            raise RenderError("dungeon template rendering failed") from exc
        return await self._capture(html, "dungeon-top3")

    async def render_ranking(
        self,
        ranking: BossRanking,
        *,
        query: str,
    ) -> str:
        try:
            html = self.templates.render_ranking(
                ranking,
                query=query,
            )
        except Exception as exc:
            raise RenderError("ranking template rendering failed") from exc
        return await self._capture(html, "ranking")

    async def _capture(self, html: str, page_kind: str) -> str:
        browser = await self._ensure_browser()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.output_dir / (
            f"zmd-{page_kind}-{uuid.uuid4().hex}.png"
        )
        context = None
        page = None
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                device_scale_factor=1,
                color_scheme="light",
            )
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
            await page.screenshot(
                path=str(output_path),
                full_page=True,
                animations="disabled",
                type="png",
                timeout=self.render_timeout_ms,
            )
            width, height = _read_png_dimensions(output_path)
            if width != 1280 or height < metrics["height"]:
                raise RenderError("captured image does not contain the full page")
            self._created_files.add(output_path)
            return str(output_path)
        except RenderError:
            output_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            output_path.unlink(missing_ok=True)
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
        if request.resource_type == "image" and origin in self.allowed_image_origins:
            await route.continue_()
            return
        await route.abort()

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
                panelBottom: Math.ceil(panelRect.bottom - rootRect.top),
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
                for output_path in tuple(self._created_files):
                    output_path.unlink(missing_ok=True)
                self._created_files.clear()


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
    else:
        raise TemplateConfigurationError(
            "local scene background has an unsupported format"
        )
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


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
