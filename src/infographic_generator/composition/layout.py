"""Turning research content into a view model the template can render dumbly.

Everything here is pure: no I/O beyond :func:`to_data_uri` reading a ``Path``
asset and :func:`font_faces` reading the bundled woff2 files, no markup, no
escaping. The template owns markup; escaping is Jinja2's job. Keeping the
decisions here means they are typed and testable, and the template stays a
layout description rather than a program.

The layout is a specimen sheet: an ink masthead with the hero punched into it, a
ledger of statistics set two to a line, and inverted "patch" rows that break the
ledger's rhythm the way the animal's markings break its coat.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from infographic_generator.core.encoding import data_uri, to_data_uri
from infographic_generator.core.models import (
    Brief,
    Fact,
    ImageAsset,
    ImageRole,
    NarrativeSection,
    ResearchContent,
    Source,
    Theme,
)

FONT_DIR: Final = Path(__file__).resolve().parent / "fonts"

_RTL_LANGUAGES: Final[frozenset[str]] = frozenset(
    {"ar", "he", "fa", "ur", "yi", "ps", "sd", "dv", "ug", "ckb"}
)
_MIN_GUTTER: Final = 32
_MAX_GUTTER: Final = 96
_GUTTER_RATIO: Final = 0.06
_ROWS_PER_BLOCK: Final = 5
_BAND_CAPACITY: Final = 3
"""Images shown beside the hero. Three to a band keeps each one a reasonable
share of a page this tall; the imagery stage hands them over most-significant
first, so taking the leading few is the cheap way to stay in proportion."""
_MIN_ASPECT: Final = 0.68
_MAX_ASPECT: Final = 1.85
_FALLBACK_ASPECT: Final = 4 / 3

_TITLE_ADVANCE: Final = 0.50
"""Advance width of one display-bold glyph as a fraction of the font size, for
the mixed-case prose of a headline."""
_VALUE_ADVANCE: Final = 0.60
"""Same, for a value: tabular figures in the embedded display face are exactly
0.6em and a value is mostly figures, so this errs wide for prose-heavy values."""
_UNIT_ADVANCE: Final = 0.17
"""Same, for the unit suffix, which sets at 0.3em in a narrower face."""
_MIN_FIT_CQW: Final = 3.0
"""Floor on the width-derived cap; below this, wrapping is the better answer."""


class Scale(StrEnum):
    """The editorial ceiling on how much room a headline string may take.

    Chosen from character count, so ``"1,864"`` gets to shout while a long value
    starts smaller. It is only a ceiling: the template additionally caps the
    font size at :attr:`Stat.fit`, derived from the width of the column the value
    actually has to sit in, so a long value cannot overflow a narrow page.
    """

    XL = "xl"
    L = "l"
    M = "m"
    S = "s"


@dataclass(frozen=True, slots=True)
class FontFace:
    """A woff2 face embedded in the document, so the render is machine-independent."""

    family: str
    weight: int
    style: str
    data_uri: str


@dataclass(frozen=True, slots=True)
class Credit:
    """Attribution rendered as visible text -- never as a link."""

    license: str
    work: str | None
    author: str | None
    license_url: str | None
    source_url: str | None
    adapted: bool


@dataclass(frozen=True, slots=True)
class Figure:
    """An embedded image plus the attribution shown beside it."""

    data_uri: str
    alt: str
    caption: str
    credit: Credit
    aspect: str
    """Width over height as a bare number, from the asset's *declared* size.

    Serves twice: as the CSS ``aspect-ratio`` that governs the rendered height,
    and as the ``flex-grow`` that widens a landscape figure more than a portrait
    one, so every figure in a band comes out the same height uncropped.
    """


@dataclass(frozen=True, slots=True)
class Stat:
    """One row of the ledger."""

    label: str
    value: str
    unit: str | None
    detail: str | None
    attribution: str | None
    scale: Scale
    fit: str
    """Width-derived cap on the value's font size, in container units (``18.4cqw``)."""
    feature: bool
    """Inverted full-bleed row -- the ink patch that breaks the rhythm."""
    full_width: bool
    """Spans both ledger columns instead of pairing up with a neighbour."""


@dataclass(frozen=True, slots=True)
class Reference:
    """A bibliography entry."""

    title: str
    publisher: str | None
    url: str


@dataclass(frozen=True, slots=True)
class Section:
    """A titled block of prose, set as a side-head."""

    heading: str
    body: str
    attributions: Sequence[str]
    """Publisher names for the section's sources; the URLs land in the bibliography."""


