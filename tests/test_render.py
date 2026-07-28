"""Renderer contract: what actually lands on disk.

Every dimension asserted here is decoded from the PNG bytes on the filesystem,
never taken from the ``RenderResult`` alone -- the file is the deliverable and
the reported numbers are just a claim about it. Each test launches chromium, so
renders are kept few and small.
"""

from __future__ import annotations

import asyncio
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import cast

import pytest

from infographic_generator.core.models import Composition
from infographic_generator.render import ExternalRequestError, PlaywrightRenderer

TIMEOUT_S = 25.0
"""Hard ceiling on a single render, so a network hang fails instead of stalling."""

CONTENT_HEIGHT_PX = 1500
REMOTE_IMAGE_URL = "https://cdn.example.invalid/x.png"
UNREACHABLE_IMAGE = f"<img src='{REMOTE_IMAGE_URL}'>"

DATA_URI_PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1Pe"
    "AAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC"
)
"""A real 1x1 truecolour PNG -- the shape every honest composition uses for imagery.

Genuinely decodable, not a placeholder: a malformed data URI would make the
"self-contained pages still render" guard pass for the wrong reason.
"""


def document(body: str = "", head: str = "") -> str:
    """A self-contained page whose background is white unless something overrides it."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>body{{margin:0;background:#ffffff}}</style>{head}</head>"
        f"<body>{body}</body></html>"
    )


def block(height_px: int) -> str:
    """A document exactly ``height_px`` tall: no body margin, one fixed-height block."""
    return document(f"<div style='height:{height_px}px'></div>")


def stylesheet_link(url: str) -> str:
    return f"<link rel='stylesheet' href='{url}'>"


TINY_HTML = document("<h1>hi</h1>")
TALL_HTML = block(CONTENT_HEIGHT_PX)


def png_size(path: Path) -> tuple[int, int]:
    """Width and height straight out of the IHDR chunk, with no image library."""
    payload = path.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    return cast(tuple[int, int], struct.unpack(">II", payload[16:24]))


async def render(
    composition: Composition,
    output_path: Path,
    renderer: PlaywrightRenderer | None = None,
) -> None:
    """Render under a timeout; a hang is a failure, not a stalled suite."""
    await asyncio.wait_for(
        (renderer or PlaywrightRenderer()).render(composition, output_path),
        timeout=TIMEOUT_S,
    )


@dataclass(frozen=True, slots=True)
class RecordingServer:
    """A loopback origin that records every path chromium asks it for."""

    url: str
    requested_paths: list[str]


@pytest.fixture
def recording_server() -> Iterator[RecordingServer]:
    """Serves a red-background stylesheet that no render is allowed to fetch."""
    requested_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested_paths.append(self.path)
            body = b"body{background:#ff0000}"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/css")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _, port = cast(tuple[str, int], server.server_address)
        yield RecordingServer(f"http://127.0.0.1:{port}", requested_paths)
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize("scale", [1.0, 2.0])
async def test_the_png_is_css_pixels_times_the_device_scale_factor(
    tmp_path: Path, scale: float
) -> None:
    width_px, height_px = 400, 300
    output_path = tmp_path / "out.png"

    result = await asyncio.wait_for(
        PlaywrightRenderer().render(
            Composition(
                html=TINY_HTML,
                width_px=width_px,
                height_px=height_px,
                device_scale_factor=scale,
            ),
            output_path,
        ),
        timeout=TIMEOUT_S,
    )

    assert png_size(output_path) == (int(width_px * scale), int(height_px * scale))
    assert (result.width_px, result.height_px) == png_size(output_path)


async def test_bytes_written_matches_the_file_size_on_disk(tmp_path: Path) -> None:
    output_path = tmp_path / "out.png"

    result = await asyncio.wait_for(
        PlaywrightRenderer().render(
            Composition(html=TINY_HTML, width_px=320, height_px=200), output_path
        ),
        timeout=TIMEOUT_S,
    )

    assert result.bytes_written == output_path.stat().st_size
    assert result.bytes_written > 0


async def test_a_null_height_is_driven_by_content_not_by_the_viewport(
    tmp_path: Path,
) -> None:
    scale = 2.0
    full_page = tmp_path / "full.png"
    clipped = tmp_path / "clipped.png"

    await render(
        Composition(
            html=TALL_HTML, width_px=400, height_px=None, device_scale_factor=scale
        ),
        full_page,
    )
    await render(
        Composition(
            html=TALL_HTML, width_px=400, height_px=300, device_scale_factor=scale
        ),
        clipped,
    )

    assert png_size(full_page) == (800, int(CONTENT_HEIGHT_PX * scale))
    assert png_size(clipped) == (800, 600)


@pytest.mark.parametrize("content_height_px", [100, 400, 900])
async def test_a_short_full_page_capture_is_not_padded_to_a_floor(
    tmp_path: Path, content_height_px: int
) -> None:
    """A full-page PNG is exactly as tall as the document, however short that is.

    Equality, not a bound: a viewport-height floor (chromium's default is 800 CSS
    px) would silently letterbox 100 and 400 px documents into 800 px PNGs.
    """
    output_path = tmp_path / "out.png"

    await render(
        Composition(
            html=block(content_height_px),
            width_px=400,
            height_px=None,
            device_scale_factor=1.0,
        ),
        output_path,
    )

    assert png_size(output_path) == (400, content_height_px)


async def test_a_remote_stylesheet_fails_loudly_and_never_reaches_the_socket(
    tmp_path: Path, recording_server: RecordingServer
) -> None:
    """Loud and sealed are independent claims: the raise and the socket both matter.

    A composition reaching for a CDN is a composer bug. Rendering it quietly hides
    that bug behind a blank page, so the abort has to surface as a failure.
    """
    output_path = tmp_path / "out.png"
    css_url = f"{recording_server.url}/red.css"
    html = document(body=UNREACHABLE_IMAGE, head=stylesheet_link(css_url))

    with pytest.raises(ExternalRequestError) as raised:
        await render(Composition(html=html, width_px=320, height_px=200), output_path)

    message = str(raised.value)
    assert css_url in message
    assert REMOTE_IMAGE_URL in message
    assert recording_server.requested_paths == []
    assert not output_path.exists()


async def test_a_remote_image_fails_loudly_and_writes_nothing(tmp_path: Path) -> None:
    """An unresolvable host, so the abort is the only thing that can end the load.

    ``RuntimeError`` is asserted separately from the subclass: callers that only
    know the port contract catch ``RuntimeError``, and that must keep working.
    """
    output_path = tmp_path / "out.png"

    matches_url = re.escape(REMOTE_IMAGE_URL)

    with pytest.raises(ExternalRequestError, match=matches_url) as raised:
        await render(
            Composition(html=document(UNREACHABLE_IMAGE), width_px=320, height_px=200),
            output_path,
        )

    assert isinstance(raised.value, RuntimeError)
    assert not output_path.exists()


async def test_a_self_contained_data_uri_image_still_renders(tmp_path: Path) -> None:
    """The guard on the guard: the loud check must not reject every real composition."""
    output_path = tmp_path / "out.png"
    html = document(f"<img src='{DATA_URI_PNG}' style='width:10px;height:10px'>")

    await render(Composition(html=html, width_px=320, height_px=200), output_path)

    assert png_size(output_path) == (640, 400)


async def test_missing_parent_directories_are_created(tmp_path: Path) -> None:
    output_path = tmp_path / "deep" / "nested" / "out.png"

    await render(Composition(html=TINY_HTML, width_px=320, height_px=200), output_path)

    assert output_path.exists()


async def test_an_unwritable_output_path_raises_oserror(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied.png"
    occupied.mkdir()

    with pytest.raises(OSError):
        await render(Composition(html=TINY_HTML, width_px=320, height_px=200), occupied)


async def test_a_failure_inside_the_browser_leaves_the_next_render_working(
    tmp_path: Path,
) -> None:
    """A torn-down browser is invisible from outside, so prove it by using one after.

    The failure has to happen while chromium is up -- an unwritable path fails
    after it has already closed and would prove nothing -- hence the 1 ms timeout.
    """
    composition = Composition(html=TINY_HTML, width_px=320, height_px=200)
    doomed = tmp_path / "doomed.png"

    with pytest.raises(Exception, match="[Tt]imeout"):
        await asyncio.wait_for(
            PlaywrightRenderer(timeout_ms=1).render(composition, doomed), TIMEOUT_S
        )
    assert not doomed.exists()

    survivor = tmp_path / "survivor.png"
    await render(composition, survivor)
    assert png_size(survivor) == (640, 400)


async def test_an_aborted_request_leaves_the_next_render_working(
    tmp_path: Path,
) -> None:
    """The raise path must tear chromium down too, or every failure leaks a browser.

    Proven from outside the renderer, the only place the leak is observable: if the
    context, browser or playwright driver survived, the next render would hang or
    fail rather than produce a PNG.
    """
    doomed = tmp_path / "doomed.png"

    with pytest.raises(ExternalRequestError):
        await render(
            Composition(html=document(UNREACHABLE_IMAGE), width_px=320, height_px=200),
            doomed,
        )
    assert not doomed.exists()

    survivor = tmp_path / "survivor.png"
    await render(Composition(html=TINY_HTML, width_px=320, height_px=200), survivor)
    assert png_size(survivor) == (640, 400)


SCRIPTED_REPAINT = document("<script>document.body.style.background='#ff0000'</script>")


async def test_script_execution_is_off_unless_asked_for(tmp_path: Path) -> None:
    """Asserted in pixels, not in a flag: a sealed context paints the plain page."""
    sealed = tmp_path / "sealed.png"
    enabled = tmp_path / "enabled.png"
    unscripted = tmp_path / "unscripted.png"
    composition = Composition(html=SCRIPTED_REPAINT, width_px=320, height_px=200)

    await render(composition, sealed)
    await render(composition, enabled, PlaywrightRenderer(java_script_enabled=True))
    await render(Composition(html=document(), width_px=320, height_px=200), unscripted)

    assert sealed.read_bytes() == unscripted.read_bytes()
    assert enabled.read_bytes() != unscripted.read_bytes()


async def test_the_seal_covers_traffic_a_script_starts(
    tmp_path: Path, recording_server: RecordingServer
) -> None:
    """Routing at the context, not the page, is what catches a fetch nobody declared.

    ``fetch`` does not participate in the ``load`` event, so on its own it races the
    renderer's check -- measured at 15/20 over 20 runs. The synchronous XHR is a
    parser barrier and nothing else: it blocks parsing until its own abort lands,
    which guarantees the earlier fetch has been routed by the time ``load`` fires.
    Measured 40/40 with the barrier. Both requests are script-initiated, and neither
    may reach the socket.
    """
    output_path = tmp_path / "out.png"
    probe_url = f"{recording_server.url}/probe.css"
    html = document(
        f"<script>fetch('{probe_url}').catch(() => {{}});"
        "const barrier = new XMLHttpRequest();"
        f"barrier.open('GET', '{recording_server.url}/barrier.css', false);"
        "try { barrier.send(); } catch (e) {}</script>"
    )

    with pytest.raises(ExternalRequestError, match=re.escape(probe_url)):
        await render(
            Composition(html=html, width_px=320, height_px=200),
            output_path,
            PlaywrightRenderer(java_script_enabled=True),
        )

    assert recording_server.requested_paths == []
    assert not output_path.exists()
