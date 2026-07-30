"""Where the glyphs land on a right-to-left page.

The eleven rules that carry ``unicode-bidi: plaintext`` -- ``.tick``, ``.hero__credit``,
``.refs li``, the four ``.credit__*`` spans, the three bodies' display values and both
``figcaption``s -- exist for one reason: on an RTL page a bidi-neutral character at the
edge of an LTR run resolves to the paragraph direction and *relocates*. Measured at
``locale="ar-EG"`` before the fix, the value ``26-84 lb (12-38 kg)`` painted as
``lb (12-38 kg) 26-84``, a licence read ``+v4.0``, a publisher read ``.example.com`` and
a photographer's credit read ``.— Ansel Adams (photographer)``. 44 of 75 elements across
the three bodies put a character somewhere it was not written.

Nothing above the paint can see that. The DOM, ``textContent``, every attribute and
every computed value are identical before and after; only the rects move. So every
assertion in this file is on measured character rects, and none of them reads a
stylesheet, a class name or ``getComputedStyle``. Three traps this file is shaped
around, all of them found by measuring:

* ``<p>``, ``<li>`` and ``<figcaption>`` already compute ``unicode-bidi: isolate`` from
  the UA stylesheet, so 9 of the 11 selectors look protected with the fix deleted -- and
  are not. Isolation is not a base direction. A fence phrased as "these elements are
  isolated" passes on a page that reorders 44 elements.
* Correct Arabic paints as ``reverse(logical)``, so ``visual == logical`` is *false* for
  content that is right in every way. An RTL cell that asserts written order asserts
  that Arabic is broken. The honest claim is a comparison against a control, which is
  what :func:`test_arabic_content_still_paints_right_to_left` is.
* An element that carries no reorderable character proves nothing, and neither does a
  selector that matched none. So the absences asserted here are backed by four separate
  controls: per selector, the same rects with the declaration neutralised in a scratch
  copy of the HTML must scramble; per selector, the Arabic paint must break under
  ``direction: ltr``; the Trojan cell must see its own spoof land; and ``.subtitle`` --
  an element the fix deliberately does not cover -- must reorder on the shipped page,
  which is the guard on the instrument itself. The one selector that no control can
  reach is named and explained at :data:`STATIC_ENGLISH`. The two narrower cells
  (:func:`test_the_reported_value_paints_in_the_order_it_is_written` and
  :func:`test_each_reported_trailing_neutral_shape_survives_its_field`) pin the exact
  strings from the report rather than carrying a control of their own; the selectors they
  measure are controlled in the parametrised cells above them.

Companion to ``tests/test_composition.py``'s
``test_a_url_on_an_rtl_page_is_painted_in_the_order_it_is_written``, which covers the
two URL-only selectors (``.refs__meta`` and ``.credit__url``). Those two carry
``isolate; direction: ltr`` instead, because a URL's direction is a fact. The other
eleven hold scraped text of unknown script, where it would be a guess -- see the long
argument above ``.tick`` in ``css/_chrome.css``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import pytest

from infographic_generator.composition import HtmlComposer
from infographic_generator.composition.registry import RENDERABLE_TEMPLATE_IDS
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
    BODIES,
    BODY_SELECTORS,
    BROWSER_LOOP,
    TEMPLATE_IDS,
    _fields,
    _number,
    _rows,
    _text,
    chromium,  # noqa: F401 -- referenced only as a fixture name
    laid_out,
    make_brief,
)

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page


# --------------------------------------------------------------------------- #
# The instrument
# --------------------------------------------------------------------------- #

PAINT_ORDER_JS: Final = """(selector) => {
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    const range = document.createRange();
    const placed = [];
    const logical = [];
    for (const node of el.childNodes) {
      if (node.nodeType !== Node.TEXT_NODE) continue;
      for (let i = 0; i < node.length; i++) {
        const char = node.data[i];
        if (/\\s/.test(char)) continue;
        range.setStart(node, i);
        range.setEnd(node, i + 1);
        const box = range.getBoundingClientRect();
        placed.push([Math.round(box.top), box.left, char]);
        logical.push(char);
      }
    }
    if (!logical.length) continue;
    const lines = new Set(placed.map((p) => p[0])).size;
    placed.sort((a, b) => (a[0] - b[0]) || (a[1] - b[1]));
    out.push({
      logical: logical.join(""),
      visual: placed.map((p) => p[2]).join(""),
      lines: lines,
    });
  }
  return out;
}"""
"""Every printing character of an element's own text, ordered by where it was painted.

The per-character ``Range`` rect technique is
``tests/test_composition.py``'s ``VISUAL_ORDER_JS``; two things here are deliberately
different, and both were needed rather than preferred.

It walks *every* direct text child instead of ``el.firstChild``, and reports the number
of distinct line boxes those characters landed on. The line count is what
:func:`test_arabic_content_still_paints_right_to_left` needs: the mirror identity it
asserts holds within a line and says nothing across a wrap, and three of the Latin
``figcaption``s here wrap at 1200px. The child walk is not needed by any cell today --
in all twelve selectors the field being measured *is* the first child -- but
``.credit__license`` is one span away from being the case where it matters, and a
``firstChild`` that stopped being a text node would silently drop the element from the
measurement instead of failing.

Whitespace is skipped at both ends of the comparison for the reasons the original gives:
the templates are pretty-printed, so every text node is bracketed by a newline and an
indent no reader transcribes, and a collapsed space has a zero-width box whose ``left``
sorts arbitrarily against its neighbours. What is left is the string a reader copies out
of the PNG.
"""

PRECEDING_TEXT_JS: Final = """(selector) => {
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    const prev = el.previousElementSibling;
    out.push({
      own: el.textContent,
      preceding: prev === null ? "" : prev.textContent,
    });
  }
  return out;
}"""
"""An element's text and its preceding sibling's, for the Trojan Source cell.