@dataclass(frozen=True, slots=True)
class Page:
    """Everything the template renders, already decided."""

    lang: str
    direction: str
    theme: Theme
    fonts: Sequence[FontFace]
    title: str
    title_scale: Scale
    title_fit: str
    subtitle: str
    summary: str
    hero: Figure | None
    ledger: Sequence[Sequence[Stat]]
    band: Sequence[Figure]
    sections: Sequence[Section]
    references: Sequence[Reference]
    credits: Sequence[Credit]
    width_px: int
    gutter_px: int
    min_height_px: int | None


def build_page(
    brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
) -> Page:
    """Assemble the view model. Raises ``OSError`` for an unreadable asset."""
    hero, band = _imagery(images)
    title = _page_title(brief, content)
    shown = tuple(figure for figure in (hero, *band) if figure is not None)
    return Page(
        lang=_bcp47(brief.locale),
        direction=_direction(brief.locale),
        theme=brief.options.theme,
        fonts=font_faces(),
        title=title,
        title_scale=_scale_for(title, (26, 46, 70)),
        title_fit=_fit(_longest_word(title), _TITLE_ADVANCE),
        subtitle=content.subtitle,
        summary=content.summary,
        hero=hero,
        ledger=_ledger(content.facts),
        band=band,
        sections=tuple(_section(section) for section in content.sections),
        references=_references(content),
        credits=tuple(f.credit for f in shown if _has_attribution(f.credit)),
        width_px=brief.options.width_px,
        gutter_px=_gutter(brief.options.width_px),
        min_height_px=brief.options.height_px,
    )


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #

_FONT_FILES: Final = (
    ("Ledger Slab", 700, "normal", "zilla-slab-700.woff2"),
    ("Ledger Text", 400, "normal", "pt-serif-400.woff2"),
    ("Ledger Text", 400, "italic", "pt-serif-400-italic.woff2"),
    ("Ledger Mono", 400, "normal", "ibm-plex-mono-400.woff2"),
)


@lru_cache(maxsize=1)
def font_faces() -> tuple[FontFace, ...]:
    """Read the bundled woff2 faces once and hold them as ``data:`` URIs.

    Raises ``OSError`` if the package is missing its fonts, which is a packaging
    fault rather than something a caller can supply.
    """
    return tuple(
        FontFace(
            family=family,
            weight=weight,
            style=style,
            data_uri=data_uri("font/woff2", (FONT_DIR / filename).read_bytes()),
        )
        for family, weight, style, filename in _FONT_FILES
    )


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def _page_title(brief: Brief, content: ResearchContent) -> str:
    return content.title.strip() or brief.prompt.strip() or "Infographic"


def _bcp47(locale: str) -> str:
    """``he_IL`` is a POSIX locale, not a language tag; ``<html lang>`` wants BCP 47."""
    return locale.replace("_", "-")


def _direction(locale: str) -> str:
    language = _bcp47(locale).split("-")[0].lower()
    return "rtl" if language in _RTL_LANGUAGES else "ltr"


def _gutter(width_px: int) -> int:
    return max(_MIN_GUTTER, min(_MAX_GUTTER, round(width_px * _GUTTER_RATIO)))


def _scale_for(text: str, thresholds: tuple[int, int, int]) -> Scale:
    short, medium, long = thresholds
    length = len(text.strip())
    if length <= short:
        return Scale.XL
    if length <= medium:
        return Scale.L
    if length <= long:
        return Scale.M
    return Scale.S


def _fit(text: str, advance: float, extra: float = 0.0) -> str:
    """The font size at which ``text`` sets on one line, in ``cqw``.

    ``1cqw`` is one percent of the containing column, so the answer holds at any
    page width: this is what stops ``26-84 lb (12-38 kg)`` from breaking across
    three lines when the page narrows.
    """
    width = max(1.0, len(text.strip()) * advance + extra)
    return f"{max(_MIN_FIT_CQW, 100 / width):.2f}cqw"


def _longest_word(text: str) -> str:
    """A headline is allowed to wrap; a single word of it is not allowed to overflow."""
    return max(text.split(), key=len, default="")


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


def _ledger(facts: Sequence[Fact]) -> tuple[tuple[Stat, ...], ...]:
    """Chunk the facts into blocks, each led by an inverted patch row.

    Every fact is rendered -- capping is the researcher's job. The blocks are
    what break the rhythm, so they keep coming however many facts arrive.
    """
    return tuple(
        _block(facts[start : start + _ROWS_PER_BLOCK])
        for start in range(0, len(facts), _ROWS_PER_BLOCK)
    )


