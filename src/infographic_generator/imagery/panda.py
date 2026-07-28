"""Hard-coded placeholder ``ImageSourcer``: the five panda JPEGs in ``assets/panda``.

It ignores the brief and the research content entirely -- there is no search, no
network and no image library here. Per-image metadata lives in
``assets/panda/credits.json``, which is the single source of truth for licensing,
attribution and alt text; only the ordering and the roles are decided in Python.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from infographic_generator.core.models import (
    Brief,
    ImageAsset,
    ImageCredit,
    ImageRole,
    ResearchContent,
    Source,
)
from infographic_generator.core.ports import ImageSourcer

MAX_DIMENSION_PX: Final[int] = 2000
MAX_IMAGES: Final[int] = 6

_LOG: Final = logging.getLogger(__name__)


def _panda_asset_dir() -> Path:
    """``assets/panda``: bundled inside the wheel, or at the repo root in a checkout."""
    package_root = Path(__file__).resolve().parents[1]
    repo_root = package_root.parents[1]
    bundled = package_root / "assets" / "panda"
    return bundled if bundled.is_dir() else repo_root / "assets" / "panda"


_ASSET_DIR: Final[Path] = _panda_asset_dir()
_CREDITS_PATH: Final[Path] = _ASSET_DIR / "credits.json"

_PUBLISHER: Final[str] = "Wikimedia Commons"

# TODO: stands in for image selection. The real implementation is an agent that
# queries licensed image APIs, filters for usable licences, downloads, resizes and
# writes attribution straight from the provider's metadata.
_SELECTION: Final[tuple[tuple[str, ImageRole], ...]] = (
    ("giant-panda-portrait.jpg", ImageRole.HERO),
    ("giant-panda-eating-bamboo.jpg", ImageRole.SUPPORTING),
    ("giant-panda-cub.jpg", ImageRole.SUPPORTING),
    ("giant-panda-full-body.jpg", ImageRole.SUPPORTING),
    ("giant-panda-in-habitat.jpg", ImageRole.BACKGROUND),
)


class PandaImageSourcer:
    """Serves the panda fixtures as display-ready assets, most significant first.

    Dimensions come from the JPEG bytes rather than from ``credits.json``; a file
    whose declared size does not match its bytes is a broken fixture and raises.
    Resizing is this stage's job but there is no image library to do it with, so an
    asset over ``MAX_DIMENSION_PX`` is dropped with a warning instead of shipped
    oversized. All five fixtures are 1600 px, so today nothing is dropped.

    Raises ``RuntimeError`` when the asset directory is missing, ``credits.json``
    is unreadable or malformed, or a selected image has no credits entry -- each of
    those is a packaging bug, not a reason to quietly return fewer images.
    """

    async def source_images(
        self, brief: Brief, content: ResearchContent
    ) -> Sequence[ImageAsset]:
        entries = _read_credits()
        selected = (_asset(entries, filename, role) for filename, role in _SELECTION)
        return tuple(asset for asset in selected if _fits(asset))[:MAX_IMAGES]


_: ImageSourcer = PandaImageSourcer()


@dataclass(frozen=True, slots=True)
class _Entry:
    """One record of ``credits.json``, validated."""

    filename: str
    title: str
    alt_text: str
    credit: str
    license: str
    license_url: str
    source_url: str
    width: int
    height: int
    mime_type: str
    modified: bool


def _asset(entries: Mapping[str, _Entry], filename: str, role: ImageRole) -> ImageAsset:
    """Build the asset for one selected filename, measuring its real dimensions."""
    entry = entries.get(filename)
    if entry is None:
        raise RuntimeError(f"{_CREDITS_PATH}: no entry for selected image {filename!r}")
    path = _ASSET_DIR / filename
    width, height = _jpeg_size(path.read_bytes())
    if (width, height) != (entry.width, entry.height):
        raise RuntimeError(
            f"{path}: credits.json declares {entry.width}x{entry.height} "
            f"but the file is {width}x{height}"
        )
    return ImageAsset(
        content=path,
        mime_type=entry.mime_type,
        width_px=width,
        height_px=height,
        alt_text=entry.alt_text,
        credit=ImageCredit(
            license=entry.license,
            author=entry.credit,
            license_url=entry.license_url,
            source=Source(
                url=entry.source_url, title=entry.title, publisher=_PUBLISHER
            ),
            modified=entry.modified,
        ),
        role=role,
    )


def _fits(asset: ImageAsset) -> bool:
    """Whether the asset honours the contract's dimension cap, warning if not."""
    if max(asset.width_px, asset.height_px) <= MAX_DIMENSION_PX:
        return True
    _LOG.warning(
        "dropping %s: %dx%d exceeds the %d px cap and this stub cannot resample",
        asset.content,
        asset.width_px,
        asset.height_px,
        MAX_DIMENSION_PX,
    )
    return False


