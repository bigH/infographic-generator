"""Contract tests for the two newer template bodies and the dispatch that picks one.

``process_flow`` and ``ranked_list`` owe their callers everything ``stat_grid``
already owes them -- a self-contained, escaped, attributed document that renders
every fact and every section it is handed -- plus one thing it does not: a
coherent text-only page when there is no image for a slot. The parsing helpers and
hostile payloads are reused from :mod:`tests.test_composition` so both paths are
held to one standard.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from infographic_generator.composition import HtmlComposer
from infographic_generator.composition.layout import (
    ProcessFlowBody,
    RankedListBody,
    StatGridBody,
    build_page,
    build_page_for,
)
from infographic_generator.composition.registry import (
    RENDERABLE_TEMPLATE_IDS,
    TEMPLATE_REGISTRY,
)
from infographic_generator.core.encoding import to_data_uri
from infographic_generator.core.models import (
    Brief,
    Composition,
    Fact,
    ImageAsset,
    ImageCredit,
    ImageRole,
    NarrativeSection,
    RenderOptions,
    ResearchContent,
    Source,
    Theme,
)

from tests.test_composition import (
    ASPECT_RATIO_JS,
    AT_FIT_FLOOR,
    BODY_SELECTORS,
    BROWSER_LOOP,
    MARKUP_PAYLOADS,
    MIN_EXAMINED_BOXES,
    OVERFLOW_JS,
    PANDAS,
    REMOTE_SCHEMES,
    _declared_ratio,
    _fields,
    _number,
    _rows,
    _text,
    assert_structurally_valid,
    chromium,  # noqa: F401 -- referenced only as a fixture name
    compose_cell,
    hostile_inputs,
    laid_out,
    make_brief,
    make_content,
    make_facts,
    make_source,
    parse,
)

if TYPE_CHECKING:
    from playwright.async_api import Browser

NEW_TEMPLATE_IDS = ("process_flow", "ranked_list")
BLOCKED_TEMPLATE_IDS = ("timeline", "comparison", "quote_spotlight")


def make_sections(count: int) -> tuple[NarrativeSection, ...]:
    return tuple(
        NarrativeSection(
            heading=f"Stage {index} of the procedure",
            body=f"What happens at stage {index}, described at some length.",
            sources=(Source(url=f"https://example.org/stage-{index}"),),
        )
        for index in range(1, count + 1)
    )


def panda_assets(*, hero: bool = True) -> tuple[ImageAsset, ...]:
    """The real fixtures, optionally with the first one promoted to ``HERO``."""
    roles = [ImageRole.SUPPORTING] * len(PANDAS)
    if hero:
        roles[0] = ImageRole.HERO
    return tuple(
        panda.as_asset(role=role) for panda, role in zip(PANDAS, roles, strict=True)
    )


def unreadable_asset(missing: Path) -> ImageAsset:
    """A ``Path``-backed asset whose bytes will never arrive."""
    return ImageAsset(
        content=missing,
        mime_type="image/png",
        width_px=1600,
        height_px=1066,
        alt_text="A panda that cannot be read from disk",
        credit=ImageCredit(
            license="CC-BY-UNREADABLE-9.9",
            author="Nonexistent Photographer",
            license_url="https://example.org/licence/unreadable",
            source=Source(url="https://example.org/never-loaded", title="Absent Plate"),
        ),
        role=ImageRole.HERO,
    )


async def render(
    template_id: str,
    brief: Brief | None = None,
    content: ResearchContent | None = None,
    images: Sequence[ImageAsset] = (),
) -> str:
    composition = await HtmlComposer(template_id=template_id).compose(
        brief if brief is not None else make_brief(),
        content if content is not None else make_content(),
        images,
    )
    return composition.html


# --------------------------------------------------------------------------- #
# 1. Both new templates are real documents
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
async def test_new_bodies_render_a_valid_document(template_id: str) -> None:
    brief = make_brief(locale="fr-CA")

    html = await render(template_id, brief=brief, images=panda_assets())
    parsed = assert_structurally_valid(html)

    assert parsed.tagged("html")[0].get("lang") == brief.locale


@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
async def test_new_bodies_fetch_nothing_from_the_network(template_id: str) -> None:
    content = make_content(facts=make_facts(6), sections=make_sections(4))

    html = await render(template_id, content=content, images=panda_assets())
    parsed = parse(html)

    assert not parsed.tagged("link"), "<link> pulls in an external resource"
    assert all("src" not in attrs for attrs in parsed.tagged("script"))
    for url in parsed.fetchable_urls:
        assert not url.lower().startswith(REMOTE_SCHEMES), f"remote URL: {url!r}"
    for url in parsed.css_urls:
        assert url.startswith("data:"), f"CSS url() must be a data URI: {url!r}"
    assert "@import" not in html.lower()
    assert any(url.startswith("data:") for url in parsed.fetchable_urls)


@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
async def test_new_bodies_never_emit_svg(template_id: str) -> None:
    """SVG can carry script and remote references, so no slot may ever hold one."""
    html = await render(template_id, images=panda_assets())

    lowered = html.lower()
    assert "image/svg" not in lowered
    assert "<svg" not in lowered


# --------------------------------------------------------------------------- #
# 2. Completeness -- capping is the researcher's job (ports.py)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
async def test_every_fact_and_section_is_rendered_as_visible_text(
    template_id: str,
) -> None:
    facts = make_facts(10)
    sections = make_sections(8)
    content = make_content(facts=facts, sections=sections)

    html = await render(template_id, content=content, images=panda_assets())
    text = parse(html).text

    missing_facts = [
        fact.label
        for fact in facts
        if fact.label not in text or fact.value not in text
    ]
    missing_sections = [
        section.heading
        for section in sections
        if section.heading not in text or section.body not in text
    ]
    assert not missing_facts, f"facts dropped from {template_id}: {missing_facts}"
    assert not missing_sections, f"sections dropped from {template_id}: {missing_sections}"


# --------------------------------------------------------------------------- #
# 3. Missing imagery: text-only where there is nothing, loud where there is a fault
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
async def test_new_bodies_lay_out_text_only_with_no_images_at_all(
    template_id: str,
) -> None:
    content = make_content(title="Pandas Without Pictures", facts=make_facts(4))

    html = await render(template_id, content=content, images=())
    parsed = assert_structurally_valid(html)

    assert content.title in parsed.text
    assert not parsed.tagged("img"), "there was nothing to show an image of"


@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
async def test_new_bodies_cope_with_images_but_no_hero(template_id: str) -> None:
    images = panda_assets(hero=False)

    html = await render(template_id, images=images)
    parsed = assert_structurally_valid(html)

    embedded = set(parsed.fetchable_urls)
    for asset in images:
        assert to_data_uri(asset) in embedded, "a supplied asset went unused"


@pytest.mark.parametrize("template_id", sorted(RENDERABLE_TEMPLATE_IDS))
async def test_an_unreadable_asset_raises_oserror_from_every_renderable_body(
    template_id: str, tmp_path: Path
) -> None:
    """No body degrades a read failure into a page that quietly lost an image."""
    images = (unreadable_asset(tmp_path / "absent.png"), *panda_assets(hero=False))

    with pytest.raises(OSError, match=r"absent\.png"):
        await render(template_id, images=images)


# --------------------------------------------------------------------------- #
# 4. Attribution
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
async def test_every_displayed_image_is_credited_as_visible_text(
    template_id: str,
) -> None:
    images = panda_assets()

    html = await render(template_id, images=images)
    parsed = parse(html)
    embedded = set(parsed.fetchable_urls)

    displayed = [
        panda
        for panda, asset in zip(PANDAS, images, strict=True)
        if to_data_uri(asset) in embedded
    ]
    assert len(displayed) == len(PANDAS), "every supplied asset should be displayed"
    for panda in displayed:
        assert panda.license_text in parsed.text, f"no licence for {panda.filename}"
        assert panda.author in parsed.text, f"no author for {panda.filename}"
        assert panda.source_url in parsed.text or panda.filename in parsed.text, (
            f"no source or work for {panda.filename}"
        )


# --------------------------------------------------------------------------- #
# 5. Escaping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
async def test_hostile_payloads_are_escaped_by_the_new_bodies(template_id: str) -> None:
    content, images = hostile_inputs()

    html = await render(template_id, content=content, images=images)

    for payload in MARKUP_PAYLOADS:
        assert payload not in html, f"unescaped payload in output: {payload!r}"
    assert "&lt;script&gt;" in html, "the payload should survive in escaped form"

    parsed = parse(html)
    assert not parsed.tagged("script"), "a <script> element reached the document"
    assert all("onerror" not in attrs for _, attrs in parsed.tags)
    for url in (*parsed.fetchable_urls, *parsed.css_urls):
        assert "javascript:" not in url.lower(), f"javascript: URL in the DOM: {url!r}"

    rendered = parsed.rendered_strings
    for payload in MARKUP_PAYLOADS:
        assert payload in rendered, f"escaped payload lost entirely: {payload!r}"


# --------------------------------------------------------------------------- #
# 6. Theme
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
async def test_light_and_dark_are_different_valid_documents(template_id: str) -> None:
    images = panda_assets()
    content = make_content(facts=make_facts(5), sections=make_sections(3))

    light = await render(
        template_id,
        brief=make_brief(options=RenderOptions(theme=Theme.LIGHT)),
        content=content,
        images=images,
    )
    dark = await render(
        template_id,
        brief=make_brief(options=RenderOptions(theme=Theme.DARK)),
        content=content,
        images=images,
    )

    assert_structurally_valid(light)
    assert_structurally_valid(dark)
    assert light != dark, "the theme had no effect on the output"


# --------------------------------------------------------------------------- #
# 7. Dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("template_id", "expected"),
    [
        ("stat_grid", StatGridBody),
        ("process_flow", ProcessFlowBody),
        ("ranked_list", RankedListBody),
    ],
)
def test_dispatch_builds_the_body_the_template_reads(
    template_id: str, expected: type[object]
) -> None:
    page = build_page_for(template_id, make_brief(), make_content(), panda_assets())

    assert isinstance(page.body, expected)


def test_every_renderable_id_has_a_body_builder() -> None:
    for template_id in RENDERABLE_TEMPLATE_IDS:
        page = build_page_for(template_id, make_brief(), make_content(), ())
        assert isinstance(page.body, StatGridBody | ProcessFlowBody | RankedListBody)


@pytest.mark.parametrize(
    "template_id", ["", "  ", "not_a_template", "STAT_GRID", *BLOCKED_TEMPLATE_IDS]
)
def test_unknown_and_blocked_ids_fall_back_to_stat_grid_without_raising(
    template_id: str,
) -> None:
    page = build_page_for(template_id, make_brief(), make_content(), panda_assets())

    assert isinstance(page.body, StatGridBody)


def test_the_blocked_ids_under_test_are_actually_registered_and_blocked() -> None:
    """Guards the tests above against silently testing nothing.

    Walking the tuple is not a pin: emptied, this loop runs zero times and passes,
    while the two dispatch axes it guards keep only their literal ids and stop
    covering a blocked template at all -- six cells that vanish into pytest's
    ``got empty parameter set`` skip, green and silent. So the tuple is pinned by
    equality first, and derived from the registry rather than transcribed, which
    also means unblocking a template fails here exactly once, by name.
    """
    blocked_in_registry = set(TEMPLATE_REGISTRY) - RENDERABLE_TEMPLATE_IDS
    assert set(BLOCKED_TEMPLATE_IDS) == blocked_in_registry, (
        f"BLOCKED_TEMPLATE_IDS is {list(BLOCKED_TEMPLATE_IDS)} but the registry blocks "
        f"{sorted(blocked_in_registry)}: untested "
        f"{sorted(blocked_in_registry - set(BLOCKED_TEMPLATE_IDS))}, stale "
        f"{sorted(set(BLOCKED_TEMPLATE_IDS) - blocked_in_registry)}. Emptied, the two "
        "fall-back axes above quietly stop testing a blocked id and this loop stops "
        "running; a template that just became renderable belongs in NEW_TEMPLATE_IDS "
        "with measured box floors instead"
    )
    assert len(BLOCKED_TEMPLATE_IDS) == len(set(BLOCKED_TEMPLATE_IDS)), (
        f"BLOCKED_TEMPLATE_IDS repeats an id: {list(BLOCKED_TEMPLATE_IDS)}"
    )
    assert RENDERABLE_TEMPLATE_IDS, (
        "no template is renderable, so the axis over sorted(RENDERABLE_TEMPLATE_IDS) "
        "above collapses to an empty parameter set and skips instead of failing"
    )
    for template_id in BLOCKED_TEMPLATE_IDS:
        spec = TEMPLATE_REGISTRY[template_id]
        assert spec.blocked_on is not None


def test_dispatching_to_stat_grid_is_the_default_page_exactly() -> None:
    brief, content = make_brief(), make_content(facts=make_facts(8))
    images = panda_assets()

    assert build_page_for("stat_grid", brief, content, images) == build_page(
        brief, content, images
    )


async def test_composing_stat_grid_by_id_is_byte_identical_to_the_default() -> None:
    brief, content = make_brief(), make_content(facts=make_facts(8))
    images = panda_assets()

    default = await HtmlComposer().compose(brief, content, images)
    by_id = await HtmlComposer(template_id="stat_grid").compose(brief, content, images)

    assert by_id.html == default.html
    assert by_id.title == default.title


@pytest.mark.parametrize("template_id", ["not_a_template", *BLOCKED_TEMPLATE_IDS])
async def test_composing_an_unusable_id_degrades_to_the_default_page(
    template_id: str,
) -> None:
    brief, content = make_brief(), make_content()

    degraded = await HtmlComposer(template_id=template_id).compose(brief, content, ())
    default = await HtmlComposer().compose(brief, content, ())

    assert degraded.html == default.html


# --------------------------------------------------------------------------- #
# 8. What the browser does with hostile content
# --------------------------------------------------------------------------- #
# Everything above reads the markup as a string. A parser cannot see a chip
# sitting 40px outside its strip, a value that overran the column that sizes it,
# or an ``aspect-ratio`` a later rule overrode -- and those are exactly the
# failures adversarial *content* produces in these two bodies. So the same
# hostile shapes get laid out in real chromium and measured.
#
# One axis is crossed and two are pinned, deliberately:
#
# * ``template_id`` is crossed, because a promise about a rendered page is a
#   promise about both bodies.
# * The theme is pinned to ``LIGHT``. ``data-theme`` switches colour tokens and
#   nothing else: ~11,600 computed values were compared light against dark
#   across 82-122 elements per page and none differed. Crossing it would double
#   every cell here for zero geometry, and ``test_composition.py`` already
#   crosses it on ordinary content.
# * The width is pinned to 1200 except in the fit-floor cell below, where 640
#   genuinely reaches different code. Every other 1200-vs-640 difference in
#   these bodies is the shared-chrome title advance, which belongs to the
#   masthead fences in ``test_composition.py`` -- measuring it here would report
#   somebody else's bug twice per body.

CELL_WIDTH_PX: Final = 1200
"""The page width every cell here uses. ``RenderOptions``' own default, so the
matrix measures the geometry the pipeline actually ships."""

NARROW_WIDTH_PX: Final = 640
"""The one second width, used only below the fit floor and only with no images:
a narrow page *with* a hero fails on the shared masthead, not on these bodies."""

ASPECT_TOLERANCE: Final = 0.02
"""Rendered-versus-declared slack for an image's aspect ratio.

