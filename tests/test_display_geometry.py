"""Rendered-geometry fences for three defects that made the PNG itself wrong.

Every promise here is about *measured geometry* -- a scroll width, a computed font
size in pixels, a painted content box -- and not about a declaration existing. That
distinction is the whole reason this file exists: the three defects below all shipped
past a suite that checked a class name, a stylesheet substring or a declared
attribute, and every one of those checks stayed green while the page came out broken.

1. **An unbreakable credit token widened the page.** ``body`` is ``width: var(--w)``
   with no ``overflow: hidden``, so a scraped author or licence arriving as one
   space-free run does not clip -- it pushes the document wider and the renderer
   screenshots the wider box. Measured at a 1200px page before ``overflow-wrap:
   anywhere`` reached ``.credit``, ``.tick``, ``.hero__credit`` and ``.band
   figcaption``: a 200-character author took ``scrollWidth`` to 1598 (a 3196px PNG),
   800 to 6177, 4000 to 30596, and 10240 to a 156426x2248 PNG -- on a taller page,
   to ``RuntimeError: Unable to capture screenshot``.

2. **The auto-fitted display size had no lower bound.** ``--fit`` is a ``cqw``, a
   proportion of a container with no floor of its own, and ``.fitted`` was
   ``min(--size, --fit)``. Measured title size: **0.00px at every width from 0 to
   120**, 1.19px at 150, 6.67px at 200 -- and 0.00 / 1.67 / 9.33px at 100 / 150 / 200
   on this file's own ten-fact fixture with the floor term deleted again, in all three
   bodies. A heading painted at zero pixels is not a small heading; it is a page that
   silently lost its title.

3. **No ``object-fit``, so a lying declared size stretched a licensed photograph.**
   The figure box is sized from ``ImageAsset.width_px``/``height_px``, which
   ``layout._aspect`` clamps into ``[0.68, 1.85]`` -- so a true 9:16 portrait is
   handed a 0.68 box and the ``object-fit: fill`` default squeezed the photograph
   about 21% to match. Distorting a CC BY work misstates what was licensed, which is
   nearer an attribution fault than a rag.

The browser machinery -- the module-scoped chromium, ``laid_out``, the fixture
builders and the body matrix -- is :mod:`tests.test_composition`'s, imported by name
the way :mod:`tests.test_template_bodies` and :mod:`tests.test_palette_fence` import
it, so all four modules measure the same page the same way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import pytest

from infographic_generator.core.models import (
    ImageAsset,
    ImageCredit,
    ImageRole,
    ResearchContent,
    Source,
    Theme,
)

from tests.test_composition import (
    BODIES,
    BROWSER_LOOP,
    PANDA_SET,
    PANDAS,
    TEMPLATE_IDS,
    _fields,
    _number,
    _rows,
    _text,
    chromium,  # noqa: F401 -- referenced only as a fixture name
    compose_cell,
    laid_out,
    make_content,
    make_facts,
)

if TYPE_CHECKING:
    from playwright.async_api import Browser


# --------------------------------------------------------------------------- #
# Shared fixture shape
# --------------------------------------------------------------------------- #

FACT_COUNT: Final = 10
"""Ten facts, which is what the real panda content carries.

It also fixes every element count this file asserts against: ten display values plus
the title is eleven ``.fitted`` elements, and ten fact sources are ten ``*__src``
lines inside the thirteen ``.tick`` on the page."""

FITTED_PER_PAGE: Final = 1 + FACT_COUNT
"""The title and one display value per fact. Derived rather than written down as 11,
so a fixture that changes its fact count cannot leave this asserting the old number."""

THEME: Final = Theme.LIGHT
"""One theme. Every measurement in this file is a length, a font size or an aspect
ratio, and a theme changes only which colour tokens resolve -- ``tests/
test_palette_fence.py`` is where the two themes have to be crossed. Rendering the 150
browser cells below twice would double a 25-second module to re-measure identical
geometry."""


def base_content() -> ResearchContent:
    """``make_content`` with a full ledger, and with its sources left alone."""
    return make_content(facts=make_facts(FACT_COUNT))


# --------------------------------------------------------------------------- #
# 1. An unbreakable token in a credit
# --------------------------------------------------------------------------- #
# Five fields, because "the credit apparatus wraps" is five separate rules in three
# stylesheets and they were not all present: `.credit` in `_chrome_apparatus.css`,
# `.tick` and `.hero__credit` in `_chrome.css`, `.band figcaption` in
# `stat_grid.css`. A single injection site would have fenced one of them.

IN_ENVELOPE_CHARS: Final = 160
"""The longest author the imagery zone will hand over: ``imagery/licensing.py``'s
``MAX_AUTHOR_CHARS``. Written down rather than imported, because a collection-time
``ImportError`` from another zone would take this whole file out over a rename that
has nothing to do with page geometry -- but it is the length that matters most, since
it is the only one that needs no bug upstream to arrive. Measured before the fix: a
160-character author still widened the page 68%."""

TOKEN_LENGTHS: Final = (IN_ENVELOPE_CHARS, 200, 800, 4000)
"""The three lengths whose damage was measured, plus the in-envelope one.

Not 10240, which produced a 156426px-wide PNG and, on a taller page, a chromium
screenshot failure: the numbers this file asserts do not need a cell that takes
seconds to lay out to say the same thing."""


def unbroken_run(length: int) -> str:
    """``length`` characters with nothing to break on.

    Alternating case rather than one repeated letter so a failure message shows
    where a truncation landed, and no space, hyphen or slash anywhere: this string
    exists to have no break opportunity at all, which is what ``overflow-wrap:
    anywhere`` is the only rule that helps with.
    """
    return ("Aa" * (length // 2 + 1))[:length]


def spaced_run(length: int) -> str:
    """The same length, breakable every third character.

    The control. It was green before the fix and is green after it, so a cell that
    passes for the wrong reason -- a payload that never reached the DOM, a colophon
    that stopped rendering -- passes here identically and the pair says nothing.
    The trailing character is forced non-blank so the payload can be looked for
    verbatim in ``textContent``.
    """
    run = ("Aa " * (length // 3 + 2))[:length]
    return run if not run.endswith(" ") else f"{run[:-1]}a"


@dataclass(frozen=True, slots=True)
class Payload:
    """One string injected into a credit field, and whether it can wrap."""

    id: str
    text: str
    unbroken: bool


PAYLOADS: Final = tuple(
    Payload(
        id=f"{'token' if unbroken else 'spaced'}-{length}",
        text=(unbroken_run if unbroken else spaced_run)(length),
        unbroken=unbroken,
    )
    for length in TOKEN_LENGTHS
    for unbroken in (True, False)
)
PAYLOAD_IDS: Final = tuple(payload.id for payload in PAYLOADS)


@dataclass(frozen=True, slots=True)
class Injected:
    """The content and images one injection produces."""

    content: ResearchContent
    images: tuple[ImageAsset, ...]


PLATE: Final = 1
"""Which ``PANDA_SET`` asset carries a colophon payload. Index 0 is the hero, whose
caption is a separate site with a separate stylesheet rule and a separate failure
mode, so a colophon payload must not go there."""


def _plate_credit() -> ImageCredit:
    return PANDA_SET[PLATE].credit


def _plate_source() -> Source:
    """``ImageCredit.source`` is optional, so the narrowing is asserted, not assumed.

    A ``PANDA_SET`` that lost its sources would otherwise turn the work-title cell
    into an ``AttributeError`` on ``None`` rather than into a named failure.
    """
    source = _plate_credit().source
    assert source is not None, (
        f"PANDA_SET[{PLATE}] carries no credit source, so there is no work title to "
        "inject a payload into and the .credit__work site is fenced nowhere"
    )
    return source


def _with_credit(index: int, credit: ImageCredit) -> tuple[ImageAsset, ...]:
    """``PANDA_SET`` with one asset's credit replaced, everything else untouched."""
    return tuple(
        replace(asset, credit=credit) if position == index else asset
        for position, asset in enumerate(PANDA_SET)
    )