The spoof lives in the *neighbour*, so a cell that only reads the element under test
cannot tell an attacked page from a clean one."""


@dataclass(frozen=True, slots=True)
class Painted:
    """One element: the characters as written, as painted, and how many lines they took."""

    logical: str
    visual: str
    lines: int

    @property
    def scrambled(self) -> bool:
        return self.logical != self.visual

    def __str__(self) -> str:
        return f"{self.logical!r} -> {self.visual!r}"


def _painted(measured: object) -> tuple[Painted, ...]:
    """Untyped JSON from the browser into the type the assertions are written against."""
    return tuple(
        Painted(
            logical=_text(_fields(row)["logical"]),
            visual=_text(_fields(row)["visual"]),
            lines=int(_number(_fields(row)["lines"])),
        )
        for row in _rows(measured)
    )


async def paint_order(page: Page, selector: str) -> tuple[Painted, ...]:
    return _painted(await page.evaluate(PAINT_ORDER_JS, selector))


def dense(text: str) -> str:
    """``text`` with its whitespace removed, which is how the instrument reports it."""
    return "".join(text.split())


def describe(rows: Sequence[Painted]) -> str:
    return "\n".join(f"    {row}" for row in rows)


# --------------------------------------------------------------------------- #
# Neutralising the fix in a scratch copy of the HTML
# --------------------------------------------------------------------------- #

DECLARATION: Final = "unicode-bidi: plaintext;"
"""The shipped declaration, as the eleven rules spell it."""

UNPROTECTED: Final = "unicode-bidi: normal;"
"""What the eleven rules computed before the fix: the CSS initial value."""

AS_A_URL: Final = "unicode-bidi: isolate; direction: ltr;"
"""The declaration that is right for ``.refs__meta`` and ``.credit__url`` and wrong
here. It repairs the Latin-on-RTL case identically -- 0 difference on 99 measured
elements -- and corrupts genuinely Arabic content, which is what
:func:`test_arabic_content_still_paints_right_to_left` exists to catch."""

DECLARATIONS_PER_PAGE: Final = 9
"""How many of the eleven reach one page: seven from the shared chrome, two from
whichever body is rendering. Asserted rather than assumed, because a scratch copy whose
substitution matched nothing is a control that controls for nothing."""


def with_declaration(composition: Composition, replacement: str) -> Composition:
    """The same page with every ``plaintext`` swapped for ``replacement``.

    A scratch copy of the HTML string, never an edit to the stylesheet: this is how a
    cell measures its own falsification without the suite depending on the state of a
    file on disk.

    The count is asserted here rather than in each caller, because a substitution that
    matched nothing produces a "control" identical to the page it is controlling for --
    and the resulting failure would read as "the attack is inert" when what actually
    happened is that the shipped declaration changed under the test.
    """
    found = composition.html.count(DECLARATION)
    assert found == DECLARATIONS_PER_PAGE, (
        f"this page composes {found} copies of {DECLARATION!r}, expected "
        f"{DECLARATIONS_PER_PAGE} -- seven rules from the shared chrome and two from the "
        "body. The cell that called this measures its own falsification by substituting "
        f"{replacement!r} for that string, so a count of 0 means the eleven rules no "
        "longer carry the declaration this file was written to fence and every control "
        "below is comparing a page against itself"
    )
    return replace(composition, html=composition.html.replace(DECLARATION, replacement))


# --------------------------------------------------------------------------- #
# Fixtures: the shapes that were measured to move
# --------------------------------------------------------------------------- #
# Every string below ends in a bidi-neutral character, because that is the character
# that relocates. They are not decoration: each one is a field the pipeline really
# produces -- a licence identifier, a publisher fallen back to a host, a photographer's
# name, a fact value with a converted unit in brackets.

PANDA_DIR: Final = (
    Path(__file__).resolve().parent.parent / "assets" / "panda"
)

HEADLINE_VALUE: Final = "26-84 lb (12-38 kg)"
"""The value from the defect report. Its interior brackets are what made the failure
so bad: this did not lose a trailing character, it painted as ``lb (12-38 kg) 26-84``."""

BRACKETED_VALUE: Final = "1,864 (2015 census)"
"""A value whose last character is a closing bracket rather than a full stop."""

HOST_PUBLISHER: Final = "example.com."
"""``_attribution`` falls back to ``_host(source.url)``, so a ``.tick`` line is a
sentence, a publisher or a bare host -- and a host with a trailing stop after it."""

PLUS_LICENCE: Final = "CC BY-SA v4.0+"
"""A licence identifier whose version suffix is a ``+``. Measured as ``+v4.0``."""

STOP_AUTHOR: Final = "Ansel Adams (photographer)."
"""The credit from the defect report, which painted as
``.— Ansel Adams (photographer)`` -- the full stop moved past a name *and* past the em
dash the template puts in front of it."""

ISOLATE_INITIATOR: Final = "\u2067"
"""U+2067 RIGHT-TO-LEFT ISOLATE, unterminated: no U+2069 follows it.

Spelled as an escape, the way ``layout._ILLEGIBLE_IN_TEXT`` spells its own: a literal
here would be an invisible character in a source file about invisible characters, and it
reorders this docstring in any editor that applies the algorithm.

``layout._legible_text`` deliberately keeps U+2066-U+2069 -- they are how correct
mixed-direction text is written, and the deprecated embeddings it *does* replace are the
dangerous ones. So an initiator in a scraped title reaches the DOM, and without a base
direction of its own the field beside it is reordered inside the initiator's scope. This
is the Trojan Source spoof pointed at a colophon."""


@dataclass(frozen=True, slots=True)
class CreditFields:
    """One colophon row's scraped text, as the imagery stage would hand it over."""

    license: str
    author: str | None
    work: str
    modified: bool