The same 0.02 that ``test_composition.py``'s
``test_a_declared_aspect_ratio_governs_the_rendered_image`` uses inline; that
copy is not this module's to name, so if either moves the two want reconciling.
Measured deviation in these cells is under 0.006%, three orders of magnitude of
headroom against a real override."""


def cell_brief(locale: str = "en-US") -> Brief:
    """The brief every adversarial cell renders through: ``LIGHT`` at 1200px."""
    return make_brief(
        locale=locale,
        options=RenderOptions(width_px=CELL_WIDTH_PX, theme=Theme.LIGHT),
    )


def sparse_content(
    *,
    title: str = "The Giant Panda",
    facts: Sequence[Fact] = (),
    sections: Sequence[NarrativeSection] = (),
) -> ResearchContent:
    """``make_content``, but taking an empty ``facts`` or ``sections`` literally.

    ``make_content`` falls back to three facts on a falsy ``facts`` -- so
    ``make_content(facts=())`` renders a three-row body -- and does the same for
    ``sections``. That fallback is load-bearing for three other modules, so this
    goes around it rather than widening it, and goes around it with
    ``dataclasses.replace`` so the subtitle, summary, keywords and bibliography
    stay whatever the shared builder says they are.
    """
    return replace(make_content(title=title), facts=facts, sections=sections)


def bare_fact(index: int) -> Fact:
    """Only the two fields ``Fact`` requires.

    ``unit``, ``detail`` and ``source`` all default to ``None`` and the body owes
    the reader the value without them. ``make_facts`` populates all five
    unconditionally and is imported by three modules, so this is local.
    """
    return Fact(label=f"Bare metric {index:02d}", value=f"{index:02d}7.5")


TITLE_CHARS: Final = 200
SPACED_TITLE: Final = ("Bamboo " * 40)[:TITLE_CHARS]
"""200 characters a headline is allowed to break: ``_longest_word`` caps the
title's font size from ``Bamboo``, and wrapping does the rest."""
TOKEN_TITLE: Final = ("Bamboo" * 40)[:TITLE_CHARS]
"""The same 200 characters as one unbreakable word, which is what
``.title { overflow-wrap: anywhere }`` exists for: before it landed this spilled
786px out of a 714px masthead. The cell asserts nothing about how many lines it
takes -- ``.title``'s ``line-height: 0.98`` overlaps consecutive line boxes, so
naive line counting reports one line for a visibly four-line headline."""

