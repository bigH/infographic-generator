"""Headless-chromium implementation of :class:`core.ports.Renderer`.

The composition is already self-contained, so this stage stays dumb: seal the
network off, drop the HTML into a page, screenshot it. Every reported number in
the ``RenderResult`` is read back out of the bytes actually written, never
recomputed from the composition.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page, Route, ViewportSize, async_playwright

from infographic_generator.core.models import Composition, RenderResult

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IHDR_DIMENSIONS = slice(16, 24)

_AUTO_HEIGHT_VIEWPORT_PX = 800
"""Placeholder viewport height when the document sets its own; ``full_page``
capture overrides it, but chromium still needs a number to start with."""


@dataclass(frozen=True, slots=True)
class PlaywrightRenderer:
    """Rasterises a Composition to a PNG with headless chromium."""

    timeout_ms: int = 30_000

    async def render(self, composition: Composition, output_path: Path) -> RenderResult:
        """Screenshot ``composition`` into ``output_path``, reporting what landed there.

        Playwright's timeout and browser errors propagate; so does ``OSError``
        from an unwritable path.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = await self._capture(composition)
        output_path.write_bytes(payload)
        width_px, height_px = _png_dimensions(payload)
        return RenderResult(
            output_path=output_path,
            width_px=width_px,
            height_px=height_px,
            bytes_written=len(payload),
        )

    async def _capture(self, composition: Composition) -> bytes:
        """Drive a throwaway chromium, closing it on every exit path."""
        async with async_playwright() as playwright:
            async with await playwright.chromium.launch(headless=True) as browser:
                async with await browser.new_context(
                    viewport=_viewport(composition),
                    device_scale_factor=composition.device_scale_factor,
                ) as context:
                    return await self._screenshot(await context.new_page(), composition)

    async def _screenshot(self, page: Page, composition: Composition) -> bytes:
        """Load the HTML with the network cut off, then rasterise to PNG bytes."""
        await page.route("**/*", _abort)
        await page.set_content(
            composition.html, wait_until="load", timeout=self.timeout_ms
        )
        return await page.screenshot(
            type="png",
            full_page=composition.height_px is None,
            timeout=self.timeout_ms,
        )


async def _abort(route: Route) -> None:
    """Refuse every request, so a composition reaching for a CDN fails loudly."""
    await route.abort()


def _viewport(composition: Composition) -> ViewportSize:
    """Viewport in CSS pixels, sized before any content loads."""
    height_px = composition.height_px
    return ViewportSize(
        width=composition.width_px,
        height=_AUTO_HEIGHT_VIEWPORT_PX if height_px is None else height_px,
    )


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk. Raises ValueError if not a PNG."""
    if not payload.startswith(_PNG_SIGNATURE) or len(payload) < _IHDR_DIMENSIONS.stop:
        raise ValueError("payload is not a PNG")
    width_px, height_px = struct.unpack(">II", payload[_IHDR_DIMENSIONS])
    return int(width_px), int(height_px)