def inject_plate_author(payload: str) -> Injected:
    return Injected(
        base_content(), _with_credit(PLATE, replace(_plate_credit(), author=payload))
    )


def inject_plate_license(payload: str) -> Injected:
    return Injected(
        base_content(), _with_credit(PLATE, replace(_plate_credit(), license=payload))
    )


def inject_plate_work(payload: str) -> Injected:
    source = replace(_plate_source(), title=payload)
    return Injected(
        base_content(), _with_credit(PLATE, replace(_plate_credit(), source=source))
    )


def inject_hero_author(payload: str) -> Injected:
    """The hero's author, which is the only way into ``.hero__credit``.

    ``_caption`` builds the hero's caption from ``work · author · license``, so the
    hero's own author is the payload's route to the absolutely-positioned plate
    caption -- and, because ``_credits_of`` credits the hero too, to a colophon row
    as well.
    """
    hero = PANDA_SET[0].credit
    return Injected(base_content(), _with_credit(0, replace(hero, author=payload)))


def inject_fact_publisher(payload: str) -> Injected:
    """Every fact's publisher, which is what ``.tick`` renders.

    ``_attribution`` prefers ``Source.publisher``, and each body prints the result as
    ``<p class="X__src tick">`` beside the display value -- ten of the thirteen
    ``.tick`` on the page. It does *not* reach the bibliography: ``make_content``'s
    document sources are separate from its facts' sources, so ``.refs li`` keeps its
    own publisher and is fenced by ``test_composition``'s untitled-host cell instead.
    """
    facts = make_facts(FACT_COUNT)
    amended = tuple(
        replace(fact, source=replace(fact.source, publisher=payload))
        for fact in facts
        if fact.source is not None
    )
    assert len(amended) == len(facts), (
        f"{len(facts) - len(amended)} of {len(facts)} make_facts() facts arrived with "
        "no source, so their publisher cannot be set and the .tick site would be "
        "measured on fewer lines than this cell claims"
    )
    return Injected(make_content(facts=amended), PANDA_SET)


@dataclass(frozen=True, slots=True)
class Injection:
    """One credit field a payload can arrive in, and where it must land on the page.

    ``lands_in`` is not decoration. Four of the five fields flow into overlapping
    elements, so a cell that only measured ``.credit`` could not tell an author from
    a licence -- and a payload that stopped reaching the page at all would leave
    every width assertion trivially green.
    """

    id: str
    inject: Callable[[str], Injected]
    lands_in: tuple[str, ...]


INJECTIONS: Final = (
    Injection(
        id="plate-author",
        inject=inject_plate_author,
        lands_in=(".credit__author", "figcaption:not(.hero__credit)"),
    ),
    Injection(
        id="plate-license",
        inject=inject_plate_license,
        lands_in=(".credit__license", "figcaption:not(.hero__credit)"),
    ),
    Injection(
        id="plate-work",
        inject=inject_plate_work,
        lands_in=(".credit__work", "figcaption:not(.hero__credit)"),
    ),
    Injection(
        id="hero-author",
        inject=inject_hero_author,
        lands_in=(".hero__credit", ".credit__author"),
    ),
    Injection(
        id="fact-publisher",
        inject=inject_fact_publisher,
        lands_in=(".row__src, .chip__src, .rank__src",),
    ),
)
INJECTION_IDS: Final = tuple(injection.id for injection in INJECTIONS)

WRAP_BOXES: Final = (
    ".credit",
    ".tick",
    ".hero__credit",
    "figcaption:not(.hero__credit)",
)
"""The four block boxes whose own horizontal overflow is measured in every cell.

One per rule the fix touched. ``figcaption`` is split on ``.hero__credit`` because
the two are different rules in different sheets with different failure modes -- the
plate caption widens the page, the hero caption cannot and overhangs the photograph
instead -- and a bare ``figcaption`` would let either hide behind the other."""

OVERHANG_TOLERANCE_PX: Final = 1.0
"""Subpixel slack, matching ``OVERFLOW_JS``'s, for both the per-box scroll check and
the hero credit's glyph overhang. Measured with the fix in place, the hero credit's
ink sits 22.2px inside the plate at the start and 16px inside at the end -- its own
``padding-inline`` -- so this is headroom against rounding and nothing else."""

WRAP_JS = """
(config) => {
  const name = el => {
    const classes = String(el.className || '').trim().split(/\\s+/).filter(Boolean);
    return el.tagName.toLowerCase() + classes.map(c => '.' + c).join('');
  };
  const survey = (selector) => {
    const elements = Array.from(document.querySelectorAll(selector));
    return {
      selector: selector,
      examined: elements.length,
      carrying: elements.filter(el => el.textContent.includes(config.payload)).length,
      offenders: elements
        .filter(el => el.scrollWidth > el.clientWidth + config.tolerance)
        .map(el => ({
          element: name(el),
          scroll: el.scrollWidth,
          client: el.clientWidth,
        })),
    };
  };
  const plate = document.querySelector('.hero');
  const caption = document.querySelector('.hero__credit');
  let ink = null;
  if (plate !== null && caption !== null) {
    const outer = plate.getBoundingClientRect();
    // The glyph run, line by line, not the caption's own box: the box is
    // `inset-inline: 0` and stays pinned to the plate whatever the text does.
    const range = document.createRange();
    range.selectNodeContents(caption);
    const lines = Array.from(range.getClientRects())
      .filter(rect => rect.width > 0 || rect.height > 0);
    ink = {
      lines: lines.length,
      start: lines.length === 0
        ? null : outer.left - Math.min(...lines.map(rect => rect.left)),
      end: lines.length === 0
        ? null : Math.max(...lines.map(rect => rect.right)) - outer.right,
      widest: lines.length === 0
        ? null : Math.max(...lines.map(rect => rect.width)),
      plate_width: outer.width,
    };
  }
  return {
    page_scroll: document.documentElement.scrollWidth,
    page_client: document.body.clientWidth,
    boxes: config.boxes.map(survey),
    carriers: config.carriers.map(survey),
    hero_ink: ink,
  };
}
"""
"""One page, surveyed once.

``carrying`` is counted with ``textContent.includes`` rather than inferred from the
markup, because the assertion this file needs is that the payload reached *the
rendered element* -- ten instances of "asserted a proxy for the thing" were found in
this zone in one pass, and "the string is in the HTML somewhere" is that shape
exactly.

``hero_ink`` measures the caption's **glyphs** and not its box, and that is the whole
point of the field. ``.hero__credit`` is ``position: absolute; inset-inline: 0``, so
its ``getBoundingClientRect`` is pinned to the plate: measured with the wrap rule
deleted, the box overhang stayed exactly 0.0px at every length while the text ran
27088px past the photograph. A box-versus-box comparison there is a tautology --
precisely the defect class this file exists to stop -- so the range's client rects
are read instead. A missing box yields ``null`` and a caption with no ink yields
``null`` lengths, neither of which can be mistaken for a passing zero."""