ARABIC_FACTS: Final = (
    Fact(
        label="استهلاك الخيزران اليومي",
        value="١٢-٣٨",
        unit="كجم",
        detail="قياس عبر محميات الصين الوسطى",
        source=make_source(url="https://example.org/ar-intake"),
    ),
    Fact(
        label="وزن المولود الجديد",
        value="١٠٠",
        unit="جرام",
        detail="أقل من واحد على ثمانمائة من وزن أمه",
        source=make_source(url="https://example.org/ar-cub"),
    ),
)
ARABIC_SECTIONS: Final = (
    NarrativeSection(
        heading="الغذاء والموطن",
        body="يقضي الباندا العملاق معظم ساعات يقظته في مضغ الخيزران في غابات الصين.",
        sources=(make_source(url="https://example.org/ar-habitat"),),
    ),
)
"""Arabic facts *and* prose, not just an Arabic locale. Nothing in this repo has
ever composed right-to-left body text, and a right-to-left spill reports as
``past_start`` where a left-to-right one reports ``past_end`` -- so a cell that
only flipped ``<html dir>`` would measure the instrument, not the layout."""


@dataclass(frozen=True, slots=True)
class Shape:
    """One image slot whose declared pixel size is chosen, not defaulted.

    ``PandaFixture.as_asset`` derives its default size from ``credits.json``,
    the authoritative record of what each file really is, and all but one of
    the panda files is 1600x1066 -- so a matrix left on the defaults would hand
    two of these three slots the same 3:2 ratio and prove nothing about shape.
    It would also leave the matrix hostage to the asset files rather than
    stated by the test, which is why every shape declares its own size,
    including the one that restates its file's real 1600x1600.
    """

    asset: ImageAsset
    in_band: bool
    """Whether ``layout``'s clamp leaves :attr:`true_ratio` alone."""

    @property
    def true_ratio(self) -> float:
        """The ratio as *supplied*, before the layout clamps it."""
        return self.asset.width_px / self.asset.height_px


