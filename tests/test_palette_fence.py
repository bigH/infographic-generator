"""The palette fence: proof that the design tokens reach the rasterised PNG.

A purely chromatic regression -- a token edited to a colour that makes text
unreadable, or simply to the wrong colour -- used to pass this entire suite. There
was no golden file, no pixel diff, and no assertion anywhere that the values
declared in ``css/_chrome.css`` survive as far as the deliverable.

The perceptual hash that was first proposed for the job cannot do it.
``imagery.prepare.fingerprint`` is an 8x8 average hash: it converts to greyscale
(colour discarded outright) and thresholds each cell against the image's own mean
(a global shift cancels), so it is invariant to a palette regression *by
construction*. Measured on this page: an 8% ``--accent-paper`` nudge, a ``--paper``
hue swap touching 41% of the pixels, a body-text colour swap, a credit line turned
red and a 1.5px font-size reflow all score Hamming **0** -- while ``stat_grid``
against ``ranked_list``, two entirely different layouts, score **3**. The signal
band sits inside the noise band, so no threshold exists, at side 8, 16 or 32. None
of that is a criticism of ``fingerprint``, which is correct for its own job:
recognising the same photograph after a re-encode. It is the wrong instrument here.

What works instead is three layers, and it is worth being exact about which layer
catches what, because they are not interchangeable.

**A token edited to the wrong value is caught in pure Python, with no browser at
all** -- ``declared_tokens`` reads the stylesheet's own declarations and
``test_every_colour_the_chrome_declares_is_pinned_by_a_probe`` compares them to the
literals here. That is the cheapest and strongest single assertion in the module.
**A token that resolves wrongly on the page is caught by computed style**, which is
also where the six tokens with no flat fill to sample get watched -- among them the
licence URI a reader has to retype out of the PNG.

**The rasterised pixels are here for a third class the other two cannot see: paint
time.** A colour can be declared correctly, resolve correctly, and still not reach
the deliverable. Measured against injected regressions: ``.rule { opacity: 0.5 }``
reads ``#a4aa7a`` where the token says ``#5c6a12``; ``body { filter: grayscale(1) }``
turns all three samples grey; a masthead pulled up by ``margin-top: -6px`` covers the
accent bar entirely. **Every one of those leaves both other layers green.** Opacity,
filters and occlusion are exactly what gets added when someone is nudging a design,
so do not delete the browser cells on the grounds that the CSS-parse test already
compares the colours. It does -- and it cannot see any of this.

A flat fill is one colour memset into the surface -- no antialiasing at an interior
point, no compositing -- so a pixel taken well inside one lands on the token hex
**exactly**. Three properties make that trustworthy rather than lucky:

* **Coordinates are derived, never hardcoded.** Every sample point comes from the
  element's own ``getBoundingClientRect()`` at run time. Rect ``y`` is fractional and
  differs per body, so a committed pixel offset would be a fence that rots.
* **Flatness is a checked precondition.** The 5x5 device-pixel neighbourhood of
  each point is measured to be a single colour before that colour is compared. If a
  gradient or a texture ever lands on a sampled region, the guard fails first and
  names the selector, instead of the comparison failing for a reason nobody can
  read.
* **Exact equality, no tolerance.** Even +/-1 per channel would swallow the accent
  nudge this module was built for: ``#5c6a12`` to ``#657414`` is a peak channel delta
  of 10 and 0.66% of the page, and it is an entirely realistic edit.

Committed here: token hex strings, three computed literals, and selector names.
**No PNG, and no byte-exact ``sha256`` golden either.** Determinism is not the
problem: three renders of one composition are byte-identical in and across
processes, and so are chromium 148, 149 and 151, with all four faces embedded as
data URIs. The problem is that a sha is one bit and the platform moves more of the
page than the defect does. Dropping the CoreText-only
``-webkit-font-smoothing: antialiased`` moves 26,921 pixels at peak channel delta
195, where the accent regression this fence exists for moves 8,339 at peak 10 --
3.2x less area, 19.5x less amplitude. The false positive strictly contains the
signal, so no exact hash and no per-pixel budget separates them, and a laptop hash
would fire on every other machine and never on the defect. That is the same shape
of result that killed the perceptual hash, measured on a different axis.

Seen from the other side, though, the sampled pixels below *are* a golden -- taken
over a mask chosen to contain no glyphs, with the expected values transcribed as hex
instead of committed as bytes. Text is the whole reason a whole-frame golden fails
here, so a fence that never samples text keeps both properties at once: sensitive to
a peak-10 nudge, and indifferent to which font rasteriser drew the page.

TODO: the colophon's ``--dim-patch`` and ``--accent-patch`` are pinned by computed
style only, so a scrim or an ``opacity`` over that panel would leave legally
load-bearing attribution unreadable with this module green. The right instrument is a
computed-contrast fence that composites the colour stack and asserts a WCAG ratio --
a sibling module, not another sampled pixel.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from PIL import Image, ImageColor

from infographic_generator.composition import HtmlComposer
from infographic_generator.composition.composer import TEMPLATE_DIR
from infographic_generator.core.models import (
    Composition,
    ImageAsset,
    RenderOptions,
    Theme,
)
from infographic_generator.render import PlaywrightRenderer
from tests.test_composition import (  # noqa: F401 -- ``chromium`` is a fixture,
    BODIES,  # requested by name in the signatures below and never called
    BROWSER_LOOP,
    IN_BOTH_THEMES,
    PANDA_SET,
    THEMES,
    _number,
    _rows,
    chromium,
    laid_out,
    make_brief,
    make_content,
)

# ``BODIES``/``IN_BOTH_THEMES``/``THEMES`` are imported rather than rebuilt: they are
# the matrix every rendered-page fence in this repo runs over, they come with the test
# that proves both axes are the whole population, and a second copy here would be a
# second thing to remember when a body or a theme is added. ``test_css_injection.py``
# imports the same two marks. ``_number`` and ``_rows`` narrow untyped browser JSON and
# are imported across the underscore deliberately -- cloning three-line assertions into
# a second module is the worse of the two evils.


if TYPE_CHECKING:
    from playwright.async_api import Browser, Page


# --------------------------------------------------------------------------- #
# The tokens, exactly as ``css/_chrome.css`` declares them
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ThemedColour:
    """One design token's value in each theme, lowercase six-digit hex."""

    light: str
    dark: str

    def hex_for(self, theme: Theme) -> str:
        return self.light if theme is Theme.LIGHT else self.dark

    def rgb_for(self, theme: Theme) -> str:
        """The same colour spelled the way ``getComputedStyle`` returns it.

        Derived rather than typed out a second time: the hex above is the one
        committed value per token per theme, so the two halves of this fence cannot
        drift apart, and twenty hand-written ``rgb(...)`` strings cannot harbour a
        transposed digit that quietly weakens an assertion.
        """
        value = self.hex_for(theme)
        channels = (value[1:3], value[3:5], value[5:7])
        return "rgb({})".format(", ".join(str(int(pair, 16)) for pair in channels))


