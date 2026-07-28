"""Data spine of the infographic pipeline.

The flow is ``Brief -> ResearchContent -> Sequence[ImageAsset] -> Composition ->
RenderResult``; each arrow is a port in :mod:`infographic_generator.core.ports`.
Everything here is pure data: frozen, slotted, fully typed, no behaviour and no
validation. Stages depend on these types, never on each other.

``Composition.html`` carries the contract that makes rendering deterministic: it
must be fully self-contained -- inline ``<style>``, images as ``data:`` URIs, no
external requests at render time -- so the renderer never needs a base URL, a
network, or a working directory.

Hashing: these types are frozen and so hashable exactly when their contents are.
Sequence fields default to ``()`` and stay hashable while callers pass tuples.
``Brief.extras`` is a mapping and is never hashable, so a ``Brief`` is not usable
as a dict key. That is a deliberate trade -- ``extras`` is an open-ended bag read
by name, and keeping it a ``Mapping`` beats dict-key support no stage wants.
Equality is structural throughout.

Strings in ``ResearchContent`` and ``ImageCredit`` are untrusted text scraped from
the web and may be attacker-influenced. They are plain text, never markup;
whoever puts them in HTML escapes them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

EMPTY_EXTRAS: Final[Mapping[str, str]] = MappingProxyType({})


class Theme(StrEnum):
    """Visual palette the composer renders against."""

    LIGHT = "light"
    DARK = "dark"


class ImageRole(StrEnum):
    """How an image is meant to be used in the layout."""

    HERO = "hero"
    SUPPORTING = "supporting"
    ICON = "icon"
    BACKGROUND = "background"


@dataclass(frozen=True, slots=True)
class RenderOptions:
    """Requested output geometry.

    The **composer** reads these and copies what the renderer needs into the
    ``Composition`` -- the renderer never sees a ``Brief``.
    """

    width_px: int = 1200
    height_px: int | None = None
    """``None`` renders a full-page screenshot. The composer copies this into
    ``Composition.height_px``; the renderer only ever reads it there."""
    theme: Theme = Theme.LIGHT
    device_scale_factor: float = 2.0


@dataclass(frozen=True, slots=True)
class Brief:
    """The user's request: what to make, for whom, and how to render it."""

    prompt: str
    options: RenderOptions = field(default_factory=RenderOptions)
    audience: str | None = None
    locale: str = "en-US"
    """BCP 47 tag. The researcher writes content in this language; the composer
    sets ``<html lang>`` and picks text direction from it."""
    max_facts: int | None = None
    extras: Mapping[str, str] = field(default=EMPTY_EXTRAS)
    """Free-form stage hints, keys namespaced by stage (``research.*``,
    ``imagery.*``, ``composition.*``, ``render.*``). Ignore keys outside your
    namespace and never raise on an unknown key. Makes the instance unhashable."""


@dataclass(frozen=True, slots=True)
class Source:
    """Where a claim came from."""

    url: str
    title: str | None = None
    publisher: str | None = None
    retrieved_at: datetime | None = None
    """Timezone-aware UTC."""


@dataclass(frozen=True, slots=True)
class Fact:
    """A single headline statistic, sized for a stat card."""

    label: str
    value: str
    unit: str | None = None
    """Rendered small beside ``value``; ``None`` for non-numeric values like "Vulnerable"."""
    detail: str | None = None
    source: Source | None = None


@dataclass(frozen=True, slots=True)
class NarrativeSection:
    """A titled block of prose supporting the infographic's story."""

    heading: str
    body: str
    sources: Sequence[Source] = ()


@dataclass(frozen=True, slots=True)
class ResearchContent:
    """Everything the researcher learned, ready to lay out."""

    title: str
    subtitle: str
    summary: str
    facts: Sequence[Fact] = ()
    sections: Sequence[NarrativeSection] = ()
    keywords: Sequence[str] = ()
    """Literal image-search queries handed to the image sourcer: 3-8 short
    phrases, most important first."""
    sources: Sequence[Source] = ()
    """Document-level bibliography, including sources not tied to one fact."""


@dataclass(frozen=True, slots=True)
class ImageCredit:
    """Attribution and licensing an image may not be published without."""

    license: str
    """Short identifier, SPDX-style where one exists (``CC-BY-4.0``,
    ``CC-BY-SA-4.0``, ``CC0-1.0``, ``public-domain``). The composer renders this
    string verbatim."""
    author: str | None = None
    license_url: str | None = None
    source: Source | None = None
    """Required for anything fetched from the web: it carries the work's page URL
    and title, both of which CC BY/BY-SA attribution must display."""
    modified: bool = False
    """True if the asset was cropped or adapted; CC BY-SA requires that be stated."""


@dataclass(frozen=True, slots=True)
class ImageAsset:
    """An image already in hand, as raw bytes or a local file."""

    content: bytes | Path
    """Bytes for downloaded images; ``Path`` for local fixtures. A ``Path`` must be
    absolute -- resolve it against your own package
    (``Path(__file__).resolve().parent / ...``), never against the process CWD."""
    mime_type: str
    """One of ``image/png``, ``image/jpeg``, ``image/webp``. SVG is not accepted --
    it can carry script and remote references."""
    width_px: int
    height_px: int
    alt_text: str
    credit: ImageCredit
    role: ImageRole = ImageRole.SUPPORTING


@dataclass(frozen=True, slots=True)
class Composition:
    """Self-contained HTML document: inline styles, ``data:`` URIs, no external requests."""

    html: str
    width_px: int
    height_px: int | None = None
    """``None`` means the document sets its own height and is captured full-page."""
    device_scale_factor: float = 2.0
    """Copied verbatim from ``brief.options``; the renderer's only source for it."""
    title: str = ""
    """The document's ``<title>``, duplicated here so callers need not parse the
    HTML. The renderer ignores it."""


@dataclass(frozen=True, slots=True)
class RenderResult:
    """What the renderer actually wrote to disk.

    ``width_px``/``height_px`` are the PNG's own pixel dimensions -- CSS pixels
    multiplied by the device scale factor -- so they will not equal
    ``Composition.width_px`` unless that factor is 1.0.
    """

    output_path: Path
    width_px: int
    height_px: int
    bytes_written: int