CLAMPED_SHAPES: Final = (
    Shape(PANDAS[1].as_asset(width_px=1000, height_px=1600), in_band=False),
    Shape(PANDAS[2].as_asset(width_px=1600, height_px=1600), in_band=True),
    Shape(PANDAS[0].as_asset(width_px=3000, height_px=1000), in_band=False),
)
"""A portrait below the clamp, a square inside it, and an ultra-wide above it.

"Ultra-wide" cannot be tested as ultra-wide: the clamp collapses 3000x1000 and
2000x1000 to the same declared string, so what is testable is the *clamp
contract* -- the declared string is the bound, and the rendered box obeys the
declared string. The three are also distinguished by ``alt``, which is how each
measured box is matched back to the slot that asked for it."""

CLAMPED_IMAGES: Final = (
    PANDAS[0].as_asset(role=ImageRole.HERO),
    *(shape.asset for shape in CLAMPED_SHAPES),
)
"""The clamped shapes behind a hero, which is where a body places figures. The
hero itself carries no inline ``aspect-ratio`` in any body, so it is *not* in
``CLAMPED_SHAPES`` and no cell may assert that every ``<img>`` declares one."""


FLOOR_VALUE: Final = "26-84 lb of bamboo shoots, culms and leaves every single day"
"""A value long enough that ``_fit`` stops shrinking it and pins the cap to the
floor -- the point at which ``layout`` has decided wrapping is the better answer
than any font size that would fit on one line."""

FLOOR_CONTENT: Final = sparse_content(
    facts=(
        Fact(
            label="Daily bamboo intake",
            value=FLOOR_VALUE,
            unit="kg",
            detail="Measured across the central Chinese reserves",
            source=make_source(url="https://example.org/intake"),
        ),
    ),
    sections=make_sections(4),
)
"""One fact, so "exactly one value at the floor" is a claim about the body and
not about the fixture. Four sections because a page has to lay out enough boxes
for the overflow walk to mean anything."""

FIT_JS = """
(selector) => Array.from(document.querySelectorAll(selector))
  .map(el => ({
    text: el.textContent.trim(),
    fit: el.style.getPropertyValue('--fit').trim(),
  }))
"""

UNIT_SIZE_JS = """
(selector) => Array.from(document.querySelectorAll(selector))
  .map(el => ({
    text: el.textContent.trim(),
    font_px: parseFloat(getComputedStyle(el).fontSize),
  }))
"""
"""The rendered size of every unit suffix on the page.

Only meaningful next to a value the floor has caught. Both bodies size the suffix as a
fraction of the value it follows -- ``0.28em`` in ``ranked_list``, ``0.3em`` in
``process_flow`` -- so a value that has shrunk takes the suffix down with it, and the
one fixture that drives a value to its floor is the one that can see it."""

SMALLEST_TYPE_PX: Final = 10.5
"""The smallest size anything on this page is set at: ``.tick``, ``.plates
figcaption`` and ``.credit__url`` are all 10.5px, and the last of those is a licence
URI a reader has to transcribe out of a PNG by hand. Nothing may render smaller than
the type the design already asks that much of."""


@dataclass(frozen=True, slots=True)
class Annotations:
    """The three optional lines a body prints with a display value."""

    unit: str
    detail: str
    source: str


def annotations_of(template_id: str) -> Annotations:
    """Derived from ``BODY_SELECTORS`` so it cannot name a body's markup twice.

    ``.chip__value`` and ``.rank__value`` each share their stem with the three
    optional lines beside them, so the stem is the only thing worth writing
    down -- and it is already written down once, in ``BODY_SELECTORS``.
    """
    value = BODY_SELECTORS[template_id].value
    stem = value.removesuffix("__value")
    assert stem != value, (
        f"{template_id}'s display value is {value!r}, which does not end in "
        "'__value', so the unit, detail and source selectors below cannot be "
        "derived from it and every annotation count silently measures zero"
    )
    return Annotations(f"{stem}__unit", f"{stem}__detail", f"{stem}__src")


CENSUS_JS = """
(sel) => {
  const count = q => document.querySelectorAll(q).length;
  return {
    hero: count('.hero'),
    images: count('img'),
    figures: count('figure'),
    credits: count('.credit'),
    values: count(sel.value),
    units: count(sel.unit),
    details: count(sel.detail),
    sources: count(sel.source),
    lang: document.documentElement.lang,
    direction: getComputedStyle(document.documentElement).direction,
  };
}
"""
"""What the page is actually made of. ``not offenders`` is green for a page with
nothing on it, so no overflow cell is allowed to run without also counting the
things it was supposed to be laying out."""


@dataclass(frozen=True, slots=True)
class Census:
    """``CENSUS_JS``'s counts for one page."""

    hero: int
    images: int
    figures: int
    credits: int
    values: int
    units: int
    details: int
    sources: int
    lang: str
    direction: str