TOKENS: Final[Mapping[str, ThemedColour]] = MappingProxyType(
    {
        "--paper": ThemedColour(light="#eceae2", dark="#14130d"),
        "--patch": ThemedColour(light="#17150f", dark="#eceae2"),
        "--on-paper": ThemedColour(light="#1a1712", dark="#eeece3"),
        "--on-patch": ThemedColour(light="#f1efe6", dark="#17150f"),
        "--dim-paper": ThemedColour(light="#6a6558", dark="#8e8a79"),
        "--dim-patch": ThemedColour(light="#a29e8d", dark="#6a6558"),
        "--rule-paper": ThemedColour(light="#c8c4b7", dark="#343024"),
        "--rule-patch": ThemedColour(light="#3b3729", dark="#c8c4b7"),
        "--accent-paper": ThemedColour(light="#5c6a12", dark="#bccb4e"),
        "--accent-patch": ThemedColour(light="#bccb4e", dark="#5c6a12"),
    }
)
"""Every colour the chrome declares, transcribed from ``:root`` and
``:root[data-theme="dark"]``.

Transcribed on purpose. A fence that read these out of the stylesheet it is
fencing would pass any palette edit whatsoever -- it would only ever prove that
Chromium can parse a hex triple. Because the values live here instead, changing a
token is a deliberate two-file edit and an accidental one is three red tests.
"""


# --------------------------------------------------------------------------- #
# Where each token is supposed to land
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StyleProbe:
    """One token, and the one computed property that has to resolve to it."""

    token: str
    selector: str
    prop: str
    """The property in ``getComputedStyle``'s camelCase spelling."""
    catches: str
    """What breaks in the output if this resolves to something else."""


TOKEN_STYLES: Final = (
    StyleProbe("--paper", "body", "backgroundColor", "the page background"),
    StyleProbe("--on-paper", "body", "color", "every word of body copy"),
    StyleProbe("--patch", ".masthead", "backgroundColor", "the masthead panel"),
    StyleProbe("--on-patch", ".masthead", "color", "the title and subtitle"),
    StyleProbe("--dim-paper", ".refs__meta", "color", "publisher and URL of a source"),
    StyleProbe("--dim-patch", ".credit__url", "color", "the licence URI to transcribe"),
    StyleProbe("--rule-paper", ".apparatus", "borderTopColor", "the bibliography rule"),
    StyleProbe("--rule-patch", ".credit", "borderTopColor", "the rule above a credit"),
    StyleProbe("--accent-paper", ".rule", "backgroundColor", "the accent bar up top"),
    StyleProbe("--accent-patch", ".credit__license", "color", "the licence name"),
)
"""One probe per token, on the element where that token is legible in the output.

Ten probes for ten tokens is not decoration: ``--dim-patch`` is the licence URI a
reader has to retype out of a PNG, and ``--accent-patch`` is the licence name
above it. A token nobody pins is a token free to turn the attribution the same
colour as the panel it sits on."""


@dataclass(frozen=True, slots=True)
class SampledPixel:
    """A token whose value is also read back out of the PNG itself.

    ``inset_y_css`` is measured down from the element's own top edge, in CSS
    pixels; the horizontal inset is shared. Both are small, both are checked
    against the element's box at run time, and neither is a page coordinate.
    """

    token: str
    selector: str
    inset_y_css: int
    through: str
    """Why this element shows this token -- ``.apparatus`` paints no background of its
    own, so what is sampled there is the page's ``--paper`` showing through. Named in
    the failure message, because a wrong colour here can also mean something was
    painted *over* the element rather than by it."""