LATIN_CREDITS: Final = (
    CreditFields(PLUS_LICENCE, STOP_AUTHOR, "Panda in habitat (Qinling, 2019).", False),
    CreditFields("CC BY v2.0+", "Gzen92 (Wikimedia).", "Panda geant tete (2016).", True),
    CreditFields("CC0 v1.0+", "Kevin Dooley (Flickr).", "Bamboo, peeled (2011).", False),
    CreditFields("CC BY-SA v3.0+", None, "Qinling giant panda (2007).", True),
)
"""Four rows, and every field of every row distinct, because ``_credits_of`` keys the
colophon on the whole ``Credit`` and collapses two rows that would render identically --
four assets sharing a licence and an author are *one* measured element.

Three details are load-bearing and each was chosen after measuring the alternative:

``modified`` alternates. It is the only thing that renders ``.credit__adapted``, and it
also appends a literal ``adapted`` to that figure's caption -- so a set that is modified
throughout leaves every ``figcaption`` ending in a strong Latin letter, with nothing in
it that any bidi algorithm could move. With the mix, ``.credit__adapted`` renders twice
and one caption still ends in the licence's ``+``.

The hero is *not* modified, for the same reason: ``.hero__credit`` is a population of
one, so if that one caption ends in ``adapted`` the selector has no falsifiable cell at
all.

The last row has no author. ``.credit__work`` and ``.credit__author`` share a line, so
while an author follows it the work's trailing full stop is an *interior* neutral
between two Latin runs and cannot move -- measured 0 scrambles with the fix neutralised,
which is a green cell that proves nothing. ``author=None`` is legal on ``ImageCredit``
and puts the work last on its line, where it does move: measured
``Qinling giant panda (2007).`` -> ``.Qinling giant panda (2007)``.
"""

TROJAN_CREDITS: Final = tuple(
    replace(credit, work=credit.work + ISOLATE_INITIATOR) if index < 2 else credit
    for index, credit in enumerate(LATIN_CREDITS)
)
"""The same rows with an unterminated initiator ending the first two works.

Two attacked and two not, so the cell can tell the neighbour attack from the ordinary
RTL-page bug: with the fix neutralised the attacked authors paint
``.Ansel Adams (photographer)—`` -- the em dash relocated to the far end -- where the
unattacked ones only lose their full stop to the front."""

FACT_VALUES: Final = (
    HEADLINE_VALUE,
    BRACKETED_VALUE,
    "12.5 kg/day.",
    "1.2-1.5 m (adult)",
)
FACT_PUBLISHERS: Final = (
    HOST_PUBLISHER,
    "wwf.org.uk.",
    "iucnredlist.org.",
    "smithsonianmag.com.",
)

UNPROTECTED_PROSE: Final = "A bear that eats a grass (mostly)."
"""The subtitle, and the only string in this file deliberately left broken.

``.subtitle`` is not one of the eleven and carries no ``unicode-bidi`` of its own, so on
an RTL page its trailing full stop really does move. That makes it the control for the
instrument itself -- see
:func:`test_the_paint_order_walker_reports_an_element_that_really_does_reorder`."""


def latin_content() -> ResearchContent:
    """Latin research whose every attributable field ends in a bidi-neutral character."""
    facts = tuple(
        Fact(
            label=f"Bamboo metric {index:02d}",
            value=value,
            detail=f"Measured across {index:02d} reserves (2024).",
            source=Source(
                url=f"https://example.org/study-{index:02d}",
                title=f"Bamboo intake across reserves {index:02d} (2024).",
                publisher=FACT_PUBLISHERS[index],
            ),
        )
        for index, value in enumerate(FACT_VALUES)
    )
    return ResearchContent(
        title="The Giant Panda",
        subtitle=UNPROTECTED_PROSE,
        summary="Pandas spend most of their waking hours eating bamboo (roughly 14).",
        facts=facts,
        sections=(
            NarrativeSection(
                heading="Diet",
                body="Bamboo makes up almost the whole diet (99%).",
                sources=(
                    Source(
                        url="https://example.org/diet",
                        title="Diet of the giant panda (2019).",
                        publisher="nature.com.",
                    ),
                ),
            ),
        ),
        keywords=("giant panda",),
        sources=tuple(fact.source for fact in facts if fact.source is not None),
    )


ARABIC_VALUES: Final = (
    "ثمانية وثلاثون.",
    "مائة جرام.",
    "اثنا عشر.",
    "خمسة كيلوجرامات.",
)
"""Amounts spelled out in Arabic words rather than in Arabic-Indic digits.

Digits are the reason. A digit is ``AN`` or ``EN`` to the bidi algorithm and gets its
own embedding level inside an RTL run, so ``١٢-٣٨`` paints as ``٣٨-١٢`` -- correct, and
*not* the reverse of what was written. The mirror identity the Arabic cell asserts holds
only where every strong and numeric character in the string is right-to-left, which
:func:`is_uniformly_rtl` is what enforces rather than assumes."""

ARABIC_TITLES: Final = (
    "استهلاك الخيزران اليومي.",
    "وزن المولود الجديد.",
    "مساحة الموطن المحمي.",
    "عدد المحميات الطبيعية.",
)
ARABIC_PUBLISHERS: Final = (
    "الصندوق العالمي.",
    "مجلة سميثسونيان.",
    "القائمة الحمراء.",
    "دار النشر.",
)
ARABIC_CREDITS: Final = (
    CreditFields("رخصة المشاع.", "أحمد الطيب.", "باندا في موطنها.", False),
    CreditFields("رخصة حرة.", "سعاد منصور.", "رأس الباندا.", False),
    CreditFields("رخصة عامة.", "كريم دولي.", "باندا تأكل الخيزران.", False),
    CreditFields("رخصة النسبة.", "جيمس المرشد.", "باندا تشينلينغ.", False),
)
"""Arabic credits, none of them ``modified``.

``adapted`` is a literal English word in ``_caption``, so a modified Arabic figure gets
a caption that is no longer uniformly right-to-left and drops out of the mirror
comparison. Not modifying any of them keeps all four captions in the measurement; the
cost is that ``.credit__adapted`` renders on no Arabic page, which costs nothing because
its text is an English literal in the template either way -- see
:data:`STATIC_ENGLISH`."""