def read_census(measured: Mapping[str, object]) -> Census:
    return Census(
        hero=int(_number(measured["hero"])),
        images=int(_number(measured["images"])),
        figures=int(_number(measured["figures"])),
        credits=int(_number(measured["credits"])),
        values=int(_number(measured["values"])),
        units=int(_number(measured["units"])),
        details=int(_number(measured["details"])),
        sources=int(_number(measured["sources"])),
        lang=_text(measured["lang"]),
        direction=_text(measured["direction"]),
    )


def describe_offenders(offenders: Sequence[Mapping[str, object]]) -> str:
    """``OVERFLOW_JS``'s rows as one line each, for a failure message."""
    return "\n".join(
        f"  {row['element']} overflows {row['parent']} "
        f"({row['problem']}) by {row['pixels']}px"
        for row in offenders
    )


@dataclass(frozen=True, slots=True)
class Case:
    """One hostile page, and everything it should be made of once laid out."""

    id: str
    brief: Brief
    content: ResearchContent
    images: tuple[ImageAsset, ...]
    min_boxes: int
    """Floor on the non-zero-size boxes the overflow walk judges.

    An empty body block does *not* collapse the page to nothing. Only the
    ``.plates`` figures, the steps or ranks, and the lede live in
    ``{% block body %}``; the masthead -- hero ``<figure>`` included -- the
    bibliography and the colophon are all shared chrome, and the colophon alone
    runs about seven elements per credit row. Measured with the body block's
    output excised, an otherwise identical page still lays out **37-45 boxes**,
    not the "near ten" that ``MIN_EXAMINED_BOXES`` claims: ten is only what
    ``images=()`` gives, because that is the one case with no hero and no
    colophon to survive.

    So each floor is set near the midpoint between the gutted page and the real
    one, which leaves both margins visible in :data:`ADVERSARIAL_CASES`.
    """
    hero: int
    """``.hero`` figures. One whenever any image was supplied, because
    ``_hero_index`` promotes the first asset when none claims the role; only
    ``images=()`` flattens the masthead."""
    figures: int
    """``<figure>`` elements, and equally ``<img>`` elements: these bodies place
    every asset they are handed, hero first, with no band capacity.

    This is the count that carries the cases where an empty page would satisfy
    :attr:`values` and :attr:`annotated` by design. A gutted page keeps the hero
    but loses the ``.plates`` figures, so it reports one figure against three
    while still claiming three colophon rows -- caught here rather than left to
    :attr:`min_boxes` alone. Conversely ``images=()`` legitimately expects zero
    figures, and there :attr:`values` is what catches it."""
    credits: int
    """Colophon rows, which are *not* one per figure. Credits collapse on the
    whole ``Credit``, so six images drawn from three fixtures owe three rows."""
    values: int
    """Display values, one per fact in both bodies."""
    annotated: int
    """How many of those values carry a unit, a detail *and* a source line --
    all three counted separately, so a blank-field case proves it blanked
    something rather than proving the selectors were wrong."""
    direction: str = "ltr"


ADVERSARIAL_CASES: Final = (
    Case(
        id="baseline",
        brief=cell_brief(),
        content=make_content(facts=make_facts(5)),
        images=panda_assets(),
        min_boxes=60,
        hero=1,
        figures=3,
        credits=3,
        values=5,
        annotated=5,
    ),
    Case(
        id="no-facts",
        brief=cell_brief(),
        content=sparse_content(facts=(), sections=make_sections(2)),
        images=panda_assets(),
        min_boxes=50,
        hero=1,
        figures=3,
        credits=3,
        values=0,
        annotated=0,
    ),
    Case(
        id="one-fact",
        brief=cell_brief(),
        content=make_content(facts=make_facts(1)),
        images=panda_assets(),
        min_boxes=50,
        hero=1,
        figures=3,
        credits=3,
        values=1,
        annotated=1,
    ),
    Case(
        id="23-facts",
        brief=cell_brief(),
        content=make_content(facts=make_facts(23), sections=make_sections(4)),
        images=panda_assets(),
        min_boxes=160,
        hero=1,
        figures=3,
        credits=3,
        values=23,
        annotated=23,
    ),
    Case(
        id="short-title",
        brief=cell_brief(),
        content=make_content(title="Pandas Eat Bamboo", facts=make_facts(4)),
        images=panda_assets(),
        min_boxes=55,
        hero=1,
        figures=3,
        credits=3,
        values=4,
        annotated=4,
    ),
    Case(
        id="title-200-spaced",
        brief=cell_brief(),
        content=make_content(title=SPACED_TITLE, facts=make_facts(4)),
        images=panda_assets(),
        min_boxes=55,
        hero=1,
        figures=3,
        credits=3,
        values=4,
        annotated=4,
    ),
    Case(
        id="title-200-token",
        brief=cell_brief(),
        content=make_content(title=TOKEN_TITLE, facts=make_facts(4)),
        images=panda_assets(),
        min_boxes=55,
        hero=1,
        figures=3,
        credits=3,
        values=4,
        annotated=4,
    ),
    Case(
        id="rtl-arabic",
        brief=cell_brief(locale="ar"),
        content=sparse_content(
            title="الباندا العملاق",
            facts=ARABIC_FACTS,
            sections=ARABIC_SECTIONS,
        ),
        images=panda_assets(),
        min_boxes=55,
        hero=1,
        figures=3,
        credits=3,
        values=2,
        annotated=2,
        direction="rtl",
    ),
    Case(
        id="no-images",
        brief=cell_brief(),
        content=make_content(facts=make_facts(5)),
        images=(),
        min_boxes=40,
        hero=0,
        figures=0,
        credits=0,
        values=5,
        annotated=5,
    ),
    Case(
        id="six-images",
        brief=cell_brief(),
        content=make_content(facts=make_facts(5)),
        images=(*panda_assets(), *panda_assets(hero=False)),
        min_boxes=65,
        hero=1,
        figures=6,
        credits=3,
        values=5,
        annotated=5,
    ),
    Case(
        id="no-hero",
        brief=cell_brief(),
        content=make_content(facts=make_facts(5)),
        images=panda_assets(hero=False),
        min_boxes=60,
        hero=1,
        figures=3,
        credits=3,
        values=5,
        annotated=5,
    ),
    Case(
        id="bare-fields",
        brief=cell_brief(),
        content=sparse_content(
            facts=tuple(bare_fact(index) for index in range(1, 5)),
            sections=make_sections(2),
        ),
        images=panda_assets(),
        min_boxes=55,
        hero=1,
        figures=3,
        credits=3,
        values=4,
        annotated=0,
    ),
    Case(
        id="clamped-shapes",
        brief=cell_brief(),
        content=make_content(facts=make_facts(5)),
        images=CLAMPED_IMAGES,
        min_boxes=60,
        hero=1,
        figures=4,
        credits=3,
        values=5,
        annotated=5,
    ),
)
"""Twelve hostile pages and the ordinary one they are measured against.

Boxes laid out, as *real page* (``process_flow`` / ``ranked_list``) versus the
same page with ``{% block body %}``'s output excised, versus the floor:

===================  ===========  =========  =======  ======
case                 real         empty body   floor   gap
===================  ===========  =========  =======  ======
baseline             85 / 98             37      60      23
no-facts             61 / 61             41      50       9
one-fact             61 / 62             37      50      13
23-facts            216 / 283            45     160     115
short-title          79 / 89             37      55      18
title-200-spaced     79 / 89             37      55      18
title-200-token      79 / 89             37      55      18
rtl-arabic           69 / 73             39      55      16
no-images            51 / 64             10      40      30
six-images           94 / 107            37      65      28
no-hero              85 / 98             37      60      23
bare-fields          76 / 86             41      55      14
clamped-shapes       88 / 101            37      60      23
===================  ===========  =========  =======  ======

``gap`` is the floor's margin over a page whose body rendered nothing, which is
the failure the floor exists to catch; the margin the other way -- real minus
floor -- is the room ordinary restyling has before the floor cries wolf, and it
is 11 boxes at the tightest (``no-facts``, ``one-fact``). ``no-images`` is the
only case that really does fall to ten, because it is the only one with no hero
and no colophon to survive the gutting."""