SAMPLED_PIXELS: Final = (
    SampledPixel("--accent-paper", ".rule", 3, "its own background"),
    SampledPixel("--patch", ".masthead", 8, "its own background"),
    SampledPixel("--paper", ".apparatus", 8, "a transparent background, over body"),
)
"""The three flat fills big enough to sample, one per structural region.

``.colophon`` and ``.credit__url`` are deliberately absent: the pixel half renders
with ``images=()`` so that every sampled region is a flat token fill rather than a
photograph, and with no images there are no credits, so the whole colophon is
absent from the document (``_base.html.j2`` guards the footer on
``page.chrome.credits``). Those two are pinned by ``TOKEN_STYLES`` instead, on a
page built with the real panda assets.

``body`` at mid-height was tried as the ``--paper`` probe and rejected: in
``stat_grid`` and ``ranked_list`` the midpoint of the page lands on an ``on-ink``
panel and reads ``--patch``. ``.apparatus`` exists in every body, paints nothing,
and is flat for hundreds of pixels."""


@dataclass(frozen=True, slots=True)
class LiteralProbe:
    """A computed value that is a literal in the stylesheet rather than a token."""

    selector: str
    prop: str
    expected: str
    catches: str


BODY_FONT_SIZE: Final = "16.5px"
"""The reading size, pinned exactly.

Deliberately moving it means touching four places: the declaration in
``_chrome.css``, this constant, and the relative floor plus its prose in
``test_composition.py``'s ``test_body_copy_is_never_smaller_than_the_reading
_size``-shaped fence, which asserts a *band* on purpose so that it does not slide
when the reading size does. That fence and this pin are complements: one says
"never illegible", this one says "not changed by accident"."""

LITERAL_PROBES: Final = (
    LiteralProbe(
        "body", "fontSize", BODY_FONT_SIZE, "body copy grows and the page reflows"
    ),
    LiteralProbe(".rule", "height", "6px", "the accent bar thickens"),
    LiteralProbe(
        ".apparatus",
        "backgroundColor",
        "rgba(0, 0, 0, 0)",
        "the --paper pixel sample stops reading the page and starts reading a panel",
    ),
)
"""Three values that are not tokens but are load-bearing anyway.

The two lengths are here because the hash slept through them as thoroughly as
through the colours: body copy at 16.5px -> 18px reflowed the whole page and scored
Hamming 0, and the accent bar at 6px -> 7px scored 0. The third is the premise of
the ``--paper`` pixel sample -- ``.apparatus`` paints nothing, so what that sample
reads is the page behind it. Give ``.apparatus`` a background of its own and the
sample quietly measures the wrong thing."""


# --------------------------------------------------------------------------- #
# Sampling geometry
# --------------------------------------------------------------------------- #

DEVICE_SCALE: Final = 1.0
"""The scale factor this module renders at, passed explicitly.

``RenderOptions.device_scale_factor`` defaults to 2.0 and ``core/`` is frozen, so
this is a value the test hands over, not a default it changes. Sampling is
scale-invariant: all 18 hexes were measured identical at 1.0 and 2.0, and the 5x5
guard holds at both. Only the cost differs, and at 2.0 it is four times the pixels
for the same measurement. Read from the composition rather than from here wherever a
coordinate is derived, so that a cell rendered at another scale samples the scale it
was actually rendered at."""

LEFT_INSET_CSS: Final = 8
"""How far in from an element's left edge to sample, in CSS pixels.

Not a delicate number: every sampled element spans the full 1200px page, the
gutter is 72px at that width, and the padding and the content box are the same
fill -- a scan of x from 0 to 73 reads one colour at every sampled row. 8 is
simply clear of the page edge."""

GUARD_RADIUS_PX: Final = 2
"""Half-width of the uniformity window, in *device* pixels: 2 gives 5x5.

This is what makes ``.rule``'s inset 3 rather than 1. The bar is 6 CSS px tall -- so
device rows 0 to 5 at scale 1.0 -- and only insets 2 and 3 put a whole 5x5 window
inside it. 3 is the lower of the two, clear of the bar's top edge; its bottom row is
the bar's last row, which is safe exactly while ``.rule``'s ``y`` is integral. Give
the bar a fractional edge and that row blends, at which point the flatness guard
fires rather than a comparison quietly reading a blend."""


@dataclass(frozen=True, slots=True)
class Rect:
    """An element's box, in CSS pixels, as the browser measured it."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class Point:
    """A pixel in the PNG's own coordinates."""

    x: int
    y: int


RECTS_JS: Final = """
(selectors) => Object.fromEntries(selectors.map((selector) => {
  const node = document.querySelector(selector);
  if (node === null) { return [selector, null]; }
  const box = node.getBoundingClientRect();
  return [selector, {x: box.x, y: box.y, width: box.width, height: box.height}];
}))
"""

STYLES_JS: Final = """
(probes) => probes.map(([selector, property]) => {
  const node = document.querySelector(selector);
  return node === null ? null : getComputedStyle(node)[property];
})
"""

