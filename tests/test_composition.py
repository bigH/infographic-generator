"""Contract tests for the composition stage (``core.ports.Composer``).

Everything here is an outcome the ``Composer`` docstring promises: a
self-contained document, escaped untrusted text, honoured render options,
visible attribution, and every fact on the page. The HTML is parsed with the
stdlib rather than pattern-matched.

Some promises are only observable once a browser has laid the page out -- a
``text-transform`` that mangles a licence URI, an ``aspect-ratio`` that a later
rule overrides, a caption that spills out of its figure. Those are asserted in a
real chromium DOM against one browser shared by the whole module.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import pytest
import pytest_asyncio

from infographic_generator.composition import HtmlComposer
from infographic_generator.composition.layout import _MIN_FIT_CQW, font_faces
from infographic_generator.composition.registry import RENDERABLE_TEMPLATE_IDS
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
from infographic_generator.core.ports import Composer

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page

REPO_ROOT = Path(__file__).resolve().parents[1]
PANDA_DIR = REPO_ROOT / "assets" / "panda"

PNG_PAYLOAD = b"\x89PNG\r\n\x1a\n"

SCRIPT_PAYLOAD = "<script>alert(1)</script>"
ATTR_PAYLOAD = '"><img src=x onerror=alert(1)>'
STYLE_PAYLOAD = "</style><script>alert(2)</script>"
URL_PAYLOAD = "javascript:alert(1)"
MARKUP_PAYLOADS = (SCRIPT_PAYLOAD, ATTR_PAYLOAD, STYLE_PAYLOAD)

REMOTE_SCHEMES = ("http://", "https://", "//")
VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
FETCHABLE_ATTRS = frozenset({"src", "href", "srcset", "poster"})

_CSS_URL = re.compile(
    r"""url\(\s*(?P<quote>['"]?)(?P<url>.*?)(?P=quote)\s*\)""", re.DOTALL
)

WOFF2_DATA_PREFIX = "data:font/woff2;base64,"

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_FONT_FACE = re.compile(r"@font-face\s*\{(?P<body>[^}]*)\}", re.IGNORECASE)
_SRC_DECL = re.compile(
    r"\bsrc\s*:\s*(?P<value>(?:url\([^)]*\)|[^;{}])*)", re.IGNORECASE
)
"""``url(...)`` is matched whole because a data URI contains the ``;`` that would
otherwise look like the end of the declaration."""
_FONT_STACK_DECL = re.compile(
    r"(?:font-family|--display|--body|--data)\s*:\s*(?P<stack>[^;{}]+)", re.IGNORECASE
)
"""Every declaration that names a typeface. The custom properties hold the three
stacks the rest of the sheet refers to through ``var()``, so they count too."""

GENERIC_FAMILIES = frozenset(
    {
        # CSS generic families: resolved by the browser, never by the machine.
        "serif",
        "sans-serif",
        "monospace",
        "cursive",
        "fantasy",
        "system-ui",
        "ui-serif",
        "ui-sans-serif",
        "ui-monospace",
        "ui-rounded",
        "math",
        "emoji",
        "fangsong",
        # Web-safe families, allowed only because they may sit *behind* an
        # embedded face to supply a glyph its subset lacks -- never to carry the
        # design. The current stylesheet uses none of them; the list exists so
        # adding one is a deliberate act rather than a silent regression.
        "arial",
        "helvetica",
        "georgia",
        "times new roman",
        "times",
        "courier new",
        "courier",
        "verdana",
        "tahoma",
        "trebuchet ms",
        "dejavu sans mono",
        "liberation mono",
    }
)

LOCAL_FONT_NAMES = (
    "superclarendon",
    "iowan",
    "charter",
    "rockwell",
    "ptserif",
    "menlo",
    "sf mono",
    "bookman",
    "consolas",
    "bitstream",
)
"""Families that only exist on one vendor's machines. Every one of these has been
in this stylesheet at some point; each made the render machine-dependent."""


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


class _Collector(HTMLParser):
    """Records the parts of a document a browser would act on."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.doctypes: list[str] = []
        self.text_chunks: list[str] = []
        self.style_chunks: list[str] = []
        self.title_chunks: list[str] = []
        self.class_chunks: dict[str, list[str]] = {}
        self._open: list[tuple[str, tuple[str, ...]]] = []

    def handle_decl(self, decl: str) -> None:
        self.doctypes.append(decl)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        self.tags.append((tag, attributes))
        if tag not in VOID_ELEMENTS:
            self._open.append((tag, tuple(attributes.get("class", "").split())))

    def handle_endtag(self, tag: str) -> None:
        if all(open_tag != tag for open_tag, _ in self._open):
            return
        while self._open and self._open.pop()[0] != tag:
            pass

    def handle_data(self, data: str) -> None:
        tag = self._open[-1][0] if self._open else ""
        match tag:
            case "style":
                self.style_chunks.append(data)
            case "script":
                pass
            case "title":
                self.title_chunks.append(data)
            case _:
                self.text_chunks.append(data)
                for _, classes in self._open:
                    for name in classes:
                        self.class_chunks.setdefault(name, []).append(data)


@dataclass(frozen=True, slots=True)
class ParsedHtml:
    """A document reduced to what the assertions below care about."""

    tags: tuple[tuple[str, Mapping[str, str]], ...]
    doctypes: tuple[str, ...]
    text: str
    css: str
    titles: tuple[str, ...]
    class_text: Mapping[str, str]
    """Text rendered inside each CSS class, that class's descendants included.

    Lets an assertion say *where* a string landed, which matters when the same
    string is legitimately rendered twice -- a publisher shows up beside its
    section and again in the bibliography.
    """

    @property
    def title_count(self) -> int:
        return sum(1 for tag, _ in self.tags if tag == "title")

    def tagged(self, name: str) -> tuple[Mapping[str, str], ...]:
        return tuple(attrs for tag, attrs in self.tags if tag == name)

    def classed(self, name: str) -> tuple[Mapping[str, str], ...]:
        return tuple(
            attrs
            for _, attrs in self.tags
            if name in attrs.get("class", "").split()
        )

    def text_in(self, name: str) -> str:
        return self.class_text.get(name, "")

    @property
    def attr_values(self) -> tuple[str, ...]:
        return tuple(value for _, attrs in self.tags for value in attrs.values())

    @property
    def fetchable_urls(self) -> tuple[str, ...]:
        return tuple(_iter_fetchable_urls(self.tags))

    @property
    def css_urls(self) -> tuple[str, ...]:
        inline = " ".join(attrs.get("style", "") for _, attrs in self.tags)
        return tuple(m.group("url") for m in _CSS_URL.finditer(self.css + " " + inline))

    @property
    def rendered_strings(self) -> str:
        """Everything a browser would treat as text or attribute data."""
        return "\n".join((self.text, *self.titles, *self.attr_values))


def _iter_fetchable_urls(
    tags: Sequence[tuple[str, Mapping[str, str]]],
) -> Iterator[str]:
    for tag, attrs in tags:
        for name, value in attrs.items():
            if name == "srcset":
                yield from _srcset_urls(value)
            elif name in FETCHABLE_ATTRS or (tag == "object" and name == "data"):
                yield value


def _srcset_urls(value: str) -> Iterator[str]:
    for candidate in value.split(","):
        head = candidate.split()
        if head:
            yield head[0]


def parse(html: str) -> ParsedHtml:
    collector = _Collector()
    collector.feed(html)
    collector.close()
    return ParsedHtml(
        tags=tuple((tag, attrs) for tag, attrs in collector.tags),
        doctypes=tuple(collector.doctypes),
        text="".join(collector.text_chunks),
        css="".join(collector.style_chunks),
        titles=tuple(collector.title_chunks),
        class_text={
            name: "".join(chunks) for name, chunks in collector.class_chunks.items()
        },
    )


def without_payloads(css: str) -> str:
    """CSS with every ``url(...)`` body elided.

    Base64 is 70 kB of near-random letters here, so ``"//" in css`` and even
    ``"iowan" in css.lower()`` can hit inside a font payload. Searching for a font
    name only means anything once the payloads are gone.
    """
    return _CSS_URL.sub("url()", css)


def css_declarations(css: str) -> str:
    """The stylesheet reduced to what a browser acts on: payloads elided and
    comments dropped, since a comment may legitimately name the very families it
    is warning the next reader away from.
    """
    return without_payloads(_CSS_COMMENT.sub(" ", css))


def font_face_bodies(css: str) -> tuple[str, ...]:
    return tuple(match.group("body") for match in _FONT_FACE.finditer(css))


def font_families(css: str) -> tuple[str, ...]:
    """Every family named by the sheet, lowercased, ``var()`` references dropped."""
    return tuple(
        name
        for match in _FONT_STACK_DECL.finditer(css)
        for name in _split_stack(match.group("stack"))
    )


def _split_stack(stack: str) -> Iterator[str]:
    for entry in stack.split(","):
        name = entry.strip().strip("\"'").strip().lower()
        if name and not name.startswith("var("):
            yield name


def elide(value: str, keep: int = 60) -> str:
    """Base64 in a failure message drowns the message."""
    return value if len(value) <= keep else f"{value[:keep]}... ({len(value)} chars)"


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #


def make_source(
    url: str = "https://www.worldwildlife.org/species/giant-panda",
    title: str | None = "Giant panda species profile",
) -> Source:
    return Source(url=url, title=title, publisher="WWF")


def make_credit(
    license_text: str = "CC0 1.0",
    author: str | None = "james_birdtourasia",
    license_url: str | None = "https://creativecommons.org/publicdomain/zero/1.0/",
    source: Source | None = None,
) -> ImageCredit:
    return ImageCredit(
        license=license_text,
        author=author,
        license_url=license_url,
        source=source if source is not None else make_source(),
    )


def make_asset(
    content: bytes | Path = PNG_PAYLOAD,
    alt_text: str = "A giant panda chewing bamboo",
    credit: ImageCredit | None = None,
    mime_type: str = "image/png",
    role: ImageRole = ImageRole.HERO,
) -> ImageAsset:
    return ImageAsset(
        content=content,
        mime_type=mime_type,
        width_px=1600,
        height_px=1066,
        alt_text=alt_text,
        credit=credit if credit is not None else make_credit(),
        role=role,
    )


def make_facts(count: int) -> tuple[Fact, ...]:
    """Facts whose labels and values are all zero-padded, hence none a prefix of
    another: ``"Bamboo metric 1" in text`` would otherwise pass on fact 11 alone.
    """
    return tuple(
        Fact(
            label=f"Bamboo metric {index:02d}",
            value=f"{index:02d}7.5",
            unit="kg",
            detail=f"Measured across {index:02d} reserves",
            source=make_source(url=f"https://example.org/study-{index:02d}"),
        )
        for index in range(count)
    )


def make_numbered_assets(count: int) -> tuple[ImageAsset, ...]:
    """Assets that are distinguishable one from another in the rendered output:
    distinct bytes, so distinct data URIs, and distinct credit strings."""
    return tuple(
        make_asset(
            content=PNG_PAYLOAD + bytes((index,)),
            alt_text=f"Panda photograph {index:02d}",
            credit=make_credit(
                license_text=f"CC BY {index:02d}.0",
                author=f"Photographer {index:02d}",
                license_url=f"https://creativecommons.org/licenses/by/{index:02d}.0/",
                source=make_source(
                    url=f"https://example.org/photo-{index:02d}",
                    title=f"Panda photograph {index:02d}",
                ),
            ),
            role=ImageRole.HERO if index == 0 else ImageRole.SUPPORTING,
        )
        for index in range(count)
    )


def make_content(
    title: str = "The Giant Panda",
    facts: Sequence[Fact] = (),
    sections: Sequence[NarrativeSection] = (),
    sources: Sequence[Source] = (),
) -> ResearchContent:
    return ResearchContent(
        title=title,
        subtitle="A bear that eats a grass",
        summary="Giant pandas spend most of their waking hours eating bamboo.",
        facts=facts if facts else make_facts(3),
        sections=sections
        if sections
        else (
            NarrativeSection(
                heading="Diet",
                body="Bamboo makes up almost the whole diet.",
                sources=(make_source(),),
            ),
        ),
        keywords=("giant panda", "bamboo"),
        sources=sources if sources else (make_source(),),
    )


def make_brief(
    options: RenderOptions | None = None,
    locale: str = "en-US",
) -> Brief:
    return Brief(
        prompt="an infographic about giant pandas",
        options=options if options is not None else RenderOptions(),
        audience="curious adults",
        locale=locale,
    )


# --------------------------------------------------------------------------- #
# Panda fixtures with their real licences
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PandaFixture:
    filename: str
    author: str
    license_text: str
    license_url: str
    source_url: str
    alt_text: str

    def as_asset(
        self,
        role: ImageRole = ImageRole.SUPPORTING,
        width_px: int = 1600,
        height_px: int = 1066,
    ) -> ImageAsset:
        return ImageAsset(
            content=PANDA_DIR / self.filename,
            mime_type="image/jpeg",
            width_px=width_px,
            height_px=height_px,
            alt_text=self.alt_text,
            credit=ImageCredit(
                license=self.license_text,
                author=self.author,
                license_url=self.license_url,
                source=Source(url=self.source_url, title=self.filename),
            ),
            role=role,
        )


PANDAS = (
    PandaFixture(
        filename="giant-panda-in-habitat.jpg",
        author="james_birdtourasia",
        license_text="CC0 1.0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        source_url="https://commons.wikimedia.org/wiki/File:Qinling_Giant_Panda",
        alt_text="A wild giant panda in a bamboo thicket",
    ),
    PandaFixture(
        filename="giant-panda-portrait.jpg",
        author="Gzen92",
        license_text="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
        source_url="https://commons.wikimedia.org/wiki/File:Panda_geant_tete",
        alt_text="Sunlit close-up of a giant panda's head",
    ),
    PandaFixture(
        filename="giant-panda-eating-bamboo.jpg",
        author="Kevin Dooley",
        license_text="CC BY 2.0",
        license_url="https://creativecommons.org/licenses/by/2.0/",
        source_url="https://commons.wikimedia.org/wiki/File:Giant_panda_eating_bamboo",
        alt_text="A giant panda chewing a peeled bamboo stalk",
    ),
)

PANDA_SET = tuple(
    panda.as_asset(role=ImageRole.HERO if index == 0 else ImageRole.SUPPORTING)
    for index, panda in enumerate(PANDAS)
)
"""The real photographs with the roles the imagery stage would give them.

The explicit ``HERO`` documents intent and does not change the layout: ``_hero_index``
promotes the first asset given when none carries ``HERO``, so this set lays out
identically to a plain ``tuple(panda.as_asset() for panda in PANDAS)``. A lead asset
of some kind is what narrows the masthead into a ``46fr 54fr`` grid track, the only
configuration where the ``--fit`` cap on the title binds -- and ``images=()`` is the
only thing that flattens the masthead back out."""


# --------------------------------------------------------------------------- #
# Shared assertions
# --------------------------------------------------------------------------- #


def assert_structurally_valid(html: str) -> ParsedHtml:
    assert html.lstrip().lower().startswith("<!doctype html"), (
        "document must open with an HTML5 doctype"
    )
    parsed = parse(html)
    assert parsed.tagged("html"), "document must have an <html> element"
    count = parsed.title_count
    assert count == 1, f"expected exactly one <title>, got {count}"
    return parsed


# --------------------------------------------------------------------------- #
# A real browser, launched once
# --------------------------------------------------------------------------- #

BROWSER_LOOP = pytest.mark.asyncio(loop_scope="module")
"""Tests sharing the module-scoped browser must share its event loop too:
playwright objects belong to the loop that created them."""


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def chromium() -> AsyncIterator[Browser]:
    """One browser for the whole module. Launching costs more than every
    measurement taken in it put together."""
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - playwright is a declared dependency
        pytest.skip("playwright is not installed")

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except PlaywrightError as error:  # pragma: no cover - browser not installed
            pytest.skip(f"chromium is unavailable: {error}")
        try:
            yield browser
        finally:
            await browser.close()


@asynccontextmanager
async def laid_out(browser: Browser, composition: Composition) -> AsyncIterator[Page]:
    """The composition in a DOM: viewport width from the composition, every network
    request aborted exactly as ``render/`` aborts them.

    Two things deliberately diverge from the renderer. JavaScript is left enabled
    where ``render/`` sets ``java_script_enabled = False`` -- every measurement here
    is taken through ``page.evaluate``, so it has to be. And the viewport height is a
    fixed 900 where the renderer floors at 1px and lets a full-page screenshot grow;
    height does not enter CSS layout for this page, whose sizing is driven by the
    width and by container queries, so neither divergence can move a number measured
    here."""
    page = await browser.new_page(
        viewport={"width": composition.width_px, "height": 900}
    )
    try:
        await page.route("**/*", lambda route: route.abort())
        await page.set_content(composition.html, wait_until="load")
        yield page
    finally:
        await page.close()


# --------------------------------------------------------------------------- #
# The matrix every rendered-page fence runs over
# --------------------------------------------------------------------------- #
# A promise about the rendered page is a promise about every body that ships, in
# every theme it ships in. Asserting it on one body leaves the other two free to
# break it, and a fourth body free to arrive with no coverage at all.

TEMPLATE_IDS: Final = tuple(sorted(RENDERABLE_TEMPLATE_IDS))
"""Every body a selector may return today. Sorted, because
``RENDERABLE_TEMPLATE_IDS`` is a ``frozenset`` and iterating it in set order gives
different test ids on different runs."""

THEMES: Final = (Theme.LIGHT, Theme.DARK)

BODIES = pytest.mark.parametrize("template_id", TEMPLATE_IDS)
IN_BOTH_THEMES = pytest.mark.parametrize("theme", THEMES, ids=[t.value for t in THEMES])
"""The theme ids are spelled out rather than inferred, so a ``Theme`` that stops
being a ``str`` cannot silently turn the cells into ``theme0``/``theme1``."""


def test_the_matrix_covers_every_body_and_every_theme() -> None:
    """Both axes are the whole population, not a sample of it.

    ``TEMPLATE_IDS`` is derived at runtime, so an empty registry would turn all 54
    browser cells into pytest's ``got empty parameter set`` skips -- green, silent, and
    measuring nothing. ``THEMES`` is written by hand, so a new ``Theme`` member ships a
    palette no cell renders.
    """
    assert TEMPLATE_IDS, (
        "no template is renderable, so every parametrized browser fence collapses to "
        "an empty parameter set and skips instead of failing"
    )
    assert set(THEMES) == set(Theme), (
        f"THEMES covers {[t.value for t in THEMES]} but Theme now has "
        f"{sorted(t.value for t in Theme)}: "
        f"{sorted(t.value for t in set(Theme) - set(THEMES))} would render in no cell"
    )


@dataclass(frozen=True, slots=True)
class BodySelectors:
    """Where a body puts its display number, and the cell that sizes it."""

    value: str
    """The ``.fitted`` headline figure."""
    container: str
    """Its query container -- the element carrying ``container-type: inline-size``,
    whose width every ``cqw`` in the value's size resolves against."""
    scale_prefix: str
    """The ``--size`` rung class prefix the body puts on the value."""


BODY_SELECTORS: Final[Mapping[str, BodySelectors]] = MappingProxyType(
    {
        "stat_grid": BodySelectors(".row__value", ".row__head", "value--"),
        "process_flow": BodySelectors(".chip__value", ".chip", "chip--"),
        "ranked_list": BodySelectors(".rank__value", ".rank__figure", "value--"),
    }
)
"""One body's display-number vocabulary per renderable template.

A table rather than a ``data-`` attribute on the templates: it buys the same
"impossible to silently measure nothing" property with no diff to shipped HTML. The
cost is that the table can drift from the markup, which is what the test below and
the measured element counts, container types and computed sizes in the browser cells
exist to catch."""


def test_every_renderable_template_declares_its_display_number_selectors() -> None:
    assert set(BODY_SELECTORS) == set(RENDERABLE_TEMPLATE_IDS), (
        "BODY_SELECTORS and RENDERABLE_TEMPLATE_IDS have diverged: "
        f"missing {sorted(RENDERABLE_TEMPLATE_IDS - set(BODY_SELECTORS))}, "
        f"stale {sorted(set(BODY_SELECTORS) - RENDERABLE_TEMPLATE_IDS)}. A new "
        "renderable template must supply its value and container selectors or every "
        "browser fence silently measures nothing."
    )
    blank = {
        template_id: selectors
        for template_id, selectors in BODY_SELECTORS.items()
        if not (selectors.value and selectors.container and selectors.scale_prefix)
    }
    assert not blank, (
        f"blank selectors in BODY_SELECTORS: {blank}. An empty string is not a "
        "placeholder -- querySelectorAll raises on it, so the cell dies as a raw "
        "playwright SyntaxError instead of naming the template that never filled "
        "its row in."
    )


async def compose_cell(
    template_id: str,
    theme: Theme,
    content: ResearchContent,
    images: Sequence[ImageAsset] = (),
    width_px: int = RenderOptions().width_px,
) -> Composition:
    """One cell of the matrix: one body, one theme, one page width.

    Both axes reach the page the only way production reaches it -- the body through
    ``HtmlComposer``'s keyword-only ``template_id``, the theme through the brief's
    ``RenderOptions``, which the chrome turns into ``<html data-theme>``.

    Naming a ``template_id`` also selects that path's error behaviour: it *skips* an
    unreadable ``Path`` asset where the bare ``HtmlComposer()`` raises ``OSError``.
    Every fence here hands over readable fixtures, so the divergence cannot move a
    measurement -- but ``HtmlComposer(template_id="stat_grid")`` is not the same
    object as ``HtmlComposer()``.
    """
    return await HtmlComposer(template_id=template_id).compose(
        make_brief(options=RenderOptions(width_px=width_px, theme=theme)),
        content,
        images,
    )


# A browser measurement arrives as untyped JSON. These four turn it into the types the
# assertions are written against, so a shape that changed under us fails here with the
# offending value rather than three lines later as a confusing comparison.


def _number(value: object) -> float:
    assert isinstance(value, (int, float)), f"expected a number, measured {value!r}"
    return float(value)


def _text(value: object) -> str:
    assert isinstance(value, str), f"expected text, measured {value!r}"
    return value


def _rows(value: object) -> Sequence[object]:
    assert isinstance(value, list), f"expected a list, measured {value!r}"
    return value


def _fields(value: object) -> Mapping[str, object]:
    assert isinstance(value, dict), f"expected an object, measured {value!r}"
    return value


# --------------------------------------------------------------------------- #
# 1. Self-containment
# --------------------------------------------------------------------------- #


async def test_composed_document_fetches_nothing_from_the_network() -> None:
    content = make_content(
        sources=(
            Source(url="https://www.worldwildlife.org/species/panda", title="WWF"),
            Source(url="https://www.iucnredlist.org/species/712", title="IUCN"),
        )
    )
    images = (make_asset(), make_asset(role=ImageRole.SUPPORTING))

    composition = await HtmlComposer().compose(make_brief(), content, images)
    parsed = assert_structurally_valid(composition.html)

    for url in parsed.fetchable_urls:
        assert not url.lower().startswith(REMOTE_SCHEMES), (
            f"fetchable remote URL: {url!r}"
        )
    for url in parsed.css_urls:
        assert url.startswith("data:"), f"CSS url() must be a data URI, got {url!r}"

    assert not parsed.tagged("link"), "<link> pulls in an external resource"
    assert all("src" not in attrs for attrs in parsed.tagged("script")), (
        "<script src=...> pulls in an external resource"
    )
    assert "@import" not in composition.html.lower(), (
        "@import pulls in an external stylesheet"
    )

    assert any(url.startswith("data:") for url in parsed.fetchable_urls), (
        "images should be embedded as data URIs"
    )


async def test_researched_urls_survive_as_visible_text_not_as_links() -> None:
    url = "https://www.worldwildlife.org/species/giant-panda"
    content = make_content(sources=(Source(url=url, title="WWF"),))

    composition = await HtmlComposer().compose(make_brief(), content, ())
    parsed = parse(composition.html)

    assert url not in parsed.fetchable_urls, (
        "a researched URL must never become an href"
    )


# --------------------------------------------------------------------------- #
# 2. Escaping
# --------------------------------------------------------------------------- #


def hostile_inputs() -> tuple[ResearchContent, tuple[ImageAsset, ...]]:
    """Untrusted web text with an XSS payload in every string-bearing field."""
    hostile_source = Source(
        url=URL_PAYLOAD, title=SCRIPT_PAYLOAD, publisher=ATTR_PAYLOAD
    )
    content = ResearchContent(
        title=SCRIPT_PAYLOAD,
        subtitle=ATTR_PAYLOAD,
        summary=STYLE_PAYLOAD,
        facts=(
            Fact(
                label=ATTR_PAYLOAD,
                value=STYLE_PAYLOAD,
                unit=None,
                detail=SCRIPT_PAYLOAD,
                source=hostile_source,
            ),
        ),
        sections=(
            NarrativeSection(
                heading=STYLE_PAYLOAD, body=ATTR_PAYLOAD, sources=(hostile_source,)
            ),
        ),
        keywords=(SCRIPT_PAYLOAD,),
        sources=(hostile_source,),
    )
    images = (
        make_asset(
            alt_text=SCRIPT_PAYLOAD,
            credit=make_credit(
                license_text=STYLE_PAYLOAD,
                author=ATTR_PAYLOAD,
                license_url=URL_PAYLOAD,
                source=hostile_source,
            ),
        ),
    )
    return content, images


async def test_xss_payloads_are_escaped_rather_than_emitted_as_markup() -> None:
    content, images = hostile_inputs()

    composition = await HtmlComposer().compose(make_brief(), content, images)
    html = composition.html

    for payload in MARKUP_PAYLOADS:
        assert payload not in html, f"unescaped payload in output: {payload!r}"
    assert "&lt;script&gt;" in html, "the payload should survive in escaped form"

    parsed = parse(html)
    rendered = parsed.rendered_strings
    for payload in MARKUP_PAYLOADS:
        assert payload in rendered, f"escaped payload lost entirely: {payload!r}"


async def test_hostile_urls_never_reach_a_fetchable_attribute() -> None:
    content, images = hostile_inputs()

    composition = await HtmlComposer().compose(make_brief(), content, images)
    parsed = parse(composition.html)

    for url in (*parsed.fetchable_urls, *parsed.css_urls):
        assert "javascript:" not in url.lower(), (
            f"javascript: URL reached the DOM: {url!r}"
        )


VALUE_TEXT_JS = """
(selector) => Array.from(document.querySelectorAll(selector))
  .map(el => el.innerText)
  .join('\\n')
"""
"""The rendered text of one body's display numbers. An empty match gives an empty
string, so an assertion against it cannot pass on a page that rendered none."""


@IN_BOTH_THEMES
@BODIES
@BROWSER_LOOP
async def test_payloads_are_inert_text_in_a_real_browser_dom(
    chromium: Browser, template_id: str, theme: Theme
) -> None:
    """Every body renders a different subset of the payload carriers -- one puts a
    fact's value in a chip, another in a ranked row -- so escaping has to hold in all
    of them, not just in the one the default composer happens to pick."""
    selectors = BODY_SELECTORS[template_id]
    content, images = hostile_inputs()
    composition = await compose_cell(template_id, theme, content, images)

    async with laid_out(chromium, composition) as page:
        scripts: int = await page.evaluate("document.querySelectorAll('script').length")
        on_error: int = await page.evaluate(
            "document.querySelectorAll('[onerror]').length"
        )
        injected_img: int = await page.evaluate(
            "document.querySelectorAll('img[src=\"x\"]').length"
        )
        body_text: str = await page.evaluate("document.body.innerText")
        value_text: str = await page.evaluate(VALUE_TEXT_JS, selectors.value)

    assert scripts == 0, "a <script> element made it into the DOM"
    assert on_error == 0, "an element with an onerror handler made it into the DOM"
    assert injected_img == 0, "the injected <img src=x> made it into the DOM"

    for payload in MARKUP_PAYLOADS:
        assert payload in body_text, f"payload should be visible as text: {payload!r}"

    # The three payloads above all reach `innerText` through shared chrome -- title,
    # subtitle, summary -- so that loop alone would report the same thing six times.
    # The hostile fact's value is the one payload carried by a per-body element.
    assert STYLE_PAYLOAD in value_text, (
        f"{template_id}: the hostile fact value is not rendered inside "
        f"{selectors.value!r} (measured {value_text!r}), so this cell only ever "
        "asserted escaping in the shared chrome that every body inherits"
    )


# --------------------------------------------------------------------------- #
# 3. Empty imagery
# --------------------------------------------------------------------------- #


async def test_text_only_layout_when_no_images_are_supplied() -> None:
    content = make_content(title="Pandas Without Pictures", facts=make_facts(4))

    composition = await HtmlComposer().compose(make_brief(), content, ())
    parsed = assert_structurally_valid(composition.html)

    assert content.title in parsed.text
    for fact in content.facts:
        assert fact.label in parsed.text
        assert fact.value in parsed.text


# --------------------------------------------------------------------------- #
# 4. Render options
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "options",
    [
        RenderOptions(width_px=900, height_px=4321, device_scale_factor=1.5),
        RenderOptions(width_px=777, height_px=None, device_scale_factor=3.0),
    ],
    ids=["fixed-height", "full-page"],
)
async def test_render_options_are_copied_into_the_composition(
    options: RenderOptions,
) -> None:
    brief = make_brief(options=options)

    composition = await HtmlComposer().compose(brief, make_content(), ())

    assert composition.width_px == options.width_px
    assert composition.height_px == options.height_px
    assert composition.device_scale_factor == options.device_scale_factor


# --------------------------------------------------------------------------- #
# 5. Attribution
# --------------------------------------------------------------------------- #


async def test_every_supplied_image_is_credited_by_licence_author_and_source() -> None:
    images = tuple(panda.as_asset() for panda in PANDAS)

    composition = await HtmlComposer().compose(make_brief(), make_content(), images)
    parsed = assert_structurally_valid(composition.html)

    for panda in PANDAS:
        assert panda.license_text in parsed.text, (
            f"missing licence for {panda.filename}"
        )
        assert panda.author in parsed.text, f"missing author for {panda.filename}"
        assert panda.source_url in parsed.text or panda.filename in parsed.text, (
            f"missing source attribution for {panda.filename}"
        )


async def test_every_embedded_image_carries_its_licence() -> None:
    images = tuple(panda.as_asset() for panda in PANDAS)

    composition = await HtmlComposer().compose(make_brief(), make_content(), images)
    parsed = parse(composition.html)
    embedded = set(parsed.fetchable_urls)

    displayed = [
        panda
        for panda, asset in zip(PANDAS, images, strict=True)
        if to_data_uri(asset) in embedded
    ]
    assert displayed, "no supplied image was embedded at all"
    for panda in displayed:
        assert panda.license_text in parsed.text, (
            f"{panda.filename} is displayed but its licence is not rendered"
        )
        assert panda.license_url in parsed.text, (
            f"{panda.filename} is displayed but its licence URL is not rendered"
        )


async def test_exactly_the_displayed_images_are_embedded_and_credited() -> None:
    """"You may use a subset of ``images``, but embed and credit exactly the ones
    you display." Embedding an unused asset bloats the document; crediting one
    claims a use that was never made."""
    assets = make_numbered_assets(6)

    composition = await HtmlComposer().compose(make_brief(), make_content(), assets)
    parsed = assert_structurally_valid(composition.html)
    embedded = {url for url in parsed.fetchable_urls if url.startswith("data:image/")}
    credit_blocks = parsed.classed("credit")

    assert embedded, "no image was embedded at all"
    assert len(embedded) < len(assets), (
        f"expected a subset of {len(assets)} assets to be displayed, "
        f"but all {len(embedded)} were embedded -- the test no longer proves anything"
    )
    assert len(embedded) == len(credit_blocks), (
        f"{len(embedded)} embedded images but {len(credit_blocks)} credit blocks: "
        "every displayed image needs exactly one credit"
    )

    for asset in assets:
        credit = asset.credit
        assert credit.license_url is not None and credit.source is not None
        if to_data_uri(asset) in embedded:
            assert credit.license in parsed.text, (
                f"{asset.alt_text} is embedded but its licence is not rendered"
            )
            assert credit.license_url in parsed.text, (
                f"{asset.alt_text} is embedded but its licence URL is not rendered"
            )
        else:
            for unused in (credit.license, credit.license_url, credit.source.url):
                assert unused not in parsed.text, (
                    f"{asset.alt_text} was never displayed, yet {unused!r} is "
                    "rendered -- that credits a use the page did not make"
                )


async def test_a_blank_credit_renders_no_credit_row_at_all() -> None:
    """An ``ImageCredit`` with nothing in it would otherwise draw a ruled row that
    says nothing."""
    blank = ImageCredit(license="", author=None, license_url=None, source=None)
    asset = make_asset(credit=blank)

    composition = await HtmlComposer().compose(make_brief(), make_content(), (asset,))
    parsed = assert_structurally_valid(composition.html)

    assert not parsed.classed("credit"), (
        "a credit with no licence, author, licence URL or source rendered a row anyway"
    )
    assert any(url.startswith("data:image/") for url in parsed.fetchable_urls), (
        "the image itself should still be displayed; only its empty credit is dropped"
    )


@pytest.mark.parametrize(
    "credit",
    [
        ImageCredit(license="CC0 1.0", author=None, license_url=None, source=None),
        ImageCredit(license="", author="Gzen92", license_url=None, source=None),
        ImageCredit(
            license="",
            author=None,
            license_url="https://creativecommons.org/licenses/by-sa/4.0/",
            source=None,
        ),
        ImageCredit(license="", author=None, license_url=None, source=make_source()),
    ],
    ids=["licence-only", "author-only", "licence-url-only", "source-only"],
)
async def test_any_single_piece_of_attribution_earns_a_credit_row(
    credit: ImageCredit,
) -> None:
    asset = make_asset(credit=credit)

    composition = await HtmlComposer().compose(make_brief(), make_content(), (asset,))
    parsed = assert_structurally_valid(composition.html)

    blocks = parsed.classed("credit")
    assert len(blocks) == 1, (
        f"expected one credit row for {credit}, got {len(blocks)}: attribution that "
        "reaches the composer must reach the page"
    )


# --------------------------------------------------------------------------- #
# 6. Completeness
# --------------------------------------------------------------------------- #


async def test_every_fact_is_rendered_because_capping_is_the_researchers_job() -> None:
    facts = make_facts(10)
    content = make_content(facts=facts)

    composition = await HtmlComposer().compose(make_brief(), content, ())
    parsed = parse(composition.html)

    missing = [
        fact.label
        for fact in facts
        if fact.label not in parsed.text or fact.value not in parsed.text
    ]
    assert not missing, f"facts dropped from the layout: {missing}"


@pytest.mark.parametrize("count", [1, 2, 4, 5, 6, 9, 10, 23])
async def test_every_fact_survives_however_the_ledger_chunks_them(count: int) -> None:
    """The ledger sets five rows to a block. Counts that straddle a block boundary
    are where rows go missing; 23 is where an implementation that sliced once
    instead of chunking silently dropped everything past the tenth."""
    facts = make_facts(count)

    composition = await HtmlComposer().compose(
        make_brief(), make_content(facts=facts), ()
    )
    parsed = assert_structurally_valid(composition.html)

    missing = [
        fact.label
        for fact in facts
        if fact.label not in parsed.text or fact.value not in parsed.text
    ]
    assert not missing, f"{len(missing)} of {count} facts dropped: {missing}"

    rows = parsed.classed("row__label")
    assert len(rows) == count, f"expected {count} ledger rows, rendered {len(rows)}"


async def test_a_sections_own_source_is_shown_beside_it_and_in_the_bibliography() -> (
    None
):
    """A section's sources are as real as the document's. They used to be dropped
    on the floor -- and the publisher alone proves nothing, because the
    bibliography renders it too."""
    source = Source(
        url="https://www.smithsonianmag.com/giant-panda-diet",
        title="What giant pandas actually eat",
        publisher="Smithsonian Magazine",
    )
    content = make_content(
        sections=(
            NarrativeSection(
                heading="Diet",
                body="Bamboo makes up almost the whole diet.",
                sources=(source,),
            ),
        )
    )

    composition = await HtmlComposer().compose(make_brief(), content, ())
    parsed = assert_structurally_valid(composition.html)

    assert source.publisher is not None
    assert source.publisher in parsed.text_in("section__src"), (
        f"{source.publisher!r} is not shown beside the prose it supports; "
        f"the section attribution reads {parsed.text_in('section__src')!r}"
    )
    assert source.url in parsed.text_in("refs__meta"), (
        f"{source.url!r} never reached the bibliography, so the citation is "
        "unverifiable from the PNG"
    )


# --------------------------------------------------------------------------- #
# 7. Theme
# --------------------------------------------------------------------------- #


async def test_light_and_dark_themes_produce_different_valid_documents() -> None:
    composer = HtmlComposer()
    content = make_content()

    light = await composer.compose(
        make_brief(options=RenderOptions(theme=Theme.LIGHT)), content, ()
    )
    dark = await composer.compose(
        make_brief(options=RenderOptions(theme=Theme.DARK)), content, ()
    )

    assert_structurally_valid(light.html)
    assert_structurally_valid(dark.html)
    assert light.html != dark.html, "the theme had no effect on the output"


# --------------------------------------------------------------------------- #
# 8. Structure
# --------------------------------------------------------------------------- #


async def test_document_declares_the_brief_locale_and_a_single_title() -> None:
    brief = make_brief(locale="fr-CA")
    content = make_content(title="Le Panda Géant")

    composition = await HtmlComposer().compose(brief, content, ())
    parsed = assert_structurally_valid(composition.html)

    assert parsed.tagged("html")[0].get("lang") == brief.locale
    assert composition.title, "Composition.title must be populated"


@pytest.mark.parametrize(
    ("locale", "lang", "direction"),
    [
        ("en-US", "en-US", "ltr"),
        ("he_IL", "he-IL", "rtl"),
        ("ar", "ar", "rtl"),
        ("fr_CA", "fr-CA", "ltr"),
    ],
)
async def test_html_lang_is_a_language_tag_and_direction_follows_it(
    locale: str, lang: str, direction: str
) -> None:
    """``he_IL`` is a POSIX locale name. ``<html lang>`` takes a BCP 47 tag, where
    the separator is a hyphen -- an underscore makes the attribute invalid and the
    language unknown to the browser."""
    composition = await HtmlComposer().compose(
        make_brief(locale=locale), make_content(), ()
    )
    parsed = assert_structurally_valid(composition.html)
    root = parsed.tagged("html")[0]

    assert root.get("lang") == lang, (
        f"locale {locale!r} must render as BCP 47 {lang!r}, got {root.get('lang')!r}"
    )
    assert root.get("dir") == direction, (
        f"locale {locale!r} is a {direction} language, "
        f"but dir is {root.get('dir')!r}"
    )


# --------------------------------------------------------------------------- #
# 9. Unreadable assets
# --------------------------------------------------------------------------- #


async def test_missing_path_backed_asset_raises_oserror(tmp_path: Path) -> None:
    asset = make_asset(content=tmp_path / "absent.png")

    with pytest.raises(OSError):
        await HtmlComposer().compose(make_brief(), make_content(), (asset,))


# --------------------------------------------------------------------------- #
# 10. Structural typing
# --------------------------------------------------------------------------- #


async def test_html_composer_satisfies_the_composer_port() -> None:
    composer: Composer = HtmlComposer()

    composition = await composer.compose(make_brief(), make_content(), ())

    assert isinstance(composition, Composition)


# --------------------------------------------------------------------------- #
# 11. Fonts
# --------------------------------------------------------------------------- #
# "Fonts must be a generic stack or a woff2 embedded as a data URI; a family that
# only exists on your machine makes the render machine-dependent." The whole
# stylesheet once set Superclarendon / Iowan Old Style / Menlo, so the same page
# rendered as three different documents on three different machines -- and
# nothing here noticed.


async def test_no_font_family_names_a_typeface_only_some_machines_have() -> None:
    composition = await HtmlComposer().compose(make_brief(), make_content(), ())
    parsed = assert_structurally_valid(composition.html)
    css = css_declarations(parsed.css)
    embedded = {face.family.lower() for face in font_faces()}

    families = font_families(css)
    assert families, "the stylesheet declares no font-family at all"

    unknown = sorted(set(families) - embedded - GENERIC_FAMILIES)
    assert not unknown, (
        f"font families that are neither embedded in the document nor generic: "
        f"{unknown}. Embedded: {sorted(embedded)}. If one of these is genuinely "
        f"safe everywhere, add it to GENERIC_FAMILIES on purpose."
    )

    reintroduced = [name for name in LOCAL_FONT_NAMES if name in css.lower()]
    assert not reintroduced, (
        f"machine-specific font names are back in the stylesheet: {reintroduced}. "
        "Embed a woff2 as a data URI instead; a local family makes the PNG depend "
        "on which machine rendered it."
    )


async def test_every_font_is_embedded_as_woff2_and_none_is_fetched() -> None:
    composition = await HtmlComposer().compose(make_brief(), make_content(), ())
    parsed = assert_structurally_valid(composition.html)

    bodies = font_face_bodies(parsed.css)
    assert bodies, "no @font-face block: nothing is embedded, so nothing is portable"
    assert len(bodies) == len(font_faces()), (
        f"layout.font_faces() reports {len(font_faces())} faces but the document "
        f"declares {len(bodies)} @font-face blocks"
    )

    for body in bodies:
        sources = tuple(match.group("value") for match in _SRC_DECL.finditer(body))
        assert sources, f"@font-face with no src: {elide(without_payloads(body))!r}"
        for source in sources:
            urls = tuple(match.group("url") for match in _CSS_URL.finditer(source))
            assert urls, f"@font-face src names no url(): {elide(source)!r}"
            for url in urls:
                assert url.startswith(WOFF2_DATA_PREFIX), (
                    f"@font-face src must be an embedded woff2 "
                    f"({WOFF2_DATA_PREFIX}...), got {elide(url)!r}"
                )
            outside = without_payloads(source).lower()
            for reach in ("http", "//", "local("):
                assert reach not in outside, (
                    f"@font-face src reaches outside the document via {reach!r}: "
                    f"{outside!r}"
                )


# --------------------------------------------------------------------------- #
# 12. What the browser actually renders
# --------------------------------------------------------------------------- #
# Markup that reads correctly can still lay out wrongly: a licence URI
# uppercased by an inherited text-transform, an aspect-ratio overridden by a
# later height rule, a caption spilling out of its figure. None of these are
# visible to a parser, and all three shipped.


@IN_BOTH_THEMES
@BODIES
@BROWSER_LOOP
async def test_licence_uris_are_rendered_exactly_as_supplied(
    chromium: Browser, template_id: str, theme: Theme
) -> None:
    """``.credit__url`` inherited ``text-transform: uppercase`` from
    ``.credit__license``, so every licence URI rendered UPPERCASED -- unusable,
    because a URI in a PNG has to be retyped by hand. The markup was right the
    whole time, which is why only the rendered page can catch it.
    """
    # Iterating PANDAS holds only while every body displays all of them. At six
    # assets stat_grid renders four images and four credits -- a hero plus a band
    # that caps at three -- where the other bodies render six of each. Grow PANDAS
    # past four and this breaks on stat_grid alone; iterate rendered credits then.
    images = tuple(panda.as_asset() for panda in PANDAS)
    composition = await compose_cell(template_id, theme, make_content(), images)

    async with laid_out(chromium, composition) as page:
        transformed: list[dict[str, str]] = await page.evaluate(TRANSFORMED_URL_TEXT_JS)
        rendered: str = await page.evaluate("document.body.innerText")

    assert not transformed, (
        "text containing a URI is being case-transformed by CSS, which corrupts it: "
        f"{transformed}"
    )
    for panda in PANDAS:
        for url in (panda.license_url, panda.source_url):
            assert url != url.upper(), (
                f"fixture {url!r} has no lowercase letters, so it cannot show "
                "whether the page uppercased it"
            )
            assert url in rendered, (
                f"{url!r} is not in the rendered text of the page "
                "(it may be there in a mangled form)"
            )
            assert url.upper() not in rendered, (
                f"{url!r} was rendered uppercased; a licence URI has to be "
                "transcribable by hand out of the PNG"
            )


TRANSFORMED_URL_TEXT_JS = """
() => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const offenders = [];
  const seen = new Set();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.nodeValue.includes('://')) continue;
    const el = node.parentElement;
    if (!el || seen.has(el)) continue;
    seen.add(el);
    const transform = getComputedStyle(el).textTransform;
    if (transform !== 'none') {
      offenders.push({
        element: el.tagName.toLowerCase() + '.' + el.className,
        textTransform: transform,
        text: node.nodeValue.trim().slice(0, 60),
      });
    }
  }
  return offenders;
}
"""
"""Own text nodes only. Judging by ``textContent`` would blame ``<body>`` for
every URI on the page and report a uniform false positive."""


ASPECT_RATIO_JS = """
() => Array.from(document.querySelectorAll('img'))
  .filter(el => el.style.aspectRatio)
  .map(el => {
    const box = el.getBoundingClientRect();
    return {
      declared: el.style.aspectRatio,
      width: box.width,
      height: box.height,
      alt: el.alt,
    };
  })
"""


@IN_BOTH_THEMES
@BODIES
@BROWSER_LOOP
async def test_a_declared_aspect_ratio_governs_the_rendered_image(
    chromium: Browser, template_id: str, theme: Theme
) -> None:
    """``img { height: 100% }`` used to beat the inline ``aspect-ratio``, so every
    figure in a band came out the height of its tallest sibling: a 3:2 image
    rendered at 0.951 and lost about 37% of itself. The two band images here have
    deliberately different ratios, so no single rendered height can satisfy both.
    """
    # The explicit dimensions are the whole setup: as_asset() defaults every fixture
    # to 1600x1066, which declares one ratio for all three and leaves the guard below
    # nothing to compare. Both shapes also have to stay inside the layout's clamp
    # band, or the clamp itself collapses them back to a single declared ratio.
    images = (
        PANDAS[0].as_asset(role=ImageRole.HERO),
        PANDAS[1].as_asset(width_px=1600, height_px=1066),
        PANDAS[2].as_asset(width_px=1600, height_px=1600),
    )
    composition = await compose_cell(template_id, theme, make_content(), images)

    async with laid_out(chromium, composition) as page:
        measured: list[dict[str, float | str]] = await page.evaluate(ASPECT_RATIO_JS)

    assert len(measured) >= 2, (
        f"expected at least two images with an inline aspect-ratio, got {measured}"
    )
    declared_ratios = {_declared_ratio(str(box["declared"])) for box in measured}
    assert len(declared_ratios) >= 2, (
        f"every measured image declares the same ratio ({declared_ratios}), so this "
        "test cannot tell a governing aspect-ratio from a shared rendered height"
    )

    for box in measured:
        declared = _declared_ratio(str(box["declared"]))
        width, height = float(box["width"]), float(box["height"])
        assert height > 0, f"image {box['alt']!r} rendered with no height"
        rendered = width / height
        assert abs(rendered / declared - 1) <= 0.02, (
            f"image {box['alt']!r} declares aspect-ratio {box['declared']!r} "
            f"({declared:.4f}) but rendered {width:.1f}x{height:.1f} "
            f"= {rendered:.4f}: a {abs(rendered / declared - 1):.1%} distortion, so "
            "some other rule is deciding its height"
        )


def _declared_ratio(value: str) -> float:
    """Chromium serialises ``aspect-ratio: 1.5`` back out as ``"1.5 / 1"``."""
    numerator, _, denominator = value.partition("/")
    return float(numerator) / float(denominator or 1)


OVERFLOW_JS = """
() => {
  const name = el =>
    el.tagName.toLowerCase() + (el.className ? '.' + el.className : '');
  const offenders = [];
  let examined = 0;
  for (const el of document.body.querySelectorAll('*')) {
    const box = el.getBoundingClientRect();
    if (box.width === 0 && box.height === 0) continue;
    examined += 1;
    const parent = el.parentElement;
    if (parent) {
      const bounds = parent.getBoundingClientRect();
      const spills = {
        below: box.bottom - bounds.bottom,
        above: bounds.top - box.top,
        past_start: bounds.left - box.left,
        past_end: box.right - bounds.right,
      };
      for (const [side, amount] of Object.entries(spills)) {
        if (amount > 1) {
          offenders.push({
            element: name(el), parent: name(parent),
            problem: side, pixels: Math.round(amount),
          });
        }
      }
    }
    if (el.scrollWidth > el.clientWidth + 1) {
      offenders.push({
        element: name(el), parent: parent ? name(parent) : null,
        problem: 'scrolls_horizontally',
        pixels: el.scrollWidth - el.clientWidth,
      });
    }
  }
  return {examined, offenders};
}
"""
"""``examined`` is every box the walk actually judged. Without it ``not offenders`` is
green for a page that laid out nothing at all -- a body whose ``{% block body %}``
guards never match its layout's fields renders as bare chrome and overflows nothing."""

MIN_EXAMINED_BOXES: Final = 40
"""Measured: 63 boxes in the leanest overflow cell (``process_flow``, no images) and
116 in the richest. The masthead and bibliography together are 7-10, so a body block
that matched nothing lands near ten and 40 sits clear of both ends."""


@pytest.mark.parametrize("images", [PANDA_SET, ()], ids=["hero", "no-images"])
@pytest.mark.parametrize("width_px", [1200, 640])
@IN_BOTH_THEMES
@BODIES
@BROWSER_LOOP
async def test_nothing_overflows_the_box_it_is_laid_out_in(
    chromium: Browser,
    template_id: str,
    theme: Theme,
    width_px: int,
    images: Sequence[ImageAsset],
) -> None:
    """A screenshot has no scrollbars: anything outside its parent is either
    clipped away or painted over a neighbour. Band captions used to hang 27-58px
    below their own ``<figure>``.

    Width and images are crossed because the ``--fit`` cap only binds when the page
    is narrow *and* a hero has squeezed the masthead into its own column, so neither
    axis on its own reaches the code that derives it.
    """
    content = make_content(facts=make_facts(7))
    composition = await compose_cell(
        template_id, theme, content, images, width_px=width_px
    )

    async with laid_out(chromium, composition) as page:
        measured: dict[str, object] = await page.evaluate(OVERFLOW_JS)

    examined = int(_number(measured["examined"]))
    offenders = [_fields(row) for row in _rows(measured["offenders"])]

    assert examined >= MIN_EXAMINED_BOXES, (
        f"{template_id} at a {width_px}px {theme.value} page with {len(images)} images "
        f"laid out only {examined} boxes, under the {MIN_EXAMINED_BOXES} this fence "
        "needs to mean anything: the body block rendered next to nothing, and a page "
        "with nothing on it overflows nothing"
    )
    assert not offenders, (
        f"in {template_id} at a {width_px}px {theme.value} page with "
        f"{len(images)} images, elements overflow their parents:\n"
        + "\n".join(
            f"  {row['element']} overflows {row['parent']} "
            f"({row['problem']}) by {row['pixels']}px"
            for row in offenders
        )
    )


TITLE_LINES_JS = """
() => {
  const title = document.querySelector('h1.title');
  const walker = document.createTreeWalker(title, NodeFilter.SHOW_TEXT);
  const lines = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    for (let index = 0; index < node.data.length; index += 1) {
      const range = document.createRange();
      range.setStart(node, index);
      range.setEnd(node, index + 1);
      const rect = range.getBoundingClientRect();
      if (rect.width < 0.01 && rect.height < 0.01) continue;
      const line = lines.find(existing => Math.abs(existing.top - rect.top) < 0.5);
      if (line) line.text += node.data[index];
      else lines.push({top: rect.top, text: node.data[index]});
    }
  }
  return lines.sort((a, b) => a.top - b.top).map(line => line.text.trim());
}
"""
"""The title's lines, in order, read one character at a time and grouped on their
``top`` to within half a pixel: glyphs of one font on one line share a ``top``
exactly. ``DISPLAY_NUMBER_JS``'s overlap clustering cannot be reused here -- ``.title``
sets ``line-height: 0.98``, so consecutive line boxes overlap and every line merges
into one."""


@pytest.mark.parametrize(
    "title",
    ["The Giant Panda", "Giant Pandas by the Numbers"],
    ids=["fixture-title", "shipped-title"],
)
@BROWSER_LOOP
async def test_a_title_word_is_never_broken_to_make_it_fit(
    chromium: Browser, title: str
) -> None:
    """A headline may wrap between its words; it may never break inside one. That is
    exactly the guarantee ``_fit(_longest_word(title), _TITLE_ADVANCE)`` exists to
    give, so joining the rendered lines back together must reproduce the title's
    words exactly. A line count alone is too weak: a broken word hides inside it
    whenever some other word already had a line to itself.

    Both the fixture default and the title ``assets/panda/facts.json`` actually
    ships are crossed, so the fence is anchored to shipped content rather than to
    one fixture's favourable word lengths.

    640px with a hero is the one configuration where that cap binds -- the masthead
    text column narrows to 218px -- and since ``.title`` sets
    ``overflow-wrap: anywhere``, an underestimated advance shows up here as a
    mid-word break rather than as overflow, which the overflow fence cannot see.
    """
    content = make_content(title=title, facts=make_facts(7))
    composition = await HtmlComposer().compose(
        make_brief(options=RenderOptions(width_px=640)), content, PANDA_SET
    )

    async with laid_out(chromium, composition) as page:
        lines: list[str] = await page.evaluate(TITLE_LINES_JS)

    assert lines, (
        f"the character-rect walk returned no lines at all for {title!r}, so the "
        "reconstruction below would hold vacuously -- the walk or the markup broke"
    )

    words = title.split()
    assert " ".join(lines).split() == words, (
        f"the {len(words)}-word title {title!r} set on {len(lines)} lines "
        f"{lines}, so at least one word was broken: the --fit cap derived from its "
        "longest word is too generous for the type it allows"
    )


NARROW_FACTS = (
    Fact(
        label="Adult weight",
        value="26-84 lb (12-38 kg)",
        unit=None,
        detail="Adults vary widely between subspecies and sexes.",
        source=make_source(url="https://www.worldwildlife.org/species/giant-panda"),
    ),
    Fact(
        label="Daily bamboo intake",
        value="26-84",
        unit="lb per day",
        detail="WWF's range; other bodies measure different parts of the plant.",
    ),
    Fact(label="Wild population", value="1,864", unit="adults", detail="2014 census."),
    Fact(label="Red List status", value="Vulnerable", unit=None, detail="IUCN, 2016."),
    Fact(label="Hours spent eating", value="Up to 14 hours", unit="a day", detail=None),
    Fact(label="Cub birth weight", value="3.5", unit="oz", detail="Blind and pink."),
)


DISPLAY_NUMBER_JS = """
(sel) => {
  const values = Array.from(document.querySelectorAll(sel.value));
  const housed = values.filter(el => el.closest(sel.container));
  return {
    value_count: values.length,
    container_count: document.querySelectorAll(sel.container).length,
    body_px: parseFloat(getComputedStyle(document.body).fontSize),
    orphans: values
      .filter(el => !el.closest(sel.container))
      .map(el => el.innerText.replace(/\\s+/g, ' ').trim().slice(0, 40)),
    values: housed.map(el => {
      const range = document.createRange();
      range.selectNodeContents(el);
      const rects = Array.from(range.getClientRects())
        .filter(r => r.width > 0.5 && r.height > 0.5);
      const lines = [];
      for (const r of rects.slice().sort((a, b) => a.top - b.top)) {
        const line = lines.find(l => r.top < l.bottom - 1 && r.bottom > l.top + 1);
        if (line) {
          line.top = Math.min(line.top, r.top);
          line.bottom = Math.max(line.bottom, r.bottom);
        } else {
          lines.push({top: r.top, bottom: r.bottom});
        }
      }
      const cell = el.closest(sel.container);
      return {
        text: el.innerText.replace(/\\s+/g, ' ').trim(),
        lines: lines.length,
        rungs: [...el.classList].filter(c => c.startsWith(sel.scale_prefix)),
        size: getComputedStyle(el).getPropertyValue('--size').trim(),
        fit: el.style.getPropertyValue('--fit').trim(),
        font_px: parseFloat(getComputedStyle(el).fontSize),
        container_type: getComputedStyle(cell).containerType,
        cell_overflow: cell.scrollWidth - cell.clientWidth,
        value_overflow: el.scrollWidth - el.clientWidth,
      };
    }),
  };
}
"""
"""Both selectors are arguments, so one measurement serves every body.

Three things make "the container was not found" a failure rather than a zero.
``container_count`` is counted straight off the document, independently of the
values. Any value without its container as an ancestor comes back in ``orphans`` as
text, so the failure can name the element. And ``values`` measures only the ones that
are housed, which is why ``cell`` is never null and there is no ternary reporting a
comfortable zero for a cell that does not exist.

Lines are counted by clustering the range's client rects on vertical overlap, not by
counting the rects: a value carries a small-font unit span, so an unwrapped value
already yields three rects at two different ``top`` coordinates, and only overlap
tells a second line from a smaller sibling on the same one. That clustering suits
display numbers and nothing else -- ``.title`` sets ``line-height: 0.98``, where
consecutive line boxes overlap and every line merges into one.

``size`` is the computed ``--size``, and it is what the assertions read: a class is
only a proxy for a *matching rule*, and a rung renamed on one side of the split --
``value--zzz`` in the markup, or ``stat_grid.css``'s selector renamed under it --
leaves the class present, ``--size`` empty, ``min(var(--size), var(--fit))`` invalid,
and every figure sitting at body copy. ``rungs`` is kept as the diagnostic that tells
those two cases apart. ``container_type`` is the cell's own ``container-type``,
because "an ancestor matched" is not "the query container matched": a real but
non-query ancestor measures a box three times too wide and reports no overflow.
``body_px`` is the page's own body-copy size, the only honest thing to measure a
headline figure against; ``_chrome.css`` says 16.5px today."""


@dataclass(frozen=True, slots=True)
class DisplayNumber:
    """One rendered display number, and what it did to the cell that sizes it."""

    text: str
    lines: int
    rungs: tuple[str, ...]
    """The ``--size`` rung classes the body put on it, e.g. ``("value--xl",)``."""
    size: str
    """What those classes actually computed to, e.g. ``"116px"``. Empty means no rule
    matched, whatever the classes say."""
    fit: str
    """The inline ``--fit`` cap, verbatim, e.g. ``"8.77cqw"``."""
    font_px: float
    container_type: str
    """The cell's computed ``container-type``: ``"inline-size"`` if the value's
    ``cqw`` units really resolve against it, ``"normal"`` if they do not."""
    cell_overflow: float
    value_overflow: float


@dataclass(frozen=True, slots=True)
class DisplayNumberReport:
    """``DISPLAY_NUMBER_JS``'s measurements for one page."""

    value_count: int
    container_count: int
    body_px: float
    orphans: tuple[str, ...]
    values: tuple[DisplayNumber, ...]
    """Only the values that have their container as an ancestor; the rest are in
    ``orphans``, so this is shorter than ``value_count`` exactly when something is
    wrong."""


def read_display_numbers(report: Mapping[str, object]) -> DisplayNumberReport:
    """The browser's JSON, checked into types instead of indexed as ``object``."""
    return DisplayNumberReport(
        value_count=int(_number(report["value_count"])),
        container_count=int(_number(report["container_count"])),
        body_px=_number(report["body_px"]),
        orphans=tuple(_text(row) for row in _rows(report["orphans"])),
        values=tuple(
            _read_display_number(_fields(row)) for row in _rows(report["values"])
        ),
    )


def _read_display_number(row: Mapping[str, object]) -> DisplayNumber:
    return DisplayNumber(
        text=_text(row["text"]),
        lines=int(_number(row["lines"])),
        rungs=tuple(_text(rung) for rung in _rows(row["rungs"])),
        size=_text(row["size"]),
        fit=_text(row["fit"]),
        font_px=_number(row["font_px"]),
        container_type=_text(row["container_type"]),
        cell_overflow=_number(row["cell_overflow"]),
        value_overflow=_number(row["value_overflow"]),
    )


AT_FIT_FLOOR: Final = "3.00cqw"
"""``layout._MIN_FIT_CQW`` exactly as a template spells it, since ``_fit`` formats
the cap to two decimals.

A value pinned to the floor is one ``layout`` has stopped shrinking on purpose,
having decided wrapping is the better answer -- so it is outside what the fence
below can claim. Read off the DOM and compared as a string: recomputing ``_fit()``
in Python would only re-derive the number under test."""


def test_the_no_wrap_envelope_still_names_the_current_fit_floor() -> None:
    """A pure constant check, so it fails in microseconds and once -- not twelve times
    behind a browser launch."""
    assert AT_FIT_FLOOR == f"{_MIN_FIT_CQW:.2f}cqw", (
        f"layout._MIN_FIT_CQW is now {_MIN_FIT_CQW}, so AT_FIT_FLOOR no longer names "
        "the floor: the no-wrap fence would stop recognising the values layout has "
        "already given up on shrinking"
    )


@pytest.mark.parametrize("width_px", [1000, 640])
@IN_BOTH_THEMES
@BODIES
@BROWSER_LOOP
async def test_display_numbers_stay_on_one_line_inside_their_cell(
    chromium: Browser, template_id: str, theme: Theme, width_px: int
) -> None:
    """The body's headline numbers never wrap, and they shrink to fit their cell
    rather than overflowing it.

    Every body sizes its display number from the width of the cell it sits in --
    ``.row__head``, ``.chip``, ``.rank__figure`` -- so narrowing the page shrinks the
    type instead of breaking it. 1000px puts a ``stat_grid`` half-column at about
    410px. 640px is where the ``--fit`` cap starts carrying the page on its own:
    without it ``26-84 lb (12-38 kg)`` sets at 76px in a 269px column and breaks
    across three lines.
    """
    selectors = BODY_SELECTORS[template_id]
    composition = await compose_cell(
        template_id, theme, make_content(facts=NARROW_FACTS), width_px=width_px
    )

    async with laid_out(chromium, composition) as page:
        measured: dict[str, object] = await page.evaluate(
            DISPLAY_NUMBER_JS,
            {
                "value": selectors.value,
                "container": selectors.container,
                "scale_prefix": selectors.scale_prefix,
            },
        )
    report = read_display_numbers(measured)

    assert report.value_count == len(NARROW_FACTS), (
        f"{template_id}: {selectors.value!r} matched {report.value_count} elements, "
        f"expected one per fact ({len(NARROW_FACTS)}) -- at zero this cell measured "
        "nothing at all"
    )
    assert report.container_count == report.value_count, (
        f"{template_id}: {report.value_count} display numbers but "
        f"{report.container_count} {selectors.container!r} cells -- at zero the "
        "cell-overflow measurement is a constant zero, and below that count they are "
        "sharing a cell that sizes none of them individually"
    )
    assert not report.orphans, (
        f"{template_id}: display numbers with no {selectors.container!r} ancestor, so "
        f"nothing sizes them: {list(report.orphans)}"
    )

    at_floor = [value.text for value in report.values if value.fit == AT_FIT_FLOOR]
    assert not at_floor, (
        f"{template_id} at {width_px}px: {at_floor} sit at the {AT_FIT_FLOOR} fit "
        "floor, where layout calls wrapping the better answer, so they fall outside "
        "everything below. Either these fixtures grew past what this fence can claim "
        "or the floor moved -- fix whichever it was, do not let the loop skip them"
    )

    for value in report.values:
        assert value.size, (
            f"{template_id}: {value.text!r} computes no --size (classes "
            f"{list(value.rungs)}), so min(var(--size), var(--fit)) is invalid and the "
            f"figure fell back to body copy at {value.font_px}px against "
            f"{report.body_px}px prose -- it sits on one line inside its cell while "
            "reading as prose, passing every assertion below on a broken page"
        )
        assert value.container_type == "inline-size", (
            f"{template_id}: {selectors.container!r} computes "
            f"container-type: {value.container_type}, so it is an ordinary ancestor "
            "rather than the query container the value's cqw units resolve against -- "
            "the overflow below is measured on the wrong box"
        )
        assert value.cell_overflow <= 1, (
            f"{template_id}: {value.text!r} overflows its {selectors.container!r} by "
            f"{value.cell_overflow}px at a {width_px}px page"
        )
        assert value.value_overflow <= 1, (
            f"{template_id}: {value.text!r} overflows its own box by "
            f"{value.value_overflow}px"
        )
        assert value.lines == 1, (
            f"{template_id}: {value.text!r} wrapped onto {value.lines} lines at a "
            f"{width_px}px page; a headline figure is meant to shrink to fit, not break"
        )