ADVERSARIAL_CASE_IDS: Final = tuple(case.id for case in ADVERSARIAL_CASES)


EXPECTED_NEW_TEMPLATE_IDS: Final = frozenset({"process_flow", "ranked_list"})
"""What ``NEW_TEMPLATE_IDS`` must contain, spelled out where a test can check it.

``NEW_TEMPLATE_IDS`` is the parametrize axis for sixteen tests in this module and
nothing else in the repo pins its contents. Empty it and every one of them
becomes pytest's ``got empty parameter set`` skip -- green, silent, measuring
nothing at all."""


def test_the_adversarial_matrix_still_covers_every_case_and_both_bodies() -> None:
    """Both axes are pinned by *content*, so an accidental deletion fails.

    A subset check is not a pin: ``set(()) <= anything`` is ``True``, so an
    emptied axis would sail through one. Each axis therefore gets an equality
    against a hand-written expectation, and the subset check stays on as the
    separate claim it always was -- that every body under test declares the
    selectors the browser cells read it through.
    """
    assert set(NEW_TEMPLATE_IDS) == set(EXPECTED_NEW_TEMPLATE_IDS), (
        f"NEW_TEMPLATE_IDS is now {list(NEW_TEMPLATE_IDS)}, not "
        f"{sorted(EXPECTED_NEW_TEMPLATE_IDS)}. Emptied, every parametrized test in "
        "this module skips instead of failing; changed, the new body needs measured "
        "box floors before it can join the matrix"
    )
    assert len(NEW_TEMPLATE_IDS) == len(set(NEW_TEMPLATE_IDS)), (
        f"NEW_TEMPLATE_IDS repeats a body: {list(NEW_TEMPLATE_IDS)}"
    )
    assert set(NEW_TEMPLATE_IDS) <= set(BODY_SELECTORS), (
        f"{sorted(set(NEW_TEMPLATE_IDS) - set(BODY_SELECTORS))} have no display-value "
        "selectors, so every browser cell below reads shared chrome only"
    )
    assert len(ADVERSARIAL_CASES) == 13, (
        f"the matrix now has {len(ADVERSARIAL_CASES)} cases, not the 13 designed: "
        f"{list(ADVERSARIAL_CASE_IDS)}"
    )
    assert len(set(ADVERSARIAL_CASE_IDS)) == len(ADVERSARIAL_CASES), (
        f"duplicate case ids in {list(ADVERSARIAL_CASE_IDS)}: pytest numbers the "
        "collisions, so the report stops naming which hostile page failed"
    )
    below_floor = {
        case.id: case.min_boxes
        for case in ADVERSARIAL_CASES
        if case.min_boxes < MIN_EXAMINED_BOXES
    }
    assert not below_floor, (
        f"{below_floor} sit under the shared {MIN_EXAMINED_BOXES}-box floor, so those "
        "cells would accept a page barer than every other fence in this repo tolerates"
    )
    assert any(case.values == 0 for case in ADVERSARIAL_CASES), (
        "no case renders zero display values, so the emptiness the fixtures cannot "
        "produce by themselves is not being tested at all"
    )
    resting_on_the_floor = [
        case.id
        for case in ADVERSARIAL_CASES
        if case.values == 0 and case.figures == 0
    ]
    assert not resting_on_the_floor, (
        f"{resting_on_the_floor} expect neither a display value nor a figure, so a "
        "page whose body rendered nothing satisfies every count and only min_boxes "
        "stands between this fence and a vacuous pass"
    )