@dataclass(frozen=True, slots=True)
class WideBox:
    element: str
    scroll: float
    client: float

    def __str__(self) -> str:
        return (
            f"{self.element} scrolls to {self.scroll:.0f}px inside a "
            f"{self.client:.0f}px box (+{self.scroll - self.client:.0f}px)"
        )


@dataclass(frozen=True, slots=True)
class Survey:
    """What one selector matched, and which of its boxes overflow."""

    selector: str
    examined: int
    carrying: int
    offenders: tuple[WideBox, ...]


def read_survey(measured: Mapping[str, object]) -> Survey:
    return Survey(
        selector=_text(measured["selector"]),
        examined=int(_number(measured["examined"])),
        carrying=int(_number(measured["carrying"])),
        offenders=tuple(
            WideBox(
                element=_text(row["element"]),
                scroll=_number(row["scroll"]),
                client=_number(row["client"]),
            )
            for row in map(_fields, _rows(measured["offenders"]))
        ),
    )


@pytest.mark.parametrize("payload", PAYLOADS, ids=PAYLOAD_IDS)
@pytest.mark.parametrize("injection", INJECTIONS, ids=INJECTION_IDS)
@BODIES
@BROWSER_LOOP
async def test_an_unbreakable_credit_token_cannot_widen_the_rendered_page(
    chromium: Browser,
    template_id: str,
    injection: Injection,
    payload: Payload,
) -> None:
    """A credit field with no break opportunity must wrap, never widen.

    The PNG is the width the brief asked for or it is the wrong PNG: ``body`` is
    ``width: var(--w)`` with no ``overflow: hidden``, so a run that does not fit
    grows the document and the renderer screenshots what it finds.

    Measured on *this cell's own* fixture with ``overflow-wrap`` reverted to
    ``normal``, at the 1200px default: a 160-character author -- the longest the
    imagery zone will hand over -- took ``documentElement.scrollWidth`` to 1854, 200
    to 2162, 800 to 6796 and 4000 to 31506, with the ``.credit`` row itself scrolling
    to 30888px inside a 510px column. Those numbers are how far this cell is from
    passing vacuously; the page-as-found numbers are in the module docstring.

    Both the page and each box are asserted, because they fail independently. A
    caption inside a grid track can overflow its own box while the page stays 1200px
    wide, and ``anywhere`` rather than ``break-word`` is what also shrinks the
    ``min-content`` contribution the ``.credits`` tracks are sized against -- so the
    page-level assertion alone would accept a rule that clips instead of wrapping.

    The hero caption is measured a third way, on its glyphs. It is ``position:
    absolute; inset-inline: 0``, so it cannot widen the page and its box cannot
    overhang the plate either -- with the wrap rule deleted, the box overhang stayed
    exactly 0.0px while the text ran 27088px past a 648px photograph. What escapes is
    the ink, so the ink is what is measured.

    The spaced payloads are controls at the same lengths. They passed before the fix
    and pass after it; a token cell that goes green while its spaced twin does too,
    with ``carrying`` at zero, is a cell measuring an empty page.
    """
    injected = injection.inject(payload.text)
    composition = await compose_cell(
        template_id, THEME, injected.content, injected.images
    )

    assert payload.text in composition.html, (
        f"the {len(payload.text)}-character {payload.id} payload never reached "
        f"{template_id}'s document, so every measurement below is of a page that "
        f"does not contain it. Injection {injection.id!r} no longer writes into a "
        "field the layout renders, or something truncated it"
    )

    async with laid_out(chromium, composition) as page:
        measured = _fields(
            await page.evaluate(
                WRAP_JS,
                {
                    "payload": payload.text,
                    "boxes": list(WRAP_BOXES),
                    "carriers": list(injection.lands_in),
                    "tolerance": OVERHANG_TOLERANCE_PX,
                },
            )
        )

    carriers = tuple(map(read_survey, map(_fields, _rows(measured["carriers"]))))
    boxes = tuple(map(read_survey, map(_fields, _rows(measured["boxes"]))))
    page_scroll = _number(measured["page_scroll"])
    page_client = _number(measured["page_client"])

    where = f"{template_id}/{injection.id}/{payload.id}"

    # The cell means something: the payload is in the elements it was aimed at.
    for carrier in carriers:
        assert carrier.examined > 0, (
            f"{where}: {carrier.selector!r} matched no element, so the site this "
            "injection exists to reach is not on the page and nothing below measures "
            "it. A body stopped rendering its credits, or the selector went stale"
        )
        assert carrier.carrying > 0, (
            f"{where}: none of the {carrier.examined} {carrier.selector!r} elements "
            f"contains the {len(payload.text)}-character payload, so the width "
            "assertions below are about a page the payload never landed on"
        )

    # Every box the fix touched was examined, and none of them scrolls.
    for box in boxes:
        assert box.examined > 0, (
            f"{where}: {box.selector!r} matched no element, so this cell examined "
            "zero of the boxes the wrap rules apply to and would pass for a page "
            f"that had dropped its attribution entirely. Expected all of {WRAP_BOXES}"
        )

    assert page_scroll <= page_client, (
        f"{where}: the document scrolls to {page_scroll:.0f}px inside a "
        f"{page_client:.0f}px body, so the PNG comes out "
        f"{page_scroll - page_client:.0f}px wider than the width the brief asked "
        f"for -- a {page_scroll / page_client:.2f}x screenshot. A "
        f"{len(payload.text)}-character run with no break opportunity needs "
        "overflow-wrap: anywhere on the element that holds it"
    )

    scrolling = tuple(box for box in boxes if box.offenders)
    assert not scrolling, (
        f"{where}: boxes overflow horizontally even though the page did not widen, "
        "so the run is being clipped rather than wrapped:\n"
        + "\n".join(
            f"  {box.selector}: " + "; ".join(map(str, box.offenders))
            for box in scrolling
        )
    )

    ink = measured["hero_ink"]
    assert ink is not None, (
        f"{where}: the page has no .hero or no .hero__credit, so the one box that "
        "cannot widen the page was not measured at all. Every cell here supplies a "
        "hero with a caption"
    )
    hero = _fields(ink)
    lines = int(_number(hero["lines"]))
    assert lines > 0, (
        f"{where}: .hero__credit paints no glyphs at all, so the credit that sits on "
        "the photograph is invisible and the overhang below is measured over nothing"
    )
    plate_width = _number(hero["plate_width"])
    start, end, widest = (
        _number(hero["start"]),
        _number(hero["end"]),
        _number(hero["widest"]),
    )
    # Inline only. The box is `bottom: 0`, so wrapping grows the caption *upward* out
    # of the plate by design -- measured 365.8px above the top of a 431.7px hero at
    # 4000 characters, which `.hero { overflow: hidden }` clips. The inline axis is the
    # one the wrap rule governs, and the one where a missing rule painted legally
    # required attribution off the photograph and then off the page.
    assert max(start, end) <= OVERHANG_TOLERANCE_PX, (
        f"{where}: the hero credit's {lines} line(s) of glyphs overhang the "
        f"{plate_width:.0f}px .hero plate by {start:.1f}px at the start and "
        f"{end:.1f}px at the end -- the widest line is {widest:.0f}px. The box is "
        "inset-inline: 0, so it cannot widen the PNG; it paints the credit off the "
        "plate and off the page instead, which is the same loss by another route"
    )
    assert widest <= plate_width + OVERHANG_TOLERANCE_PX, (
        f"{where}: the hero credit's widest line is {widest:.0f}px on a "
        f"{plate_width:.0f}px photograph, over {lines} line(s). Attribution has to be "
        "legible in the PNG, and a line wider than the plate it is painted on is not"
    )