CHROME_CSS: Final = TEMPLATE_DIR / "css" / "_chrome.css"
LIGHT_SELECTOR: Final = ":root"
DARK_SELECTOR: Final = ':root[data-theme="dark"]'

_COMMENT = re.compile(r"/\*.*?\*/|\{#.*?#\}", re.DOTALL)


def _without_comments(css: str) -> str:
    """The stylesheet with its CSS and Jinja comments replaced by a space.

    ``_colours_in`` and the stray-sheet scan both read the output rather than the
    file. Prose is not a declaration -- ``/* --paper: #fff was the old value */`` is
    not a palette entry -- and, more sharply, half of ``_COLOURISH`` is a list of
    ordinary English words:
    ``ranked_list.css`` has a comment saying a fence "goes red", and ``_chrome.css``
    reasons about compositing over "white". Scanning prose for colour keywords would
    fire on both. A space rather than nothing, so that stripping a comment can never
    fuse the tokens either side of it into one word."""
    return _COMMENT.sub(" ", css)


_CUSTOM_PROPERTY = re.compile(
    r"--(?P<name>[A-Za-z0-9_-]+)\s*:\s*(?P<value>(?:\{\{[^{}]*\}\}|[^;{}])*)"
)
"""One custom-property declaration: its name, and everything up to the semicolon.

Both halves are wider than they look, and for the same reason -- what this pattern
does not match, it does not reject, it *does not see*.

**Names are case-sensitive in CSS**, so the class has to be. ``--Warn: #ff0000`` in
the light block and ``#00ff00`` in the dark one is legal CSS, is a real colour on the
page, and was measured to leave all 16 non-browser cells of this module green against
an ``[a-z0-9-]+`` name class. Lowercase is this palette's convention, not the
parser's; ``_colours_in`` asserts it instead, so a stray capital is a loud style
failure rather than an invisible token.

**A Jinja interpolation is admitted whole.** ``--w: {{ page.chrome.width_px }}px``
stops a ``[^;{}]*`` value dead at the first brace and measures as ``''``, which was
harmless only while an unreadable value was silently skipped. Now that it is a
failure, the two interpolated lengths in ``:root`` have to arrive as themselves."""

_HEX6 = re.compile(r"\A#[0-9a-f]{6}\Z")
_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")

_COLOUR_FUNCTIONS: Final = (
    "rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch",
    "color", "color-mix", "light-dark", "device-cmyk",
)
_NAMED_COLOURS: Final = frozenset(ImageColor.colormap) | {"currentcolor", "transparent"}
_COLOURISH = re.compile(
    "|".join(
        (
            _HEX_COLOUR.pattern,
            r"\b(?:" + "|".join(_COLOUR_FUNCTIONS) + r")\([^;{}]*",
            r"\b(?:" + "|".join(sorted(_NAMED_COLOURS)) + r")\b",
        )
    ),
    re.IGNORECASE,
)
"""Any value that names a colour at all, in any notation -- bare keywords included,
which this docstring claimed for a while before the pattern did it.

Three arms: hex at every legal digit count in either case, every functional notation
CSS has, and all 148 colour keywords plus ``transparent`` and ``currentColor``. The
keyword list is Pillow's ``ImageColor.colormap`` rather than 148 hand-typed strings,
because a list this module maintained would be a list this module gets wrong; the
stray-sheet test guards it for the names it depends on, so a Pillow that stopped
shipping them fails rather than quietly narrows the fence. The functional arm runs on
to the end of the declaration so that a match reports ``rgb(214, 32, 32)`` and not a
bare ``rgb(``: this pattern is read by whoever has to go and find the colour.

The omission mattered. ``.step__heading { color: rgb(214, 32, 32); }`` in a body
sheet -- every heading of that layout hardcoded red -- and ``color: rebeccapurple``
were both measured to pass the whole suite while this regex sat unused and the sheet
scan looked for ``#`` alone.

Only ever run over ``_without_comments`` output: the keyword arm matches English."""

_NOT_A_COLOUR = re.compile(
    r"""\A(?:
        [0-9.]+ (?: px | rem | em | % | s | ms )?
      | \{\{ [^{}]* \}\} (?: px | rem | em | % )
      | "[^"]*" (?: \s*,\s* (?: ui- )? (?: serif | sans-serif | monospace ) )*
    )\Z""",
    re.VERBOSE,
)
"""The values in ``:root`` that are provably *not* colours, and nothing else.

This is the fail-closed inverse of ``_COLOURISH``, and the direction is the whole
point. Asking "does this look like a colour?" and skipping whatever says no is a
parser that fails open: ``--warn: rebeccapurple`` was measured to reach the palette
with this module green, because for a *new* token the population check cannot fire
either -- ``declared.keys() == TOKENS.keys()`` stays true when the token was never
parsed. Asking "is this certainly not a colour?" inverts that: everything else
reaches ``_HEX6`` and stops the suite.

Derived from what ``:root`` and ``:root[data-theme="dark"]`` declare today, which is
three shapes and no more -- a length or a bare number (``27px``, ``0.16em``), a
Jinja-interpolated length (``--w``, ``--pad``), and a quoted family with its generic
fallbacks (``--display``, ``--body``, ``--data``). A quoted string cannot be a
colour; the generics are enumerated rather than left as ``[a-z-]+`` so that
``"Ledger Slab", red`` is not waved through.

A fourth shape arriving is meant to be a red test and a two-line edit here, made by
someone who has looked at the value. It is not meant to be a skip."""