def test_the_hostile_fixtures_can_still_prove_what_they_claim() -> None:
    """Each fixture's defining property, checked without a browser.

    A 200-character "single token" that quietly gained a space, or two clamped
    shapes that ended up sharing an ``alt``, would leave the cells below green
    and measuring something else.
    """
    # Not true by construction: both are slices, so too small a repeat count
    # yields a title shorter than the slice asked for and silently stops being
    # the 200-character headline these two cases are named after.
    assert len(SPACED_TITLE) == len(TOKEN_TITLE) == TITLE_CHARS, (
        f"titles are {len(SPACED_TITLE)} and {len(TOKEN_TITLE)} characters, not "
        f"{TITLE_CHARS}: the repeated word no longer reaches the slice length"
    )
    assert " " in SPACED_TITLE, "the spaced title has nothing to break on"
    assert " " not in TOKEN_TITLE, (
        f"the single-token title contains a space, so it is {len(TOKEN_TITLE.split())} "
        "breakable words and no longer tests overflow-wrap at all"
    )
    alts = tuple(shape.asset.alt_text for shape in CLAMPED_SHAPES)
    assert len(set(alts)) == len(alts), (
        f"clamped shapes share an alt ({alts}), and alt is how a measured box is "
        "matched to the slot that declared it"
    )
    assert sum(not shape.in_band for shape in CLAMPED_SHAPES) >= 2, (
        "fewer than two shapes fall outside the clamp band, so the clamp contract "
        "is not under test"
    )
    assert len(FLOOR_CONTENT.facts) == 1, (
        f"the fit-floor fixture carries {len(FLOOR_CONTENT.facts)} facts, so "
        '"exactly one value at the floor" stops being a claim about the body'
    )
    for template_id in NEW_TEMPLATE_IDS:
        annotations = annotations_of(template_id)
        assert all((annotations.unit, annotations.detail, annotations.source)), (
            f"{template_id} derives a blank annotation selector {annotations}, and "
            "querySelectorAll raises on an empty string: the cell would die as a raw "
            "playwright SyntaxError naming nothing"
        )


async def compose_case(template_id: str, case: Case) -> Composition:
    """``compose_cell`` with the case's own brief.

    ``compose_cell`` builds its brief internally from a theme and a width, so a
    locale cannot reach it -- and the locale is what decides ``<html lang>`` and
    the writing direction. Everything else about this is ``compose_cell``.
    """
    return await HtmlComposer(template_id=template_id).compose(
        case.brief, case.content, case.images
    )


@pytest.mark.parametrize("case", ADVERSARIAL_CASES, ids=ADVERSARIAL_CASE_IDS)
@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
@BROWSER_LOOP
async def test_adversarial_content_never_spills_a_box_in_the_new_bodies(
    chromium: Browser, template_id: str, case: Case
) -> None:
    """No hostile page puts an element outside the box it was laid out in.

    A screenshot has no scrollbars: whatever leaves its parent is clipped away
    or painted over a neighbour. The content is hostile in the ways a researcher
    can actually be hostile -- nothing to say, far too much to say, a headline
    that is one 200-character word, a right-to-left script, blank optional
    fields, six images or none.

    The census beside the overflow walk is not decoration. Wrapping either
    body's ``{% block body %}`` in ``{% if false %}`` drops the walk to ten
    boxes and zero offenders: vacuously green. So every cell states what the
    page should be made of, and reads the body-specific half of that through
    ``BODY_SELECTORS`` rather than through shared chrome.
    """
    selectors = BODY_SELECTORS[template_id]
    annotations = annotations_of(template_id)
    composition = await compose_case(template_id, case)

    async with laid_out(chromium, composition) as page:
        spill: dict[str, object] = await page.evaluate(OVERFLOW_JS)
        counted: dict[str, object] = await page.evaluate(
            CENSUS_JS,
            {
                "value": selectors.value,
                "unit": annotations.unit,
                "detail": annotations.detail,
                "source": annotations.source,
            },
        )

    examined = int(_number(spill["examined"]))
    offenders = [_fields(row) for row in _rows(spill["offenders"])]
    census = read_census(counted)

    assert examined >= case.min_boxes, (
        f"{template_id}/{case.id} laid out {examined} boxes, under the "
        f"{case.min_boxes} this cell needs to mean anything: the body rendered next "
        "to nothing, and a page with nothing on it overflows nothing"
    )
    assert census.hero == case.hero, (
        f"{template_id}/{case.id}: {census.hero} .hero figures, expected "
        f"{case.hero} from {len(case.images)} supplied images"
    )
    assert (census.figures, census.images) == (case.figures, case.figures), (
        f"{template_id}/{case.id}: {census.figures} <figure> and {census.images} "
        f"<img>, expected {case.figures} of each -- these bodies place every asset "
        "they are handed"
    )
    assert census.credits == case.credits, (
        f"{template_id}/{case.id}: {census.credits} colophon rows for "
        f"{census.figures} figures, expected {case.credits}"
    )
    assert census.values == case.values, (
        f"{template_id}/{case.id}: {selectors.value!r} matched {census.values} "
        f"elements, expected one per fact ({case.values})"
    )
    assert (census.units, census.details, census.sources) == (case.annotated,) * 3, (
        f"{template_id}/{case.id}: {census.units} {annotations.unit!r}, "
        f"{census.details} {annotations.detail!r} and {census.sources} "
        f"{annotations.source!r}, expected {case.annotated} of each"
    )
    assert census.lang == case.brief.locale, (
        f"{template_id}/{case.id}: <html lang> is {census.lang!r}, not the brief's "
        f"{case.brief.locale!r}"
    )
    assert census.direction == case.direction, (
        f"{template_id}/{case.id}: locale {case.brief.locale!r} computes "
        f"direction: {census.direction}, expected {case.direction} -- a "
        "right-to-left page laid out left-to-right reads as gibberish, and its "
        "spill would report as past_start where this one looked for past_end"
    )
    assert not offenders, (
        f"in {template_id}/{case.id}, elements overflow their parents:\n"
        + describe_offenders(offenders)
    )


