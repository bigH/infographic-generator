"""Renderer contract: what actually lands on disk.

Every dimension asserted here is decoded from the PNG bytes on the filesystem,
never taken from the ``RenderResult`` alone -- the file is the deliverable and
the reported numbers are just a claim about it. Each test launches chromium, so
renders are kept few and small.
"""

from __future__ import annotations

import asyncio
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
from infographic_generator.render import PlaywrightRenderer

TIMEOUT_S = 25.0
"""Hard ceiling on a single render, so a network hang fails instead of stalling."""

CONTENT_HEIGHT_PX = 1500
UNREACHABLE_IMAGE = "<img src='https://cdn.example.invalid/x.png'>"


def document(body: str = "", head: str = "") -> str:
    """A self-contained page whose background is white unless something overrides it."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>body{{margin:0;background:#ffffff}}</style>{head}</head>"
        f"<body>{body}</body></html>"
    )


TINY_HTML = document("<h1>hi</h1>")
TALL_HTML = document(f"<div style='height:{CONTENT_HEIGHT_PX}px'></div>")


def png_size(path: Path) -> tuple[int, int]:
    """Width and height straight out of the IHDR chunk, with no image library."""
    payload = path.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    return cast(tuple[int, int], struct.unpack(">II", payload[16:24]))


async def render(composition: Composition, output_path: Path) -> None:
    """Render under a timeout; a hang is a failure, not a stalled suite."""
    await asyncio.wait_for(
        PlaywrightRenderer().render(composition, output_path), timeout=TIMEOUT_S
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


async def test_a_remote_reference_is_never_requested_and_never_hangs(
    tmp_path: Path, recording_server: RecordingServer
) -> None:
    output_path = tmp_path / "out.png"
    html = document(
        body=UNREACHABLE_IMAGE,
        head=f"<link rel='stylesheet' href='{recording_server.url}/red.css'>",
    )

    await render(Composition(html=html, width_px=320, height_px=200), output_path)

    assert recording_server.requested_paths == []
    assert png_size(output_path) == (640, 400)


async def test_an_aborted_stylesheet_leaves_the_render_byte_identical(
    tmp_path: Path, recording_server: RecordingServer
) -> None:
    with_link = tmp_path / "with_link.png"
    without_link = tmp_path / "without_link.png"
    link = f"<link rel='stylesheet' href='{recording_server.url}/red.css'>"

    await render(
        Composition(html=document(head=link), width_px=320, height_px=200), with_link
    )
    await render(
        Composition(html=document(), width_px=320, height_px=200), without_link
    )

    assert with_link.read_bytes() == without_link.read_bytes()


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