def _root_block(css: str, selector: str) -> str:
    """The body of ``selector``'s block, found by counting braces, not matching them.

    A ``[^{}]*`` body cannot be used here: this stylesheet is a Jinja template and
    the light block interpolates ``{{ page.chrome.width_px }}``, so that pattern
    silently saw only the dark half -- which is how this function first shipped.
    Depth counting is immune, because every Jinja delimiter is itself balanced:
    ``{{``/``}}`` and ``{%``/``%}`` cancel.

    Scoping to the two blocks by structure rather than by file position also means a
    local custom property in an ordinary rule -- ``.rule { --bar: #f00; }`` -- is out
    of scope instead of being reported as a missing theme override, and the order of
    the two blocks in the file stops mattering.

    The selector is matched with ``\\s*\\{`` after it, which is what keeps ``:root``
    from also matching ``:root[data-theme="dark"]``: the dark selector has a ``[``
    where the light one has its brace.
    """
    openings = list(re.finditer(re.escape(selector) + r"\s*\{", css))
    assert len(openings) == 1, (
        f"expected exactly one `{selector}` block in {CHROME_CSS.name}, found "
        f"{len(openings)}: none means the palette moved out of this file, and two "
        "means the later one silently wins over whichever this fence read"
    )
    opened = openings[0].end() - 1
    depth = 0
    for index in range(opened, len(css)):
        depth += (css[index] == "{") - (css[index] == "}")
        if depth == 0:
            return css[opened + 1 : index]
    raise AssertionError(f"{selector} is never closed in {CHROME_CSS.name}")


def _colours_in(block: str, where: str) -> Mapping[str, str]:
    """Every colour token one ``:root`` block declares, as lowercase six-digit hex.

    Fails closed on anything it cannot classify. Only the shapes ``_NOT_A_COLOUR``
    proves are not colours are skipped; every other value reaches the ``_HEX6``
    assertion, so a token spelled ``rebeccapurple``, ``rgb(214, 32, 32)``,
    ``currentColor`` or ``var(--other)`` stops the suite instead of leaving the
    palette with a colour nothing in this repo watches.
    """
    found: dict[str, str] = {}
    for declaration in _CUSTOM_PROPERTY.finditer(_without_comments(block)):
        name, value = declaration["name"], declaration["value"].strip()
        assert name == name.lower(), (
            f"{where} declares --{name}, and CSS custom-property names are "
            f"case-sensitive: --{name} and --{name.lower()} are two different "
            "properties that read as one token. Every colour in this palette is "
            "lowercase -- respell it"
        )
        if _NOT_A_COLOUR.match(value):
            continue
        assert _HEX6.match(value), (
            f"{where} declares --{name} as {value!r}, which this fence can neither "
            "read as a colour nor prove is not one. It reads only lowercase "
            "six-digit hex, and skipping what it cannot read is how a colour ends up "
            "unwatched -- respell it as hex, or, if it genuinely is not a colour, "
            "teach _NOT_A_COLOUR that shape deliberately"
        )
        found[name] = value
    return found


def declared_tokens() -> Mapping[str, ThemedColour]:
    """The colour tokens ``_chrome.css`` actually declares, read out of the file.

    Used to check that ``TOKENS`` is the whole population, and -- through
    ``drifted`` in the test below -- that its values still match the stylesheet. A
    token added with no probe is a colour this fence does not watch, and the point of
    reading the file is to make that arrive as a failure rather than as silence.

    This does not weaken the pixel and computed-style halves into tautologies: those
    compare against the literals in ``TOKENS``, never against the file. What the file
    is read for is the *population* and the *drift notice*.
    """
    css = CHROME_CSS.read_text(encoding="utf-8")
    light = _colours_in(_root_block(css, LIGHT_SELECTOR), f"{CHROME_CSS.name} :root")
    dark = _colours_in(
        _root_block(css, DARK_SELECTOR), f"{CHROME_CSS.name} {DARK_SELECTOR}"
    )
    assert light and light.keys() == dark.keys(), (
        f"{CHROME_CSS.name} declares "
        f"{sorted(light.keys() ^ dark.keys())} in one theme only: whichever theme "
        "is missing the override inherits the other's colour, which is a palette "
        "regression that renders perfectly well"
    )
    return {
        f"--{name}": ThemedColour(light=value, dark=dark[name])
        for name, value in sorted(light.items())
    }


async def compose_palette_cell(
    template_id: str, theme: Theme, images: Sequence[ImageAsset] = ()
) -> Composition:
    """One body in one theme, at this module's own scale factor.

    ``test_composition.compose_cell`` cannot serve here: it takes no scale factor,
    so it always composes at the ``RenderOptions`` default of 2.0.
    """
    options = RenderOptions(theme=theme, device_scale_factor=DEVICE_SCALE)
    return await HtmlComposer(template_id=template_id).compose(
        make_brief(options=options), make_content(), images
    )


