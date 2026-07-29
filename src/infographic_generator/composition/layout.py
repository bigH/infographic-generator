"""Turning research content into a view model the template can render dumbly.

Everything here is pure: no I/O beyond :func:`to_data_uri` reading a ``Path``
asset and :func:`font_faces` reading the bundled woff2 files, no markup, no
escaping. The template owns markup; escaping is Jinja2's job. Keeping the
decisions here means they are typed and testable, and the template stays a
layout description rather than a program.

The house layout is a specimen sheet: an ink masthead with the hero punched into
it, a ledger of statistics set two to a line, and inverted "patch" rows that
break the ledger's rhythm the way the animal's markings break its coat. The other
bodies borrow that vocabulary -- same tokens, same ticks, same rules -- so they
read as siblings.

One chrome, three bodies. :func:`build_chrome` computes the furniture once and
each body builder decides only its own information architecture, which is what
keeps the colophon honest: credits are derived from the figures the *chosen* body
actually places, never from the assets it was handed.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Final, TypeAlias, assert_never
from urllib.parse import urlsplit

from infographic_generator.composition.registry import TEMPLATE_REGISTRY
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

_TITLE_ADVANCE: Final = 0.60
"""Advance width of the widest realistic *single word* of a headline, as a
fraction of the font size. :func:`_fit` is fed :func:`_longest_word`, not the
whole string, so an average taken over a title would under-cap its longest word.
Measured in chromium against the embedded display face: 0.5907em per glyph on
``Numbers``, the worst word of the real ``assets/panda`` title, rounded up.

It deliberately does not cover an all-caps acronym -- ``WWF`` sets at 0.989em --
because inflating this to 0.99 would shrink every ordinary title by about 40% for
a case no researcher emits. ``.title``'s ``overflow-wrap: anywhere`` is the
backstop there: an under-capped word breaks rather than escaping the page.
"""
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
class PageChrome:
    """The page furniture every layout shares: shell, masthead, apparatus."""

    lang: str
    direction: str
    theme: Theme
    fonts: Sequence[FontFace]
    title: str
    title_scale: Scale
    title_fit: str
    subtitle: str
    summary: str
    references: Sequence[Reference]
    credits: Sequence[Credit]
    width_px: int
    gutter_px: int
    min_height_px: int | None


@dataclass(frozen=True, slots=True)
class StatGridBody:
    """The stat-grid layout's own information architecture."""

    hero: Figure | None
    ledger: Sequence[Sequence[Stat]]
    band: Sequence[Figure]
    sections: Sequence[Section]


@dataclass(frozen=True, slots=True)
class Step:
    """One rung of a process flow."""

    ordinal: int
    """1-based; the number actually printed in the badge. Numbering lives here
    rather than in a CSS counter so the view model is what the reader sees."""
    heading: str
    body: str


@dataclass(frozen=True, slots=True)
class ProcessFlowBody:
    """The process-flow layout: a numbered rail of steps over a fact strip."""

    hero: Figure | None
    steps: Sequence[Step]
    facts: Sequence[Stat]
    """Surviving facts, rendered as a supporting strip -- the sequence carries
    the story but a fact handed to us is still a fact we owe the reader."""
    figures: Sequence[Figure]
    """Non-hero images this body places, and therefore credits."""


@dataclass(frozen=True, slots=True)
class Rank:
    """One row of a ranked list. Position is the rank; nothing else encodes it."""

    ordinal: int
    label: str
    value: str
    unit: str | None
    detail: str | None
    attribution: str | None
    scale: Scale
    """The editorial ceiling on the value's size, as in the ledger."""
    fit: str
    """Width-derived cap on the value's font size, in container units."""


@dataclass(frozen=True, slots=True)
class RankedListBody:
    """The ranked-list layout: an ordered ledger of items, prose beneath."""

    hero: Figure | None
    ranks: Sequence[Rank]
    sections: Sequence[Section]
    figures: Sequence[Figure]


PageBody: TypeAlias = StatGridBody | ProcessFlowBody | RankedListBody
"""One per renderable template. A template reads exactly one of these shapes."""


@dataclass(frozen=True, slots=True)
class Page:
    """Everything the template renders, already decided."""

    chrome: PageChrome
    body: PageBody


_PageBuilder: TypeAlias = Callable[
    [Brief, ResearchContent, Sequence[ImageAsset]], "Page"
]