def arabic_content() -> ResearchContent:
    """Arabic facts *and* Arabic prose, not merely an Arabic locale.

    A page that only flipped ``<html dir>`` would measure the instrument rather than the
    layout, which is the point ``tests/test_template_bodies.py``'s ``ARABIC_SECTIONS``
    makes at length. These are shaped like those and kept short enough to fit one line
    at 1200px, because the mirror identity is a within-line claim.
    """
    facts = tuple(
        Fact(
            label=f"مؤشر الخيزران رقم {'ابجد'[index]}",
            value=value,
            detail="قياس عبر محميات الصين الوسطى.",
            source=Source(
                url=f"https://example.org/ar-{index}",
                title=ARABIC_TITLES[index],
                publisher=ARABIC_PUBLISHERS[index],
            ),
        )
        for index, value in enumerate(ARABIC_VALUES)
    )
    return ResearchContent(
        title="الباندا العملاق",
        subtitle="دب يأكل نوعا من العشب.",
        summary="يقضي الباندا العملاق معظم ساعات يقظته في مضغ الخيزران في غابات الصين.",
        facts=facts,
        sections=(
            NarrativeSection(
                heading="الغذاء والموطن",
                body="يقضي الباندا العملاق معظم ساعات يقظته في مضغ الخيزران.",
                sources=(
                    Source(
                        url="https://example.org/ar-habitat",
                        title="الغذاء والموطن.",
                        publisher="دار المعرفة.",
                    ),
                ),
            ),
        ),
        keywords=("الباندا",),
        sources=tuple(fact.source for fact in facts if fact.source is not None),
    )


PANDA_FILES: Final = (
    "giant-panda-in-habitat.jpg",
    "giant-panda-portrait.jpg",
    "giant-panda-eating-bamboo.jpg",
    "giant-panda-cub.jpg",
)
"""Four real fixtures, so the page carries a hero plus three figures in every body:
``stat_grid``'s band holds three and the other two place everything they are given."""


def assets(credits: Sequence[CreditFields]) -> tuple[ImageAsset, ...]:
    return tuple(
        ImageAsset(
            content=PANDA_DIR / PANDA_FILES[index],
            mime_type="image/jpeg",
            width_px=1600,
            height_px=1066,
            alt_text=f"Panda plate {index:02d}",
            credit=ImageCredit(
                license=fields.license,
                author=fields.author,
                license_url=f"https://creativecommons.org/licenses/x/{index}.0/",
                source=Source(
                    url=f"https://commons.example.org/file-{index}",
                    title=fields.work,
                ),
                modified=fields.modified,
            ),
            role=ImageRole.HERO if index == 0 else ImageRole.SUPPORTING,
        )
        for index, fields in enumerate(credits)
    )


RTL_LOCALE: Final = "ar-EG"
"""The honest lever. ``_bcp47`` turns this into ``<html lang="ar-EG" dir="rtl">``, so
the page is right-to-left the way a real brief makes it right-to-left -- not by a
``str.replace`` on the composed document, which measures the instrument."""


def rtl_brief() -> Brief:
    return make_brief(
        options=RenderOptions(width_px=RenderOptions().width_px, theme=Theme.LIGHT),
        locale=RTL_LOCALE,
    )


async def compose_rtl(
    template_id: str, content: ResearchContent, images: Sequence[ImageAsset]
) -> Composition:
    """One body at ``ar-EG``.

    ``compose_cell`` hard-codes ``locale="en-US"`` and builds its brief internally, so a
    locale cannot reach it; this is otherwise the same call it makes.
    """
    return await HtmlComposer(template_id=template_id).compose(
        rtl_brief(), content, images
    )


# --------------------------------------------------------------------------- #
# The twelve selectors the eleven rules cover
# --------------------------------------------------------------------------- #

SHARED_SELECTORS: Final = (
    ".tick",
    ".hero__credit",
    ".refs li",
    ".credit__license",
    ".credit__work",
    ".credit__author",
    ".credit__adapted",
)
"""The seven that come from the chrome, so every body renders all of them.

``.tick`` is the whole ``*__src`` attribution family -- every one is
``<p class="X__src tick">`` -- plus the ``Sources`` / ``Image credits`` /
``By the numbers`` chrome labels. It is one selector and one rule; it is not one
element."""

FIGURE_CAPTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "stat_grid": ".band figcaption",
        "process_flow": ".plates figcaption",
        "ranked_list": ".plates figcaption",
    }
)
"""Where each body captions its non-hero figures. ``stat_grid`` calls the row a band and
the other two call it plates, which is why eleven rules need twelve selector strings:
``.plates figcaption`` is declared once in ``process_flow.css`` and once in
``ranked_list.css``."""

STATIC_ENGLISH: Final = frozenset({".credit__adapted"})
"""The one selector whose entire content is a literal in ``_base.html.j2``.

``adapted from the original`` is written by the template, not scraped, so every
character in it is strong Latin and *no* bidi algorithm can move any of them: measured
0 scrambles with the declaration neutralised, with it replaced by ``direction: ltr``,
and as shipped. Its rule is not pointless -- it is one of the three bare ``<span>``s the
UA sheet leaves at ``unicode-bidi: normal``, and the day the phrase becomes localised or
interpolated it is the only thing standing between a colophon and a reordered one -- but
it cannot be falsified by paint order today, and pretending otherwise is exactly the
kind of green cell this file exists to avoid. It is measured for coverage and excluded
from the two cells that need content that can move."""


def protected_selectors(template_id: str) -> tuple[str, ...]:
    """The nine rules that reach one page: the shared seven, plus the body's two."""
    return (
        *SHARED_SELECTORS,
        BODY_SELECTORS[template_id].value,
        FIGURE_CAPTIONS[template_id],
    )