async def measure_rects(page: Page, selectors: Sequence[str]) -> Mapping[str, Rect]:
    """Every selector's box, or a failure naming the ones that matched nothing."""
    measured: Mapping[str, object] = await page.evaluate(RECTS_JS, list(selectors))
    missing = [selector for selector in selectors if measured.get(selector) is None]
    assert not missing, (
        f"{missing} matched no element, so those samples would measure nothing. "
        "`.apparatus` is conditional on the content having sources and `.rule` on "
        "the chrome rendering at all"
    )
    return {selector: _rect(measured[selector]) for selector in selectors}


def _rect(value: object) -> Rect:
    assert isinstance(value, dict), f"expected a box, measured {value!r}"
    numbers = {key: _number(value[key]) for key in ("x", "y", "width", "height")}
    return Rect(**numbers)


def _text_or_none(value: object) -> str | None:
    assert value is None or isinstance(value, str), (
        f"expected text or null, measured {value!r}"
    )
    return value


def sample_point(rect: Rect, inset_y_css: int, scale: float) -> Point:
    """The device pixel to sample, floored *after* the inset is added.

    Rect ``y`` is fractional, so flooring the rect first and adding the inset
    afterwards would land up to a pixel higher than intended -- which for a 6px bar
    is the difference between the middle of the fill and its antialiased edge.

    Worth seeing why the rect is not optional. ``.masthead``'s inset of 8 derives
    to device row **14**, because the 6px accent bar sits above it. A page
    coordinate of 8 would sample the *bar* -- whose colour is a real token, and in
    the light theme a valid-looking one -- so the shortcut fails silently, in the
    single most tempting place to take it.
    """
    return Point(
        x=math.floor((rect.x + LEFT_INSET_CSS) * scale),
        y=math.floor((rect.y + inset_y_css) * scale),
    )


def window_box(point: Point) -> tuple[int, int, int, int]:
    """The 5x5 crop box around ``point``, in Pillow's half-open convention."""
    return (
        point.x - GUARD_RADIUS_PX,
        point.y - GUARD_RADIUS_PX,
        point.x + GUARD_RADIUS_PX + 1,
        point.y + GUARD_RADIUS_PX + 1,
    )


def encloses(rect: Rect, point: Point, scale: float) -> bool:
    """Whether the whole 5x5 window lies inside ``rect``'s own device pixels.

    Conservative on both edges: a partially covered edge pixel is blended with
    whatever is behind the element, so the window has to clear it. This is the
    guard that turns "2px from any edge" from a comment into a check -- shrink
    ``.rule`` to 4px and this fails rather than sampling the page behind it.
    """
    left, top, right, bottom = window_box(point)
    return (
        left >= math.ceil(rect.x * scale)
        and top >= math.ceil(rect.y * scale)
        and right <= math.floor((rect.x + rect.width) * scale)
        and bottom <= math.floor((rect.y + rect.height) * scale)
    )


def window_colours(image: Image.Image, point: Point) -> Mapping[str, int]:
    """Every colour in the 5x5 neighbourhood of ``point``, hex -> pixel count.

    The bounds check is not paranoia: ``Image.crop`` outside the image *pads with
    black* rather than raising, so without it a window that fell off the page would
    report a flat ``#000000`` -- a confident measurement of a colour that is not in
    the PNG at all.
    """
    box = window_box(point)
    assert box[0] >= 0 and box[1] >= 0, f"window {box} starts outside the PNG"
    assert box[2] <= image.width and box[3] <= image.height, (
        f"window {box} runs past the {image.width}x{image.height} PNG"
    )
    window = image.crop(box)
    counted = window.getcolors(maxcolors=window.width * window.height)
    assert counted is not None, "a 5x5 window cannot hold more than 25 colours"
    return {_hex(colour): count for count, colour in counted}


def _hex(colour: object) -> str:
    assert isinstance(colour, tuple) and len(colour) == 3, (
        f"expected an RGB triple, measured {colour!r}"
    )
    channels = tuple(int(channel) for channel in colour)
    return "#{:02x}{:02x}{:02x}".format(*channels)


# --------------------------------------------------------------------------- #
# Non-vacuity
# --------------------------------------------------------------------------- #


def test_every_probe_table_has_rows_to_walk() -> None:
    """No table here may empty out, because a loop over no rows is a pass.

    The two parametrized axes need no equivalent guard: ``BODIES`` and
    ``IN_BOTH_THEMES`` are imported from ``test_composition``, where
    ``test_the_matrix_covers_every_body_and_every_theme`` already proves an empty
    registry cannot turn these cells into ``got empty parameter set`` skips and that
    ``THEMES`` is every ``Theme`` member. This is the part that test cannot know
    about.
    """
    empty = [
        name
        for name, table in (
            ("TOKENS", TOKENS),
            ("SAMPLED_PIXELS", SAMPLED_PIXELS),
            ("TOKEN_STYLES", TOKEN_STYLES),
            ("LITERAL_PROBES", LITERAL_PROBES),
        )
        if not table
    ]
    assert not empty, (
        f"{empty} is empty, so the cells that walk it assert nothing while still "
        "reporting green -- a loop over no rows is a pass"
    )