def _read_credits() -> Mapping[str, _Entry]:
    """Load ``credits.json`` into validated records keyed by filename."""
    if not _ASSET_DIR.is_dir():
        raise RuntimeError(f"panda asset directory is missing: {_ASSET_DIR}")
    try:
        payload: object = json.loads(_CREDITS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{_CREDITS_PATH} is unreadable: {error}") from error
    if not isinstance(payload, list):
        raise RuntimeError(f"{_CREDITS_PATH}: expected a list of entries")
    entries = tuple(_entry(item) for item in payload)
    return {entry.filename: entry for entry in entries}


def _entry(item: object) -> _Entry:
    if not isinstance(item, dict):
        raise RuntimeError(f"{_CREDITS_PATH}: expected each entry to be an object")
    fields: Mapping[str, object] = item
    return _Entry(
        filename=_text(fields, "filename"),
        title=_text(fields, "title"),
        alt_text=_text(fields, "alt_text"),
        credit=_text(fields, "credit"),
        license=_text(fields, "license"),
        license_url=_text(fields, "license_url"),
        source_url=_text(fields, "source_url"),
        width=_count(fields, "width"),
        height=_count(fields, "height"),
        mime_type=_text(fields, "mime_type"),
        modified=_flag(fields, "modified"),
    )


def _malformed(key: str, expected: str) -> RuntimeError:
    return RuntimeError(f"{_CREDITS_PATH}: {key!r} is missing or not {expected}")


def _text(fields: Mapping[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str):
        raise _malformed(key, "a string")
    return value


def _count(fields: Mapping[str, object], key: str) -> int:
    value = fields.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _malformed(key, "an integer")
    return value


def _flag(fields: Mapping[str, object], key: str) -> bool:
    value = fields.get(key)
    if not isinstance(value, bool):
        raise _malformed(key, "a boolean")
    return value


_MARKER: Final[int] = 0xFF
_SOI: Final[bytes] = b"\xff\xd8"
# Markers with no length-prefixed payload: TEM, SOI, EOI and the RST run.
_STANDALONE: Final[frozenset[int]] = frozenset({0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)})
# SOF0..SOF15 share 0xC0..0xCF with DHT, JPG and DAC, which are not frame headers.
_START_OF_FRAME: Final[frozenset[int]] = frozenset(range(0xC0, 0xD0)) - {
    0xC4,
    0xC8,
    0xCC,
}


def _jpeg_size(payload: bytes) -> tuple[int, int]:
    """Read ``(width, height)`` from a JPEG's first start-of-frame segment.

    Walks the marker segments instead of decoding: each marker is ``0xFF`` plus a
    code, optionally preceded by ``0xFF`` fill bytes, and every marker outside
    :data:`_STANDALONE` carries a big-endian length covering itself.

    Raises ``ValueError`` for bytes that do not start with SOI, break the marker
    structure, end mid-segment, or hold no frame header.
    """
    if not payload.startswith(_SOI):
        raise ValueError("not a JPEG: missing SOI marker")
    index = 2
    while index < len(payload):
        if payload[index] != _MARKER:
            raise ValueError(f"not a JPEG: expected a marker at byte {index}")
        index += 1
        while index < len(payload) and payload[index] == _MARKER:
            index += 1
        if index == len(payload):
            raise ValueError("not a JPEG: truncated before a marker code")
        marker = payload[index]
        index += 1
        if marker in _STANDALONE:
            continue
        header = payload[index : index + 2]
        if len(header) < 2:
            raise ValueError("not a JPEG: truncated segment header")
        if marker in _START_OF_FRAME:
            return _frame_size(payload[index + 3 : index + 7])
        index += int.from_bytes(header, "big")
    raise ValueError("not a JPEG: no start-of-frame marker")


def _frame_size(frame: bytes) -> tuple[int, int]:
    """Decode the height-then-width pair a start-of-frame header carries."""
    if len(frame) < 4:
        raise ValueError("not a JPEG: truncated frame header")
    return int.from_bytes(frame[2:], "big"), int.from_bytes(frame[:2], "big")
