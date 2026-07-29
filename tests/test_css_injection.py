"""Nothing untrusted may be interpolated into CSS.

``Environment(autoescape=True)`` escapes for *HTML*, and a ``<style>`` element is
raw text: an escaped ``&#34;`` lands there inert, while every character that
actually matters in CSS -- ``}``, ``;``, ``url(``, ``@import``, ``/*`` -- passes
through autoescape completely untouched. Autoescape is therefore not a defence
here, and neither is a ``style=""`` attribute much better off: escaping keeps a
payload inside the attribute, but the attribute *is* CSS.

The discipline that keeps the sheets clean today is that the only runtime values
reaching them are font constants and ``_fit()``/``_aspect()``/``_gutter()``
outputs, every one of them formatted ``f"{float:.2f}"``. That discipline is
emergent -- nothing asserts it. These are the assertions.

The payloads here are CSS-shaped, not HTML-shaped, because
``test_composition.MARKUP_PAYLOADS`` are all ``<script>``/attribute-breakout
strings: not one of them contains a ``}``, a ``;`` or a ``url(``, so none of them
can catch a value landing in a stylesheet.

Pure-parser fences: no chromium, no measurement of a laid-out page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

import pytest

from infographic_generator.composition import HtmlComposer
from infographic_generator.core.models import (
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
    IN_BOTH_THEMES,
    PNG_PAYLOAD,
    ParsedHtml,
    compose_cell,
    elide,
    make_brief,
    make_content,
    make_numbered_assets,
    parse,
    without_payloads,
)

# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #

JINJA_ESCAPES: Final = frozenset("<>&\"'")
"""The characters ``markupsafe`` replaces. A payload built out of these would
prove nothing: its absence from a stylesheet would show only that *escaping*
happened, not that the value never reached CSS at all."""


@dataclass(frozen=True, slots=True)
class CssPayload:
    """A hostile string, plus the substrings that prove it reached a stylesheet.

    Whole-payload matching is too weak on its own: a template that interpolates a
    value into the middle of a declaration can leak the dangerous half of it and
    still not reproduce the string end to end. Each signature is the smallest
    fragment that is already a CSS escape.
    """

    id: str
    text: str
    signatures: tuple[str, ...]


CSS_PAYLOADS: Final[tuple[CssPayload, ...]] = (
    CssPayload(
        id="closes-the-rule",
        text="teal} body{display:none} /*",
        signatures=("teal}", "body{display:none}"),
    ),
    CssPayload(
        id="adds-a-declaration",
        text="red; background: url(https://evil.example/x.png)",
        signatures=("red; background:", "evil.example/x.png"),
    ),
    CssPayload(
        id="at-import",
        text="@import url(https://evil.example/x.css);",
        signatures=("@import", "evil.example/x.css"),
    ),
    CssPayload(
        id="closes-a-comment",
        text="*/ } * { display: none } /*",
        signatures=("*/ }", "} * { display: none }"),
    ),
    CssPayload(
        id="spans-lines",
        text="teal;\nbackground: url(https://evil.example/y.png);\n",
        signatures=("teal;\nbackground:", "evil.example/y.png"),
    ),
)

PAYLOADS = pytest.mark.parametrize(
    "payload", CSS_PAYLOADS, ids=[p.id for p in CSS_PAYLOADS]
)

# --------------------------------------------------------------------------- #
# Inline styles
# --------------------------------------------------------------------------- #

INLINE_STYLE_GRAMMARS: Final = (
    re.compile(r"^--fit: \d+\.\d{2}cqw$"),
    re.compile(r"^flex: \d+\.\d{4} 1 0$"),
    re.compile(r"^aspect-ratio: \d+\.\d{4}$"),
)
"""Every ``style=""`` the templates emit, as a whole-value allowlist.

