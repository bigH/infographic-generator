"""Headless-chromium implementation of :class:`core.ports.Renderer`.

The composition is already self-contained, so this stage stays dumb: seal the
network off at the browser context, drop the HTML into a page, screenshot it.

The seal has two halves, because one is not enough. Route interception sees every
request a frame makes and aborts it *loudly* -- a composition that reaches for a
URL raises :class:`ExternalRequestError` rather than shipping a broken-image
glyph. It does not see requests the browser starts on its own: a
``<link rel=prefetch>`` was verified to put a real ``GET`` on a real socket with
the route handler never invoked. ``offline=True`` on the context stops those, at
the cost of being silent about them -- there is no hook to be loud with.

Every reported number in the ``RenderResult`` is read back out of the PNG bytes,
never recomputed from the composition.
"""

from __future__ import annotations

import struct
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Route
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import ViewportSize, async_playwright

from infographic_generator.core.models import Composition, RenderResult

if TYPE_CHECKING:
    from infographic_generator.core.ports import Renderer

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IHDR_DIMENSIONS = slice(16, 24)

_MIN_VIEWPORT_HEIGHT_PX = 1
"""Viewport height when the document sets its own: a floor, not a default.

Chromium refuses a height below 1, and a ``full_page`` capture *grows* from the
viewport rather than replacing it -- so any larger number pads a short document
with blank pixels straight into the deliverable. Keep this minimal.

The cost is that viewport-relative CSS has nothing meaningful to resolve against:
under ``height_px=None`` a ``100vh`` block is 1 px tall, ``5vmin`` text is
invisible, and ``position:fixed;bottom:0`` pins to the top. A composition that
needs any of those must set ``height_px``. There is no floor that fixes both --
whatever pads a short document is exactly what makes ``vh`` mean something.

TODO: measuring ``document.body.scrollHeight`` at a plausible viewport and
screenshotting with ``clip`` gets both, at the cost of replacing chromium's own
content-height computation with ours -- which can *clip* the deliverable rather
than merely pad it. Latent while no composition uses ``vh``; revisit when a real
layout agent writes the CSS.
"""

_MAX_REPORTED_URLS = 5
"""Offending URLs named in an :class:`ExternalRequestError`, so a page with 200
remote images does not produce a 20 kB message."""


class ExternalRequestError(RuntimeError):
    """A composition asked for a URL the renderer refuses to fetch."""


@dataclass(frozen=True, slots=True)
class PlaywrightRenderer:
    """Rasterises a Composition to a PNG with headless chromium."""

    timeout_ms: int = 30_000
    java_script_enabled: bool = False
    """Off by default for determinism, not for the seal -- ``offline=True`` on the
    context is what seals ``fetch``, ``sendBeacon`` and WebSockets, all of which
    were verified to reach a real socket with JS on and none of which reach it with
    JS off. Script-free rendering is reproducible rendering, the composer contract
    emits no ``<script>``, and turning JS off was verified byte-identical on the
    real composition.

    It is a field rather than a hardcode so a JS-dependent composition -- a chart
    library, say -- is a deliberate, visible opt-in. The residual risk while it is
    off is that a ``<script>``-driven composition renders *silently* empty. Raising
    on ``document.scripts`` was considered and rejected: an inert
    ``<script type="application/ld+json">`` block is legitimate markup and would
    fail a perfectly good composition. This stage stays dumb.
    """

    async def render(self, composition: Composition, output_path: Path) -> RenderResult:
        """Screenshot ``composition`` into ``output_path``, reporting what landed there.

        Raises ``OSError`` for an unwritable path, ``ExternalRequestError`` when
        the composition reached for the network, a Playwright ``TimeoutError``
        when the browser ran out of time, and ``RuntimeError`` for any other
        browser failure. Nothing is written unless the capture succeeded.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = await self._capture(composition)
        except PlaywrightTimeoutError:
            raise
        except PlaywrightError as error:
            raise RuntimeError(f"chromium failed to render: {error}") from error
        width_px, height_px = _png_dimensions(payload)
        output_path.write_bytes(payload)
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
                    java_script_enabled=self.java_script_enabled,
                    offline=True,
                ) as context:
                    return await self._screenshot(context, composition)

    async def _screenshot(
        self, context: BrowserContext, composition: Composition
    ) -> bytes:
        """Seal the context, then load and rasterise the HTML inside a fresh page.

        The seal goes on the context before any page exists, so no page is ever
        unsealed. Refusals are checked after the load -- a broken page is not
        worth screenshotting -- and again after the capture, because a full-page
        screenshot scrolls and can trigger further loads.
        """
        refused: list[str] = []
        await context.route("**/*", _refusing_route(refused))
        page = await context.new_page()
        await page.set_content(
            composition.html, wait_until="load", timeout=self.timeout_ms
        )
        _raise_on_refused(refused)
        payload = await page.screenshot(
            type="png",
            full_page=composition.height_px is None,
            timeout=self.timeout_ms,
        )
        _raise_on_refused(refused)
        return payload


def _refusing_route(refused: list[str]) -> Callable[[Route], Awaitable[None]]:
    """Handler that aborts every request into ``refused``, first-seen order, no repeats.

    ``refused`` belongs to one render: this renderer is frozen and concurrent
    renders must not share a buffer.
    """

    async def abort(route: Route) -> None:
        url = route.request.url
        if url not in refused:
            refused.append(url)
        await route.abort()

    return abort


def _raise_on_refused(refused: Sequence[str]) -> None:
    """Fail loudly for a composition that was not self-contained after all."""
    if not refused:
        return
    named = ", ".join(refused[:_MAX_REPORTED_URLS])
    unnamed = len(refused) - _MAX_REPORTED_URLS
    if unnamed > 0:
        named = f"{named} (+{unnamed} more)"
    raise ExternalRequestError(
        f"composition is not self-contained: aborted {len(refused)} external "
        f"request(s) and wrote no PNG: {named}"
    )


def _viewport(composition: Composition) -> ViewportSize:
    """Viewport in CSS pixels, sized before any content loads."""
    height_px = composition.height_px
    return ViewportSize(
        width=composition.width_px,
        height=_MIN_VIEWPORT_HEIGHT_PX if height_px is None else height_px,
    )


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk. Raises ValueError if not a PNG."""
    if not payload.startswith(_PNG_SIGNATURE) or len(payload) < _IHDR_DIMENSIONS.stop:
        raise ValueError("payload is not a PNG")
    width_px, height_px = struct.unpack(">II", payload[_IHDR_DIMENSIONS])
    return int(width_px), int(height_px)


if TYPE_CHECKING:
    _conforms: Renderer = PlaywrightRenderer()