# --------------------------------------------------------------------------- #
# 2. The display floor
# --------------------------------------------------------------------------- #

DISPLAY_FLOOR_PX: Final = 19.0
"""What ``.fitted`` may not render below: ``max(19px, min(var(--size), var(--fit)))``.

The same 19 that ``ranked_list.css``'s ``.rank__value`` and ``process_flow.css``'s
``.chip__value`` already floored at in their own sheets -- the chrome rule joined
them rather than inventing a second answer, which is why one number governs all
eleven ``.fitted`` elements on every body."""

FLOOR_EPSILON_PX: Final = 0.01
"""Subpixel slack only. Measured font sizes at the floor are exactly 19.00px, and
18px must fail: the point of the fence is the *presence* of a floor, so a tolerance
wide enough to admit the next size down would give it away."""

AT_FLOOR_PX: Final = 19.5
"""Above the floor by less than a rung. An element measuring inside
``[19, 19.5]`` is pinned by the floor rather than merely clear of it, which is what
the narrow cells assert to prove the floor is doing the work."""

FULL_WIDTH_TITLE_PX: Final = 40.0
"""What the title must exceed at the shipped 1200px width. Measured 94.00px, so the
headroom is enormous -- this exists to catch a floor that became a ceiling, not to
pin a size."""


@dataclass(frozen=True, slots=True)
class WidthCase:
    """One page width, and whether the floor is expected to bind there."""

    px: int
    floor_binds: bool
    """``True`` where ``min(--size, --fit)`` measured *below* 19px before the fix, so
    the floor is what is deciding the size. Those cells assert that something on the
    page actually sits at the floor: a width where nothing does is a width where
    deleting the floor changes nothing and the cell proves nothing."""


WIDTH_CASES: Final = (
    WidthCase(100, floor_binds=True),
    WidthCase(120, floor_binds=True),
    WidthCase(150, floor_binds=True),
    WidthCase(200, floor_binds=True),
    WidthCase(300, floor_binds=False),
    WidthCase(452, floor_binds=False),
    WidthCase(640, floor_binds=False),
    WidthCase(1200, floor_binds=False),
)
"""The widths where the size collapsed, plus the ones where it must not be capped.

100-200 are the pathological band: measured title size was 0.00px from 0 to 120,
1.19px at 150 and 6.67px at 200. 452 is the page's real single-column floor, pinned
to the pixel by ``.credits``' 420px track minimum plus a 32px gutter -- 450 and 451
overflow, 452 and 453 do not -- so it is the narrowest width at which the layout
itself is sound. 1200 is what ships. Nothing here asserts anything about *overflow*,
at any width: below 452 the page cannot lay out, and a title that wraps at 19px is
the better failure."""

WIDTH_IDS: Final = tuple(f"w{case.px}" for case in WIDTH_CASES)

PATHOLOGICAL_WIDTHS: Final = frozenset({100, 120, 150, 200})
"""The widths measured at or near 0.00px before the fix. Named so the self-check can
refuse a ``WIDTH_CASES`` that quietly dropped one."""

FITTED_JS = """
() => Array.from(document.querySelectorAll('.fitted')).map(el => {
  const classes = String(el.className || '').trim().split(/\\s+/).filter(Boolean);
  return {
    element: el.tagName.toLowerCase() + classes.map(c => '.' + c).join(''),
    is_title: el.classList.contains('title'),
    font_px: parseFloat(getComputedStyle(el).fontSize),
    text: el.textContent.trim().slice(0, 40),
  };
})
"""
"""Every auto-fitted element's *computed* font size in pixels.

Not its class, and not the ``--size`` or ``--fit`` custom properties it was given: a
check on the rung class passed for every figure on a page where all of them rendered
at body-copy size, and the whole defect lives in what ``max``/``min`` resolves those
two properties to. ``parseFloat`` of ``getComputedStyle().fontSize`` is the number
the glyphs are actually set at."""


@dataclass(frozen=True, slots=True)
class Fitted:
    """One measured ``.fitted`` element."""

    element: str
    is_title: bool
    font_px: float
    text: str

    def __str__(self) -> str:
        return f"{self.element} at {self.font_px:.2f}px ({self.text!r})"


def read_fitted(measured: object) -> tuple[Fitted, ...]:
    return tuple(
        Fitted(
            element=_text(row["element"]),
            is_title=bool(row["is_title"]),
            font_px=_number(row["font_px"]),
            text=_text(row["text"]),
        )
        for row in map(_fields, _rows(measured))
    )