def build_page(
    brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
) -> Page:
    """Assemble the stat-grid view model. Raises ``OSError`` for an unreadable asset.

    All three bodies do; they did not always. ``process_flow`` and ``ranked_list``
    shipped dropping what they could not read, and that docstring asked for a
    ``ports.py`` conversation before anyone made the two consistent. It happened:
    ``core.ports.Composer`` already documents ``OSError`` for an unreadable
    ``Path`` asset, so the two newer bodies were changed to match and ``ports.py``
    was not. A dropped image is not credited either, which made the quiet path a
    silent success rather than a graceful one.

    Only the assets this body places are encoded, so an unreadable asset past the
    band's capacity costs nothing and raises nothing -- the page never shows it.
    """
    hero, band = _imagery(images)
    body = StatGridBody(
        hero=hero,
        ledger=_ledger(content.facts),
        band=band,
        sections=tuple(_section(section) for section in content.sections),
    )
    return Page(chrome=build_chrome(brief, content, body), body=body)


def build_process_flow_page(
    brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
) -> Page:
    """Sections become numbered steps, in the order the researcher gave them."""
    hero, rest = _all_figures(images)
    body = ProcessFlowBody(
        hero=hero,
        steps=tuple(
            Step(
                ordinal=index,
                heading=_legible_text(section.heading),
                body=_legible_text(section.body),
            )
            for index, section in enumerate(content.sections, start=1)
        ),
        facts=tuple(
            _stat(fact, feature=False, full_width=False) for fact in content.facts
        ),
        figures=rest,
    )
    return Page(chrome=build_chrome(brief, content, body), body=body)


def build_ranked_list_page(
    brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
) -> Page:
    """List order *is* the ranking -- ``ports.py`` already guarantees fact order."""
    hero, rest = _all_figures(images)
    body = RankedListBody(
        hero=hero,
        ranks=tuple(
            _rank(fact, ordinal=index)
            for index, fact in enumerate(content.facts, start=1)
        ),
        sections=tuple(_section(section) for section in content.sections),
        figures=rest,
    )
    return Page(chrome=build_chrome(brief, content, body), body=body)


_BUILDERS: Final[Mapping[str, _PageBuilder]] = MappingProxyType(
    {
        "stat_grid": build_page,
        "process_flow": build_process_flow_page,
        "ranked_list": build_ranked_list_page,
    }
)


def build_page_for(
    template_id: str,
    brief: Brief,
    content: ResearchContent,
    images: Sequence[ImageAsset],
) -> Page:
    """Build the body the named template reads, falling back to ``stat_grid``.

    Never raises on an unrecognised or blocked id: the selection chain is allowed
    to hand us a garbled extras value or a template registered but not yet
    buildable, and neither may cost a render.
    """
    spec = TEMPLATE_REGISTRY.get(template_id)
    if spec is None or spec.blocked_on is not None:
        return build_page(brief, content, images)
    return _BUILDERS.get(spec.id, build_page)(brief, content, images)


def build_chrome(brief: Brief, content: ResearchContent, body: PageBody) -> PageChrome:
    """The furniture every layout shares, plus credits for what ``body`` displays.

    Raises ``TypeError`` if the brief's page geometry is not made of integers --
    see :func:`_css_px`, which is where the render options stop being caller data
    and become CSS lengths.
    """
    title = _page_title(brief, content)
    width_px = _css_px(brief.options.width_px, "width_px")
    return PageChrome(
        lang=_bcp47(brief.locale),
        direction=_direction(brief.locale),
        theme=brief.options.theme,
        fonts=font_faces(),
        title=title,
        title_scale=_scale_for(title, (26, 46, 70)),
        title_fit=_fit(_longest_word(title), _TITLE_ADVANCE),
        subtitle=_legible_text(content.subtitle),
        summary=_legible_text(content.summary),
        references=_references(content),
        credits=_credits_of(body),
        width_px=width_px,
        gutter_px=_gutter(width_px),
        min_height_px=_css_px_or_none(brief.options.height_px, "height_px"),
    )


