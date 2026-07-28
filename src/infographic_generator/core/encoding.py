"""Turning an :class:`ImageAsset` into something HTML can embed.

The only logic in ``core``: pure, no caching, no network, no image library. The
composer calls :func:`to_data_uri` and inlines the result. The two failure modes
are an unreadable ``Path`` and a ``mime_type`` unsafe to interpolate.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Final, assert_never

from infographic_generator.core.models import ImageAsset

_MIME_TYPE: Final = re.compile(r"[a-z]+/[a-z0-9.+-]+")


def image_bytes(asset: ImageAsset) -> bytes:
    """Return the asset's raw bytes, reading from disk for the ``Path`` variant.

    Raises ``FileNotFoundError`` (or another ``OSError``) if a ``Path`` asset is
    missing or unreadable. The ``bytes`` variant cannot fail.
    """
    match asset.content:
        case bytes() as payload:
            return payload
        case Path() as path:
            return path.read_bytes()
        case unreachable:
            assert_never(unreachable)


def data_uri(mime_type: str, payload: bytes) -> str:
    """Encode bytes as ``data:{mime_type};base64,{...}``.

    ``mime_type`` is interpolated verbatim into an HTML attribute, so anything
    beyond a bare ``type/subtype`` is rejected with ``ValueError`` -- both a raw
    ``Content-Type`` header (``image/png;charset=utf-8``) and an injection
    attempt, rather than embedding either.
    """
    if not _MIME_TYPE.fullmatch(mime_type):
        raise ValueError(f"unsafe mime type: {mime_type!r}")
    return f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}"


def to_data_uri(asset: ImageAsset) -> str:
    """Encode an asset for direct use in ``src`` or ``url(...)``.

    Raises whatever :func:`image_bytes` and :func:`data_uri` raise.
    """
    return data_uri(asset.mime_type, image_bytes(asset))