@pytest.mark.parametrize("case", WIDTH_CASES, ids=WIDTH_IDS)
@BODIES
@BROWSER_LOOP
async def test_an_auto_fitted_display_size_never_renders_below_its_floor(
    chromium: Browser, template_id: str, case: WidthCase
) -> None:
    """No display element renders smaller than 19px, at any page width.

    ``--fit`` is a ``cqw``: a proportion of a query container, with no lower bound of
    its own. While ``.fitted`` was ``min(--size, --fit)`` a narrow container took the
    title to **0.00px at every width from 0 to 120**, 1.19px at 150 and 6.67px at
    200 -- and nothing failed, because the rung class was still on the element and
    the stylesheet still declared a size. Only the computed pixel size can see it.

    A hero is supplied in every cell because the ``--fit`` cap only binds hard once
    the masthead has been squeezed into a ``46fr`` column; without one the container
    is the whole page and the narrow cells would clear the floor for the wrong
    reason.
    """
    composition = await compose_cell(
        template_id, THEME, base_content(), PANDA_SET, width_px=case.px
    )

    async with laid_out(chromium, composition) as page:
        fitted = read_fitted(await page.evaluate(FITTED_JS))

    where = f"{template_id} at {case.px}px"

    assert len(fitted) == FITTED_PER_PAGE, (
        f"{where}: .fitted matched {len(fitted)} elements, expected "
        f"{FITTED_PER_PAGE} -- the title plus one display value per fact "
        f"({FACT_COUNT}). At a lower count this cell is not measuring the elements "
        f"the floor governs. Measured: {[str(row) for row in fitted]}"
    )
    titles = tuple(row for row in fitted if row.is_title)
    assert len(titles) == 1, (
        f"{where}: {len(titles)} of the {len(fitted)} .fitted elements are the "
        ".title, expected exactly one. The title is the element that measured "
        "0.00px, so a cell that has lost track of it has lost the headline case"
    )

    smallest = min(row.font_px for row in fitted)
    below = tuple(
        row for row in fitted if row.font_px < DISPLAY_FLOOR_PX - FLOOR_EPSILON_PX
    )
    assert not below, (
        f"{where}: {len(below)} of {len(fitted)} .fitted elements render below the "
        f"{DISPLAY_FLOOR_PX:.0f}px floor:\n"
        + "\n".join(f"  {row}" for row in below)
        + "\nThe smallest type this design sets anywhere is 10.50px, and these are "
        "its display figures. A heading painted at zero pixels is not a small "
        "heading, it is a page that silently lost its title"
    )

    if case.floor_binds:
        assert smallest <= AT_FLOOR_PX, (
            f"{where}: the smallest .fitted element is {smallest:.2f}px, clear of "
            f"the {DISPLAY_FLOOR_PX:.0f}px floor by more than a rung -- so nothing "
            "on this page is pinned by the floor, deleting it would change nothing "
            f"here, and this cell is green either way. Measured: "
            f"{sorted(round(row.font_px, 2) for row in fitted)}"
        )


@BODIES
@BROWSER_LOOP
async def test_the_display_floor_is_not_also_a_ceiling(
    chromium: Browser, template_id: str
) -> None:
    """At the width that ships, the title is display type and not 19px type.

    A floor is one clamp away from a cap. ``max(19px, min(--size, --fit))`` collapses
    to a flat 19px the moment either inner term is mis-authored -- and every cell
    above would still pass, because 19 satisfies a floor of 19. Measured at 1200px:
    the title sets at 94.00px and the ledger's figures span 27-116px.
    """
    composition = await compose_cell(
        template_id, THEME, base_content(), PANDA_SET, width_px=1200
    )

    async with laid_out(chromium, composition) as page:
        fitted = read_fitted(await page.evaluate(FITTED_JS))

    titles = tuple(row for row in fitted if row.is_title)
    assert len(titles) == 1, (
        f"{template_id} at 1200px: {len(titles)} .title.fitted elements, expected "
        f"one. Measured .fitted: {[str(row) for row in fitted]}"
    )
    title = titles[0]

    assert title.font_px >= FULL_WIDTH_TITLE_PX, (
        f"{template_id} at 1200px: the title renders at {title.font_px:.2f}px, under "
        f"{FULL_WIDTH_TITLE_PX:.0f}px, on the width that ships. It measured 94.00px "
        "-- so either --size or --fit has stopped resolving and everything is now "
        f"pinned to the {DISPLAY_FLOOR_PX:.0f}px floor, which every floor assertion "
        "in this file accepts. Measured .fitted: "
        f"{sorted(round(row.font_px, 2) for row in fitted)}"
    )


# --------------------------------------------------------------------------- #
# 3. A lying declared size and the photograph it stretched
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LyingShape:
    """A real photograph whose declared pixel size is not its own shape."""

    asset: ImageAsset
    why: str
    """What the declaration claims, and what the layout does with it. Quoted in the
    failure message so a broken cell names the metadata that caused it."""

    @property
    def declared_ratio(self) -> float:
        """The ratio as supplied -- *before* ``layout._aspect`` clamps it."""
        return self.asset.width_px / self.asset.height_px


LYING_SHAPES: Final = (
    LyingShape(
        asset=PANDAS[1].as_asset(width_px=1, height_px=3000),
        why="a declared 1x3000, metadata no photograph could have",
    ),
    LyingShape(
        asset=PANDAS[2].as_asset(width_px=900, height_px=1600),
        why="an honest 9:16 portrait (0.5625), which _MIN_ASPECT clamps up to 0.68",
    ),
    LyingShape(
        asset=PANDAS[0].as_asset(width_px=3000, height_px=1000),
        why="an honest 3:1 panorama, which _MAX_ASPECT clamps down to 1.85",
    ),
)
"""Three declarations the box will be built from and the photograph cannot satisfy.

The middle one is the case that needs no bug upstream: 9:16 is a real shape a real
camera produces, ``_aspect`` clamps it to ``_MIN_ASPECT`` because the layout cannot
place an arbitrarily tall figure, and the ``object-fit: fill`` default then squeezed
the photograph about 21% to fill the box it was given. It is declared *truthfully*;
the distortion is the clamp's, which is precisely the case metadata alone cannot fix.

The fixtures are all landscape or square on disk -- 1600x1066 for ``PANDAS[0]`` and
``PANDAS[1]``, 1600x1600 for ``PANDAS[2]`` -- so the intrinsic ratio behind the 9:16
declaration is not itself 9:16. That is deliberate and it is what makes the cell
measurable: the declaration is what sizes the box, the file is what supplies the
pixels, and the fence is that the second is not reshaped to match the first. Two of
the three clamp to the same 0.68, which is why they sit on *different* fixtures --
1.5009 against 1.0000 intrinsic -- so a single painted ratio cannot satisfy both.

``PANDAS[0]`` appears twice, once as the hero. The hero carries no inline
``aspect-ratio`` and is excluded from the measurement, so the three alts that are
measured stay distinct and each box is matchable to the slot that declared it."""