PROTECTED_CELLS: Final = tuple(
    (template_id, selector)
    for template_id in TEMPLATE_IDS
    for selector in protected_selectors(template_id)
)
PROTECTED_IDS: Final = [f"{body}:{selector}" for body, selector in PROTECTED_CELLS]

FALSIFIABLE_CELLS: Final = tuple(
    cell for cell in PROTECTED_CELLS if cell[1] not in STATIC_ENGLISH
)
FALSIFIABLE_IDS: Final = [f"{body}:{selector}" for body, selector in FALSIFIABLE_CELLS]

EVERY_SELECTOR = pytest.mark.parametrize(
    ("template_id", "selector"), PROTECTED_CELLS, ids=PROTECTED_IDS
)
EVERY_MOVABLE_SELECTOR = pytest.mark.parametrize(
    ("template_id", "selector"), FALSIFIABLE_CELLS, ids=FALSIFIABLE_IDS
)


def test_the_selector_table_covers_every_rule_the_fix_touches() -> None:
    """The parametrisation is the whole population of protected rules, not a sample.

    Two ways this file could silently measure nothing. ``FIGURE_CAPTIONS`` is written by
    hand, so a fourth renderable body would raise ``KeyError`` here rather than ship with
    no bidi coverage. And ``PROTECTED_CELLS`` is derived at runtime, so an empty registry
    would turn every browser cell into pytest's ``got empty parameter set`` skip --
    green, silent, and measuring nothing at all.
    """
    assert set(FIGURE_CAPTIONS) == RENDERABLE_TEMPLATE_IDS, (
        "FIGURE_CAPTIONS and RENDERABLE_TEMPLATE_IDS have diverged: missing "
        f"{sorted(RENDERABLE_TEMPLATE_IDS - set(FIGURE_CAPTIONS))}, stale "
        f"{sorted(set(FIGURE_CAPTIONS) - RENDERABLE_TEMPLATE_IDS)}. A renderable body "
        "that names no figure caption gets no bidi cell for its captions"
    )
    assert PROTECTED_CELLS, (
        "no body is renderable, so every browser cell in this file collapses to an "
        "empty parameter set and skips instead of failing"
    )
    measured = {selector for _, selector in PROTECTED_CELLS}
    expected = {
        *SHARED_SELECTORS,
        *(BODY_SELECTORS[body].value for body in TEMPLATE_IDS),
        *FIGURE_CAPTIONS.values(),
    }
    assert measured == expected, (
        f"the selector table has drifted: missing {sorted(expected - measured)}, "
        f"stale {sorted(measured - expected)}"
    )
    assert len(measured) == 12, (
        f"expected the eleven protected rules to need twelve selector strings, got "
        f"{len(measured)}: {sorted(measured)}. Eleven rules, twelve strings, because "
        "'.plates figcaption' is declared in both process_flow.css and ranked_list.css"
    )
    assert STATIC_ENGLISH <= measured, (
        f"{sorted(STATIC_ENGLISH - measured)} is excluded from the falsifiable cells "
        "but is not in the table at all, so it is measured nowhere"
    )
    assert not any(selector.strip() == "" for _, selector in PROTECTED_CELLS), (
        "a blank selector is not a placeholder -- querySelectorAll raises on it, so the "
        "cell dies as a raw playwright SyntaxError naming nothing"
    )


# --------------------------------------------------------------------------- #
# 1. The headline case
# --------------------------------------------------------------------------- #


@BROWSER_LOOP
@BODIES
async def test_the_reported_value_paints_in_the_order_it_is_written(
    chromium: Browser, template_id: str
) -> None:
    """``26-84 lb (12-38 kg)`` reads the same on an Arabic page as on an English one.

    The string from the defect report, in the element the report named, in every body
    that has one. It painted as ``lb (12-38 kg) 26-84``: not a lost trailing character
    but a whole bracketed conversion swapped in front of the number it converts. A
    reader of that PNG gets a figure nobody published, with no cue that anything moved.
    """
    selector = BODY_SELECTORS[template_id].value
    composition = await compose_rtl(template_id, latin_content(), assets(LATIN_CREDITS))

    async with laid_out(chromium, composition) as page:
        measured = await paint_order(page, selector)

    assert measured, (
        f"{selector} matched no element of {template_id}, so this cell examined zero "
        "values and would pass for a body that had stopped rendering its facts"
    )
    reported = [row for row in measured if dense(HEADLINE_VALUE) in row.logical]
    assert reported, (
        f"none of the {len(measured)} {selector} elements on {template_id} holds "
        f"{HEADLINE_VALUE!r}, so this cell is no longer measuring the reported defect. "
        f"Measured: {[row.logical for row in measured]}"
    )
    scrambled = [row for row in measured if row.scrambled]
    assert not scrambled, (
        f"{len(scrambled)} of {len(measured)} {selector} elements on {template_id} are "
        f"painted in an order other than the one they are written in:\n"
        f"{describe(scrambled)}\n"
        "A display figure is the one thing on this page a reader is certain to read, "
        "and a reordered one is a number that was never measured"
    )


# --------------------------------------------------------------------------- #
# 2. Every protected rule, and its own falsification
# --------------------------------------------------------------------------- #