An allowlist rather than a denylist because it cannot be defeated by finding a
spelling nobody thought of: these three are `f"{float:.2f}"` and `f"{float:.4f}"`
outputs, and a payload cannot look like one. A tenth site with a fourth grammar
has to be added here deliberately, which is the point.
"""


def inline_styles(parsed: ParsedHtml) -> tuple[tuple[str, str], ...]:
    """Every ``(tag, style)`` pair in the document.

    ``ParsedHtml`` has no accessor for this and lives in a file this fence may not
    edit, so it is reconstructed from ``tags`` here.
    """
    return tuple(
        (tag, attrs["style"]) for tag, attrs in parsed.tags if "style" in attrs
    )


# --------------------------------------------------------------------------- #
# Hostile fixtures -- a payload in every string that crosses the port
# --------------------------------------------------------------------------- #


def hostile_content(payload: str) -> ResearchContent:
    source = Source(url=payload, title=payload, publisher=payload)
    return ResearchContent(
        title=payload,
        subtitle=payload,
        summary=payload,
        facts=tuple(
            Fact(
                label=payload,
                value=payload,
                unit=payload,
                detail=payload,
                source=source,
            )
            for _ in range(3)
        ),
        sections=(
            NarrativeSection(heading=payload, body=payload, sources=(source,)),
        ),
        keywords=(payload,),
        sources=(source,),
    )


def hostile_images(payload: str) -> tuple[ImageAsset, ...]:
    """Two assets, so a body that lays out a hero and a supporting figure reaches
    both of its ``style=""`` sites."""
    credit = ImageCredit(
        license=payload,
        author=payload,
        license_url=payload,
        source=Source(url=payload, title=payload, publisher=payload),
        modified=True,
    )
    return tuple(
        ImageAsset(
            content=PNG_PAYLOAD + bytes((index,)),
            mime_type="image/png",
            width_px=1600,
            height_px=1066,
            alt_text=payload,
            credit=credit,
            role=ImageRole.HERO if index == 0 else ImageRole.SUPPORTING,
        )
        for index in range(2)
    )


async def hostile_cell(
    template_id: str, theme: Theme, payload: str
) -> Composition:
    """One cell of the matrix with the payload in every untrusted string.

    Composed through ``HtmlComposer`` directly rather than through
    ``compose_cell``, which fixes ``Brief.locale`` at ``en-US``. ``locale`` is
    laundered through ``_bcp47`` and only reaches ``<html lang>``, so it is not a
    plausible CSS leak today -- planting it anyway is what makes its absence an
    assertion rather than an accident.

    ``RenderOptions`` is left at its defaults apart from the theme, so the sheet
    is comparable byte for byte with a benign ``compose_cell`` of the same body.
    """
    return await HtmlComposer(template_id=template_id).compose(
        make_brief(options=RenderOptions(theme=theme), locale=payload),
        hostile_content(payload),
        hostile_images(payload),
    )


# --------------------------------------------------------------------------- #
# The premises the fence rests on
# --------------------------------------------------------------------------- #


def test_no_payload_relies_on_a_character_jinja_escapes() -> None:
    """A payload made of ``<>&"'`` would turn this whole file into a test of
    autoescape, which is precisely the mechanism that does *not* protect CSS."""
    escapable = {
        payload.id: sorted(JINJA_ESCAPES.intersection(payload.text))
        for payload in CSS_PAYLOADS
        if JINJA_ESCAPES.intersection(payload.text)
    }
    assert not escapable, (
        f"payloads contain characters markupsafe escapes: {escapable}. Their "
        "absence from a stylesheet would prove escaping happened, not that the "
        "value never reached CSS"
    )


def test_every_payload_is_css_shaped() -> None:
    """The gap ``MARKUP_PAYLOADS`` leaves: each payload must carry at least one
    character that only matters once a value is inside a stylesheet."""
    inert = [
        payload.id
        for payload in CSS_PAYLOADS
        if not any(token in payload.text for token in ("}", ";", "url(", "@", "*/"))
    ]
    assert not inert, (
        f"payloads {inert} contain nothing that escapes a CSS context, so they "
        "would pass against a template that interpolates them raw"
    )


def test_every_signature_is_a_fragment_of_its_payload() -> None:
    stray = {
        payload.id: [sig for sig in payload.signatures if sig not in payload.text]
        for payload in CSS_PAYLOADS
        if any(sig not in payload.text for sig in payload.signatures)
    }
    assert not stray, (
        f"signatures that their own payload does not contain: {stray}. A "
        "signature that cannot appear can never fail"
    )


@BODIES
@IN_BOTH_THEMES
async def test_no_signature_already_occurs_in_a_benign_sheet(
    template_id: str, theme: Theme
) -> None:
    """Every signature is searched for in the *raw* sheet text, base64 font
    payloads and all -- ``without_payloads`` would elide a leaked
    ``url(https://evil.example/x.png)`` down to ``url()`` and hide the very thing
    this file is looking for.

    That makes false positives the risk instead, so it is fenced here: no
    signature may occur in a sheet composed from benign content. This is what
    makes a hit in the tests below mean the payload arrived, rather than meaning
    two of the 70 kB of base64 happened to line up.
    """
    composition = await compose_cell(
        template_id, theme, make_content(), make_numbered_assets(2)
    )
    parsed = parse(composition.html)
    assert len(parsed.tagged("style")) >= 1, (
        f"{template_id}/{theme.value}: no <style> element, so every absence "
        "asserted in this file is the absence of a stylesheet"
    )
    assert len(without_payloads(parsed.css)) > 0, (
        f"{template_id}/{theme.value}: the <style> element is empty once font "
        "payloads are elided; there is no CSS here to keep clean"
    )
    collisions = {
        payload.id: [sig for sig in payload.signatures if sig in parsed.css]
        for payload in CSS_PAYLOADS
        if any(sig in parsed.css for sig in payload.signatures)
    }
    assert not collisions, (
        f"{template_id}/{theme.value}: signatures occur in a benign sheet: "
        f"{collisions}. They cannot distinguish a leak from the sheet's own text"
    )


# --------------------------------------------------------------------------- #
# The fence
# --------------------------------------------------------------------------- #


@PAYLOADS
@BODIES
@IN_BOTH_THEMES
async def test_no_payload_reaches_the_style_element(
    payload: CssPayload, template_id: str, theme: Theme
) -> None:
    composition = await hostile_cell(template_id, theme, payload.text)
    parsed = parse(composition.html)

    sheets = parsed.tagged("style")
    assert len(sheets) >= 1, (
        f"{template_id}/{theme.value}: no <style> element to inspect -- an "
        "absence measured in a document with no stylesheet is not a result"
    )
    elided = without_payloads(parsed.css)
    assert len(elided) > 0, (
        f"{template_id}/{theme.value}: {len(sheets)} <style> element(s) holding "
        "no CSS once font payloads are elided"
    )

    leaked = [sig for sig in payload.signatures if sig in parsed.css]
    assert not leaked, (
        f"{template_id}/{theme.value}: {payload.id} reached the stylesheet. "
        f"Leaked {leaked!r} from {payload.text!r}. autoescape does not escape "
        f"for CSS; sheet begins {elide(elided)!r}"
    )


@PAYLOADS
@BODIES
@IN_BOTH_THEMES
async def test_no_payload_reaches_an_inline_style_attribute(
    payload: CssPayload, template_id: str, theme: Theme
) -> None:
    """A ``style=""`` attribute is escaped but still CSS: ``}`` and ``url(``
    survive escaping untouched, and ``;`` needs no help at all to add a
    declaration to the one the template opened.
    """
    composition = await hostile_cell(template_id, theme, payload.text)
    parsed = parse(composition.html)

    styles = inline_styles(parsed)
    assert styles, (
        f"{template_id}/{theme.value}: no element carries a style attribute, so "
        "this cell measures nothing"
    )

    leaked = {
        f"{tag}[{elide(value)}]": [sig for sig in payload.signatures if sig in value]
        for tag, value in styles
        if any(sig in value for sig in payload.signatures)
    }
    assert not leaked, (
        f"{template_id}/{theme.value}: {payload.id} reached a style attribute: "
        f"{leaked}"
    )

    unknown = [
        (tag, value)
        for tag, value in styles
        if not any(grammar.fullmatch(value) for grammar in INLINE_STYLE_GRAMMARS)
    ]
    assert not unknown, (
        f"{template_id}/{theme.value}: style attribute values outside the "
        f"allowlist: {[(tag, elide(value)) for tag, value in unknown]}. Every "
        "inline style the templates emit is a formatted float; anything else is "
        "either a new site that needs a grammar here or a value that came from "
        "content"
    )


@BODIES
@IN_BOTH_THEMES
async def test_the_style_element_does_not_depend_on_content_at_all(
    template_id: str, theme: Theme
) -> None:
    """The sheet is a function of ``(template_id, width_px, height_px)`` and
    nothing else.

    Every value the five CSS files interpolate is a font constant or a number this
    package computed from the render options -- so for fixed ``RenderOptions``, a
    page composed from hostile research and hostile images must produce the same
    bytes as one composed from the panda fixtures. This is the cheap version of
    every assertion above and strictly stronger than all of them: it fails on
    *any* research field reaching *any* rule, escaped or not, whether or not the
    value happens to look hostile.

    Font payloads are elided from both sides only to keep a failure readable;
    they are identical constants either way.
    """
    hostile = await hostile_cell(template_id, theme, CSS_PAYLOADS[0].text)
    benign = await compose_cell(
        template_id, theme, make_content(), make_numbered_assets(2)
    )
    hostile_parsed, benign_parsed = parse(hostile.html), parse(benign.html)

    assert len(hostile_parsed.tagged("style")) >= 1, (
        f"{template_id}/{theme.value}: hostile page has no <style> element"
    )
    assert len(benign_parsed.tagged("style")) >= 1, (
        f"{template_id}/{theme.value}: benign page has no <style> element"
    )
    hostile_css = without_payloads(hostile_parsed.css)
    benign_css = without_payloads(benign_parsed.css)
    assert len(benign_css) > 0, (
        f"{template_id}/{theme.value}: benign sheet is empty once font payloads "
        "are elided, so byte-identity here would compare nothing"
    )

    assert hostile_css == benign_css, (
        f"{template_id}/{theme.value}: the stylesheet changed with the content, "
        "so something researched is being interpolated into CSS. First "
        f"divergence at char {_first_divergence(benign_css, hostile_css)}: "
        f"benign {elide(benign_css[_first_divergence(benign_css, hostile_css):])!r} "
        f"vs hostile "
        f"{elide(hostile_css[_first_divergence(benign_css, hostile_css):])!r}"
    )


def _first_divergence(left: str, right: str) -> int:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return min(len(left), len(right))