LYING_IMAGES: Final = (
    PANDAS[0].as_asset(role=ImageRole.HERO),
    *(shape.asset for shape in LYING_SHAPES),
)
"""The lying shapes behind a hero, which is where every body places its figures.
Three of them because ``stat_grid``'s band takes ``rest[:3]``: a fourth would be
placed by the other two bodies and dropped by that one, and the counts below would
have to fork per body to stay honest."""

BOX_RATIO_TOLERANCE: Final = 0.02
"""Rendered-versus-declared slack for the figure box, the same 0.02
``test_composition`` and ``test_template_bodies`` use. Measured deviation here is
under 0.01%."""

PAINT_RATIO_TOLERANCE: Final = 0.01
"""Painted-versus-intrinsic slack. Under ``cover`` the reconstruction is exact, so
any real value here is a distortion and not rounding."""

LIE_THRESHOLD: Final = 0.10
"""How far the box ratio must sit from the intrinsic ratio for a figure to be worth
measuring. Below this the declaration is close enough to honest that ``cover`` is
nearly the identity and the cell would pass with the fix deleted. Measured lies:
55%, 32% and 23%."""

COVER_SLACK_PX: Final = 0.5
"""Subpixel slack when asserting the painted content covers the box on both axes."""

PAINT_JS = """
() => Array.from(document.querySelectorAll('figure:not(.hero) > img')).map(el => {
  const box = el.getBoundingClientRect();
  const object_fit = getComputedStyle(el).objectFit;
  const natural_width = el.naturalWidth;
  const natural_height = el.naturalHeight;
  const base = {
    alt: el.alt,
    object_fit: object_fit,
    declared: el.style.aspectRatio,
    natural_width: natural_width,
    natural_height: natural_height,
    box_width: box.width,
    box_height: box.height,
  };
  if (!natural_width || !natural_height) {
    return Object.assign(base, {error: 'IMAGE NOT DECODED'});
  }
  if (!box.width || !box.height) {
    return Object.assign(base, {error: 'BOX HAS NO AREA'});
  }
  // What each keyword paints, per the object-fit sizing algorithm. `fill` -- the
  // browser default, and the bug -- takes the box whole on both axes, which is what
  // stretches the photograph. Everything else scales the intrinsic size uniformly.
  const wide = box.width / natural_width;
  const tall = box.height / natural_height;
  const uniform = (s) => [s, s];
  const scales = {
    'fill': [wide, tall],
    'cover': uniform(Math.max(wide, tall)),
    'contain': uniform(Math.min(wide, tall)),
    'none': uniform(1),
    'scale-down': uniform(Math.min(1, Math.min(wide, tall))),
  };
  const scale = scales[object_fit];
  if (scale === undefined) {
    return Object.assign(base, {error: 'UNMODELLED OBJECT-FIT: ' + object_fit});
  }
  return Object.assign(base, {
    error: null,
    painted_width: natural_width * scale[0],
    painted_height: natural_height * scale[1],
  });
})
"""
"""The intrinsic size, the box, and the content box the two of them paint.

The painted size is derived from the ``object-fit`` value *read off the rendered
element*, not from the one the stylesheet is supposed to declare, and that is what
makes this a measurement rather than a restatement: delete ``object-fit: cover`` and
the computed value becomes ``fill``, the painted box becomes the layout box, and the
painted ratio becomes the lying declared ratio. No DOM API reports the pixels the
compositor actually put down, so this is a model -- but it is the spec's own sizing
algorithm applied to measured inputs, and an unmodelled keyword fails loudly rather
than falling through to a passing number."""


@dataclass(frozen=True, slots=True)
class Painted:
    """One figure image: what it is, what box it got, and what it paints."""

    alt: str
    object_fit: str
    declared: str
    natural_width: float
    natural_height: float
    box_width: float
    box_height: float
    painted_width: float
    painted_height: float

    @property
    def intrinsic_ratio(self) -> float:
        return self.natural_width / self.natural_height

    @property
    def box_ratio(self) -> float:
        return self.box_width / self.box_height

    @property
    def painted_ratio(self) -> float:
        return self.painted_width / self.painted_height


def read_painted(measured: Mapping[str, object]) -> Painted:
    error = measured["error"]
    assert error is None, (
        f"the figure {_text(measured['alt'])!r} could not be measured: {error!r} "
        f"(object-fit {_text(measured['object_fit'])!r}, box "
        f"{_number(measured['box_width']):.1f}x{_number(measured['box_height']):.1f})"
    )
    return Painted(
        alt=_text(measured["alt"]),
        object_fit=_text(measured["object_fit"]),
        declared=_text(measured["declared"]),
        natural_width=_number(measured["natural_width"]),
        natural_height=_number(measured["natural_height"]),
        box_width=_number(measured["box_width"]),
        box_height=_number(measured["box_height"]),
        painted_width=_number(measured["painted_width"]),
        painted_height=_number(measured["painted_height"]),
    )


def relative_gap(measured: float, expected: float) -> float:
    """How far ``measured`` sits from ``expected``, as a fraction of ``expected``."""
    return abs(measured / expected - 1)


def declared_ratio(value: str) -> float:
    """Chromium serialises ``aspect-ratio: 1.85`` back out as ``"1.85 / 1"``."""
    numerator, _, denominator = value.partition("/")
    return float(numerator) / float(denominator or 1)