def _css_px(value: object, field: str) -> int:
    """The one door a caller-supplied pixel count walks through on its way into CSS.

    ``RenderOptions.width_px`` and ``height_px`` are annotated ``int`` and validated
    nowhere -- ``core/`` is pure data by design -- and Python does not enforce an
    annotation, so nothing stops ``RenderOptions(height_px="auto} body{display:none}
    /*")``. Both values are interpolated into ``<style>`` as CSS lengths, and
    ``autoescape=True`` is HTML-only, so a string arriving there closes the rule and
    the rest of the sheet belongs to whoever supplied it. Measured before this
    check existed: that exact value rendered ``body { min-height: auto}
    body{display:none} /*px; }``. These two are the only values in this package that
    reach CSS straight from a caller; every other length in the sheets and in the
    ``style=""`` attributes is a number computed here.

    ``TypeError`` rather than the ``ValueError`` ``int(value)`` would raise, because
    the fault is a type and not a value: the annotation says ``int`` and the caller
    passed something that is not one. It also names the field, which the
    interpreter's own message does not -- a hostile ``width_px`` used to die one
    frame down in :func:`_gutter` as "can't multiply sequence by non-int of type
    'float'", which mentions neither ``RenderOptions`` nor the page width.
    ``int(value)`` would additionally accept ``"1200"`` and truncate ``1200.9``,
    normalising one programming error while exploding on another; the contract is
    ``int``, so the check is "is an int". No fallback and no clamp: a caller asking
    for a nonsense page size hears about it rather than receiving a plausible PNG
    that is not the one they asked for.

    ``bool`` is refused despite being an ``int`` subclass, because it is the one that
    does not render as a number: ``width_px=True`` would emit ``--w: Truepx``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"RenderOptions.{field} must be an int, not "
            f"{type(value).__name__} ({value!r}); it is interpolated into the "
            "stylesheet as a CSS length, which nothing downstream escapes"
        )
    return value


def _css_px_or_none(value: object, field: str) -> int | None:
    """The same, for the height a caller is allowed to omit entirely."""
    return None if value is None else _css_px(value, field)


def _credits_of(body: PageBody) -> tuple[Credit, ...]:
    """Credit exactly the figures the body places, in the order it places them.

    Attribution has to track what is *displayed*: crediting an image nobody can
    see is noise, and the same derivation is what guarantees a displayed one is
    never missed. An asset the body did not place was never encoded either -- one
    past the band's capacity -- so it is neither shown nor credited. A credit with
    nothing in it is dropped too, rather than drawing a ruled row that says
    nothing.

    Nothing stops the imagery stage handing the same photograph over twice, and
    two byte-identical ruled rows read as a bug. So the key is the whole
    :class:`Credit` -- every field the colophon prints -- and two rows collapse
    only when they would render identically. It is deliberately not the licence
    string: two photographs under one licence are two obligations the moment
    their author, work title or source URL differ, and CC BY names the *author*,
    not the licence. Nor does it ignore ``adapted``, because CC BY-SA's duty to
    state that a work was modified attaches to the use and not to the file.
    ``dict.fromkeys`` keeps the first occurrence, which is the order this
    docstring promises.

    The key is every field :class:`Credit` has and therefore every field the
    colophon prints, so "would render identically" and "compares equal" are the
    same statement by construction -- add a field to ``Credit`` and it joins the
    key. The residue is two genuinely different photographs whose attribution
    coincides in all six: same author, same licence, no source either side. They
    collapse to one row, and one row is all the page could say about them anyway.
    """
    return tuple(
        dict.fromkeys(
            figure.credit
            for figure in _figures_of(body)
            if _has_attribution(figure.credit)
        )
    )


def _figures_of(body: PageBody) -> tuple[Figure, ...]:
    """Every figure the body places, hero first."""
    lead: tuple[Figure, ...] = () if body.hero is None else (body.hero,)
    match body:
        case StatGridBody():
            return (*lead, *body.band)
        case ProcessFlowBody() | RankedListBody():
            return (*lead, *body.figures)
        case unreachable:
            assert_never(unreachable)


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
    raw = content.title.strip() or brief.prompt.strip() or "Infographic"
    return _legible_text(raw)


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
# Legibility
# --------------------------------------------------------------------------- #
# Nothing this package renders may contain a character that occupies no column and
# reorders or hides the ones around it. Two classes, because a URL and a sentence do
# not have the same rights: the URL class below is a strict superset of the prose one.
#
# ``Zl``/``Zp`` (U+2028/U+2029) are out of scope for both: they render as a visible
# line break, not as an invisible reordering. Every replacement is one character for
# one character, so no size this module derives from a string length can move.

_REPLACEMENT: Final = "\ufffd"
"""U+FFFD REPLACEMENT CHARACTER: category ``So``, so both helpers below are
idempotent, and visible, so neither of them can hide what it did."""

_ILLEGIBLE_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf"})
"""Every Unicode control and format character, for URLs only. ``Cc`` covers NUL,
``\\t``, ``\\n``, ``\\r`` and U+007F; ``Cf`` covers U+061C, U+200B-U+200F,
U+202A-U+202E, U+2060-U+2064, U+2066-U+2069 and U+FEFF."""

_KEPT_IN_TEXT: Final[frozenset[str]] = frozenset("\t\n\r")
"""The three controls prose is allowed to keep. They are whitespace, so they collapse
in HTML rather than hiding anything, and the templates are full of them."""

_ILLEGIBLE_IN_TEXT: Final[frozenset[str]] = frozenset(
    "\u200b\u202a\u202b\u202c\u202d\u202e\ufeff"
)
"""The ``Cf`` characters prose may not keep: ZWSP, the four bidi embeddings and the
override (U+202A-U+202E), and the BOM.