def test_every_colour_the_chrome_declares_is_pinned_by_a_probe() -> None:
    """``TOKENS`` is the whole palette, and every entry has somewhere to land.

    Three failures in one, all of them the same shape -- a colour nobody watches.
    A token added to ``_chrome.css`` with no row here would be unfenced; a row here
    with no ``StyleProbe`` would be a value asserted nowhere; and a value that
    disagrees with the stylesheet means the palette moved and this module was not
    told.
    """
    declared = declared_tokens()
    assert declared.keys() == TOKENS.keys(), (
        f"the chrome's colour tokens and TOKENS have diverged: unfenced "
        f"{sorted(declared.keys() - TOKENS.keys())}, stale "
        f"{sorted(TOKENS.keys() - declared.keys())}"
    )
    drifted = {
        token: (colour, TOKENS[token]) for token, colour in declared.items()
        if colour != TOKENS[token]
    }
    assert not drifted, (
        "the palette moved and this fence still expects the old values:\n"
        + "\n".join(
            f"  {token}: {CHROME_CSS.name} declares "
            f"{found.light}/{found.dark}, this module expects "
            f"{expected.light}/{expected.dark}"
            for token, (found, expected) in sorted(drifted.items())
        )
        + "\nIf the new palette is intended, update TOKENS deliberately -- that is "
        "the review this fence exists to force."
    )
    probed = {probe.token for probe in TOKEN_STYLES}
    assert probed == TOKENS.keys(), (
        f"unpinned {sorted(TOKENS.keys() - probed)} -- declared but asserted nowhere "
        f"on the page; unknown {sorted(probed - TOKENS.keys())} -- probed but not a "
        "token, which would otherwise surface as a KeyError inside a browser cell"
    )
    sampled = {sample.token for sample in SAMPLED_PIXELS}
    assert sampled <= TOKENS.keys(), (
        f"{sorted(sampled - TOKENS.keys())} are sampled in the PNG but are not "
        "tokens, so there is no declared value to compare them against"
    )


def test_no_other_stylesheet_smuggles_in_a_colour_of_its_own() -> None:
    """Every colour on the page comes from the chrome's palette.

    Five stylesheets are concatenated into the one ``<style>`` element and a body's
    sheet is included *after* the chrome's, so any of them could declare a token of
    its own or paint a literal colour -- and ``declared_tokens`` only reads
    ``_chrome.css``, so neither would register as part of the palette. Measured: the
    other four sheets name **no colour at all** today, in any notation.

    In any notation is the assertion, and hex alone was not it. With this scan
    narrowed to ``#``, ``.step__heading { color: rgb(214, 32, 32); }`` in
    ``process_flow.css`` turned every heading of that layout hardcoded red with the
    whole suite green, and ``color: rebeccapurple`` did the same -- while
    ``_COLOURISH``, which exists for exactly this, sat unused 280 lines above. Both
    halves of the module now read a colour the same way, so they cannot disagree about
    what one is.

    A body that needs a colour should name a token. If a literal really is right,
    it belongs in the chrome next to the others, where this fence can see it.
    """
    unnamed = sorted({"red", "rebeccapurple", "white"} - _NAMED_COLOURS)
    assert not unnamed, (
        f"{unnamed} are missing from Pillow's colormap, so the keyword arm of "
        "_COLOURISH is not the CSS colour list this test assumes -- a body sheet "
        "could paint in whichever names went absent and still read as green"
    )
    strays = {
        path.name: _COLOURISH.findall(
            _without_comments(path.read_text(encoding="utf-8"))
        )
        for path in sorted(CHROME_CSS.parent.glob("*.css"))
        if path != CHROME_CSS
    }
    smuggled = {name: found for name, found in strays.items() if found}
    assert strays, f"no stylesheets found beside {CHROME_CSS.name} to check"
    assert not smuggled, (
        f"colours declared outside {CHROME_CSS.name}: {smuggled}. Either use a token "
        f"or move the literal into the palette -- as it stands the palette fence "
        f"cannot see it, and a body sheet overrides the chrome"
    )


def test_every_token_is_a_different_colour_in_each_theme() -> None:
    """No token may hold the same value in both themes.

    The cheap way to "fix" a red assertion is to paste the observed colour into both
    theme columns, at which point the row still passes and no longer distinguishes a
    theme swap from a palette swap. ``--paper`` in light and ``--patch`` in dark
    really are both ``#eceae2``; that is fine, because a swap *within* a theme still
    separates them. What must never happen is one token whose two themes agree.

    All ten, not just the sampled three: the same shortcut is available on every row,
    and today every token does differ, so this costs nothing to hold.
    """
    for token, colour in sorted(TOKENS.items()):
        assert colour.light != colour.dark, (
            f"{token} is {colour.light} in both themes, so no probe on it can tell a "
            "theme swap from a palette swap"
        )