@BODIES
@BROWSER_LOOP
async def test_a_lying_declared_size_crops_a_photograph_instead_of_stretching_it(
    chromium: Browser, template_id: str
) -> None:
    """The box comes from the declaration; the pixels keep the photographer's shape.

    ``layout._aspect`` clamps a declared ratio into ``[0.68, 1.85]``, so the number
    in the markup is routinely not the shape the file has -- and the figure box is
    built from that number. With the ``object-fit: fill`` default the photograph was
    then scaled independently on each axis to fill it: a declared ``1x3000`` and an
    honest 9:16 portrait both land in a 0.68 box, and a 1.5009 photograph squeezed
    into one is distorted 55%. Misrepresenting the proportions of a CC BY work
    misstates what was licensed, which is nearer an attribution fault than a rag.

    Measured on these three shapes with the declaration reverted to ``fill``, in all
    three bodies: the painted ratio collapses onto the box ratio -- 0.6800 against a
    1.5009 file, 0.6800 against a 1.0000 file and 1.8500 against a 1.5009 file, for
    distortions of 54.7%, 32.0% and 23.3%.

    So three things are asserted together, and none of them alone is the fix. The box
    obeys the declaration -- otherwise the clamp is not what is being tested. The box
    ratio is far from the intrinsic ratio -- otherwise ``cover`` is the identity and
    the cell is green with the declaration deleted. And the *painted* ratio equals
    the intrinsic one while covering the box on both axes -- which is ``cover``, is
    not ``fill``, and is not ``contain`` either, since the figure's ``flex-grow`` has
    already been paid for out of the row's width.

    The hero is excluded rather than handled: it carries no inline ``aspect-ratio``
    in any body -- ``test_template_bodies`` pins that -- so it is not a figure whose
    box came from a declaration, and its ``object-fit: cover`` is a deliberate crop
    to the masthead's height that predates all of this.
    """
    composition = await compose_cell(
        template_id, THEME, base_content(), LYING_IMAGES, width_px=1200
    )

    async with laid_out(chromium, composition) as page:
        figures = tuple(
            read_painted(row)
            for row in map(_fields, _rows(await page.evaluate(PAINT_JS)))
        )

    assert len(figures) == len(LYING_SHAPES), (
        f"{template_id}: measured {len(figures)} non-hero figure images, expected "
        f"{len(LYING_SHAPES)} -- one per declared shape. A figure went unplaced, or "
        "the hero started matching the selector, and either way the shapes below are "
        f"not all under test. Measured alts: {[figure.alt for figure in figures]}"
    )
    by_alt = {figure.alt: figure for figure in figures}
    assert len(by_alt) == len(figures), (
        f"{template_id}: measured figures share an alt "
        f"({[figure.alt for figure in figures]}), so a box cannot be matched back to "
        "the declaration that sized it"
    )
    declared = {figure.declared for figure in figures}
    assert len(declared) >= 2, (
        f"{template_id}: every measured figure declares the same ratio ({declared}), "
        "so one rendered height could satisfy all of them and this cell cannot tell "
        "a governing declaration from a shared row height"
    )

    for shape in LYING_SHAPES:
        alt = shape.asset.alt_text
        assert alt in by_alt, (
            f"{template_id}: no measured box for {alt!r}, so the slot declaring "
            f"{shape.asset.width_px}x{shape.asset.height_px} ({shape.why}) was never "
            "placed"
        )
        figure = by_alt[alt]
        told = f"{template_id}: {alt!r} -- {shape.why}"

        assert figure.object_fit != "fill", (
            f"{told} -- computes object-fit: fill, the browser default. The box is "
            f"sized {figure.box_width:.1f}x{figure.box_height:.1f} from the declared "
            f"{figure.declared!r} while the file is "
            f"{figure.natural_width:.0f}x{figure.natural_height:.0f}, so fill scales "
            "the two axes by different factors and paints a photograph nobody took"
        )

        box_gap = relative_gap(figure.box_ratio, declared_ratio(figure.declared))
        assert box_gap <= BOX_RATIO_TOLERANCE, (
            f"{told} -- declares aspect-ratio {figure.declared!r} but rendered a "
            f"{figure.box_width:.1f}x{figure.box_height:.1f} box "
            f"({figure.box_ratio:.4f}), off by {box_gap:.1%}. Some other rule is "
            "deciding this figure's height, so the declaration is not what this cell "
            "is measuring the photograph against"
        )

        lie = relative_gap(figure.box_ratio, figure.intrinsic_ratio)
        assert lie >= LIE_THRESHOLD, (
            f"{told} -- was given a {figure.box_ratio:.4f} box for a "
            f"{figure.intrinsic_ratio:.4f} photograph, only {lie:.1%} apart. Under "
            f"{LIE_THRESHOLD:.0%} the declaration is near enough honest that "
            "object-fit is nearly the identity and this cell would pass with the "
            "declaration deleted. The fixture has stopped lying"
        )

        paint_gap = relative_gap(figure.painted_ratio, figure.intrinsic_ratio)
        assert paint_gap <= PAINT_RATIO_TOLERANCE, (
            f"{told} -- paints "
            f"{figure.painted_width:.1f}x{figure.painted_height:.1f} "
            f"({figure.painted_ratio:.4f}) from a "
            f"{figure.natural_width:.0f}x{figure.natural_height:.0f} file "
            f"({figure.intrinsic_ratio:.4f}): a {paint_gap:.1%} distortion under "
            f"object-fit: {figure.object_fit}. The photograph is being reshaped to "
            "match metadata instead of cropped to it, and what the PNG shows is not "
            "the work that was licensed"
        )

        assert (
            figure.painted_width >= figure.box_width - COVER_SLACK_PX
            and figure.painted_height >= figure.box_height - COVER_SLACK_PX
        ), (
            f"{told} -- paints only "
            f"{figure.painted_width:.1f}x{figure.painted_height:.1f} into a "
            f"{figure.box_width:.1f}x{figure.box_height:.1f} box under object-fit: "
            f"{figure.object_fit}, so the figure is letterboxed. The box's width was "
            "bought with the figure's own flex-grow out of the row; contain gives it "
            "back as blank paper"
        )


# --------------------------------------------------------------------------- #
# The fixtures, checked without a browser
# --------------------------------------------------------------------------- #


def test_every_axis_in_this_file_is_non_empty() -> None:
    """A parametrize over an empty sequence is a *skip*: green, silent, measuring
    nothing. ``TEMPLATE_IDS`` in particular is derived at runtime from a frozenset."""
    assert TEMPLATE_IDS, (
        "no template is renderable, so every browser cell in this file collapses to "
        "pytest's empty parameter set and skips instead of failing"
    )
    assert BODIES, "the body matrix decorator is falsy, so nothing is parametrized"
    axes: Mapping[str, Sequence[object]] = MappingProxyType(
        {
            "PAYLOADS": PAYLOADS,
            "INJECTIONS": INJECTIONS,
            "WRAP_BOXES": WRAP_BOXES,
            "WIDTH_CASES": WIDTH_CASES,
            "LYING_SHAPES": LYING_SHAPES,
            "LYING_IMAGES": LYING_IMAGES,
        }
    )
    empty = sorted(name for name, axis in axes.items() if not axis)
    assert not empty, f"empty axes: {empty}, whose cells would all skip silently"


