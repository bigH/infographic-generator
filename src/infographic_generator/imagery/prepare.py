"""Making downloaded bytes display-ready: decode, downscale, re-encode.

This is the imagery stage's own job -- the composer only scales with CSS, so an
oversized asset inflates the data URI and the render. Everything here is
synchronous CPU work over bytes already in memory: no network, no filesystem.

The bytes come from the open web, so decoding is treated as hostile: anything
that will not decode, or that decodes to something absurd, comes back as
``None`` rather than raising.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Final

from PIL import Image, UnidentifiedImageError

_MIME_BY_FORMAT: Final[dict[str, str]] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
"""Raster formats we accept. SVG is absent on purpose -- the core models refuse
it because it can carry script and remote references."""

_JPEG_QUALITY_LADDER: Final[tuple[int, ...]] = (85, 75, 65, 55)
_FINGERPRINT_SIDE: Final = 8


@dataclass(frozen=True, slots=True)
class PreparedImage:
    """Bytes that are safe to hand to the composer, and their true dimensions."""

    payload: bytes
    mime_type: str
    width_px: int
    height_px: int
    resampled: bool
    """True when the pixels or the encoding changed -- i.e. this is an adaptation
    of the original work, which CC BY-SA requires us to state."""


def prepare(
    payload: bytes, *, max_dimension_px: int, max_encoded_bytes: int
) -> PreparedImage | None:
    """Return display-ready bytes, or ``None`` if these bytes are not usable.

    Downscales with LANCZOS until the longest side fits ``max_dimension_px`` and
    re-encodes to land under ``max_encoded_bytes``. Bytes that already satisfy
    both, in a format we accept, are passed through untouched so we do not claim
    an adaptation we did not make.
    """
    opened = _decode(payload)
    if opened is None:
        return None
    image, source_format = opened

    with image:
        if _fits(image, max_dimension_px) and len(payload) <= max_encoded_bytes:
            return PreparedImage(
                payload=payload,
                mime_type=_MIME_BY_FORMAT[source_format],
                width_px=image.width,
                height_px=image.height,
                resampled=False,
            )
        with _downscaled(image, max_dimension_px) as resized:
            encoded = _encode(resized, max_encoded_bytes)
            return PreparedImage(
                payload=encoded[0],
                mime_type=encoded[1],
                width_px=resized.width,
                height_px=resized.height,
                resampled=True,
            )


def fingerprint(payload: bytes) -> int:
    """64-bit average hash, for spotting the same picture twice.

    Two files that differ only by resolution, re-encoding or a small crop land on
    the same or a very close hash, which is what catches near-duplicates that an
    identity check on the file page would miss. Returns ``0`` for bytes that will
    not decode; callers only compare hashes of images that already decoded.
    """
    opened = _decode(payload)
    if opened is None:
        return 0
    with opened[0] as image:
        side = _FINGERPRINT_SIDE
        with image.convert("L").resize((side, side), Image.Resampling.LANCZOS) as small:
            pixels = small.tobytes()  # mode "L": one byte per pixel
    average = sum(pixels) / len(pixels)
    bits = 0
    for pixel in pixels:
        bits = (bits << 1) | int(pixel >= average)
    return bits


def hamming_distance(left: int, right: int) -> int:
    """Number of differing bits between two fingerprints."""
    return (left ^ right).bit_count()


def _decode(payload: bytes) -> tuple[Image.Image, str] | None:
    """Decode to a Pillow image plus its format name, or ``None`` if hostile."""
    try:
        image = Image.open(io.BytesIO(payload))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        # Truncated, mislabelled, or a decompression bomb: not a candidate.
        return None
    if image.format not in _MIME_BY_FORMAT or min(image.width, image.height) < 1:
        image.close()
        return None
    return image, image.format


def _fits(image: Image.Image, max_dimension_px: int) -> bool:
    return max(image.width, image.height) <= max_dimension_px


def _downscaled(image: Image.Image, max_dimension_px: int) -> Image.Image:
    """Shrink so the longest side is at most ``max_dimension_px``, keeping ratio."""
    longest = max(image.width, image.height)
    if longest <= max_dimension_px:
        return image.copy()
    scale = max_dimension_px / longest
    size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    return image.resize(size, Image.Resampling.LANCZOS)


def _encode(image: Image.Image, max_encoded_bytes: int) -> tuple[bytes, str]:
    """Encode under the byte ceiling, preferring PNG only when alpha is real.

    If no quality in the ladder gets under the ceiling we return the smallest
    attempt: the port asks for *roughly* 1 MB, and a slightly-over asset beats
    dropping a good image.
    """
    if _has_alpha(image):
        png = _to_bytes(image, "PNG", optimize=True)
        if len(png) <= max_encoded_bytes:
            return png, "image/png"

    flattened = _without_alpha(image)
    with flattened:
        smallest = b""
        for quality in _JPEG_QUALITY_LADDER:
            smallest = _to_bytes(flattened, "JPEG", quality=quality, optimize=True)
            if len(smallest) <= max_encoded_bytes:
                break
    return smallest, "image/jpeg"


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info


def _without_alpha(image: Image.Image) -> Image.Image:
    """Composite onto white so JPEG encoding cannot turn alpha into black."""
    if not _has_alpha(image):
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    backdrop = Image.new("RGB", rgba.size, (255, 255, 255))
    backdrop.paste(rgba, mask=rgba.split()[-1])
    rgba.close()
    return backdrop


def _to_bytes(image: Image.Image, image_format: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()
