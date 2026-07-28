"""Turning research content into a view model the template can render dumbly.

Everything here is pure: no I/O beyond :func:`to_data_uri` reading a ``Path``
asset, no markup, no escaping. The template owns markup; escaping is Jinja2's
job. Keeping the decisions here means they are typed and testable, and the
template stays a layout description rather than a program.

The layout is a specimen sheet: an ink masthead with the hero punched into it,
a ledger of statistics whose type size is chosen from the length of each value,
and inverted "patch" rows that break the ledger's rhythm the way the animal's
markings break its coat.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from infographic_generator.core.encoding import to_data_uri
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

_RTL_LANGUAGES: Final[frozenset[str]] = frozenset(
    {"ar", "he", "fa", "ur", "yi", "ps", "sd", "dv", "ug", "ckb"}
)
_MIN_GUTTER: Final = 32
_MAX_GUTTER: Final = 96
_GUTTER_RATIO: Final = 0.06
_ROWS_BEFORE_BREAK: Final = 5
_BAND_CAPACITY: Final = 2
_MIN_ASPECT: Final = 0.68
_MAX_ASPECT: Final = 1.85


class Scale(StrEnum):
    """How much room a headline string is allowed to take.

    Chosen from character count so a long value like ``"26-84 lb (12-38 kg)"``
    sets at a size that still fits its column, while ``"1,864"`` gets to shout.
    """

    XL = "xl"
    L = "l"
    M = "m"
    S = "s"


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
    """CSS ``aspect-ratio`` value, derived from the asset's declared size."""


@dataclass(frozen=True, slots=True)
class Stat:
    """One row of the ledger."""

    label: str
    value: str
    unit: str | None
    detail: str | None
    attribution: str | None
    scale: Scale
    feature: bool
    """Inverted full-bleed row -- the ink patch that breaks the rhythm."""


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


@dataclass(frozen=True, slots=True)
class Page:
    """Everything the template renders, already decided."""

    lang: str
    direction: str
    theme: Theme
    title: str
    title_scale: Scale
    subtitle: str
    summary: str
    keywords: Sequence[str]
    hero: Figure | None
    ledger: Sequence[Sequence[Stat]]
    band: Sequence[Figure]
    coda: Sequence[Figure]
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
    figures = tuple(_figure(asset) for asset in images)
    hero, rest = _split_hero(images, figures)
    title = _page_title(brief, content)
    return Page(
        lang=brief.locale,
        direction=_direction(brief.locale),
        theme=brief.options.theme,
        title=title,
        title_scale=_scale_for(title, (26, 46, 70)),
        subtitle=content.subtitle,
        summary=content.summary,
        keywords=tuple(content.keywords),
        hero=hero,
        ledger=_ledger(content.facts),
        band=rest[:_BAND_CAPACITY],
        coda=rest[_BAND_CAPACITY:],
        sections=tuple(_section(s) for s in content.sections),
        references=tuple(_reference(s) for s in content.sources),
        credits=tuple(f.credit for f in figures),
        width_px=brief.options.width_px,
        gutter_px=_gutter(brief.options.width_px),
        min_height_px=brief.options.height_px,
    )


def _page_title(brief: Brief, content: ResearchContent) -> str:
    return content.title.strip() or brief.prompt.strip() or "Infographic"


def _direction(locale: str) -> str:
    language = locale.replace("_", "-").split("-")[0].lower()
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


def _ledger(facts: Sequence[Fact]) -> tuple[tuple[Stat, ...], ...]:
    """Split the facts into blocks, each led by an inverted patch row."""
    blocks = (facts[:_ROWS_BEFORE_BREAK], facts[_ROWS_BEFORE_BREAK:])
    return tuple(
        tuple(_stat(fact, feature=index == 0) for index, fact in enumerate(block))
        for block in blocks
        if block
    )


def _stat(fact: Fact, *, feature: bool) -> Stat:
    return Stat(
        label=fact.label,
        value=fact.value,
        unit=fact.unit,
        detail=fact.detail,
        attribution=_attribution(fact.source),
        scale=_scale_for(fact.value, (7, 13, 20)),
        feature=feature,
    )


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


def _section(section: NarrativeSection) -> Section:
    return Section(heading=section.heading, body=section.body)


def _reference(source: Source) -> Reference:
    return Reference(
        title=source.title or _host(source.url),
        publisher=source.publisher,
        url=source.url,
    )


def _split_hero(
    images: Sequence[ImageAsset], figures: Sequence[Figure]
) -> tuple[Figure | None, tuple[Figure, ...]]:
    """Lead with the first ``HERO`` asset, else the first asset given."""
    if not figures:
        return None, ()
    lead = next(
        (i for i, asset in enumerate(images) if asset.role is ImageRole.HERO), 0
    )
    rest = tuple(f for i, f in enumerate(figures) if i != lead)
    return figures[lead], rest


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


def _caption(credit: Credit) -> str:
    parts = [credit.work, credit.author, credit.license]
    if credit.adapted:
        parts.append("adapted")
    return " · ".join(part for part in parts if part)


def _aspect(width_px: int, height_px: int) -> str:
    if width_px <= 0 or height_px <= 0:
        return "4 / 3"
    ratio = min(_MAX_ASPECT, max(_MIN_ASPECT, width_px / height_px))
    return f"{ratio:.4f}"
