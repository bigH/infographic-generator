"""Command-line entry point: parse a prompt, wire the stages, write a PNG.

This is the one module that names concrete stage implementations. Everything
downstream of :func:`build_pipeline` talks to the ``core.ports`` Protocols only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from infographic_generator.composition import HtmlComposer
from infographic_generator.core.models import Brief, RenderOptions, RenderResult, Theme
from infographic_generator.imagery import PandaImageSourcer
from infographic_generator.pipeline import Pipeline
from infographic_generator.render import PlaywrightRenderer
from infographic_generator.research.panda import PandaResearcher

DEFAULT_OUTPUT: Final[Path] = Path("infographic.png")

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1


def build_parser() -> argparse.ArgumentParser:
    """Build the ``infographic`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="infographic",
        description=(
            "Turn a text prompt into an infographic PNG: research the topic on the "
            "web, find images for it, lay it out as a self-contained HTML page, and "
            "screenshot that page with headless chromium."
        ),
        epilog=(
            "example:\n"
            '  infographic "giant pandas" -o panda.png --theme dark '
            "--width 1600 --max-facts 5\n"
            "  # writes a 3200px-wide PNG (1600 CSS px at the default 2.0 scale)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "prompt",
        help='what the infographic should be about, in plain English (e.g. "giant pandas")',
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        metavar="PATH",
        help="file to write the PNG to; parent directories are created (default: %(default)s)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1200,
        metavar="PX",
        help="page width in CSS pixels, before the scale factor (default: %(default)s)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=None,
        metavar="PX",
        help=(
            "page height in CSS pixels; the image is clipped to it. Omit to capture "
            "the full page, however tall the content turns out (default: full page)"
        ),
    )
    parser.add_argument(
        "--theme",
        type=Theme,
        choices=tuple(Theme),
        default=Theme.LIGHT,
        help="colour palette the page is rendered against (default: %(default)s)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=2.0,
        metavar="FACTOR",
        help=(
            "device scale factor: the PNG comes out this many times larger than the "
            "CSS width, so --width 1200 --scale 2.0 writes a 2400px-wide image. Raise "
            "it for print, drop it to 1.0 for a smaller file (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--max-facts",
        type=int,
        default=None,
        metavar="N",
        help="keep at most this many facts on the page (default: no cap)",
    )
    parser.add_argument(
        "--audience",
        default=None,
        metavar="WHO",
        help=(
            'who the infographic is for (e.g. "schoolchildren", "policy analysts"); '
            "steers tone and reading level (default: a general audience)"
        ),
    )
    return parser


def parse_args(argv: Sequence[str]) -> tuple[Brief, Path]:
    """Parse ``argv`` -- without the program name -- into a Brief and output path."""
    args = build_parser().parse_args(list(argv))
    options = RenderOptions(
        width_px=args.width,
        height_px=args.height,
        theme=args.theme,
        device_scale_factor=args.scale,
    )
    brief = Brief(
        prompt=args.prompt,
        options=options,
        audience=args.audience,
        max_facts=args.max_facts,
    )
    return brief, args.output


def build_pipeline() -> Pipeline:
    """Select the concrete stage implementations and inject them into a Pipeline.

    The only module that names a concrete stage. Swapping a stub for a real AI
    agent is one import and one argument here; nothing else moves.
    """
    return Pipeline(
        researcher=PandaResearcher(),
        image_sourcer=PandaImageSourcer(),
        composer=HtmlComposer(),
        renderer=PlaywrightRenderer(),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    build: Callable[[], Pipeline] = build_pipeline,
) -> int:
    """Console-script entry point. Returns a process exit code.

    ``0`` on success and ``1`` on any failure -- an unwritable output path, a
    stage raising, a render timeout. Bad arguments exit ``2`` via argparse, as
    usual. Failures print one ``error: ...`` line to stderr, never a traceback.
    """
    brief, output_path = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = asyncio.run(build().run(brief, output_path))
    except OSError as exc:
        return _fail(f"cannot write {output_path}: {exc.strerror or exc}", EXIT_FAILED)
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}", EXIT_FAILED)
    print(_summary(result))
    return EXIT_OK


def _fail(message: str, code: int) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _summary(result: RenderResult) -> str:
    return (
        f"wrote {result.output_path}\n"
        f"{result.width_px} x {result.height_px} px, "
        f"{_human_bytes(result.bytes_written)} ({result.bytes_written} bytes)"
    )


def _human_bytes(count: int) -> str:
    """``1234567`` -> ``1.2 MiB``. Anything under a kibibyte stays exact."""
    if count < 1024:
        return f"{count} bytes"
    size = float(count)
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size / 1024:.1f} TiB"