@BROWSER_LOOP
@EVERY_SELECTOR
async def test_every_protected_selector_paints_in_the_order_it_is_written(
    chromium: Browser, template_id: str, selector: str
) -> None:
    """Scraped Latin text on an RTL page, in all nine rules that reach the page.

    The claim is only about paint: for every element this selector matches, the string a
    reader copies out of the PNG is the string the pipeline put in the DOM. The element
    and character counts are asserted alongside it because "no element matched" and "no
    character moved" are the same green cell, and the first one is a bug.

    ``.tick`` is the widest of the nine: it is every fact's and section's ``*__src``
    attribution line *and* the three static chrome labels, so its population mixes
    scraped text with literals. That is deliberate -- the labels are what a page has
    when a source carried no publisher, and they have to survive the same rule.
    """
    composition = await compose_rtl(template_id, latin_content(), assets(LATIN_CREDITS))

    async with laid_out(chromium, composition) as page:
        measured = await paint_order(page, selector)

    assert measured, (
        f"{selector} matched no element with text on {template_id}, so this cell "
        "measured nothing. Either the body stopped rendering that field or the "
        "selector was renamed; a bidi rule over an element that never appears is a "
        "rule nothing tests"
    )
    characters = sum(len(row.logical) for row in measured)
    assert characters, (
        f"{selector} matched {len(measured)} elements on {template_id} but not one "
        "printing character, so there was nothing to reorder"
    )
    scrambled = [row for row in measured if row.scrambled]
    assert not scrambled, (
        f"{len(scrambled)} of {len(measured)} {selector} elements on {template_id} "
        f"paint in a different order from the one they are written in:\n"
        f"{describe(scrambled)}\n"
        "This is attribution, citation or a display figure landing in a PNG where no "
        "link can correct it, so a character in the wrong place is the deliverable "
        "being wrong"
    )


@BROWSER_LOOP
@EVERY_MOVABLE_SELECTOR
async def test_neutralising_the_declaration_reorders_what_each_cell_measures(
    chromium: Browser, template_id: str, selector: str
) -> None:
    """The control for the cell above: this content really can move.

    ``test_every_protected_selector_paints_in_the_order_it_is_written`` asserts an
    absence, and an absence over content with no reorderable character in it is
    vacuous -- which is not a hypothetical here, because ``<p>``, ``<li>`` and
    ``<figcaption>`` already compute ``unicode-bidi: isolate`` from the UA stylesheet.
    Nine of the twelve selectors therefore *look* protected with the fix deleted. This
    cell measures the same rects on a scratch copy of the same page with the declaration
    replaced by its initial value, and requires the scramble to appear.

    So this is not a claim about a stylesheet's contents, and the assertion is still on
    character rects. What it establishes is that the eleven rules are load-bearing for
    the fixtures above: if this cell goes green the sibling cell above it has stopped
    proving anything, whatever the CSS says.
    """
    composition = await compose_rtl(template_id, latin_content(), assets(LATIN_CREDITS))

    async with laid_out(chromium, with_declaration(composition, UNPROTECTED)) as page:
        measured = await paint_order(page, selector)

    assert measured, (
        f"{selector} matched no element with text on {template_id}: the control cannot "
        "say anything about a selector that matches nothing"
    )
    scrambled = [row for row in measured if row.scrambled]
    assert scrambled, (
        f"with {DECLARATION!r} replaced by {UNPROTECTED!r}, none of the "
        f"{len(measured)} {selector} elements on {template_id} moved a character. The "
        "fixture for this selector therefore carries nothing a right-to-left paragraph "
        "could relocate, and the cell above it is green for that reason rather than "
        f"because the fix works. Measured:\n{describe(measured)}"
    )


# --------------------------------------------------------------------------- #
# 3. The individual shapes that were measured to move
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Shape:
    """A field value, and the selector that renders it."""

    supplied: str
    """The string as the research or imagery stage hands it over."""
    selector: str | None
    """Where the page paints it; ``None`` means the body's display value, which is
    spelled differently in each of the three."""
    why: str


REPORTED_SHAPES: Final = (
    Shape(PLUS_LICENCE, ".credit__license", "a licence identifier ending in '+'"),
    Shape(HOST_PUBLISHER, ".tick", "a publisher fallen back to a host, ending in '.'"),
    Shape(STOP_AUTHOR, ".credit__author", "a photographer's name ending in '.'"),
    Shape(BRACKETED_VALUE, None, "a fact value ending in ')'"),
)
"""One shape per bidi-neutral tail the defect report measured, each in the field that
really renders it. ``v4.0+`` in a licence and ``example.com.`` in an attribution line
are not the same test twice: they land in different rules, in different stylesheets, on
different tags -- a ``<p class="credit__license">`` that the UA sheet isolates, and a
``<p class="X__src tick">`` that it also isolates but which sits inside an inverted
panel with ``text-transform: uppercase`` over it."""


@BROWSER_LOOP
@BODIES
@pytest.mark.parametrize("shape", REPORTED_SHAPES, ids=[s.supplied for s in REPORTED_SHAPES])
async def test_each_reported_trailing_neutral_shape_survives_its_field(
    chromium: Browser, template_id: str, shape: Shape
) -> None:
    """Each measured shape reaches the page, and reaches it in written order.

    Both halves matter. ``dense(shape.supplied) in row.logical`` fails if the fixture
    stopped producing the string -- a renamed field, a sanitiser that started stripping
    it, an imagery stage that dropped the author -- which would otherwise leave a cell
    asserting written order over content that no longer contains the character that
    moves.
    """
    selector = shape.selector or BODY_SELECTORS[template_id].value
    composition = await compose_rtl(template_id, latin_content(), assets(LATIN_CREDITS))

    async with laid_out(chromium, composition) as page:
        measured = await paint_order(page, selector)

    holding = [row for row in measured if dense(shape.supplied) in row.logical]
    assert holding, (
        f"{shape.supplied!r} ({shape.why}) reaches no {selector} element on "
        f"{template_id}, so nothing here is measuring that shape. The "
        f"{len(measured)} elements that did render: {[r.logical for r in measured]}"
    )
    scrambled = [row for row in holding if row.scrambled]
    assert not scrambled, (
        f"on {template_id}, {selector} paints {shape.why} out of order:\n"
        f"{describe(scrambled)}\n"
        f"{shape.supplied!r} is what the pipeline produced; the second string is what "
        "a reader of the PNG gets"
    )