def _block(facts: Sequence[Fact]) -> tuple[Stat, ...]:
    """One patch row plus half-width rows, two to a line.

    An odd number of half-width rows would end the block on a lopsided orphan,
    so that last row spans the full width instead and reads as deliberate.
    """
    orphan = len(facts) - 1 if len(facts) % 2 == 0 else -1
    return tuple(
        _stat(fact, feature=index == 0, full_width=index in (0, orphan))
        for index, fact in enumerate(facts)
    )


def _stat(fact: Fact, *, feature: bool, full_width: bool) -> Stat:
    return Stat(
        label=fact.label,
        value=fact.value,
        unit=fact.unit,
        detail=fact.detail,
        attribution=_attribution(fact.source),
        scale=_scale_for(fact.value, (7, 13, 20)),
        fit=_fit(fact.value, _VALUE_ADVANCE, _unit_width(fact.unit)),
        feature=feature,
        full_width=full_width,
    )


def _unit_width(unit: str | None) -> float:
    return 0.0 if unit is None else len(unit.strip()) * _UNIT_ADVANCE + 0.5


def _attribution(source: Source | None) -> str | None:
    if source is None:
        return None
    return source.publisher or source.title or _host(source.url)


def _host(url: str) -> str:
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        return url
    return netloc.removeprefix("www.") or url


# --------------------------------------------------------------------------- #
# Prose and apparatus
# --------------------------------------------------------------------------- #


def _section(section: NarrativeSection) -> Section:
    return Section(
        heading=section.heading,
        body=section.body,
        attributions=tuple(
            attribution
            for attribution in (_attribution(source) for source in section.sources)
            if attribution
        ),
    )


def _references(content: ResearchContent) -> tuple[Reference, ...]:
    """The bibliography: document sources first, then any a section cited alone.

    A section's sources are as real as the document's; dropping them would lose a
    genuine citation, which in this pipeline is the sin next to inventing one.
    """
    return tuple(
        _reference(source)
        for source in _unique_by_url(
            (*content.sources, *(s for sec in content.sections for s in sec.sources))
        )
    )


def _unique_by_url(sources: Iterable[Source]) -> tuple[Source, ...]:
    seen: set[str] = set()
    unique: list[Source] = []
    for source in sources:
        if source.url not in seen:
            seen.add(source.url)
            unique.append(source)
    return tuple(unique)


def _reference(source: Source) -> Reference:
    return Reference(
        title=source.title or _host(source.url),
        publisher=source.publisher,
        url=source.url,
    )


# --------------------------------------------------------------------------- #
# Imagery
# --------------------------------------------------------------------------- #


def _imagery(
    images: Sequence[ImageAsset],
) -> tuple[Figure | None, tuple[Figure, ...]]:
    """Choose what to display, then embed only that.

    Encoding an asset the page never shows would bloat the document for nothing,
    and crediting one would claim a use we did not make.
    """
    lead, rest = _split_hero(images)
    if lead is None:
        return None, ()
    return _figure(lead), tuple(map(_figure, rest[:_BAND_CAPACITY]))


def _split_hero(
    images: Sequence[ImageAsset],
) -> tuple[ImageAsset | None, tuple[ImageAsset, ...]]:
    """Lead with the first ``HERO`` asset, else the first asset given."""
    if not images:
        return None, ()
    lead = next(
        (i for i, asset in enumerate(images) if asset.role is ImageRole.HERO), 0
    )
    return images[lead], tuple(a for i, a in enumerate(images) if i != lead)


def _figure(asset: ImageAsset) -> Figure:
    credit = _credit(asset)
    return Figure(
        data_uri=to_data_uri(asset),
        alt=asset.alt_text,
        caption=_caption(credit),
        credit=credit,
        aspect=_aspect(asset.width_px, asset.height_px),
    )


def _credit(asset: ImageAsset) -> Credit:
    source = asset.credit.source
    return Credit(
        license=asset.credit.license,
        work=source.title if source else None,
        author=asset.credit.author,
        license_url=asset.credit.license_url,
        source_url=source.url if source else None,
        adapted=asset.credit.modified,
    )


def _has_attribution(credit: Credit) -> bool:
    """An entirely blank credit would render as an empty ruled row saying nothing."""
    return bool(
        credit.license
        or credit.work
        or credit.author
        or credit.license_url
        or credit.source_url
    )


def _caption(credit: Credit) -> str:
    parts = [credit.work, credit.author, credit.license]
    if credit.adapted:
        parts.append("adapted")
    return " · ".join(part for part in parts if part)


def _aspect(width_px: int, height_px: int) -> str:
    """Always a bare number: it is also used as a ``flex-grow``, which needs one."""
    known = width_px > 0 and height_px > 0
    declared = width_px / height_px if known else _FALLBACK_ASPECT
    return f"{min(_MAX_ASPECT, max(_MIN_ASPECT, declared)):.4f}"