@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
@BROWSER_LOOP
async def test_a_clamped_aspect_ratio_still_governs_the_rendered_box(
    chromium: Browser, template_id: str
) -> None:
    """The clamp decides the declared ratio, and the declared ratio decides the box.

    ``layout`` clamps a declared aspect ratio into a band, which means the number
    in the markup is frequently *not* the shape the imagery stage handed over.
    Two promises follow, and both are asserted: an out-of-band shape is moved
    toward square rather than honoured or dropped, and whatever the clamp
    settled on is what the rendered box obeys. ``img { height: 100% }`` used to
    beat the inline ``aspect-ratio`` and crop a figure by about 37%.
    """
    composition = await compose_cell(
        template_id,
        Theme.LIGHT,
        make_content(facts=make_facts(5)),
        CLAMPED_IMAGES,
        width_px=CELL_WIDTH_PX,
    )

    async with laid_out(chromium, composition) as page:
        boxes = [_fields(row) for row in _rows(await page.evaluate(ASPECT_RATIO_JS))]

    assert len(boxes) == len(CLAMPED_SHAPES), (
        f"{template_id}: {len(boxes)} images carry an inline aspect-ratio, expected "
        f"{len(CLAMPED_SHAPES)} -- the hero never declares one, so a count that "
        "moved means a figure went unplaced or the hero started declaring it"
    )
    by_alt = {_text(box["alt"]): box for box in boxes}
    assert len(by_alt) == len(boxes), (
        f"{template_id}: measured images share an alt, so a box cannot be matched "
        "back to the shape that declared it"
    )
    declared_strings = {_text(box["declared"]) for box in boxes}
    assert len(declared_strings) == len(CLAMPED_SHAPES), (
        f"{template_id}: {len(boxes)} images declare only {declared_strings}, so a "
        "single rendered height could satisfy all of them and this cell could not "
        "tell a governing aspect-ratio from a shared height"
    )

    for shape in CLAMPED_SHAPES:
        alt = shape.asset.alt_text
        assert alt in by_alt, (
            f"{template_id}: no measured box for {alt!r}, so a "
            f"{shape.asset.width_px}x{shape.asset.height_px} slot was never placed"
        )
        box = by_alt[alt]
        declared = _declared_ratio(_text(box["declared"]))
        if shape.in_band:
            assert abs(declared - shape.true_ratio) <= 1e-4, (
                f"{template_id}: {alt!r} was supplied at {shape.true_ratio:.4f}, "
                f"inside the clamp band, but declares {declared:.4f}"
            )
        else:
            assert abs(declared - shape.true_ratio) > 1e-4, (
                f"{template_id}: {alt!r} declares its supplied "
                f"{shape.true_ratio:.4f} unclamped, so an ultra-wide or ultra-tall "
                "asset reaches the page at its own extreme shape"
            )
            low, high = sorted((1.0, shape.true_ratio))
            assert low <= declared <= high, (
                f"{template_id}: {alt!r} was supplied at {shape.true_ratio:.4f} and "
                f"declares {declared:.4f}, which is not between square and the "
                "shape supplied -- the clamp moved it the wrong way"
            )

        width, height = _number(box["width"]), _number(box["height"])
        assert height > 0, f"{template_id}: {alt!r} rendered with no height"
        rendered = width / height
        assert abs(rendered / declared - 1) <= ASPECT_TOLERANCE, (
            f"{template_id}: {alt!r} declares aspect-ratio {box['declared']!r} "
            f"({declared:.4f}) but rendered {width:.1f}x{height:.1f} = "
            f"{rendered:.4f}, a {abs(rendered / declared - 1):.1%} distortion: some "
            "other rule is deciding its height"
        )


@pytest.mark.parametrize("width_px", [CELL_WIDTH_PX, NARROW_WIDTH_PX])
@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
@BROWSER_LOOP
async def test_a_value_below_the_fit_floor_wraps_without_leaving_its_box(
    chromium: Browser, template_id: str, width_px: int
) -> None:
    """Past the fit floor the value may wrap -- it may not escape.

    ``_fit`` will not shrink a display value below ``_MIN_FIT_CQW``, so a long
    enough value sits at the floor and *will* break across lines. That is the
    designed outcome, so this cell asserts nothing about line count: only that
    nothing leaves the box it was laid out in, which is the promise the floor
    does not suspend.

    Both widths, because this is the one place a narrower page reaches different
    behaviour rather than the shared masthead: the same value broke across two
    lines at 1200px and three at 640px. ``images=()`` keeps the masthead flat so
    the title's own advance stays out of the measurement.
    """
    selectors = BODY_SELECTORS[template_id]
    composition = await compose_cell(
        template_id, Theme.LIGHT, FLOOR_CONTENT, (), width_px=width_px
    )

    async with laid_out(chromium, composition) as page:
        spill: dict[str, object] = await page.evaluate(OVERFLOW_JS)
        values = [
            _fields(row)
            for row in _rows(await page.evaluate(FIT_JS, selectors.value))
        ]
        units = [
            _fields(row)
            for row in _rows(
                await page.evaluate(UNIT_SIZE_JS, annotations_of(template_id).unit)
            )
        ]

    examined = int(_number(spill["examined"]))
    offenders = [_fields(row) for row in _rows(spill["offenders"])]

    assert examined >= MIN_EXAMINED_BOXES, (
        f"{template_id} at {width_px}px laid out {examined} boxes, under "
        f"{MIN_EXAMINED_BOXES}: a page with nothing on it overflows nothing"
    )
    assert len(values) == len(FLOOR_CONTENT.facts), (
        f"{template_id} at {width_px}px: {selectors.value!r} matched {len(values)} "
        f"elements, expected one per fact ({len(FLOOR_CONTENT.facts)})"
    )
    at_floor = [row for row in values if _text(row["fit"]) == AT_FIT_FLOOR]
    assert len(at_floor) == len(values), (
        f"{template_id} at {width_px}px: only {len(at_floor)} of {len(values)} values "
        f"sit at the {AT_FIT_FLOOR} floor -- "
        + str([(_text(row["text"]), _text(row["fit"])) for row in values])
        + ". This fixture is no longer out of envelope, so the cell has stopped "
        "testing what happens past the floor and duplicates the no-wrap fence instead"
    )
    assert not offenders, (
        f"in {template_id} at {width_px}px, a value pinned to the {AT_FIT_FLOOR} fit "
        "floor pushed elements out of their parents:\n"
        + describe_offenders(offenders)
    )

    assert len(units) == len(FLOOR_CONTENT.facts), (
        f"{template_id} at {width_px}px: {annotations_of(template_id).unit!r} matched "
        f"{len(units)} elements, expected one per fact ({len(FLOOR_CONTENT.facts)}) -- "
        "this fixture is the only one whose value reaches the floor, so at zero the "
        "suffix size below is asserted nowhere in the suite"
    )
    illegible = [
        (_text(row["text"]), _number(row["font_px"]))
        for row in units
        if _number(row["font_px"]) < SMALLEST_TYPE_PX
    ]
    assert not illegible, (
        f"{template_id} at {width_px}px: {illegible} -- a unit suffix is sized as a "
        f"fraction of the value it follows, so a value the floor has caught drags its "
        f"suffix under {SMALLEST_TYPE_PX}px, which is smaller than any type this design "
        "sets. Without an absolute floor of its own the suffix reads as 5-6px in the "
        "PNG: the number keeps its unit and the unit stops being readable"
    )