def test_the_wrap_payloads_can_still_prove_what_they_claim() -> None:
    """Each payload's defining property, and each injection's landing site.

    A "single token" that quietly gained a space, or a ``lands_in`` selector that
    went stale, leaves the width cells green while measuring something else.
    """
    for payload in PAYLOADS:
        assert payload.text, f"{payload.id} is empty"
        assert payload.text == payload.text.strip(), (
            f"{payload.id} has leading or trailing whitespace, so it cannot be looked "
            f"for verbatim in a rendered textContent: {payload.text[:8]!r}..."
        )
        if payload.unbroken:
            assert len(payload.text.split()) == 1, (
                f"{payload.id} contains whitespace, so it is "
                f"{len(payload.text.split())} breakable words and tests "
                "overflow-wrap: anywhere no longer -- normal wrapping handles it"
            )
        else:
            assert len(payload.text.split()) > 1, (
                f"{payload.id} is the breakable control but has no break "
                "opportunity, so it duplicates its token twin and controls nothing"
            )

    lengths = {len(payload.text) for payload in PAYLOADS}
    assert lengths == set(TOKEN_LENGTHS), (
        f"payloads measure {sorted(lengths)} characters but TOKEN_LENGTHS asks for "
        f"{sorted(TOKEN_LENGTHS)}: a repeated-word slice no longer reaches its "
        "target length, so the cells are named after damage they do not reproduce"
    )
    assert {200, 800, 4000} <= lengths, (
        f"the lengths whose damage was measured (1598px, 6177px and 30596px of "
        f"scrollWidth at a 1200px page) are not all covered: {sorted(lengths)}"
    )

    for injection in INJECTIONS:
        assert injection.lands_in, (
            f"injection {injection.id!r} names no landing site, so nothing checks "
            "that its payload reached the page and its width cells are vacuous"
        )
        blank = [selector for selector in injection.lands_in if not selector.strip()]
        assert not blank, (
            f"injection {injection.id!r} has a blank landing selector: "
            "querySelectorAll raises on an empty string, so the cell dies as a raw "
            "playwright SyntaxError naming nothing"
        )

    covered = {selector for injection in INJECTIONS for selector in injection.lands_in}
    assert ".hero__credit" in covered, (
        "no injection lands in .hero__credit, so the one wrap rule whose box cannot "
        f"widen the page is unexercised. Landing sites: {sorted(covered)}"
    )
    assert any("__src" in selector for selector in covered), (
        f"no injection lands in a *__src line, so .tick is unexercised: {sorted(covered)}"
    )


def test_the_width_sweep_still_covers_the_widths_that_collapsed() -> None:
    """The floor cells are only worth running where the floor did the damage."""
    widths = {case.px for case in WIDTH_CASES}
    assert PATHOLOGICAL_WIDTHS <= widths, (
        f"the sweep covers {sorted(widths)} and is missing "
        f"{sorted(PATHOLOGICAL_WIDTHS - widths)}, where the title measured at or "
        "near 0.00px before the fix"
    )
    assert 1200 in widths, (
        f"the shipped width is not in the sweep ({sorted(widths)}), so the floor is "
        "asserted nowhere the page is actually rendered"
    )
    assert 452 in widths, (
        f"452px -- the page's real single-column limit, pinned to the pixel -- is not "
        f"in the sweep ({sorted(widths)})"
    )
    binding = tuple(case for case in WIDTH_CASES if case.floor_binds)
    assert binding, (
        "no width expects the floor to bind, so every floor assertion in this file "
        "could pass with the floor deleted and nothing would say so"
    )
    assert len(binding) < len(WIDTH_CASES), (
        "every width expects the floor to bind, so the sweep never reaches a width "
        "where --size and --fit are supposed to decide the size on their own"
    )
    assert all(
        case.floor_binds == (case.px in PATHOLOGICAL_WIDTHS) for case in WIDTH_CASES
    ), (
        "floor_binds and PATHOLOGICAL_WIDTHS disagree: "
        f"{[(case.px, case.floor_binds) for case in WIDTH_CASES]} against "
        f"{sorted(PATHOLOGICAL_WIDTHS)}. One of the two was edited alone"
    )
    assert DISPLAY_FLOOR_PX - FLOOR_EPSILON_PX > 18.0, (
        f"the floor tolerance admits {DISPLAY_FLOOR_PX - FLOOR_EPSILON_PX:.2f}px, "
        "which is the next size down rather than subpixel slack: the fence would "
        "accept a page with no floor at all"
    )


def test_the_lying_shapes_still_lie() -> None:
    """Three declarations, three distinct alts, and none of them honest.

    The browser cell asserts the lie against the *measured* intrinsic size, which is
    the real check. This one catches the version that cannot be measured: two shapes
    that ended up on one fixture, or a declaration that quietly became truthful.
    """
    alts = tuple(shape.asset.alt_text for shape in LYING_SHAPES)
    assert len(set(alts)) == len(alts), (
        f"lying shapes share an alt ({alts}), and alt is how a measured box is "
        "matched to the declaration that sized it"
    )
    ratios = tuple(shape.declared_ratio for shape in LYING_SHAPES)
    assert len(set(ratios)) == len(ratios), (
        f"lying shapes declare the same ratio ({ratios}), so they exercise one path "
        "through layout._aspect between them"
    )
    assert len(LYING_IMAGES) == len(LYING_SHAPES) + 1, (
        f"LYING_IMAGES holds {len(LYING_IMAGES)} assets for {len(LYING_SHAPES)} "
        "shapes plus a hero: a fourth figure would be placed by two bodies and "
        "dropped by stat_grid's three-slot band"
    )
    heroes = tuple(asset for asset in LYING_IMAGES if asset.role is ImageRole.HERO)
    assert len(heroes) == 1, (
        f"{len(heroes)} of the {len(LYING_IMAGES)} assets claim the hero role. "
        "Without exactly one, the masthead flattens and the figures the cell "
        "measures are not the ones it counted"
    )
    assert all(shape.asset.role is not ImageRole.HERO for shape in LYING_SHAPES), (
        "a lying shape claims the hero role, and the hero declares no inline "
        "aspect-ratio -- it would be excluded from the measurement and its cell "
        "would fail on a count it can never satisfy"
    )


def test_the_fact_fixture_supplies_every_line_this_file_counts() -> None:
    """``FITTED_PER_PAGE`` and the ``.tick`` sites are claims about the fixture."""
    facts = make_facts(FACT_COUNT)
    assert len(facts) == FACT_COUNT, (
        f"make_facts({FACT_COUNT}) returned {len(facts)} facts, so "
        f"FITTED_PER_PAGE ({FITTED_PER_PAGE}) is the wrong count"
    )
    unsourced = tuple(fact.label for fact in facts if fact.source is None)
    assert not unsourced, (
        f"{len(unsourced)} facts arrive without a source ({unsourced[:3]}), so they "
        "render no *__src line and the publisher injection reaches fewer .tick than "
        "its cells assert"
    )
    unpublished = tuple(
        fact.label
        for fact in facts
        if fact.source is not None and not fact.source.publisher
    )
    assert not unpublished, (
        f"{len(unpublished)} fact sources carry no publisher ({unpublished[:3]}): "
        "_attribution falls back to the title or the host, so the injected payload "
        "would not be what .tick renders"
    )
    assert PANDA_SET, "PANDA_SET is empty, so no cell here has a hero or a colophon"
    assert PANDA_SET[0].role is ImageRole.HERO, (
        f"PANDA_SET[0] is {PANDA_SET[0].role}, not HERO, so inject_hero_author "
        "writes into an asset that lands in the band and .hero__credit is unexercised"
    )
    assert len(PANDA_SET) > PLATE, (
        f"PANDA_SET holds {len(PANDA_SET)} assets, so index PLATE={PLATE} does not "
        "exist and the colophon injections have no plate to write into"
    )
    assert PANDA_SET[PLATE].role is not ImageRole.HERO, (
        f"PANDA_SET[{PLATE}] is the hero, so a colophon payload would also land in "
        ".hero__credit and the two sites could not be told apart"
    )