@BODIES
@IN_BOTH_THEMES
@BROWSER_LOOP
async def test_every_sampled_token_reaches_the_rasterised_png(
    chromium: Browser, tmp_path: Path, template_id: str, theme: Theme
) -> None:
    """The declared hex is the hex in the PNG, at a point derived from the DOM.

    This is the layer that sees paint time. A token can be declared right and resolve
    right and still not arrive: measured against injected regressions,
    ``.rule { opacity: 0.5 }`` reads ``#a4aa7a``, ``body { filter: grayscale(1) }``
    greys all three samples, and a masthead pulled up 6px hides the accent bar
    entirely -- each with every computed style still correct.

    The rects come from a scripted page and the pixels from the real
    ``PlaywrightRenderer``, which is script-free at a 1px viewport -- two different
    browser configurations reading one composition. They agree because no template
    emits ``<script>`` (asserted in ``test_composition.py``) and no stylesheet here
    uses ``vh``, ``vmin`` or ``position: fixed``, so nothing in this layout resolves
    against viewport height. A body that adds any of those breaks the equivalence
    and this docstring with it.

    This test also carries the browser gate for the module. ``PlaywrightRenderer``
    launches its own throwaway chromium and has no skip of its own -- a missing
    executable surfaces there as a bare ``RuntimeError``. Depending on the
    ``chromium`` fixture means its skip fires first, so a browserless machine skips
    instead of erroring. And since that fixture now re-raises anything but
    ``MISSING_CHROMIUM``, a browser that exists and then misbehaves still fails
    loudly rather than skipping ~90 cells into silence.
    """
    composition = await compose_palette_cell(template_id, theme)
    scale = composition.device_scale_factor
    async with laid_out(chromium, composition) as page:
        rects = await measure_rects(page, [s.selector for s in SAMPLED_PIXELS])

    output = tmp_path / f"{template_id}-{theme.value}.png"
    result = await PlaywrightRenderer().render(composition, output)

    with Image.open(output) as image:
        assert (image.width, image.height) == (result.width_px, result.height_px), (
            f"opened {image.width}x{image.height} but the renderer reported "
            f"{result.width_px}x{result.height_px}"
        )
        pixels = image.convert("RGB")
        for sampled in SAMPLED_PIXELS:
            rect = rects[sampled.selector]
            point = sample_point(rect, sampled.inset_y_css, scale)
            expected = TOKENS[sampled.token].hex_for(theme)
            where = (
                f"{template_id}/{theme.value}: {sampled.selector} at "
                f"({point.x}, {point.y}), {sampled.inset_y_css}px below a box of "
                f"x={rect.x:.4f} y={rect.y:.4f} {rect.width:.4f}x{rect.height:.4f}"
                f" at scale {scale}"
            )
            assert encloses(rect, point, scale), (
                f"{where} -- the 5x5 uniformity window does not fit inside the "
                f"element, so this sample would read whatever is behind it"
            )
            colours = window_colours(pixels, point)
            assert len(colours) == 1, (
                f"{where} is not a flat fill: {colours}. Re-point this sample or "
                f"drop the row -- never loosen the comparison"
            )
            observed = next(iter(colours))
            assert observed == expected, (
                f"{where} reads {observed}, but {sampled.token} is {expected} in the "
                f"{theme.value} theme. That pixel is meant to be {sampled.through}, so "
                "either the token moved, or something is painted over the element -- "
                "an opacity, a filter or a neighbour pulled across it"
            )


@BODIES
@IN_BOTH_THEMES
@BROWSER_LOOP
async def test_the_chrome_resolves_every_palette_token_to_its_declared_value(
    chromium: Browser, template_id: str, theme: Theme
) -> None:
    """Every token, and the three literals, as the browser computes them.

    Built with the real panda assets so that the colophon exists: the credits are
    where ``--dim-patch`` and ``--accent-patch`` become legally load-bearing text,
    and they are absent from a page with no images.

    Run per body as well as per theme, because a body's own stylesheet is included
    *after* the chrome's and can therefore redefine any token on the page. Measured:
    no body does today, in either theme.

    This is the only layer that watches six of the ten tokens -- the ones with no flat
    fill big enough to sample -- and the only one that sees a reflow. It is also the
    layer that survives an OS change, where a rasterised pixel of text would not.
    Three tokens are watched here *and* in the PNG, deliberately: a computed value
    says the token resolved, a sampled pixel says it reached the deliverable, and
    neither implies the other.
    """
    composition = await compose_palette_cell(template_id, theme, PANDA_SET)
    probes: list[tuple[str, str]] = [(p.selector, p.prop) for p in TOKEN_STYLES]
    probes += [(p.selector, p.prop) for p in LITERAL_PROBES]
    expected = [TOKENS[p.token].rgb_for(theme) for p in TOKEN_STYLES]
    expected += [p.expected for p in LITERAL_PROBES]
    described = [f"{p.token} on {p.selector}: {p.catches}" for p in TOKEN_STYLES]
    described += [f"{p.selector} {p.prop}: {p.catches}" for p in LITERAL_PROBES]

    async with laid_out(chromium, composition) as page:
        measured = await page.evaluate(STYLES_JS, probes)

    values = [_text_or_none(value) for value in _rows(measured)]
    assert len(values) == len(probes), (
        f"asked for {len(probes)} computed values, got {len(values)}"
    )
    wrong = [
        (probe, description, want, got)
        for probe, description, want, got in zip(
            probes, described, expected, values, strict=True
        )
        if got != want
    ]
    assert not wrong, (
        f"in {template_id}/{theme.value} the chrome computed the wrong values:\n"
        + "\n".join(
            f"  {selector} {prop} = {got!r}, expected {want!r} ({description})"
            for (selector, prop), description, want, got in wrong
        )
        + "\nA `None` means the selector matched nothing, so the value was never "
        "asserted at all."
    )