# --------------------------------------------------------------------------- #
# 4. The regression `direction: ltr` would have caused
# --------------------------------------------------------------------------- #


def is_uniformly_rtl(text: str) -> bool:
    """True when every strong or numeric character in ``text`` is right-to-left.

    This is the precondition for the mirror identity below, and it is a statement about
    embedding *levels* rather than about scripts. ``L``, ``EN`` and ``AN`` characters
    each get a level of their own inside a right-to-left paragraph and are reordered
    within it, so a string containing one does not paint as its own reverse even when it
    is painted perfectly correctly -- an Arabic sentence with ``١٢-٣٨`` in it is the
    ordinary case. Neutrals -- brackets, the interpunct, the full stop, spaces -- take
    the surrounding level and are fine.
    """
    classes = {unicodedata.bidirectional(char) for char in text}
    return bool(classes & {"R", "AL"}) and not (classes & {"L", "EN", "AN"})


def mirrorable(rows: Sequence[Painted]) -> tuple[Painted, ...]:
    """The elements whose correct paint is exactly ``reverse(logical)``.

    One line, because the identity is a within-line claim: a wrapped element reads as
    line after line, and reversing the whole string across a wrap compares nothing.
    """
    return tuple(row for row in rows if row.lines == 1 and is_uniformly_rtl(row.logical))


ARABIC_CELLS: Final = FALSIFIABLE_CELLS
ARABIC_IDS: Final = FALSIFIABLE_IDS
"""``.credit__adapted`` is excluded for the reason :data:`STATIC_ENGLISH` gives, and
because no Arabic fixture renders it: :data:`ARABIC_CREDITS` sets ``modified=False``
throughout so the captions stay uniformly right-to-left."""


@BROWSER_LOOP
@pytest.mark.parametrize(("template_id", "selector"), ARABIC_CELLS, ids=ARABIC_IDS)
async def test_arabic_content_still_paints_right_to_left(
    chromium: Browser, template_id: str, selector: str
) -> None:
    """Genuinely Arabic text in the eleven rules is still painted right-to-left.

    This is the cell that chose ``plaintext`` over ``isolate; direction: ltr``, and the
    most valuable one in the file, because the two are indistinguishable on a Latin
    page: both repair all 44 broken elements, differing on 0 of 99 measured. What
    separates them is content in the script the page is actually in.

    ``visual == logical`` is **not** the claim, and asserting it here would be asserting
    that correct Arabic is broken -- an Arabic string paints as its own reverse, so 65 of
    75 elements "scramble" under every variant including the right one. The claim is the
    mirror identity: for an element whose every strong character is right-to-left, the
    painted order is exactly the reverse of the written order, meaning the trailing full
    stop is painted at the *left* end where an Arabic reader reads it last.

    The control is in the cell. Under ``isolate; direction: ltr`` the same element's
    stop is painted at the right end instead and reads as a *leading* period -- the
    original defect, mirrored, on content the original defect never touched: measured 59
    of 75 elements corrupted against ``plaintext``'s 0. So the cell renders the same
    Arabic page twice, once as shipped and once with the URL declaration substituted in,
    and requires the identity to hold on the first and break on the second. Without that
    second measurement this cell would pass under both declarations, since
    ``unicode-bidi: normal`` on an RTL page is *also* correct for Arabic -- the mirror
    holds there too. It only fails for ``direction: ltr``, which is precisely the
    regression it is here to prevent.
    """
    composition = await compose_rtl(
        template_id, arabic_content(), assets(ARABIC_CREDITS)
    )

    async with laid_out(chromium, composition) as page:
        as_shipped = await paint_order(page, selector)

    pool = mirrorable(as_shipped)
    assert pool, (
        f"no single-line, uniformly right-to-left element matched {selector} on "
        f"{template_id}, so this cell compared nothing. Either the Arabic fixture "
        "stopped reaching this field, or its strings grew long enough to wrap, or a "
        f"digit crept into one. Measured {len(as_shipped)} elements: "
        f"{[(r.logical, r.lines) for r in as_shipped]}"
    )
    wrong_way = [row for row in pool if row.visual != row.logical[::-1]]
    assert not wrong_way, (
        f"{len(wrong_way)} of {len(pool)} Arabic {selector} elements on {template_id} "
        f"are not painted right-to-left:\n{describe(wrong_way)}\n"
        "Each of these should paint as the exact reverse of what it holds. A trailing "
        "full stop that moved to the visual right reads as a leading period to an "
        "Arabic reader, which is the same defect the fix was for, aimed the other way"
    )

    async with laid_out(chromium, with_declaration(composition, AS_A_URL)) as page:
        as_a_url = await paint_order(page, selector)

    corrupted = [row for row in mirrorable(as_a_url) if row.visual != row.logical[::-1]]
    assert corrupted, (
        f"with {DECLARATION!r} replaced by {AS_A_URL!r}, every Arabic {selector} "
        f"element on {template_id} still painted right-to-left -- so this cell cannot "
        "tell the shipped declaration from the one that corrupts Arabic, and the cell "
        "above it is green under both. Either the Arabic fixture no longer reaches this "
        f"selector or the substitution matched nothing. Measured: {describe(as_a_url)}"
    )


# --------------------------------------------------------------------------- #
# 5. The Trojan Source neighbour
# --------------------------------------------------------------------------- #

ATTACKED_AUTHORS: Final = tuple(
    dense(credit.author)
    for credit, trojan in zip(LATIN_CREDITS, TROJAN_CREDITS, strict=True)
    if credit.author is not None and trojan.work.endswith(ISOLATE_INITIATOR)
)
"""The authors whose *preceding* sibling carries the unterminated initiator."""