Everything else in ``Cf`` stays. ZWNJ (U+200C) and ZWJ (U+200D) are orthography in
Persian, Arabic and Indic scripts; LRM (U+200E), RLM (U+200F) and ALM (U+061C) are how
a mixed-direction run is written correctly; the isolates (U+2066-U+2069) scope a
direction change without leaking it, which is the *safe* way to do what the embeddings
do badly. The embeddings and the override are deprecated in Unicode precisely because
they leak, and no legitimate content contains one."""


def _legible_url(url: str) -> str:
    """A URL with every invisible character replaced by U+FFFD, for display only.

    The research zone strips ``Cf`` from a source's title and publisher but
    structurally cannot touch its URL: there the URL is a byte-exact verification
    key, and normalising it would start matching fabricated URLs. So sanitising the
    *rendered* form -- the only form a reader ever sees -- is this zone's job.
    Without it ``https://example.com/`` + U+202E + ``gpj.exe`` displays as though it
    ends in ``exe.jpg``: display spoofing rather than injection, in the one field the
    citation apparatus exists to make trustworthy, and autoescape has nothing to say
    about it.

    Replace, do not delete. A citation URL is a verification key, so silently
    dropping bytes from its rendered form produces a URL that was never published and
    that reads as perfectly legitimate. U+FFFD keeps the rendering honest -- it says a
    character was there and does not print -- which is exactly the signal a reader
    checking a source needs. Deletion would trade a spoof for a quieter spoof.

    Strictly wider than :func:`_legible_text`, and every difference is a character a
    sentence may legitimately contain and a URL may not: a tab, a newline, a ZWNJ, an
    LRM, a bidi isolate. There is no such thing as a URL that needs one, so this asks
    no questions about scripts or direction and replaces the lot.
    """
    return "".join(
        _REPLACEMENT if unicodedata.category(char) in _ILLEGIBLE_CATEGORIES else char
        for char in url
    )


def _legible_text(text: str) -> str:
    """Researched or credited prose with every illegible character replaced by U+FFFD.

    Narrower than :func:`_legible_url` at both ends, and both differences are the
    point. ``\\t``, ``\\n`` and ``\\r`` stay, because they are whitespace in prose and
    in HTML and the templates are made of them. Most of ``Cf`` stays, because ``Cf``
    is where correct text keeps its joiners and its direction marks -- see
    :data:`_ILLEGIBLE_IN_TEXT`. What goes is the rest of ``Cc`` and the deprecated
    bidi embeddings and override, none of which any real title, publisher, author,
    licence or sentence contains.

    This exists because the research zone cleans less than its name suggests. Its
    ``_visible`` drops ``Cf`` only, and ``_clean_title`` is
    ``" ".join(_visible(raw).split())`` -- so ``str.split`` takes out the whitespace
    controls and the other 55 ``Cc`` characters walk straight through into a title or
    a publisher. ``ImageCredit.author`` and ``ImageCredit.license`` come from the
    imagery zone and are cleaned nowhere at all. Measured: a titled hostile source
    plus a hostile author and licence put 13 such characters on a rendered page,
    across the hero caption, the fact and section attributions, the bibliography and
    both halves of the colophon.
    """
    return "".join(map(_shown_in_text, text))


def _legible_optional(text: str | None) -> str | None:
    """The same, for a field the researcher is allowed to omit."""
    return None if text is None else _legible_text(text)


def _shown_in_text(char: str) -> str:
    if char in _KEPT_IN_TEXT:
        return char
    illegible = char in _ILLEGIBLE_IN_TEXT or unicodedata.category(char) == "Cc"
    return _REPLACEMENT if illegible else char


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
        label=_legible_text(fact.label),
        value=_legible_text(fact.value),
        unit=_legible_optional(fact.unit),
        detail=_legible_optional(fact.detail),
        attribution=_attribution(fact.source),
        scale=_scale_for(fact.value, (7, 13, 20)),
        fit=_fit(fact.value, _VALUE_ADVANCE, _unit_width(fact.unit)),
        feature=feature,
        full_width=full_width,
    )


def _unit_width(unit: str | None) -> float:
    return 0.0 if unit is None else len(unit.strip()) * _UNIT_ADVANCE + 0.5


def _rank(fact: Fact, *, ordinal: int) -> Rank:
    return Rank(
        ordinal=ordinal,
        label=_legible_text(fact.label),
        value=_legible_text(fact.value),
        unit=_legible_optional(fact.unit),
        detail=_legible_optional(fact.detail),
        attribution=_attribution(fact.source),
        scale=_scale_for(fact.value, (7, 13, 20)),
        fit=_fit(fact.value, _VALUE_ADVANCE, _unit_width(fact.unit)),
    )


def _attribution(source: Source | None) -> str | None:
    if source is None:
        return None
    return _legible_text(source.publisher or source.title or _host(source.url))


def _host(url: str) -> str:
    """The display host, or the whole URL when there is no host to show.

    Sanitised because it is the fallback for both a reference title and an
    attribution line: an unsanitised host would undo the cleaning the research zone
    already did to ``Source.publisher`` and ``Source.title``.
    """
    return _legible_url(_netloc(url) or url)


def _netloc(url: str) -> str:
    """Empty when the URL has no host, or is malformed enough that parsing raises."""
    try:
        return urlsplit(url).netloc.removeprefix("www.")
    except ValueError:
        return ""


# --------------------------------------------------------------------------- #
# Prose and apparatus
# --------------------------------------------------------------------------- #


def _section(section: NarrativeSection) -> Section:
    return Section(
        heading=_legible_text(section.heading),
        body=_legible_text(section.body),
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
        title=_legible_text(source.title or _host(source.url)),
        publisher=_legible_optional(source.publisher),
        url=_legible_url(source.url),
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


def _all_figures(
    images: Sequence[ImageAsset],
) -> tuple[Figure | None, tuple[Figure, ...]]:
    """Embed every asset given, hero first. Raises ``OSError`` for an unreadable one.

    These bodies place everything they are handed, so there is nothing to choose
    and nothing to defer -- unlike :func:`_imagery`, no asset goes unencoded. A
    ``Path`` whose bytes will not come back fails the page rather than vanishing
    from it: a dropped image is not credited either, so the quiet version loses a
    licensed image with nothing in the colophon to say so. Both "no images at all"
    and "no hero among them" come back as ``None`` for the hero slot, the
    template's cue to lay out text only.
    """
    lead, rest = _split_hero(images)
    if lead is None:
        return None, ()
    return _figure(lead), tuple(map(_figure, rest))


def _hero_index(images: Sequence[ImageAsset]) -> int | None:
    """Lead with the first ``HERO`` asset, else the first asset given.

    ``None`` only when there is nothing to lead with. The rule lives here rather
    than inline in :func:`_split_hero` because it is a layout decision, not a
    slicing detail.
    """
    if not images:
        return None
    return next((i for i, asset in enumerate(images) if asset.role is ImageRole.HERO), 0)


def _split_hero(
    images: Sequence[ImageAsset],
) -> tuple[ImageAsset | None, tuple[ImageAsset, ...]]:
    """The lead asset and the rest, in order."""
    lead = _hero_index(images)
    if lead is None:
        return None, ()
    return images[lead], tuple(a for i, a in enumerate(images) if i != lead)


def _figure(asset: ImageAsset) -> Figure:
    credit = _credit(asset)
    return Figure(
        data_uri=to_data_uri(asset),
        alt=_legible_text(asset.alt_text),
        caption=_caption(credit),
        credit=credit,
        aspect=_aspect(asset.width_px, asset.height_px),
    )


def _credit(asset: ImageAsset) -> Credit:
    source = asset.credit.source
    license_url = asset.credit.license_url
    return Credit(
        license=_legible_text(asset.credit.license),
        work=_legible_optional(source.title) if source else None,
        author=_legible_optional(asset.credit.author),
        license_url=None if license_url is None else _legible_url(license_url),
        source_url=_legible_url(source.url) if source else None,
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