@BROWSER_LOOP
@BODIES
async def test_an_unterminated_isolate_initiator_does_not_reorder_the_next_field(
    chromium: Browser, template_id: str
) -> None:
    """A U+2067 at the end of a scraped work title leaves the author beside it alone.

    ``.credit__work`` and ``.credit__author`` are two bare ``<span>``s on one line of one
    ``<p>``, and ``_legible_text`` deliberately keeps U+2066-U+2069 -- they are how
    correct mixed-direction prose is written, and the deprecated embeddings it does
    replace are the leaky ones. So an unterminated initiator in a work title is
    reachable, and without a base direction of its own the author span is reordered
    inside the initiator's scope. Measured with the declaration neutralised:
    ``— Ansel Adams (photographer).`` painted as ``.Ansel Adams (photographer)—``, with
    the em dash relocated to the far end -- a signature only a *neighbour's* initiator
    produces, since the ordinary RTL-page bug moves the trailing stop and nothing else.

    Two of the four colophon rows are attacked and two are not, so the cell also
    establishes that the attack is what it thinks it is. The precondition asserts the
    initiator survived the sanitiser and reached the DOM: if it were stripped this would
    be a cell about an attack that never happened.
    """
    composition = await compose_rtl(template_id, latin_content(), assets(TROJAN_CREDITS))

    async with laid_out(chromium, composition) as page:
        neighbours = _rows(await page.evaluate(PRECEDING_TEXT_JS, ".credit__author"))
        as_shipped = await paint_order(page, ".credit__author")

    spoofed = [
        _text(_fields(row)["own"])
        for row in neighbours
        if ISOLATE_INITIATOR in _text(_fields(row)["preceding"])
    ]
    assert spoofed, (
        f"no .credit__author on {template_id} has a preceding sibling containing "
        f"U+2067, so the spoof never reached the DOM and this cell measures a clean "
        "page. Either layout's sanitiser started replacing the isolate initiators -- "
        "which would be a change of contract, not a bug fix -- or TROJAN_CREDITS "
        f"stopped carrying one. Siblings seen: {[_fields(r)['preceding'] for r in neighbours]}"
    )
    assert len(spoofed) == len(ATTACKED_AUTHORS), (
        f"{len(spoofed)} attacked .credit__author elements on {template_id}, expected "
        f"{len(ATTACKED_AUTHORS)}: the cell distinguishes the neighbour attack from the "
        "ordinary right-to-left bug by having rows of both kinds on one page"
    )

    scrambled = [row for row in as_shipped if row.scrambled]
    assert not scrambled, (
        f"an unterminated U+2067 in a neighbouring field reordered "
        f"{len(scrambled)} of {len(as_shipped)} .credit__author elements on "
        f"{template_id}:\n{describe(scrambled)}\n"
        "A scraped title can therefore rewrite the photographer's name printed beside "
        "it, which is a spoofed attribution in a PNG that cannot be corrected"
    )

    async with laid_out(chromium, with_declaration(composition, UNPROTECTED)) as page:
        unprotected = await paint_order(page, ".credit__author")

    victims = [
        row
        for row in unprotected
        if row.scrambled and any(author in row.logical for author in ATTACKED_AUTHORS)
    ]
    assert victims, (
        f"with {DECLARATION!r} replaced by {UNPROTECTED!r}, no attacked "
        f".credit__author on {template_id} moved a character -- so the spoof is inert "
        "against this fixture and the assertion above it is vacuous. Measured:\n"
        f"{describe(unprotected)}"
    )


# --------------------------------------------------------------------------- #
# 6. A guard on the instrument itself
# --------------------------------------------------------------------------- #


@BROWSER_LOOP
@BODIES
async def test_the_paint_order_walker_reports_an_element_that_really_does_reorder(
    chromium: Browser, template_id: str
) -> None:
    """The measurement can see a scramble, on a page nobody edited to make it visible.

    Every other cell above asserts an absence, and an absence is also what a broken
    instrument reports: a ``PAINT_ORDER_JS`` that stopped finding text nodes, a
    ``Range`` whose rects came back empty, a sort that lost its comparator would each
    turn this whole file green while the page reordered 44 elements.

    ``.subtitle`` is the control because it is *not* one of the eleven: it carries no
    ``unicode-bidi`` of its own, so on an RTL page its trailing full stop genuinely
    relocates -- measured ``A bear that eats a grass (mostly).`` ->
    ``.A bear that eats a grass (mostly)``. It needs no substitution and no scratch copy
    of the HTML; it is the shipped page, painting wrongly, in an element the fix
    deliberately does not cover.

    If this cell ever fails, the two possibilities are worth separating before touching
    anything else: either the instrument stopped measuring, or ``.subtitle`` gained bidi
    protection of its own. The second is a fine change to make -- the subtitle is
    scraped prose like everything else -- but it costs this file its only control that
    does not depend on a string substitution, so it needs a new one first.
    """
    composition = await compose_rtl(template_id, latin_content(), assets(LATIN_CREDITS))

    async with laid_out(chromium, composition) as page:
        measured = await paint_order(page, ".subtitle")

    assert measured, (
        f"no .subtitle rendered on {template_id}, so the instrument's own control "
        "measured nothing"
    )
    assert any(dense(UNPROTECTED_PROSE) == row.logical for row in measured), (
        f".subtitle on {template_id} does not hold {UNPROTECTED_PROSE!r}, whose "
        f"trailing full stop is the character this control watches move. Measured: "
        f"{[row.logical for row in measured]}"
    )
    scrambled = [row for row in measured if row.scrambled]
    assert scrambled, (
        f"the paint-order walker reports every .subtitle on {template_id} as painted "
        f"in written order:\n{describe(measured)}\n"
        "That element is not one of the eleven and carries no unicode-bidi, so on an "
        "ar-EG page its trailing full stop must relocate. Reporting no movement means "
        "either the instrument has stopped measuring -- in which case every other cell "
        "in this file is vacuously green -- or .subtitle has been given bidi protection "
        "and this control needs replacing with an element that still lacks it"
    )
