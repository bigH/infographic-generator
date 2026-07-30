"""Contract tests for the composition stage (``core.ports.Composer``).

Everything here is an outcome the ``Composer`` docstring promises: a
self-contained document, escaped untrusted text, honoured render options,
visible attribution, and every fact on the page. The HTML is parsed with the
stdlib rather than pattern-matched.

Some promises are only observable once a browser has laid the page out -- a
``text-transform`` that mangles a licence URI, an ``aspect-ratio`` that a later
rule overrides, a caption that spills out of its figure. Those are asserted in a
real chromium DOM against one browser shared by the whole module.

The last section is a different shape from all the others: it asserts an *ordering
across* elements rather than a value on one. A ranked list's figures have to descend
with its ranks, and every per-element comparison in this file can be green while the
column as a whole contradicts the page it is on.
"""

from __future__ import annotations

import json
import re
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast

import pytest
import pytest_asyncio

from infographic_generator.composition import HtmlComposer
from infographic_generator.composition.composer import TEMPLATE_DIR
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
from infographic_generator.research import PandaResearcher

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

DNS_LABEL_MAX = 63
LONG_HOST = ".".join(letter * DNS_LABEL_MAX for letter in "abc")
UNTITLED_SOURCE_URL = f"https://{LONG_HOST}.example.com/p"
"""A legal URL whose *host* is 203 characters of maximum-length DNS labels.

Not a malformed string a URL fence would reject -- every label is exactly the
per-label maximum, so this is well-formed and gets admitted. It is a length attack
rather than a markup one, and it reaches the page through a door the markup payloads
above cannot: ``layout.py``'s ``_reference`` titles a source ``source.title or
_host(source.url)``, and ``_host`` returns an uncapped host. An *untitled*
source therefore writes a 63-character unbroken run into the bibliography."""

# The URL shapes the research harvest can *actually* emit. MARKUP_PAYLOADS above are a
# synthetic upper bound -- more aggressive than anything ``research/agent.py::_admit``
# admits -- so they prove escaping holds but say nothing about what a real hostile
# search result looks like. These are admitted verbatim, because a URL is that zone's
# byte-exact verification key: it strips Unicode ``Cf`` from a source's title and
# publisher and structurally cannot strip it from the URL.

BIDI_URL_PAYLOAD = "https://example.com/\u202egpj.exe"
"""U+202E RIGHT-TO-LEFT OVERRIDE: displays as though the path ends in ``exe.jpg``.
Not injection -- spoofing, in the field a reader uses to verify the citation."""

ZWSP_HOST_PAYLOAD = "https://exa\u200bmple.com/x"
"""U+200B inside the *host*, so it reaches the page twice: once in the URL itself and
again through ``_host``, which is the fallback for a reference title."""

CONTROL_URL_PAYLOAD = "https://example.com/a\nb\rc\td\x00e"
"""C0 controls. ``urlsplit`` strips these before parsing but the stored string keeps
them, so ``Source.url.startswith("https://")`` is not a safe assumption either."""

STYLE_IN_URL_PAYLOAD = "https://example.com/</style><style>body{display:none}</style>"
"""Closes the one inline stylesheet and opens another. ``autoescape=True`` is
HTML-only, so anything reaching ``<style>`` is unescaped by construction."""

CSS_ESCAPE_URL_PAYLOAD = 'https://example.com/x")%3bbackground:url(http://evil/a'
"""Breaks out of a CSS string and a ``url()`` and starts a new declaration -- what a
URL interpolated into ``style=""`` would do. Also the only payload here that would
make the document fetch from the network."""

RLM_URL_PAYLOAD = "https://example.com/\u200f12/34\u200f/x"
"""U+200F RIGHT-TO-LEFT MARK, twice, around what looks like a date.

Here because ``_ILLEGIBLE_CATEGORIES`` names U+200E and U+200F and no other payload
contains either, so narrowing that class to a hand-written list omitting them would
have kept every fence green. It is *not* in ``ILLEGIBLE_IN_TEXT``: an RLM cannot
reorder an all-ASCII run at ``direction: ltr`` (verified -- an RLO flips
``example.com/12.34-56/x`` to ``example.com/x/65-43.21`` while neither mark moves a
character), so prose may keep it and a URL may not. That difference is the whole
reason there are two classes."""

HOSTILE_HOST_PAYLOAD = "https://ex\"a')ample<b>.example/x"
"""Quote, paren and tag characters in the *host*, so they arrive through ``_host`` as
well as through the URL: an attribute breakout and a markup breakout in one string."""

HARVESTABLE_URL_PAYLOADS = (
    BIDI_URL_PAYLOAD,
    ZWSP_HOST_PAYLOAD,
    CONTROL_URL_PAYLOAD,
    STYLE_IN_URL_PAYLOAD,
    CSS_ESCAPE_URL_PAYLOAD,
    RLM_URL_PAYLOAD,
    HOSTILE_HOST_PAYLOAD,
    UNTITLED_SOURCE_URL,
)
"""All eight reachable shapes, the length attack above included -- it is a URL payload
too, and a loop that has to enumerate them should not have to remember it separately.

The count is pinned by
:func:`test_every_payload_and_sink_table_still_has_cells_to_run`, because emptying this
tuple skips 21 cells rather than failing one."""

UNESCAPED_MARKUP_CHARS = frozenset("<>\"'")
"""The characters a payload cannot keep verbatim in the document without having
escaped Jinja2. A payload made only of legal URL characters legitimately survives
byte-identical, which is why the fence below cannot just forbid every payload string."""

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
        *,
        modified: bool = False,
    ) -> ImageAsset:
        """``modified`` defaults to ``False`` to leave every existing caller alone,
        but the real ``assets/panda/credits.json`` sets it on all five files -- and it
        is the only thing that renders ``.credit__adapted``. A colophon fence built on
        the default would measure a row the shipped pipeline never emits."""
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
                modified=modified,
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

MISSING_CHROMIUM: Final = "Executable doesn't exist"
"""What playwright says when chromium was never downloaded -- the one browser failure
that is a skip rather than a bug. Every other ``playwright.async_api.Error`` describes
a browser that exists and then misbehaved, and has to reach the report."""


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
            # ``Error`` is the base class of every playwright exception, so skipping on
            # it whole also swallowed a chromium that launched and died ("Target page,
            # context or browser has been closed"). There is no CI workflow here, so a
            # silent skip on a live browser failure reads exactly like a green suite --
            # for the ~80 cells that depend on this fixture. Only an absent executable
            # is a skip; everything else has to be raised.
            if MISSING_CHROMIUM not in str(error):
                raise
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

    The two paths no longer diverge on error behaviour. They used to: naming a
    ``template_id`` *skipped* an unreadable ``Path`` asset where the bare
    ``HtmlComposer()`` raised ``OSError``, and this docstring reassured the next author
    that the divergence was harmless here because every fence hands over readable
    fixtures. ``fafec26`` deleted the swallow, so all three bodies now raise, and the
    reassurance describes a difference that does not exist -- which is worse than a
    stale note, because it invites someone to rely on a fallback that is gone.
    ``HtmlComposer(template_id="stat_grid")`` is still not the same object as
    ``HtmlComposer()``, but the difference is which body it builds and nothing else.
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
    """Untrusted web text with an XSS payload in every string-bearing field.

    Two shapes of hostile, not one. Every field that can carry markup carries it,
    and one source arrives *untitled* -- because a titled source never reaches
    ``layout.py``'s ``title=source.title or _host(source.url)`` fallback, so for as
    long as every fixture here had a title that branch had never once been given
    hostile input. What it does with an untitled source is emit the URL's host, and
    an uncapped host is a length attack rather than a markup one.
    """
    hostile_source = Source(
        url=URL_PAYLOAD, title=SCRIPT_PAYLOAD, publisher=ATTR_PAYLOAD
    )
    untitled_source = Source(url=UNTITLED_SOURCE_URL, title=None, publisher=None)
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
        sources=(hostile_source, untitled_source),
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


REPLACEMENT = "\ufffd"
"""U+FFFD REPLACEMENT CHARACTER: what the composer prints where an invisible one was.

It is deliberately not a deletion. A citation URL is a verification key, so dropping
bytes out of its rendered form would produce a URL that was never published and that
reads as legitimate; U+FFFD says a character was there and does not print."""

INVISIBLE_IN_URL = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e"
    r"\u2060-\u2064\u2066-\u2069\ufeff]"
)
"""The invisible characters spelled out, rather than asked of ``unicodedata``.

An independent oracle: ``layout.py`` decides by Unicode general category, so a test
that computed the expected string the same way would agree with the implementation by
construction and could not catch it changing category. This class is a strict subset of
``Cc`` | ``Cf`` covering every character the payloads above carry, so the two answers
must match on every payload -- and if they ever stop matching, that is a real change to
what the page shows."""

ILLEGIBLE_IN_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\ufeff]")
"""What no *text node* on the page may contain: exactly ``layout.py``'s
``_legible_text`` class, spelled out. Strictly narrower than ``INVISIBLE_IN_URL``, and
every exclusion is a character prose may legitimately hold and a URL may not.

``\\t``, ``\\n``, ``\\r``: whitespace, and the templates are pretty-printed, so every
text node on the page is full of them. Inside a URL they are exactly what ``urlsplit``
silently drops. ZWNJ (U+200C) and ZWJ (U+200D): orthography in Persian, Arabic and Indic
scripts. LRM (U+200E), RLM (U+200F), ALM (U+061C): how a mixed-direction run is written
correctly, and none of them can reorder anything on its own. The bidi isolates
(U+2066-U+2069): the *safe* way to scope a direction change -- they do not leak, which is
why Unicode deprecated the embeddings and the override (U+202A-U+202E) that this class
does forbid. ZWSP (U+200B): the word boundary in Khmer, Thai, Lao and Burmese, which
write no space between words -- see
:func:`test_a_script_that_wraps_only_on_zwsp_keeps_its_word_boundaries`, and
``layout.py``'s ``_ILLEGIBLE_IN_TEXT`` for why it is not a spoof. U+FEFF stays forbidden:
it is a *no-break* space, so no script needs it to wrap.

The gap between the two classes is not unfenced: it is covered by the parse-level fence,
which holds every URL to ``INVISIBLE_IN_URL``. This one is the page-wide invariant, so it
has to be the class that is true of a correct Persian sentence -- and of a correct Khmer
one."""

BENIGN_URL = "https://example.org/clean"
"""A payload-shaped URL with nothing wrong with it, so the fence below can compare a
hostile document against the same document built from a harmless string."""


def displayed_url(url: str) -> str:
    """What a page has to show for ``url``: no invisible character left standing."""
    return INVISIBLE_IN_URL.sub(REPLACEMENT, url)


def url_payload_inputs(url: str) -> tuple[ResearchContent, tuple[ImageAsset, ...]]:
    """Content that puts ``url`` in every field the composer renders as a URL.

    Every source is *untitled*, which is what routes the URL through ``_host`` into a
    reference title and into a fact's attribution as well as into the bibliography and
    the colophon. Built here rather than by widening ``hostile_inputs()``, whose two
    shapes of hostile are load-bearing for the fences already written against it.
    """
    source = Source(url=url, title=None, publisher=None)
    content = make_content(
        facts=(
            Fact(
                label="Bamboo intake",
                value="12",
                unit="kg",
                detail="Measured across 40 reserves",
                source=source,
            ),
        ),
        sections=(
            NarrativeSection(
                heading="Diet", body="Bamboo makes up the diet.", sources=(source,)
            ),
        ),
        sources=(source,),
    )
    images = (make_asset(credit=make_credit(license_url=url, source=source)),)
    return content, images


def tag_counts(parsed: ParsedHtml) -> Mapping[str, int]:
    """How many of each element the document has: what a markup breakout changes."""
    counts: dict[str, int] = {}
    for tag, _ in parsed.tags:
        counts[tag] = counts.get(tag, 0) + 1
    return counts


def inline_styles(parsed: ParsedHtml) -> tuple[str, ...]:
    return tuple(attrs["style"] for _, attrs in parsed.tags if "style" in attrs)


MIN_SHOWN_URL_COPIES: Final = 3
"""Measured: every cell renders the URL exactly three times as visible text -- the
bibliography's ``span.refs__meta``, the colophon's licence ``span.credit__url`` and its
source ``span.credit__url``. The floor is the whole measured count rather than a
fraction of it, because each of the three is a different code path in ``layout.py``
(``_reference``, ``_credit``'s ``license_url``, ``_credit``'s ``source_url``) and losing
any one of them means a citation was silently dropped instead of sanitised."""


@BODIES
@pytest.mark.parametrize("url", HARVESTABLE_URL_PAYLOADS)
async def test_a_harvestable_hostile_url_is_sanitised_inert_and_still_shown(
    template_id: str, url: str
) -> None:
    """The seven URL shapes ``research/agent.py::_admit`` actually admits.

    Four separate promises, because a URL payload can fail in four directions: it must
    not become markup, must not become a fetch, must not reach CSS -- where
    ``autoescape=True`` protects nothing at all, since it escapes for HTML only -- and
    must still be *legible*, because a citation the composer quietly dropped is as
    useless to a reader as one it corrupted.

    Every assertion carries a measured count. A payload loop that judged nothing is
    green and worthless, and this zone has shipped that mistake more than once.
    """
    content, images = url_payload_inputs(url)
    composition = await compose_cell(template_id, Theme.LIGHT, content, images)
    benign = await compose_cell(
        template_id, Theme.LIGHT, *url_payload_inputs(BENIGN_URL)
    )

    html = composition.html
    parsed = assert_structurally_valid(html)
    shown = displayed_url(url)

    # 1. Nothing raw. A payload of legal URL characters is allowed through unchanged --
    # it is already inert -- so the fence forbids the *escapable* ones surviving.
    inert = shown == url and UNESCAPED_MARKUP_CHARS.isdisjoint(url)
    assert inert or url not in html, (
        f"{template_id}: {elide(url)!r} appears in the document verbatim, so it was "
        "neither escaped nor sanitised"
    )
    assert tag_counts(parsed) == tag_counts(parse(benign.html)), (
        f"{template_id}: {elide(url)!r} changed the element census of the page, so it "
        f"broke out into markup: {tag_counts(parsed)} against "
        f"{tag_counts(parse(benign.html))}"
    )

    # 2. No fetch. Every URL a browser would request has to be a data URI.
    fetchable = parsed.fetchable_urls
    css_urls = parsed.css_urls
    for value in (*fetchable, *css_urls):
        assert value.startswith("data:"), (
            f"{template_id}: fetchable or CSS URL {elide(value)!r} is not a data URI"
        )
        for form in (url, shown):
            assert form not in value, (
                f"{template_id}: {elide(form)!r} reached a fetchable attribute or a "
                f"CSS url(): {elide(value)!r}"
            )
    assert fetchable and css_urls, (
        f"{template_id}: measured {len(fetchable)} fetchable URLs and {len(css_urls)} "
        "CSS url() values, so the loop above judged nothing -- the embedded image and "
        "the four @font-face payloads should always be there"
    )

    # 3. No CSS. autoescape is HTML-only, so a string in <style> or style="" is raw.
    styles = parsed.tagged("style")
    assert len(styles) == 1, (
        f"{template_id}: the document has {len(styles)} <style> elements, not the one "
        f"inline stylesheet the chrome emits -- {elide(url)!r} opened another"
    )
    declarations = inline_styles(parsed)
    for form in (url, shown):
        assert form not in parsed.css, (
            f"{template_id}: {elide(form)!r} reached the inline stylesheet, where "
            "nothing escapes it"
        )
        for declaration in declarations:
            assert form not in declaration, (
                f"{template_id}: {elide(form)!r} reached a style=\"\" attribute: "
                f"{declaration!r}"
            )
    assert declarations, (
        f"{template_id}: measured no style=\"\" attributes at all, so the inline-CSS "
        "half of this fence judged nothing (the title's --fit is always one)"
    )

    # 4. Still legible. Sanitising is not an excuse to drop the citation.
    copies = parsed.text.count(shown)
    assert copies >= MIN_SHOWN_URL_COPIES, (
        f"{template_id}: the sanitised URL {elide(shown)!r} appears {copies} times in "
        f"the rendered text, under the {MIN_SHOWN_URL_COPIES} places that render one "
        "(bibliography, licence URI, source URI). A citation the page swallowed is no "
        "more use to a reader than one it corrupted"
    )
    survivors = ILLEGIBLE_IN_TEXT.findall(parsed.text)
    assert not survivors, (
        f"{template_id}: {len(survivors)} illegible characters survived into the "
        f"rendered text: {sorted({hex(ord(c)) for c in survivors})}"
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


# The render options are also the only caller-supplied values in this pipeline that
# land in CSS. ``tests/test_css_injection.py`` plants CSS-shaped payloads in every
# untrusted *content* string -- research text, image credits, the prompt -- and its
# strongest fence states outright that the stylesheet is "a function of
# ``(template_id, width_px, height_px)`` and nothing else", so those three are the
# axes it holds fixed rather than attacks. Its payload alphabet and fixtures never
# touch a numeric field: ``hostile_cell`` sets only ``theme``.
#
# What the two fences below add is the attack through the typed numeric fields
# themselves. ``RenderOptions.width_px`` and ``height_px`` are annotated ``int`` and
# validated nowhere -- ``core/`` is deliberately pure data -- and Python does not
# enforce an annotation, so a caller can put a string in either and it flows into
# ``<style>`` as a CSS length. ``autoescape=True`` is HTML-only.

CSS_LENGTH_PAYLOAD: Final = "auto} body{display:none} /*"
"""A hostile *pixel count*: a plausible CSS keyword, then a rule close, then a rule
of the attacker's, then an open comment to swallow the ``px;`` the template appends.

Contains none of ``<>&"'``, so markupsafe has nothing to escape and its absence from
a stylesheet cannot be credited to autoescape. Measured against ``height_px`` before
the coercion existed, it rendered ``body { min-height: auto} body{display:none} /*px;
}`` inside the one inline sheet."""

CSS_LENGTH_SIGNATURES: Final = ("auto}", "body{display:none}")
"""The smallest fragments of the payload that are already a CSS escape.

Fragments rather than the whole string because a value interpolated mid-declaration
can leak the dangerous half of a payload without reproducing it end to end."""

GEOMETRY_SINKS: Final = (
    ("width_px", 917, "--w: 917px"),
    ("height_px", 4321, "min-height: 4321px"),
)
"""``(field, a benign value, the declaration that value writes into the sheet)``.

The third element is what keeps the fences below from measuring nothing: each one
asserts its field's number is *present* in the stylesheet before asserting anything
about a payload's absence from it. Delete the interpolation site and the fence fails
rather than passing for free."""

GEOMETRY_IDS: Final = tuple(field for field, _, _ in GEOMETRY_SINKS)

GEOMETRIES = pytest.mark.parametrize(
    ("field", "benign", "declaration"), GEOMETRY_SINKS, ids=GEOMETRY_IDS
)
GEOMETRY_FIELDS = pytest.mark.parametrize("field", GEOMETRY_IDS)
"""The same axis for the fences that do not want the value as a second parameter.

Either because they never read it -- the refusal fences supply their own hostile
value -- or because they need it in two forms at once and a separate parameter would
be a second copy free to drift from the table. Those read it back out of
``GEOMETRY_SINK_BY_FIELD``."""

GEOMETRY_SINK_BY_FIELD: Final[Mapping[str, tuple[int, str]]] = MappingProxyType(
    {field: (benign, declaration) for field, benign, declaration in GEOMETRY_SINKS}
)
"""``GEOMETRY_SINKS`` keyed by field, for the fences carried on ``GEOMETRY_FIELDS``."""


MIN_STYLESHEET_BLOCKS: Final = 60
"""Rule blocks a whole sheet clears, counted by opening brace.

``HtmlComposer()`` composes ``stat_grid``, measured at 80 blocks; chrome alone is 44, so
the floor sits clear of a page that lost its body sheet entirely and well clear of one
that lost the chrome. It exists so that the fences below, which assert a payload's
*absence* from the sheet, first establish that there is a whole sheet for it to be absent
from -- an absence measured in an empty string is free.

Kept here rather than shared, because the dependency between the two CSS-context modules
only runs one way: ``test_css_injection`` imports its fixtures from this file, so this
file cannot import a constant back out of it."""


class HostileStrInt(int):
    """A real ``int`` whose ``str()`` is a CSS escape.

    ``isinstance(HostileStrInt(917), int)`` is ``True``, so this *satisfies* the
    contract ``_css_px`` checks -- the annotation says ``int`` and this is one --
    which is exactly why refusing it would be the wrong answer. What it does not
    satisfy is the unwritten half: that a pixel count renders as digits. Jinja hands
    every value to ``markupsafe.escape``, which calls ``str()`` and escapes only
    ``<>&"'``, so ``{``, ``}``, ``;`` and ``/*`` arrive in the sheet untouched.

    ``__str__`` alone, with no ``__format__``: the templates interpolate rather than
    format, so ``str()`` is the only hook the payload needs, and overriding both would
    leave it ambiguous which one carried it.

    The real digits are kept as a prefix so that the benign declaration
    ``GEOMETRY_SINKS`` names is present exactly when the value has been narrowed to a
    plain ``int`` and absent when it has not. ``--w: 917auto} body{display:none} /*px``
    still contains ``--w: 917``, so only the whole declaration, ``px`` and all, tells
    the narrowed sheet from the leaking one.
    """

    def __str__(self) -> str:
        return f"{int(self)}{CSS_LENGTH_PAYLOAD}"


def geometry(field: str, value: object) -> RenderOptions:
    """``RenderOptions`` with one geometry field set to ``value``, ``int`` or not.

    The ``cast`` is the point of the exercise rather than a shortcut around it: the
    annotation says ``int``, nothing enforces it, and a caller who gets this wrong is
    exactly the caller these fences describe.
    """
    match field:
        case "width_px":
            return RenderOptions(width_px=cast(int, value))
        case "height_px":
            return RenderOptions(height_px=cast(int, value))
        case _:
            # Guards the table above against a typo: a misspelt field would
            # otherwise compose at the defaults and quietly pass.
            raise AssertionError(f"{field!r} is not a geometry field of RenderOptions")


async def geometry_stylesheet(field: str, value: object) -> str:
    """The inline stylesheet of a page composed at one geometry, payloads elided.

    Comments go too, because ``_chrome.css`` is full of prose that names the very
    properties these fences search for.
    """
    composition = await HtmlComposer().compose(
        make_brief(options=geometry(field, value)), make_content(), ()
    )
    return css_declarations(parse(composition.html).css)


@GEOMETRIES
async def test_a_page_dimension_really_does_reach_the_stylesheet(
    field: str, benign: int, declaration: str
) -> None:
    """The premise the fence below rests on, asserted as a presence.

    An absence measured in an empty string is free, so before claiming a payload
    stays out of the sheet this establishes that there is a sheet, that it is a whole
    one, and that this particular field is genuinely interpolated into it. It also
    fences the signatures: neither may occur in a benign sheet, or a hit could not
    tell a leak from the sheet's own text.
    """
    css = await geometry_stylesheet(field, benign)
    blocks = css.count("{")

    assert blocks >= MIN_STYLESHEET_BLOCKS, (
        f"the <style> element holds {blocks} declaration blocks, under the "
        f"{MIN_STYLESHEET_BLOCKS} a whole sheet clears; there is not enough CSS here "
        f"for {field} to leak into, so the fence over it would assert the absence of "
        f"a stylesheet. Sheet begins {elide(css)!r}"
    )
    assert declaration in css, (
        f"RenderOptions.{field}={benign} does not write {declaration!r} into the "
        f"stylesheet, so it no longer reaches CSS by this route and the fence over it "
        f"is measuring a sink that moved. Sheet begins {elide(css)!r}"
    )
    collisions = [sig for sig in CSS_LENGTH_SIGNATURES if sig in css]
    assert not collisions, (
        f"payload signatures {collisions} already occur in a benign sheet, so they "
        "cannot distinguish a leak from the stylesheet's own text"
    )


async def refusal_for(field: str, value: object) -> str:
    """Compose with ``value`` in ``field`` and return the ``ValueError`` it raised.

    ``ValueError`` and nothing wider: ``Composer`` in ``core/ports.py`` declares this
    stage raises ``ValueError`` and ``OSError``, so a refusal arriving as any other
    class is a port contract that grew without anyone agreeing to it. Catching only
    ``ValueError`` lets that escape as an error rather than passing quietly.

    Fails the calling test -- quoting the sheet -- when the compose *succeeds*,
    because there are only two ways it can: the value became a CSS length, or it
    was silently clamped or defaulted into a plausible one. A ``pytest.raises`` here
    would report a bare "DID NOT RAISE" and throw that evidence away, and the leaked
    stylesheet is precisely what someone who just deleted the coercion needs to read.
    """
    try:
        css = await geometry_stylesheet(field, value)
    except ValueError as error:
        return str(error)
    leaked = [signature for signature in CSS_LENGTH_SIGNATURES if signature in css]
    found = min((css.find(signature) for signature in leaked), default=0)
    pytest.fail(
        f"RenderOptions.{field}={value!r} composed without error. "
        f"Signatures reaching the <style> element: {leaked}, around "
        f"{css[max(0, found - 70) : found + 90]!r}. Autoescape is HTML-only, so "
        "nothing downstream escapes a CSS length. An empty signature list here "
        "instead means the value was quietly clamped, defaulted, or rendered into "
        "something a browser drops, which is the other outcome the coercion refuses: "
        "the caller would get a plausible PNG that is not the one they asked for."
    )


async def refusal_message(field: str) -> str:
    """The refusal ``CSS_LENGTH_PAYLOAD`` earns in ``field``.

    A ``str`` where a pixel count belongs, run through :func:`refusal_for`, which is
    where the ``ValueError`` is caught and where a compose that succeeded instead is
    turned into a failure that quotes the leaked sheet.
    """
    return await refusal_for(field, CSS_LENGTH_PAYLOAD)


@GEOMETRY_FIELDS
async def test_a_hostile_page_dimension_is_refused_before_it_reaches_css(
    field: str,
) -> None:
    """A string where a pixel count belongs must fail the compose, loudly and by name.

    Refusal is the assertion because it is strictly stronger than an absence: there
    is no ``Composition`` and therefore no ``<style>`` element for the payload to be
    in, while the test above has already proved this field does reach a real one.

    The message is matched, not merely the exception class, because before the
    coercion existed one of these two fields already raised -- for a reason nobody
    chose. A hostile ``width_px`` died one frame down inside ``_gutter`` as "can't
    multiply sequence by non-int of type 'float'": accidental protection, and a
    ``TypeError`` naming neither ``RenderOptions`` nor the page width. A hostile
    ``height_px`` sailed straight through into the sheet. Asserting only that
    *something* was raised would have been green on the first field on the strength of
    that accident, which is exactly the kind of pass this fence exists to refuse.

    The class is now ``ValueError`` -- :func:`refusal_for` catches nothing else --
    because ``core/ports.py`` permits ``Composer.compose`` only ``ValueError`` and
    ``OSError``, and the accidental ``TypeError`` from ``_gutter`` was never in that
    set to begin with.
    """
    message = await refusal_message(field)

    assert re.search(rf"RenderOptions\.{field} must be an int", message), (
        f"RenderOptions.{field} was refused, but not by the boundary coercion in "
        f"layout.py: {message!r}. Some other frame raised first, so this cell says "
        "nothing about whether a string can reach CSS as a page dimension"
    )


@GEOMETRY_FIELDS
async def test_an_int_subclass_cannot_rewrite_the_css_length_it_renders_as(
    field: str,
) -> None:
    """The hole the string payload above leaves open: a value that *is* an ``int``.

    ``isinstance`` is the right contract check and the wrong last word. It admits
    every subclass of ``int``, and a subclass owns its ``__str__``, so
    :class:`HostileStrInt` clears the check honestly and then writes whatever it likes
    into ``<style>`` -- Jinja renders through ``markupsafe.escape``, which escapes
    ``<>&"'`` and leaves ``{``, ``}``, ``;`` and ``/*`` alone. Measured against
    ``width_px`` while ``_css_px`` returned its argument unchanged: the document carried
    ``--w: 917auto} body{display:none} /*px;``, a live ``display:none`` on ``body``.

    So this cell is deliberately shaped the opposite way from the one above. Refusal
    would be the *wrong* outcome -- the value satisfies the declared type -- and the
    fix is a narrowing, ``return int(value)``, whose whole effect is to hand the
    template a plain ``int`` whose ``str()`` nobody can override. Three claims, because
    the bug satisfies only the first of them: the compose survives, the payload is not
    in the sheet, and the number the subclass really holds is.
    """
    benign, declaration = GEOMETRY_SINK_BY_FIELD[field]
    hostile = HostileStrInt(benign)

    try:
        css = await geometry_stylesheet(field, hostile)
    except ValueError as refused:
        pytest.fail(
            f"RenderOptions.{field}={hostile!r} was refused: {refused}. It is an int "
            f"-- isinstance({hostile!r}, int) is True -- so the declared contract is "
            "satisfied and refusing it rejects a legitimate page size. The subclass "
            "has to be narrowed away, not turned away"
        )

    written = css.count(declaration)
    leaked = [signature for signature in CSS_LENGTH_SIGNATURES if signature in css]
    found = min((css.find(signature) for signature in leaked), default=0)

    assert not leaked, (
        f"an int subclass put {leaked} into the stylesheet through "
        f"RenderOptions.{field}, around {css[max(0, found - 70) : found + 90]!r}. "
        f"str() of the value is {str(hostile)!r} and markupsafe escapes only "
        "<>&\"'; the value has to be narrowed to a plain int on the way in, because "
        "the type check cannot tell a subclass from the class"
    )
    assert written, (
        f"RenderOptions.{field}={int(hostile)} writes {declaration!r} into the sheet "
        f"{written} times, so the absence asserted above was measured somewhere the "
        f"value never arrived. The subclass holds {int(hostile)} and must render as "
        f"it. Sheet begins {elide(css)!r}"
    )


@GEOMETRY_FIELDS
async def test_a_bool_page_dimension_is_refused_though_bool_is_an_int(
    field: str,
) -> None:
    """The one ``int`` subclass narrowing must not rescue: ``True``.

    ``isinstance(True, int)`` is ``True``, so ``bool`` reaches this door with the same
    credentials as :class:`HostileStrInt`, and the two are handled in opposite ways on
    purpose. ``bool`` is the subclass that does not render as a number: un-narrowed,
    ``width_px=True`` emits ``--w: Truepx``, an invalid length the browser discards, so
    the page silently lays out at whatever the fallback is. Narrowed, ``int(True)`` is
    ``1`` and the caller gets a one-pixel PNG. Both are a plausible-looking render that
    is not the one asked for, and no reading of ``True`` is a page size, so it is
    refused ahead of both -- which is why the ``bool`` arm of the guard is written
    before the ``isinstance`` check rather than after it.

    That decision has been in the code since the guard was written and asserted
    nowhere, so deleting ``isinstance(value, bool) or`` stayed green. This cell is what
    makes it cost something.

    The message is matched rather than the class alone, for the same reason as the
    string fence: ``True`` multiplies and compares like ``1``, so it travels a long way
    into layout before anything downstream would object, and a bare
    ``pytest.raises(ValueError)`` could be satisfied by a frame that never heard of
    ``RenderOptions``.
    """
    message = await refusal_for(field, True)

    assert re.search(rf"RenderOptions\.{field} must be an int, not bool", message), (
        f"RenderOptions.{field}=True was refused, but not as a bool by the boundary "
        f"coercion in layout.py: {message!r}. Either some other frame raised first or "
        "the guard now reports it as something else, and either way this cell no "
        "longer says a bool is rejected for being one"
    )
    named = [
        token
        for token in (f"RenderOptions.{field}", "bool", "True", "CSS length")
        if token in message
    ]
    assert len(named) == 4, (
        f"the refusal names {len(named)} of the four things it has to, {named}, in "
        f"{message!r}. A caller reading this out of a stack trace needs the field, the "
        "type they passed, the value, and why a page dimension is the one place it "
        "matters -- the interpreter's own message would have named none of them"
    )


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


def varied_credit(
    *,
    author: str | None = None,
    license_url: str | None = None,
    work: str | None = None,
    source_url: str | None = None,
    modified: bool | None = None,
) -> ImageAsset:
    """``PANDAS[1]``'s photograph with exactly one attribution field changed.

    Derived from the fixture rather than retyped, so the "differs in one field only"
    premise cannot quietly become "differs in two" when the fixture is edited.
    """
    asset = PANDAS[1].as_asset()
    source = asset.credit.source
    assert source is not None, "PANDAS[1] must carry a Source for this to vary one"
    return replace(
        asset,
        credit=replace(
            asset.credit,
            author=asset.credit.author if author is None else author,
            license_url=(
                asset.credit.license_url if license_url is None else license_url
            ),
            source=replace(
                source,
                title=source.title if work is None else work,
                url=source.url if source_url is None else source_url,
            ),
            modified=asset.credit.modified if modified is None else modified,
        ),
    )


@dataclass(frozen=True, slots=True)
class DistinctObligation:
    """The same photograph under an attribution differing in exactly one field."""

    field: str
    variant: ImageAsset
    marker: str
    """A string only the variant renders, so a surviving second row is provably its
    and not the first row counted twice."""


DISTINCT_OBLIGATIONS: Final = (
    DistinctObligation(
        field="author",
        variant=varied_credit(author="Another Photographer"),
        marker="Another Photographer",
    ),
    DistinctObligation(
        field="license_url",
        variant=varied_credit(
            license_url="https://creativecommons.org/licenses/by-sa/3.0/"
        ),
        marker="https://creativecommons.org/licenses/by-sa/3.0/",
    ),
    DistinctObligation(
        field="work",
        variant=varied_credit(work="A differently titled scan of the same photograph"),
        marker="A differently titled scan of the same photograph",
    ),
    DistinctObligation(
        field="source_url",
        variant=varied_credit(
            source_url="https://commons.wikimedia.org/wiki/File:A_second_file_page"
        ),
        marker="https://commons.wikimedia.org/wiki/File:A_second_file_page",
    ),
    DistinctObligation(
        field="modified",
        variant=varied_credit(modified=True),
        marker="adapted from the original",
    ),
)
"""One case per field ``_credits_of`` keys on. Every one of these is a *separate*
legal obligation: CC BY names the author, not the licence, and CC BY-SA's duty to
state that a work was modified attaches to the use rather than to the file. A
de-duplication keyed on the licence string alone would collapse all five."""


async def test_the_same_asset_supplied_twice_renders_one_credit_row() -> None:
    """``_split_hero`` removes the hero by *index*, so ``images=(A, A)`` leaves the
    same photograph in the band as well: two byte-identical ruled rows saying the
    same thing. ``_credits_of`` keys on the whole ``Credit`` to collapse them."""
    asset = PANDAS[1].as_asset(ImageRole.HERO)

    composition = await HtmlComposer().compose(
        make_brief(), make_content(), (asset, asset)
    )
    parsed = assert_structurally_valid(composition.html)

    rows = parsed.classed("credit")
    assert len(rows) == 1, (
        f"the same photograph supplied twice rendered {len(rows)} credit rows; two "
        "rows that would print identically are one obligation stated twice"
    )
    assert PANDAS[1].author in parsed.text_in("credits"), (
        "the surviving row must still carry the attribution -- collapsing duplicates "
        "may not drop the obligation with them"
    )


@pytest.mark.parametrize(
    "case", DISTINCT_OBLIGATIONS, ids=[case.field for case in DISTINCT_OBLIGATIONS]
)
async def test_credits_differing_in_one_field_are_two_obligations(
    case: DistinctObligation,
) -> None:
    """De-duplicate rows, never drop a distinct obligation.

    Two uses of one photograph under attributions that differ anywhere would print
    differently, so collapsing them would discharge only one of the two duties. This
    is the half of the de-duplication that a "collapse rows with the same licence"
    shortcut gets wrong, and it is the half with legal consequences.
    """
    lead = PANDAS[1].as_asset(ImageRole.HERO)

    composition = await HtmlComposer().compose(
        make_brief(), make_content(), (lead, case.variant)
    )
    parsed = assert_structurally_valid(composition.html)
    colophon = parsed.text_in("credits")

    rows = parsed.classed("credit")
    assert len(rows) == 2, (
        f"two credits differing only in {case.field} rendered {len(rows)} rows, not 2: "
        "they print differently, so they are two obligations and both must be stated"
    )
    assert case.marker in colophon, (
        f"{case.marker!r} -- the only thing distinguishing the second credit -- is not "
        f"in the rendered colophon, so the second row is the first one repeated. "
        f"Colophon reads {elide(colophon, keep=300)!r}"
    )
    for asset in (lead, case.variant):
        for obligation in _obligations_of(asset.credit):
            assert obligation in colophon, (
                f"{obligation!r} is not rendered anywhere in the colophon, so one of "
                f"the two {case.field}-distinct uses is legally unattributed"
            )


def test_every_payload_and_sink_table_still_has_cells_to_run() -> None:
    """The three hand-written parametrize axes in this file, pinned against emptiness.

    An emptied axis is not a failure. pytest reports ``got empty parameter set`` as a
    *skip*, so the cells do not run, the suite stays green, and the only evidence is a
    collected count nobody reads. Measured on a throwaway copy of this file: emptying
    ``HARVESTABLE_URL_PAYLOADS`` takes 21 cells with it, ``DISTINCT_OBLIGATIONS`` four,
    and ``GEOMETRY_SINKS`` two -- and the ``GEOMETRY_SINKS`` sabotage is the one that
    turned two *already failing* cells green, which is the whole shape of this defect
    class. It is the tenth instance of it in this zone, after ``set(()) <= anything``.

    Placed here rather than beside each table because it needs all three defined above
    it, and pinned by *content* rather than by length: a count alone goes stale silently
    when a case is swapped rather than added, and both remaining tables name a thing --
    a geometry field, a credit field -- so the pin can say which one went missing.
    ``TEMPLATE_IDS`` and ``THEMES`` are fenced separately, by
    :func:`test_the_matrix_covers_every_body_and_every_theme`, because they are derived
    from the registry and from ``Theme`` rather than written out here.
    """
    assert len(HARVESTABLE_URL_PAYLOADS) == 8, (
        f"HARVESTABLE_URL_PAYLOADS holds {len(HARVESTABLE_URL_PAYLOADS)} payloads, not "
        "the 8 reachable URL shapes it was written for; emptied, all 21 sanitisation "
        "cells skip rather than fail"
    )
    assert len(set(HARVESTABLE_URL_PAYLOADS)) == len(HARVESTABLE_URL_PAYLOADS), (
        "two URL payloads are the same string, so one shape is measured twice and "
        "another is measured not at all"
    )
    assert {field for field, _, _ in GEOMETRY_SINKS} == {"width_px", "height_px"}, (
        f"GEOMETRY_SINKS covers {GEOMETRY_IDS}, not both geometry fields of "
        "RenderOptions; emptied, every cell proving a hostile page dimension is refused "
        "before it reaches CSS skips instead of failing -- and a skip there reads "
        "identically to a fix"
    )
    assert {case.field for case in DISTINCT_OBLIGATIONS} == {
        "author",
        "license_url",
        "work",
        "source_url",
        "modified",
    }, (
        f"DISTINCT_OBLIGATIONS covers {[case.field for case in DISTINCT_OBLIGATIONS]}, "
        "not the five fields _credits_of keys on; an attribution field with no case "
        "here may be collapsed away, and this is the half of de-duplication with legal "
        "consequences"
    )


def _obligations_of(credit: ImageCredit) -> tuple[str, ...]:
    """Every string this credit is obliged to put on the page."""
    source = credit.source
    return tuple(
        part
        for part in (
            credit.license,
            credit.author,
            credit.license_url,
            source.url if source else None,
            source.title if source else None,
        )
        if part
    )


async def test_surviving_credit_rows_keep_their_first_appearance_order() -> None:
    """``dict.fromkeys`` keeps the first occurrence, which is what makes the colophon
    read in the order the page places the figures rather than in an arbitrary one."""
    lead, second = PANDAS[0].as_asset(ImageRole.HERO), PANDAS[1].as_asset()

    composition = await HtmlComposer().compose(
        make_brief(), make_content(), (lead, second, lead)
    )
    parsed = assert_structurally_valid(composition.html)
    colophon = parsed.text_in("credits")

    rows = parsed.classed("credit")
    assert len(rows) == 2, (
        f"(A, B, A) rendered {len(rows)} credit rows, expected 2: the repeat of A "
        "collapses into its first appearance and B survives on its own"
    )
    assert colophon.index(PANDAS[0].author) < colophon.index(PANDAS[1].author), (
        f"the colophon credits {PANDAS[1].author} before {PANDAS[0].author}, but A "
        "was placed first -- the surviving row is the second occurrence, not the first"
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


VISUAL_ORDER_JS: Final = """(selector) => {
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    const node = el.firstChild;
    if (!node || node.nodeType !== Node.TEXT_NODE) continue;
    const range = document.createRange();
    const placed = [];
    const logical = [];
    for (let i = 0; i < node.length; i++) {
      const char = node.data[i];
      if (/\\s/.test(char)) continue;
      range.setStart(node, i);
      range.setEnd(node, i + 1);
      const box = range.getBoundingClientRect();
      placed.push([Math.round(box.top), box.left, char]);
      logical.push(char);
    }
    placed.sort((a, b) => (a[0] - b[0]) || (a[1] - b[1]));
    out.push({logical: logical.join(""), visual: placed.map((p) => p[2]).join("")});
  }
  return out;
}"""
"""Every printing character of a text node, ordered by where it was actually painted.

Sorted by rounded top then left, so a wrapped line reads as two lines rather than as a
reordering. Whitespace is skipped at both ends of the comparison, for two reasons: the
templates are pretty-printed, so every text node here is bracketed by a newline and an
indent that no reader transcribes, and a collapsed space has a zero-width box whose
``left`` sorts arbitrarily against its neighbours. What is left is exactly the string a
reader copies out of the PNG.

The logical string is what the markup holds; the visual string is what the reader gets.
On an LTR page they are always equal, which is why this fence runs RTL."""

URL_SITES: Final = (".credit__url", ".refs__meta")
"""The two elements a reader is expected to copy by hand out of a PNG: the colophon's
licence and source URIs, and the bibliography's reference URLs."""

BIDI_NEUTRAL_TAIL: Final = "/"
"""The character that moves. A solidus has no strong direction of its own, so on an RTL
page it resolves to the paragraph's and jumps the run it was written after.

Both sites have to *carry* one for a cell to be able to see the bug. The colophon does on
real data -- three of the five fixture licence URIs end in a solidus -- but the reference
URLs do not, so ``test_a_url_on_an_rtl_page_...`` supplies its own content rather than
taking ``make_content()``'s. Measured: without that, the ``.refs__meta`` cells were green
before the fix and after it, which is a passing test that proves nothing."""


def content_with_bidi_neutral_urls() -> ResearchContent:
    """``make_content()``, but every source URL ends in the character bidi reorders.

    Built from ``make_facts(3)`` by ``replace`` rather than by hand, so the labels stay
    the zero-padded non-prefixing ones the rest of this file relies on and only the URL
    changes."""
    facts = tuple(
        replace(fact, source=make_source(url=f"{fact.source.url}/"))
        for fact in make_facts(3)
        if fact.source is not None
    )
    sources = tuple(make_source(url=f"{source.url}/") for source in (make_source(),))
    return make_content(facts=facts, sources=sources)


@BROWSER_LOOP
@BODIES
@pytest.mark.parametrize("selector", URL_SITES)
async def test_a_url_on_an_rtl_page_is_painted_in_the_order_it_is_written(
    chromium: Browser, template_id: str, selector: str
) -> None:
    """A URL renders the same left-to-right whichever direction the page runs.

    A URL is an LTR string, and its trailing solidus is bidi-neutral: on an RTL page it
    takes the paragraph direction and moves. Measured before the fix,
    ``https://creativecommons.org/licenses/by/2.0/`` painted as
    ``/https://creativecommons.org/licenses/by/2.0`` -- one character relocated from the
    end to the front, silently, in the element whose entire purpose is to be transcribed
    accurately. Attribution lands in a PNG, so a licence URI nobody can retype correctly
    is attribution that is not discharged, and the bibliography beside it is what makes a
    researched fact checkable.

    This is a *visual* invariant, so no parser can see it and neither can a computed-style
    comparison: the DOM, the text content and every computed value are identical before
    and after. Only where the glyphs land differs.
    """
    composition = await compose_cell(
        template_id,
        Theme.LIGHT,
        content_with_bidi_neutral_urls(),
        tuple(panda.as_asset() for panda in PANDAS),
    )
    rtl = composition.html.replace('dir="ltr"', 'dir="rtl"', 1)
    assert 'dir="rtl"' in rtl, (
        "the composed document does not declare a direction on <html>, so forcing RTL "
        "here changed nothing and this cell would measure an LTR page"
    )

    async with laid_out(chromium, replace(composition, html=rtl)) as page:
        measured = await page.evaluate(VISUAL_ORDER_JS, selector)

    assert measured, (
        f"{selector} renders on no element of {template_id}, so this cell examined zero "
        "URLs and would pass for a page that had dropped its attribution entirely"
    )
    reorderable = [row for row in measured if row["logical"].endswith(BIDI_NEUTRAL_TAIL)]
    assert reorderable, (
        f"none of the {len(measured)} {selector} elements on {template_id} ends in "
        f"{BIDI_NEUTRAL_TAIL!r}, so none of them can be reordered and this cell would be "
        f"green with the fix deleted. Measured: {[r['logical'] for r in measured]}"
    )
    scrambled = [row for row in measured if row["logical"] != row["visual"]]
    assert not scrambled, (
        f"{len(scrambled)} of {len(measured)} {selector} elements on {template_id} are "
        f"painted in a different order from the one they are written in: "
        f"{[(row['logical'], row['visual']) for row in scrambled]}. A reader copying "
        "one of these out of the PNG gets a URL that was never published"
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

    Both halves are measured, not read off ``innerText``. ``document.body.innerText``
    silently degrades to ``textContent`` the moment anything above the credits is
    ``display: none``, so "the URI is on the page" has to mean a span with real height
    and no hidden ancestor -- see ``CREDIT_URL_VISIBILITY_JS``.

    "Exactly as supplied" is exact for every URI a licence actually carries, which is
    what this fence is about, and not quite the whole truth: ``_legible_url`` replaces
    any control or format character with U+FFFD on the way to the page, so a URI that
    contains one is rendered faithfully rather than reproduced byte for byte. The six
    fixture URLs are clean ASCII, so nothing here is transformed. See
    ``test_no_text_node_carries_an_invisible_character`` for why a URI that *does*
    carry one must not be reproduced: printing it back unchanged is how a bidi
    override makes a rendered URL read as a different URL.
    """
    # Iterating PANDAS holds only while every body displays all of them. At six
    # assets stat_grid renders four images and four credits -- a hero plus a band
    # that caps at three -- where the other bodies render six of each. Grow PANDAS
    # past four and this breaks on stat_grid alone; iterate rendered credits then.
    images = tuple(panda.as_asset() for panda in PANDAS)
    composition = await compose_cell(template_id, theme, make_content(), images)

    async with laid_out(chromium, composition) as page:
        measured: dict[str, object] = await page.evaluate(TRANSFORMED_URL_TEXT_JS)
        shown: dict[str, object] = await page.evaluate(CREDIT_URL_VISIBILITY_JS)
        rendered: str = await page.evaluate("document.body.innerText")

    examined = int(_number(measured["examined"]))
    transformed = [_fields(row) for row in _rows(measured["offenders"])]
    spans = int(_number(shown["examined"]))
    visible_chars = int(_number(shown["visible_text"]))
    hidden = [_fields(row) for row in _rows(shown["offenders"])]

    assert examined >= MIN_EXAMINED_URI_ELEMENTS, (
        f"{template_id} in {theme.value} rendered only {examined} elements whose own "
        f"text holds a URI, under the {MIN_EXAMINED_URI_ELEMENTS} this fence needs to "
        "mean anything: nothing was judged, so nothing could be found corrupted"
    )
    assert not transformed, (
        "text containing a URI is being case-transformed by CSS, which corrupts it: "
        f"{transformed}"
    )

    assert spans >= 2 * len(PANDAS), (
        f"{template_id} in {theme.value} rendered {spans} .credit__url spans, not the "
        f"{2 * len(PANDAS)} the {len(PANDAS)} fixtures owe (a licence URI and a source "
        "URI each), so the visibility measurement below covers less than the loop after"
    )
    assert not hidden, (
        f"in {template_id} in {theme.value} a licence URI is in the markup but not on "
        "the page; attribution has to be legible in the PNG, not merely present in "
        "the DOM:\n"
        + "\n".join(
            f"  {row['element']} hidden by {row['hidden_by']} "
            f"(display: {row['display']}, visibility: {row['visibility']}, "
            f"height: {row['height_px']}px)"
            for row in hidden
        )
    )
    assert visible_chars >= MIN_VISIBLE_CREDIT_URL_CHARS, (
        f"{template_id} in {theme.value} shows {visible_chars} characters of licence "
        f"URI across {spans} visible spans, under the "
        f"{MIN_VISIBLE_CREDIT_URL_CHARS} this fence needs: the spans have height but "
        "their text is not reaching them"
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


INVISIBLE_URL_PAYLOADS = (BIDI_URL_PAYLOAD, ZWSP_HOST_PAYLOAD, CONTROL_URL_PAYLOAD)
"""The three payloads above whose damage is invisible rather than structural. The other
four announce themselves with a quote or an angle bracket; these read as ordinary URLs
in the source and as different URLs on the page."""


CONTROL_TITLE_PAYLOAD = "Panda\x7fReport\x01Q"
CONTROL_PUBLISHER_PAYLOAD = "WWF\x02X"
CONTROL_AUTHOR_PAYLOAD = "Photo\x03grapher"
CONTROL_LICENSE_PAYLOAD = "CC BY\x04 2.0"
"""Four fields the URL payloads cannot reach, each carrying a ``Cc`` character that the
research and imagery zones provably do not remove.

``research/agent.py``'s ``_visible`` drops ``Cf`` only, and ``_clean_title`` is
``" ".join(_visible(raw).split())`` -- so ``str.split`` takes out the ten whitespace
controls and the other 55 ``Cc`` characters walk through into a title or a publisher.
``ImageCredit.author`` and ``ImageCredit.license`` come from the imagery zone and are
cleaned nowhere at all.

These exist because the first version of the fence below had every source *untitled*, so
the ``source.title`` branch of ``_reference`` and every credit field were excluded from
the fixture by construction -- the exact mirror of the older bug where every source had a
title and so the ``_host`` fallback was never given hostile input. Composed with these
four, the fence measured 12 offenders on ``process_flow`` and 13 on each of the others,
in ``figcaption.hero__credit``, the ``*__src`` attributions, ``li``, ``span.refs__meta``,
``p.credit__license``, ``span.credit__work`` and ``span.credit__author``."""


def illegible_url_inputs() -> tuple[ResearchContent, tuple[ImageAsset, ...]]:
    """Content carrying every invisible-character payload at once, URL and prose alike.

    One cell then covers every door hostile text walks through: the bibliography, a
    reference title from both branches of ``source.title or _host(source.url)``, a fact's
    attribution, a section's attribution, the hero caption, both ``span.credit__url`` and
    all three prose fields of the colophon.

    Two shapes of source, and the pairing is the point. The untitled ones route their URL
    through ``_host`` into a reference title; the titled one exercises the branch that
    takes ``source.title`` instead, which no URL payload can reach.
    """
    sources = (
        *(Source(url=url, title=None, publisher=None) for url in INVISIBLE_URL_PAYLOADS),
        Source(
            url=BENIGN_URL,
            title=CONTROL_TITLE_PAYLOAD,
            publisher=CONTROL_PUBLISHER_PAYLOAD,
        ),
    )
    content = make_content(
        facts=tuple(
            Fact(
                label=f"Bamboo metric {index:02d}",
                value=f"{index:02d}7.5",
                unit="kg",
                detail=None,
                source=source,
            )
            for index, source in enumerate(sources)
        ),
        sections=(
            NarrativeSection(
                heading="Diet", body="Bamboo makes up the diet.", sources=sources
            ),
        ),
        sources=sources,
    )
    images = (
        make_asset(
            credit=make_credit(
                license_text=CONTROL_LICENSE_PAYLOAD,
                author=CONTROL_AUTHOR_PAYLOAD,
                license_url=BIDI_URL_PAYLOAD,
                source=sources[-1],
            ),
        ),
    )
    return content, images


REPLACEMENT_SITES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "bibliography": ".refs",
        "colophon": ".credits",
        "attributions": ".row__src, .chip__src, .rank__src, .section__src",
        "hero caption": ".hero__credit",
    }
)
"""The four places a sanitised string is rendered, as selectors rather than as a
total, because a total cannot say which one went quiet.

Measured: with the body block excised from the templates the page still renders its
bibliography and colophon and reports 9 U+FFFD -- so a single floor of 8 passed a
document with no body at all, and 11 - 2 (losing the colophon) and 10 (losing a fact
attribution) both cleared it too. Per-site counts reject all three, and the failure
names the site.

Spelled out per body, like ``BODY_SELECTORS``: the attribution class is
``.row__src`` in ``stat_grid``, ``.chip__src`` in ``process_flow`` and ``.rank__src``
in ``ranked_list``, with ``.section__src`` in the two that render sourced sections. A
body that renames its attribution row shows up here as a zero, which is the point.

The hero caption is here because the total assertion below found it, and that is the
whole argument for keeping a total beside the per-site counts. ``_caption`` builds it
from ``Credit.work``, ``Credit.author`` and ``Credit.license`` -- three fields the
colophon also prints -- but it is emitted in ``{% block masthead_aside %}``, so it
falls outside both ``.refs`` and ``.credits`` and was rendering four sanitised
characters per body that no per-site floor was watching. Measured per body:
bibliography 10, colophon 5, hero caption 4, attributions 4 in ``stat_grid`` and
``ranked_list`` and 2 in ``process_flow``, which has no sourced sections -- 23, 23 and
21 in total, exactly what the walk reports."""

ILLEGIBLE_TEXT_JS = """
(sites) => {
  const ILLEGIBLE = new RegExp(
    '[\\u0000-\\u0008\\u000B\\u000C\\u000E-\\u001F\\u007F-\\u009F' +
    '\\u202A-\\u202E\\uFEFF]',
    'g'
  );
  const name = el =>
    el ? el.tagName.toLowerCase() + (el.className ? '.' + el.className : '') : '(none)';
  const entries = Object.entries(sites);
  const per_site = {};
  for (const [key] of entries) per_site[key] = 0;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const offenders = [];
  let nodes = 0;
  let characters = 0;
  let replacements = 0;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.nodeValue;
    nodes += 1;
    characters += text.length;
    let here = 0;
    for (const char of text) {
      if (char === '\\uFFFD') here += 1;
    }
    if (here) {
      replacements += here;
      const el = node.parentElement;
      for (const [key, selector] of entries) {
        if (el && el.closest(selector)) per_site[key] += here;
      }
    }
    ILLEGIBLE.lastIndex = 0;
    for (let hit = ILLEGIBLE.exec(text); hit; hit = ILLEGIBLE.exec(text)) {
      offenders.push({
        codepoint:
          'U+' + hit[0].codePointAt(0).toString(16).toUpperCase().padStart(4, '0'),
        element: name(node.parentElement),
        text: text.trim().slice(0, 60),
      });
    }
  }
  return {nodes, characters, replacements, per_site, offenders};
}
"""
"""Every text node the page has, judged one node at a time.

Not ``document.body.innerText``: that degrades to ``textContent`` the moment anything
above is ``display: none``, which is how a page that painted nothing once passed six
licence assertions here. It also normalises whitespace and drops hidden subtrees, so a
zero-width character sitting in one would never be counted. A ``TreeWalker`` over
``SHOW_TEXT`` reads the DOM instead of the layout, so nothing can hide from it -- and
because it starts at ``document.body`` it never sees the ``<style>`` element's own text.

One character this cannot judge: chromium's parser *deletes* U+0000 out of a text node
(measured -- ``a\\x00b\\x0bc`` arrives as four characters, the vertical tab kept and the
NUL gone), so a raw NUL is unreachable here however hostile the fixture. It is the
parse-level fence above, reading the composed string rather than the DOM, that catches
that one.

``nodes`` and ``characters`` are what makes ``offenders`` mean something: a page whose
body failed to render at all has no text nodes and therefore no offenders. ``per_site``
counts U+FFFD per rendering site, which is the *positive* half of the measurement --
proof that the hostile strings reached the renderer and were neutralised there, rather
than vanishing somewhere upstream and leaving a clean page to pass a fence about a dirty
one. It is attributed by ``closest`` from the text node's own parent, so a count is
credited to the site the text is actually inside rather than to whatever wraps it."""

MIN_WALKED_TEXT_NODES: Final = 60
"""Measured: 99 text nodes in the leanest of the three cells (``process_flow``), 110 in
the richest (``ranked_list``). The floor sits under the leanest because the count is a
function of how much markup a body emits, which is nobody's contract -- it is here to
reject zero and to reject a body that collapsed, not to pin a layout."""

MIN_WALKED_CHARACTERS: Final = 500
"""Measured: 817 characters in the leanest cell, 884 in the richest. Same reasoning as
the node floor."""

MIN_REPLACEMENTS_PER_SITE: Final = 1
"""Every site in ``REPLACEMENT_SITES`` has to show at least one U+FFFD.

One rather than a measured floor on purpose. The interesting number here is zero: a site
that renders no sanitised character either stopped rendering or stopped sanitising, and
either way the fence has gone quiet about it. A per-site floor tuned to the measurement
would instead pin how many hostile fields the fixture happens to carry, which is a fact
about the fixture and not a promise about the page. The totals below cover magnitude."""


@BODIES
@BROWSER_LOOP
async def test_no_text_node_carries_an_invisible_character(
    chromium: Browser, template_id: str
) -> None:
    """A bidi override in a URL is not an escaping bug, so no parser can see it.

    ``research/agent.py`` cleans ``Cf`` out of a source's title and publisher and
    cannot clean the URL, which is its verification key -- so the composer's own
    ``_host`` fallback used to hand the removed characters straight back to the page,
    and ``https://example.com/`` + U+202E + ``gpj.exe`` rendered as though it ended in
    ``exe.jpg``. What a reader sees is a property of the laid-out page, which is why
    this is measured in a browser and stated over text nodes rather than over any one
    field: it stays true however the research zone changes upstream.

    One axis, not two. Text content does not vary with the theme -- the palette does --
    so crossing ``IN_BOTH_THEMES`` would double the cells and measure the same string
    twice.
    """
    content, images = illegible_url_inputs()
    composition = await compose_cell(template_id, Theme.LIGHT, content, images)

    async with laid_out(chromium, composition) as page:
        measured: dict[str, object] = await page.evaluate(
            ILLEGIBLE_TEXT_JS, dict(REPLACEMENT_SITES)
        )

    nodes = int(_number(measured["nodes"]))
    characters = int(_number(measured["characters"]))
    replacements = int(_number(measured["replacements"]))
    per_site = {
        site: int(_number(count))
        for site, count in _fields(measured["per_site"]).items()
    }
    offenders = [_fields(row) for row in _rows(measured["offenders"])]

    assert nodes >= MIN_WALKED_TEXT_NODES, (
        f"{template_id} laid out {nodes} text nodes, under the "
        f"{MIN_WALKED_TEXT_NODES} this fence needs: the walk found almost nothing, so "
        "it could not have found anything wrong"
    )
    assert characters >= MIN_WALKED_CHARACTERS, (
        f"{template_id} laid out {characters} characters across {nodes} text nodes, "
        f"under the {MIN_WALKED_CHARACTERS} this fence needs to mean anything"
    )
    assert not offenders, (
        f"{template_id} renders text a reader cannot see and cannot trust:\n"
        + "\n".join(
            f"  {row['codepoint']} in {row['element']}: {row['text']!r}"
            for row in offenders
        )
    )
    # Last, and the ones that fail on a *clean* page: the two floors above prove the
    # walk happened, and these prove the walk had something to find, in every place
    # that was supposed to have something.
    assert set(per_site) == set(REPLACEMENT_SITES), (
        f"{template_id}: the browser measured sites {sorted(per_site)} but "
        f"REPLACEMENT_SITES names {sorted(REPLACEMENT_SITES)} -- the two have drifted, "
        "so an unmeasured site would report nothing rather than fail"
    )
    quiet = sorted(
        site for site, count in per_site.items() if count < MIN_REPLACEMENTS_PER_SITE
    )
    assert not quiet, (
        f"{template_id} shows no U+FFFD in {quiet} (measured {per_site}). Those sites "
        "either stopped rendering the sanitised string or stopped being handed a hostile "
        "one, so this fence is passing on a clean document rather than on a neutralised "
        f"one. Selectors: {[REPLACEMENT_SITES[site] for site in quiet]}"
    )
    assert replacements == sum(per_site.values()), (
        f"{template_id} shows {replacements} U+FFFD in total but only "
        f"{sum(per_site.values())} inside the three named sites: something is rendering "
        "a sanitised string somewhere REPLACEMENT_SITES does not name, and that place is "
        "unfenced"
    )


ZWSP: Final = "​"
"""U+200B ZERO WIDTH SPACE. In Khmer, Thai, Lao and Burmese this *is* the word
boundary: those scripts write no space between words, so it is both the separator and
the only line-break opportunity the text carries."""

KHMER_WRAP_WIDTH_PX: Final = 640
"""The narrowest page the suite exercises, so the Khmer run has to wrap somewhere.

At the 1200px default a five-word sentence fits on one line in every body, and a fence
that cannot observe a wrap cannot tell a kept boundary from an inert one. Narrowest is
necessary and not sufficient: how long the run has to be for the wrap to happen at all
is :data:`KHMER_CLAUSES`."""

KHMER_WORDS: Final = (
    "ខ្លាឃ្មុំផេនដា",
    "ជាសត្វ",
    "ដែលរស់នៅ",
    "ក្នុងប្រទេសចិន",
    "ហើយស៊ីតែឬស្សី",
)
"""Five Khmer words, to be joined on ZWSP the way the script is actually written.

Khmer rather than Thai because chromium ships no Khmer line-breaking dictionary, so
ZWSP is the *only* thing that can break this run -- which makes the wrap half of the
claim measurable rather than incidental."""


KHMER_CLAUSES: Final = 3
"""How many times the clause repeats, so the run wraps in the *roomiest* column.

One clause was enough while every body squeezed its prose at 640px, and it stopped
being enough the moment one of them stopped: ``ranked_list`` now stacks its rows and
its side-heads below 820px, which took ``.section__body`` from a 244px track to the
full 564px and ``.rank__label``/``.rank__detail`` from 241px to 502px. Measured on one
clause after that change: all three ZWSP-bearing elements set on a single line and
``wrapped`` came back 0, so the cell failed closed exactly as its last assertion
promises -- correctly, because it could no longer tell a kept boundary from an inert
one.

Three rather than two, for margin rather than for the count. Two already wraps all
three elements in all three bodies, but its worst case is a 550px longest line in a
564px box: 14px, which is one layout tweak away from being nothing. Three sets the
same elements on 2-4 lines with the run half again as long as the widest column any
body gives it at 640px. Raising this can only make the wrap more observable, never
less, and ``boundaries >= len(KHMER_WORDS) - 1`` stays true because there are strictly
more boundaries."""


def khmer_content() -> ResearchContent:
    """Correct Khmer prose in every field a body renders as a sentence."""
    sentence = ZWSP.join(KHMER_WORDS * KHMER_CLAUSES)
    return make_content(
        title="ខ្លាឃ្មុំផេនដា",
        facts=(
            Fact(
                label=sentence,
                value="១២",
                unit="គីឡូក្រាម",
                detail=sentence,
                source=make_source(title=sentence),
            ),
        ),
        sections=(
            NarrativeSection(heading="ព័ត៌មាន", body=sentence, sources=(make_source(),)),
        ),
    )


@BODIES
@BROWSER_LOOP
async def test_a_script_that_wraps_only_on_zwsp_keeps_its_word_boundaries(
    chromium: Browser, template_id: str
) -> None:
    """Correct Khmer must not be sanitised into visible damage.

    ``dd70176`` put ZWSP in ``_legible_text``'s forbidden set alongside the deprecated
    bidi embeddings, on the reasoning that it is "simply not visible". That reasoning
    holds for a URL and breaks for a sentence. Khmer writes no space between words, so
    ZWSP is the word boundary -- replacing each one with U+FFFD printed a visible
    diamond at every boundary *and* destroyed the only wrap opportunity the text had.
    Measured on a Khmer page before this fence: **20 U+FFFD**, one per word, in content
    with nothing whatsoever wrong with it.

    The distinction that keeps the rest of the set intact: an embedding reorders glyphs
    that are already there and the override substitutes what a run says, so both can
    make the page lie. ZWSP adds no glyph, removes none and reorders nothing. It belongs
    with ZWNJ and ZWJ, kept since ``dd70176`` for the same reason -- refusing to corrupt
    correct text in a script nobody tested. ``_legible_url`` still replaces it, because
    a verification key has no words to separate.

    Two assertions, because either alone is satisfiable by an empty page: the boundaries
    survive as boundaries, and the run actually wrapped on them.
    """
    composition = await compose_cell(
        template_id, Theme.LIGHT, khmer_content(), width_px=KHMER_WRAP_WIDTH_PX
    )
    assert ZWSP in composition.html, (
        "the composed document carries no ZWSP at all, so it was stripped rather than "
        "kept and this cell would pass by having nothing to measure"
    )

    async with laid_out(chromium, composition) as page:
        measured: dict[str, object] = await page.evaluate(KHMER_TEXT_JS, ZWSP)

    boundaries = int(_number(measured["boundaries"]))
    replacements = int(_number(measured["replacements"]))
    wrapped = int(_number(measured["wrapped"]))

    assert boundaries >= len(KHMER_WORDS) - 1, (
        f"{template_id} lays out {boundaries} ZWSP word boundaries, fewer than the "
        f"{len(KHMER_WORDS) - 1} a single Khmer sentence carries: the boundaries are "
        "being removed from the rendered text, which is the other way to break it"
    )
    assert replacements == 0, (
        f"{template_id} prints {replacements} U+FFFD across text that is correct Khmer. "
        "Every one of them is a visible diamond standing where a word boundary was, in "
        "prose a reader is meant to read"
    )
    assert wrapped >= 1, (
        f"{template_id} lays out {boundaries} ZWSP boundaries but no element containing "
        "one wrapped to a second line, so the wrap opportunity is not measurably doing "
        f"anything at {KHMER_WRAP_WIDTH_PX}px and this cell cannot tell a kept boundary from "
        "an inert one"
    )


KHMER_TEXT_JS = """
(zwsp) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let boundaries = 0;
  let replacements = 0;
  let wrapped = 0;
  const seen = new Set();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.nodeValue;
    const here = text.split(zwsp).length - 1;
    boundaries += here;
    replacements += (text.match(/\\uFFFD/g) || []).length;
    if (here > 0 && !seen.has(node.parentElement)) {
      seen.add(node.parentElement);
      const range = document.createRange();
      range.selectNodeContents(node);
      const rows = new Set();
      for (const box of range.getClientRects()) rows.add(Math.round(box.top));
      if (rows.size > 1) wrapped += 1;
    }
  }
  return {boundaries, replacements, wrapped};
}"""
"""Counts ZWSP boundaries and U+FFFD over every text node, and how many ZWSP-bearing
elements laid out on more than one line.

The line count comes from distinct rounded ``top`` values across the node's client
rects, which is the same technique the RTL fence uses -- one rect per line box."""


TRANSFORMED_URL_TEXT_JS = """
() => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const offenders = [];
  const seen = new Set();
  let examined = 0;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.nodeValue.includes('://')) continue;
    const el = node.parentElement;
    if (!el || seen.has(el)) continue;
    seen.add(el);
    examined += 1;
    const transform = getComputedStyle(el).textTransform;
    if (transform !== 'none') {
      offenders.push({
        element: el.tagName.toLowerCase() + '.' + el.className,
        textTransform: transform,
        text: node.nodeValue.trim().slice(0, 60),
      });
    }
  }
  return {examined, offenders};
}
"""
"""Own text nodes only. Judging by ``textContent`` would blame ``<body>`` for
every URI on the page and report a uniform false positive.

``examined`` is every URI-bearing element the walk actually judged -- counted after
the ``'://'`` filter and the ``seen`` dedupe, so it is the number of computed
``text-transform`` values compared and not the number of text nodes crossed. Without
it ``not offenders`` is green for a page that rendered no URI at all, which is the
same page every assertion below is trying to reject."""

MIN_EXAMINED_URI_ELEMENTS: Final = 6
"""Measured: 7 in all six cells of this fence -- the six ``span.credit__url`` the
assertions below transcribe, plus the bibliography's single ``span.refs__meta``. The
floor sits at six because six is what the loop below names: a page that dropped the
bibliography is still worth judging, and a page that dropped the colophon must not
pass by having nothing left to uppercase."""


CREDIT_URL_VISIBILITY_JS = """
() => {
  const name = el =>
    el.tagName.toLowerCase() + (el.className ? '.' + el.className : '');
  const offenders = [];
  let examined = 0;
  let visible_text = 0;
  outer:
  for (const url of document.querySelectorAll('span.credit__url')) {
    examined += 1;
    for (let ancestor = url; ancestor; ancestor = ancestor.parentElement) {
      const style = getComputedStyle(ancestor);
      if (style.display === 'none' || style.visibility === 'hidden') {
        offenders.push({
          element: name(url),
          hidden_by: name(ancestor),
          display: style.display,
          visibility: style.visibility,
          height_px: url.getBoundingClientRect().height,
        });
        continue outer;
      }
    }
    const style = getComputedStyle(url);
    const height = Math.min(url.offsetHeight, url.getBoundingClientRect().height);
    if (height < 1) {
      offenders.push({
        element: name(url),
        hidden_by: name(url),
        display: style.display,
        visibility: style.visibility,
        height_px: height,
      });
      continue;
    }
    visible_text += url.innerText.trim().length;
  }
  return {examined, visible_text, offenders};
}
"""
"""Each ``span.credit__url`` holds one licence or source URI and nothing else, and the
loop below reads those URIs straight out of the page -- so each span has to be
measured as *visible*, not merely present. ``document.body.innerText`` is not that
measurement: put ``display: none`` on the body and it falls back to ``textContent``,
which reported 1449 characters and passed all six licence assertions on a body whose
own client rect measured 0px -- a page that painted nothing whatsoever.

``display`` does not inherit, and a descendant of a ``display: none`` element still
computes its own, so this has to climb to ``<body>`` rather than read one style. The
label is what lets a hidden ancestor skip the *span* and keep counting the rest, where
a bare ``break`` would fall out of the walk into the height check and blame the span
itself. ``visibility`` does inherit, but is read on the way up anyway so a failure can
name the ancestor that hid the span rather than the span that inherited from it.

Height is the weaker of ``offsetHeight`` and the client rect: the first is a rounded
integer, the second sub-pixel, and a span collapsed to 0.4px is gone from the PNG
whichever way it rounds. ``visible_text`` is summed only over spans that cleared both
checks, so ``examined`` and ``offenders`` cannot both come back empty and agree that
the attribution is fine."""

MIN_VISIBLE_CREDIT_URL_CHARS: Final = 300
"""Measured: 321 visible characters across the six spans in every cell of this fence,
which is the exact combined length of the six fixture URLs -- one URL per span, no
other text in them. 300 leaves a fixture URL room to be renamed shorter while staying
far above the ~50 that one surviving span would report."""


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


@IN_BOTH_THEMES
@BROWSER_LOOP
async def test_an_untitled_source_cannot_widen_the_page_with_its_host(
    chromium: Browser, theme: Theme
) -> None:
    """A reference with no title falls back to its URL host, which nothing caps.

    ``_host`` returns ``urlsplit().hostname`` whole -- uncapped, and 253 characters are
    legal -- so a source that arrives without a
    title -- which the port permits -- writes its host into ``Reference.title`` and
    thence into ``<li>`` at ``_base.html.j2:47``. ``body`` is ``width: var(--w)`` with
    no ``overflow: hidden``, so an unbreakable run there does not clip: it widens the
    document, and the renderer screenshots the wider box. Measured before ``.refs li``
    gained ``overflow-wrap``: the ``<li>`` set 1365px inside its 507px column and took
    ``body`` from 1200 to 1437.

    The escaping fixtures could not have caught this. ``hostile_inputs`` gives every
    source a ``title``, so the ``_host`` fallback branch has never been exercised by
    hostile input at all -- the assertion measured the wrong branch, which is the
    fifth time that shape of hole has been found in this zone.
    """
    content = make_content(
        facts=make_facts(7),
        sources=(Source(url=UNTITLED_SOURCE_URL, title=None, publisher=None),),
    )
    composition = await compose_cell("stat_grid", theme, content, PANDA_SET)

    assert LONG_HOST in composition.html, (
        "the long host never reached the document, so this cell proves nothing: "
        "_reference no longer falls back to _host, or _unique_by_url dropped the source"
    )

    async with laid_out(chromium, composition) as page:
        measured: dict[str, object] = await page.evaluate(OVERFLOW_JS)
        page_width: dict[str, object] = await page.evaluate(
            "() => ({scroll: document.documentElement.scrollWidth,"
            " client: document.body.clientWidth})"
        )

    examined = int(_number(measured["examined"]))
    offenders = [_fields(row) for row in _rows(measured["offenders"])]
    scroll = _number(page_width["scroll"])
    client = _number(page_width["client"])

    assert examined >= MIN_EXAMINED_BOXES, (
        f"only {examined} boxes laid out in {theme.value}, under "
        f"{MIN_EXAMINED_BOXES}: a page with nothing on it overflows nothing"
    )
    assert not offenders, (
        f"an untitled source's {len(LONG_HOST)}-character host overflows in "
        f"{theme.value}:\n"
        + "\n".join(
            f"  {row['element']} overflows {row['parent']} "
            f"({row['problem']}) by {row['pixels']}px"
            for row in offenders
        )
    )
    assert scroll <= client, (
        f"the document scrolls to {scroll}px inside a {client}px body in "
        f"{theme.value}, so the PNG comes out {scroll - client}px wider than the "
        "width that was asked for. An unbreakable run in the bibliography is the "
        "usual cause; .refs li needs overflow-wrap"
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


ORDINARY_FACTS = (
    *NARROW_FACTS,
    Fact(
        label="Newborn cub size",
        value="1/900th of its mother",
        unit=None,
        detail="Blind, pink and about 100 g.",
        source=make_source(url="https://www.worldwildlife.org/species/giant-panda"),
    ),
)
"""``NARROW_FACTS`` plus the one fact from the real ``assets/panda/facts.json`` with
the worst size-against-prose ratio, and nothing adversarial: this is content a
researcher plausibly returns, not a 62-character stress string.

That seventh fact is what the legibility floor turns on. 21 characters earn a
``--fit`` of 7.94cqw, so it needs 208px of column before it reaches body copy at all,
and in a 640px ``ranked_list`` row it was the figure every wider fix left behind."""


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
      const style = getComputedStyle(el);
      const lineHeight = parseFloat(style.lineHeight);
      const height = el.getBoundingClientRect().height;
      const cell = el.closest(sel.container);
      return {
        text: el.innerText.replace(/\\s+/g, ' ').trim(),
        lines: lineHeight > 0 ? Math.round(height / lineHeight) : 0,
        line_height_px: lineHeight > 0 ? lineHeight : 0,
        height_px: height,
        rungs: [...el.classList].filter(c => c.startsWith(sel.scale_prefix)),
        size: style.getPropertyValue('--size').trim(),
        fit: el.style.getPropertyValue('--fit').trim(),
        font_px: parseFloat(style.fontSize),
        container_type: getComputedStyle(cell).containerType,
        cell_px: cell.getBoundingClientRect().width,
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

Lines are counted as ``round(height / line-height)`` off the element's own box and
computed ``line-height``. Clustering the range's client rects on vertical overlap --
what this measurement did until commit 5 -- cannot count these elements: every rule
involved sets a line-height tighter than the face's em box (``.rank__value`` 0.96,
``.chip__value`` 0.98): at 50px type the line box is 48px and each line's rect is
60px, so consecutive rects overlap by 12px and merge into one cluster. Measured with
the ``--fit`` cap lifted so values genuinely wrap, the old clustering reported one
line for 13 of the 14 values in the two narrow bodies -- ``26-84 lb (12-38 kg)``
among them, a 144px box exactly three 48px lines deep -- which made ``lines == 1``
close to a tautology. Clustering on ``top`` is not the fix either: a
value carries a smaller unit span whose rects sit at a different ``top`` on the same
line, so top-grouping overcounts. (``TITLE_LINES_JS`` can group per character on
``top`` precisely because ``.title`` has no differently-sized child.)
``line_height_px`` and ``height_px`` travel with the count so a failure shows the
division, and a ``line-height`` that resolves to no length reports zero lines rather
than a plausible one.

Height over leading has its own failure mode, in the other direction: an inline child
taller than its parent's line box stretches the box without adding a line, and the
division then over-counts. Overlay ``.rank__unit { font-size: 3em; line-height: 2 }``
and values whose own text still sets on one line report 6, 13 and 19 lines; the same
overlay on ``.chip__unit`` reports 12 and 19. It takes that much: at
``line-height: 4`` on the unit's own 0.28em the box grows to 1.17 of the value's line
and ``round`` absorbs it. Unreachable as the bodies stand -- every unit span is
sub-em with a numeric ``line-height`` below 1 -- and it fails loud rather than
passing quietly, which is the right way round for a counter.

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
    """``round(height_px / line_height_px)``, or 0 if the line-height resolved to no
    length at all."""
    line_height_px: float
    """The computed ``line-height`` in px, and the divisor above: a keyword or a
    ``normal`` that resolves to no length arrives as 0 and takes ``lines`` with it."""
    height_px: float
    """The value's own box height: one ``line_height_px`` per line it set."""
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
    cell_px: float
    """The query container's own width, so ``fit`` can be turned back into pixels:
    ``7.94cqw`` of a 211px cell is 16.8px, and that product is the diagnosis where a
    bare "16.8px against 16.5px" only says which side of the line it fell on."""
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
        line_height_px=_number(row["line_height_px"]),
        height_px=_number(row["height_px"]),
        rungs=tuple(_text(rung) for rung in _rows(row["rungs"])),
        size=_text(row["size"]),
        fit=_text(row["fit"]),
        font_px=_number(row["font_px"]),
        container_type=_text(row["container_type"]),
        cell_px=_number(row["cell_px"]),
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


@pytest.mark.parametrize(
    "facts", [NARROW_FACTS, ORDINARY_FACTS], ids=["narrow", "ordinary"]
)
@pytest.mark.parametrize("width_px", [1000, 640])
@IN_BOTH_THEMES
@BODIES
@BROWSER_LOOP
async def test_display_numbers_stay_on_one_line_inside_their_cell(
    chromium: Browser,
    template_id: str,
    theme: Theme,
    width_px: int,
    facts: Sequence[Fact],
) -> None:
    """The body's headline numbers never wrap, and they shrink to fit their cell
    rather than overflowing it.

    Every body sizes its display number from the width of the cell it sits in --
    ``.row__head``, ``.chip``, ``.rank__figure`` -- so narrowing the page shrinks the
    type instead of breaking it. 1000px puts a ``stat_grid`` half-column at about
    410px. 640px is where the ``--fit`` cap starts carrying the page on its own:
    without it ``26-84 lb (12-38 kg)`` sets at 76px in a 269px column and breaks
    across three lines.

    Both fact sets, because the row the container widths were chosen for lives in
    only one of them. ``1/900th of its mother`` is what ``.rank__figure``'s basis and
    ``.chips``' column width were sized against, and ``NARROW_FACTS`` does not
    contain it -- so without ``ORDINARY_FACTS`` here the deciding row was measured
    for size and for nothing else: not for wrapping, not for overflow, and not for
    whether its cell is still a query container. That last one is why this test and
    the legibility floor are not interchangeable: with ``container-type: normal`` the
    ``cqw`` cap falls back to the viewport, and the floor then reads 1.636x and
    passes while values wrap onto three lines.
    """
    selectors = BODY_SELECTORS[template_id]
    composition = await compose_cell(
        template_id, theme, make_content(facts=facts), width_px=width_px
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

    assert report.value_count == len(facts), (
        f"{template_id}: {selectors.value!r} matched {report.value_count} elements, "
        f"expected one per fact ({len(facts)}) -- at zero this cell measured "
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
        assert value.line_height_px > 0, (
            f"{template_id}: {value.text!r} resolves its line-height to no length, so "
            "the line count is a division by nothing and reads as zero -- a keyword "
            "line-height has to be measured some other way than height over leading"
        )
        assert value.lines == 1, (
            f"{template_id}: {value.text!r} wrapped onto {value.lines} lines at a "
            f"{width_px}px page ({value.height_px}px of box over a "
            f"{value.line_height_px}px line); a headline figure is meant to shrink to "
            "fit, not break"
        )


@pytest.mark.parametrize("width_px", [1000, 640])
@IN_BOTH_THEMES
@BODIES
@BROWSER_LOOP
async def test_display_numbers_are_no_smaller_than_the_prose_they_headline(
    chromium: Browser, template_id: str, theme: Theme, width_px: int
) -> None:
    """A body's headline figure is never set smaller than the body copy around it.

    1.0x is the whole claim, and it is a fact about hierarchy rather than a taste in
    ratios: a figure that reads smaller than the prose it headlines has inverted the
    page. ``>=`` rather than ``>`` because 1.0x *is* the claim and sub-pixel rounding
    should not be what decides it. Exact equality is not a near miss, though: none of
    the 84 values these twelve cells measure lands on body copy, the closest being
    ``1/900th of its mother`` at 1.018x -- the row ``.rank__figure``'s basis was tuned
    for -- while every known way to land there exactly is a broken page, a renamed
    rung or a dropped unit leaving ``min()`` invalid. That is what the assertion above
    the comparison is for, and it is what gives ``>=`` strict ``>``'s reach without
    moving the threshold.

    Body copy is read off the page, inside a reading-size band rather than against a
    literal 16.5. ``_chrome.css`` says 16.5px today; a test that hardcoded it would
    keep passing if the page's reading size moved out from under the figures, and a
    floor that is purely relative slides down with it -- at 8px body copy both bugs
    commit 5 fixed measure over 1.6x.

    Ordinary content, no images: the fixtures plus the worst-ratio fact from the real
    ``facts.json``, and measurably identical numbers with the panda set carried, so
    this fence buys nothing by paying for it. Both widths matter -- until commit 5
    ``process_flow`` read 0.789x and ``ranked_list`` 0.903x at 640px while both
    cleared the floor comfortably at 1000px.
    """
    selectors = BODY_SELECTORS[template_id]
    composition = await compose_cell(
        template_id, theme, make_content(facts=ORDINARY_FACTS), width_px=width_px
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

    assert report.value_count == len(ORDINARY_FACTS), (
        f"{template_id}: {selectors.value!r} matched {report.value_count} elements, "
        f"expected one per fact ({len(ORDINARY_FACTS)}) -- at zero the comparison "
        "below runs over nothing and this cell passes without measuring a figure"
    )
    assert not report.orphans, (
        f"{template_id}: display numbers with no {selectors.container!r} ancestor are "
        f"left out of the measurement entirely: {list(report.orphans)}"
    )
    assert 14.0 <= report.body_px <= 20.0, (
        f"{template_id}: the page reports {report.body_px}px body copy, outside any "
        "reading size -- _chrome.css sets 16.5px. The floor below is relative, so a "
        "reading size that moved takes the floor with it: at 8px body copy both bugs "
        "commit 5 fixed measure over 1.6x and this fence passes on them"
    )

    for value in report.values:
        assert value.size and value.fit, (
            f"{template_id}: {value.text!r} resolves --size to {value.size!r} and "
            f"--fit to {value.fit!r} (classes {list(value.rungs)}). With either half "
            "missing, min(var(--size), var(--fit)) is invalid at computed-value time "
            f"and the figure silently inherits body copy -- here {value.font_px}px "
            f"against {report.body_px}px prose, which clears the floor below at "
            "exactly 1.0x on a thoroughly broken page"
        )
        assert value.font_px != report.body_px, (
            f"{template_id}: {value.text!r} is set at exactly {value.font_px}px -- the "
            f"page's own body-copy size -- with --size {value.size!r} and --fit "
            f"{value.fit!r} (classes {list(value.rungs)}). "
            "min(var(--size), var(--fit)) is invalid at computed-value time whenever "
            "either half fails to resolve to a length, not only when it is missing: "
            "a renamed rung, a dropped px, a "
            "dropped cqw. font-size then inherits prose and clears the floor below at "
            "exactly 1.0x. No working cell lands on body copy; the closest is 1.018x"
        )
        assert value.font_px >= report.body_px, (
            f"{template_id} in {theme.value} at {width_px}px: {value.text!r} is set at "
            f"{value.font_px}px against {report.body_px}px body copy "
            f"({value.font_px / report.body_px:.3f}x of it), so the figure meant to "
            f"headline the page reads smaller than the prose it headlines "
            f"({value.fit} of a {value.cell_px}px {selectors.container} cell)"
        )


# --------------------------------------------------------------------------- #
# 13. Contrast
# --------------------------------------------------------------------------- #
# ``core.ports`` requires an image's licence and licence URL to be rendered as
# *visible text*. The output is a PNG, so a credit that is technically present but
# illegible discharges nothing -- and until this section there was no contrast
# assertion anywhere in this repo. The palette itself is clean: 0 violations over
# 412 text nodes across three bodies and two themes. Exactly one node failed, and
# it was the one that matters most -- ``figcaption.hero__credit``, the attribution
# burned over the hero photograph.

RGB = tuple[float, float, float]
RGBA = tuple[float, float, float, float]

AA_NORMAL: Final = 4.5
AA_LARGE: Final = 3.0
LARGE_PX: Final = 24.0
LARGE_BOLD_PX: Final = 18.66
BOLD: Final = 700
"""WCAG 2.x large text: 24px, or 18.66px at weight 700 or more. Spelled out rather
than flattened to a comment on a bare 4.5, because the split is the rule -- and
because the colophon's legal text has to land on the strict side of it."""


def _linear(channel: float) -> float:
    """One sRGB channel, 0-255, linearized as WCAG 2.x defines it."""
    ratio = channel / 255
    return ratio / 12.92 if ratio <= 0.03928 else ((ratio + 0.055) / 1.055) ** 2.4


def _luminance(rgb: RGB) -> float:
    red, green, blue = (_linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _ratio(one: RGB, other: RGB) -> float:
    low, high = sorted((_luminance(one), _luminance(other)))
    return (high + 0.05) / (low + 0.05)


def _over(front: RGBA, back: RGB) -> RGB:
    """Source-over composite in sRGB 0-255, which is what a browser paints."""
    alpha = front[3]
    red, green, blue = front[:3]
    return (
        red * alpha + back[0] * (1 - alpha),
        green * alpha + back[1] * (1 - alpha),
        blue * alpha + back[2] * (1 - alpha),
    )


def _hex(rgb: RGB) -> str:
    return "#" + "".join(f"{round(channel):02x}" for channel in rgb)


TEXT_CONTRAST_JS = """
() => {
  const parse = (value) => {
    const inner = value.match(/rgba?\\(([^)]+)\\)/);
    if (!inner) return null;
    const parts = inner[1].split(/[,\\s\\/]+/).filter(Boolean).map(Number);
    return [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1];
  };
  const layersOf = (el) => {
    const layers = [];
    for (let n = el; n; n = n.parentElement) {
      const colour = parse(getComputedStyle(n).backgroundColor);
      if (!colour || colour[3] === 0) continue;
      layers.push(colour);
      if (colour[3] === 1) break;
    }
    return layers;
  };
  const photos = Array.from(document.querySelectorAll('img'))
    .map(img => img.getBoundingClientRect());
  const overPhoto = (r) => photos.some(
    b => b.left < r.right && b.right > r.left && b.top < r.bottom && b.bottom > r.top
  );
  const measured = [];
  outer: for (const el of document.querySelectorAll('*')) {
    const parts = [...el.childNodes].filter(n => n.nodeType === 3 && n.nodeValue.trim());
    if (parts.length === 0) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0 || el.getClientRects().length === 0) continue;
    for (let n = el; n; n = n.parentElement) {
      const s = getComputedStyle(n);
      if (s.display === 'none' || s.contentVisibility === 'hidden') continue outer;
      if (s.visibility === 'hidden' || s.visibility === 'collapse') continue outer;
      if (parseFloat(s.opacity) === 0) continue outer;
    }
    const style = getComputedStyle(el);
    measured.push({
      tag: el.tagName.toLowerCase(),
      classes: el.getAttribute('class') || '',
      fontSizePx: parseFloat(style.fontSize),
      fontWeight: parseInt(style.fontWeight, 10),
      colorRGBA: parse(style.color),
      backgroundLayers: layersOf(el),
      overlapsImage: overPhoto(rect),
      text: parts.map(n => n.nodeValue).join(''),
    });
  }
  return measured;
}
"""
"""Raw measurements only: every ratio, every threshold and every verdict is decided
in Python.

An element is measured when it owns a non-blank text node of its own -- judging by
``textContent`` would blame ``<body>`` for the whole page -- and when it is really
painted: a zero-width or zero-height box, an empty ``getClientRects()``, or
``display:none`` / ``visibility:hidden`` / ``content-visibility:hidden`` /
``opacity:0`` anywhere up the ancestor chain all take it out. That last part is why
the loop is labelled: an ancestor's ``display:none`` has to skip the *element*, not
just the ancestor.

Colours are parsed with a split on ``[,\\s/]+`` rather than on commas. Chromium
emits ``rgb(r, g, b)`` today and newer builds emit ``rgb(r g b / a)``; a
comma-only parser turns the second into one enormous channel.

``overlapsImage`` is rect intersection against every ``<img>`` on the page, and
nothing weaker. "Does an ancestor contain an ``<img>``" over-fires on the dozen
in-flow ``figcaption``s that sit *below* their photograph, and an ancestor
``background-color`` walk on its own is exactly how the first version of this fence
scored the hero credit a clean 15.17:1 against a ``#17150f`` that the photograph
paints straight over."""


@dataclass(frozen=True, slots=True)
class TextNode:
    """One element that owns rendered text, as the browser measured it."""

    tag: str
    classes: tuple[str, ...]
    font_px: float
    weight: int
    color: RGBA
    background_layers: tuple[RGBA, ...]
    """Nearest first, opaque base last. Translucent layers above the base are kept
    so Python can composite them; an empty tuple means nothing opaque was found."""
    overlaps_image: bool
    text: str

    @property
    def name(self) -> str:
        return f"{self.tag}.{'.'.join(self.classes) or '(no class)'}"

    @property
    def large(self) -> bool:
        return self.font_px >= LARGE_PX or (
            self.font_px >= LARGE_BOLD_PX and self.weight >= BOLD
        )

    @property
    def threshold(self) -> float:
        return AA_LARGE if self.large else AA_NORMAL

    @property
    def excerpt(self) -> str:
        return elide(" ".join(self.text.split()))


@dataclass(frozen=True, slots=True)
class Contrast:
    """One text node scored against the background it is actually painted on."""

    node: TextNode
    foreground: RGB
    background: RGB
    ratio: float

    @property
    def passes(self) -> bool:
        return self.ratio >= self.node.threshold


def read_text_nodes(rows: Sequence[object]) -> tuple[TextNode, ...]:
    return tuple(_read_text_node(_fields(row)) for row in rows)


def _read_text_node(row: Mapping[str, object]) -> TextNode:
    return TextNode(
        tag=_text(row["tag"]),
        classes=tuple(_text(row["classes"]).split()),
        font_px=_number(row["fontSizePx"]),
        weight=int(_number(row["fontWeight"])),
        color=_rgba(row["colorRGBA"]),
        background_layers=tuple(
            _rgba(layer) for layer in _rows(row["backgroundLayers"])
        ),
        overlaps_image=_flag(row["overlapsImage"]),
        text=_text(row["text"]),
    )


def _rgba(value: object) -> RGBA:
    parts = _rows(value)
    assert len(parts) == 4, f"expected an r,g,b,a quadruple, measured {value!r}"
    red, green, blue, alpha = (_number(part) for part in parts)
    return red, green, blue, alpha


def _flag(value: object) -> bool:
    assert isinstance(value, bool), f"expected a boolean, measured {value!r}"
    return value


def _flatten(layers: Sequence[RGBA]) -> RGB | None:
    """The layer stack composited down to one opaque colour, or ``None`` if the walk
    reached ``<html>`` without finding an opaque background.

    Currently unexercised: 0 of the 412 measured nodes carry a translucent layer, so
    every stack is a single opaque entry. It is implemented anyway, because a future
    translucent design token would otherwise be scored against the wrong colour
    rather than caught.
    """
    if not layers or layers[-1][3] != 1:
        return None
    base: RGB = layers[-1][:3]
    for layer in reversed(layers[:-1]):
        base = _over(layer, base)
    return base


def score(node: TextNode) -> Contrast:
    """One text node's WCAG 2.x ratio, computed here and not in the browser.

    No opaque background anywhere up the chain is a hard failure and never a skip.
    ``_chrome.css`` sets an opaque background on *both* ``html`` and ``body``, so
    reaching the root means the chrome contract has broken -- and then nothing on the
    page has a defined contrast at all, which is not a thing to pass quietly.
    """
    background = _flatten(node.background_layers)
    assert background is not None, (
        f"{node.name} has no opaque background anywhere between it and <html>, so "
        "nothing on this page has a defined contrast ratio -- _chrome.css sets an "
        "opaque background on both html and body, and reaching the root means that "
        f"chrome contract has broken. Measured layers: {list(node.background_layers)}"
    )
    foreground = _over(node.color, background) if node.color[3] < 1 else node.color[:3]
    return Contrast(
        node=node,
        foreground=foreground,
        background=background,
        ratio=_ratio(foreground, background),
    )


def scored_nodes(nodes: Sequence[TextNode]) -> tuple[Contrast, ...]:
    """Every measured node whose background is resolvable by colour alone.

    Text over a photograph is excluded here and allow-listed by the caller: its
    background is an image, so a colour-only ratio there is not a lenient number but
    a meaningless one. Test B measures those pixels for real.
    """
    return tuple(score(node) for node in nodes if not node.overlaps_image)


def contrast_report(failures: Sequence[Contrast]) -> str:
    """One line per violation. Text is whitespace-collapsed, elided and ``repr``'d --
    it is untrusted scraped-web-shaped content, and ``repr`` is what stops a hostile
    string forging extra lines in the report."""
    return "\n".join(
        f"{contrast.node.name}  {contrast.node.font_px}px/{contrast.node.weight}  "
        f"{_hex(contrast.foreground)} on {_hex(contrast.background)}\n"
        f"  = {contrast.ratio:.2f}:1, needs {contrast.node.threshold}:1 "
        f"({'large' if contrast.node.large else 'normal'} text)\n"
        f"  text: {contrast.node.excerpt!r}"
        for contrast in failures
    )


TEXT_NODE_FLOOR: Final = 50
"""Measured with the fixture below: 66 nodes on ``stat_grid``, 67 on
``process_flow``, 73 on ``ranked_list``, identical in both themes. 50 is loose
enough that dropping a fact or a credit line is not a spurious failure and tight
enough that a selector matching nothing (0) or only the shared chrome (~20) fails
immediately."""

TEXT_OVER_PHOTO: Final = frozenset({"figcaption.hero__credit"})
"""The only text on the page painted over a photograph, and therefore the only node
a colour-only ratio cannot judge. An allowlist and not a silent skip: a second node
here is a new piece of text over an image that nothing is measuring."""

COLOPHON_LEGAL_CLASSES: Final = frozenset(
    {
        "credit__license",
        "credit__work",
        "credit__author",
        "credit__adapted",
        "credit__url",
        "colophon__label",
    }
)
"""The colophon's legally load-bearing text: 10.5-13px at weight 400 throughout, so
every one of these lands on the strict 4.5 arm of the size split. That is the point
of implementing the split rather than commenting a flat 4.5 -- a future rule that
inflated one of these past 24px would otherwise silently relax the attribution to
3:1."""


def contrast_fixture() -> tuple[ResearchContent, tuple[ImageAsset, ...]]:
    """Seven facts and three *modified* panda photographs, at the default 1200px.

    ``modified=True`` is what makes ``.credit__adapted`` render at all
    (``layout.py:_credits_of`` -> ``_caption``/the colophon template), and the real
    ``assets/panda/credits.json`` sets it on all five files. Left at the fixture
    default, this fence would cover none of the one colophon line that ships on every
    real page.
    """
    images = (
        PANDAS[0].as_asset(ImageRole.HERO, modified=True),
        PANDAS[1].as_asset(modified=True),
        PANDAS[2].as_asset(modified=True),
    )
    return make_content(facts=make_facts(7)), images


@IN_BOTH_THEMES
@BODIES
@BROWSER_LOOP
async def test_every_visible_text_node_meets_wcag_aa(
    chromium: Browser, template_id: str, theme: Theme
) -> None:
    """Every word a reader has to read out of the PNG clears WCAG 2.x AA.

    ``core.ports`` requires an image's licence and licence URL to be rendered as
    visible text, and in a PNG "visible" means legible: there is no zoom, no
    selection and no link to fall back on. The threshold follows the real rule --
    3:1 for large text, 4.5:1 for everything else -- and the colophon's legal text
    is asserted to stay on the 4.5 side of it.

    The 3.0 arm rescues nothing today: of 412 measured nodes 94 qualify as large,
    the lowest large ratio is 4.82, and nothing at all measures inside [3.0, 4.5).
    It is documentary here, which is exactly why Test C injects a 3.57:1 probe at
    both sizes -- otherwise the branch is dead code no test would miss.
    """
    content, images = contrast_fixture()
    composition = await compose_cell(template_id, theme, content, images)

    async with laid_out(chromium, composition) as page:
        measured: list[object] = await page.evaluate(TEXT_CONTRAST_JS)

    nodes = read_text_nodes(measured)
    assert len(nodes) >= TEXT_NODE_FLOOR, (
        f"contrast walker measured only {len(nodes)} text nodes on "
        f"{template_id}/{theme.value}; expected at least {TEXT_NODE_FLOOR}. "
        "A walker that selects nothing passes vacuously."
    )

    over_photo = frozenset(node.name for node in nodes if node.overlaps_image)
    assert over_photo == TEXT_OVER_PHOTO, (
        f"{template_id}/{theme.value}: the text nodes painted over an <img> are "
        f"{sorted(over_photo)}, expected {sorted(TEXT_OVER_PHOTO)}. A colour-only "
        "contrast ratio is meaningless over a photograph -- resolving the ancestor "
        "backgrounds of the hero credit scored it 15.17:1 while the photograph's own "
        "pixels measured 2.87:1. Either move this text off the photograph or extend "
        "the canvas "
        "readback in test_the_hero_credit_is_legible_over_every_real_hero_photograph "
        "to cover it; do not add it here."
    )

    scored = scored_nodes(nodes)
    failures = [contrast for contrast in scored if not contrast.passes]
    assert not failures, (
        f"{len(failures)} of {len(scored)} text nodes on {template_id}/{theme.value} "
        f"are below WCAG AA:\n{contrast_report(failures)}"
    )

    adapted = [node for node in nodes if "credit__adapted" in node.classes]
    assert len(adapted) == 3, (
        f"{template_id}/{theme.value} rendered {len(adapted)} .credit__adapted lines, "
        "expected one per displayed photograph (3). At zero the colophon coverage "
        "above has silently evaporated: that line only renders for a credit whose "
        "ImageCredit.modified is true, which every entry in the real credits.json is"
    )

    legal = [
        contrast
        for contrast in scored
        if COLOPHON_LEGAL_CLASSES & set(contrast.node.classes)
    ]
    assert legal, (
        f"{template_id}/{theme.value} measured none of {sorted(COLOPHON_LEGAL_CLASSES)}"
        ", so the attribution this whole section exists for went unjudged"
    )
    relaxed = [contrast.node for contrast in legal if contrast.node.large]
    assert not relaxed, (
        "the colophon's legal text now qualifies as WCAG large text and is only held "
        f"to {AA_LARGE}:1: "
        + ", ".join(
            f"{node.name} at {node.font_px}px/{node.weight}" for node in relaxed
        )
        + ". Attribution has to be transcribed by hand out of the PNG, so it belongs "
        f"on the {AA_NORMAL}:1 arm; a display size does not make small print legible."
    )


CONTRAST_PROBE_JS = """
() => {
  const probe = (variant, px) => {
    const el = document.createElement('p');
    el.className = 'contrast-probe--' + variant;
    el.textContent = 'Injected ' + variant + ' probe at ' + px + 'px';
    el.style.color = '#7d7a70';
    el.style.backgroundColor = '#eceae2';
    el.style.fontSize = px + 'px';
    el.style.fontWeight = '400';
    document.body.appendChild(el);
  };
  probe('normal', 13);
  probe('large', 24);
}
"""
"""Two probes at 3.57:1 -- inside the 3.0-4.5 window -- differing only in size. The
13px one must be reported and the 24px one must not, which is what proves the size
split is a rule and not a comment.

Leak-proof by construction: ``laid_out()`` opens its own page and closes it in a
``finally``, so this injected DOM dies with the page and no parametrized cell can
ever inherit a probe."""

PROBE_VIOLATION: Final = "p.contrast-probe--normal"
PROBE_RATIO: Final = 3.57


@BROWSER_LOOP
async def test_the_contrast_walker_reports_an_injected_illegible_node(
    chromium: Browser,
) -> None:
    """The falsifier for Test A, running the same walker and the same scoring.

    A fence that has never been seen to fail is a fence of unknown strength, and
    this one is measuring a palette that happens to be clean -- 0 violations over
    412 nodes. So an illegible node is injected and the walker has to name it, and
    a *legible-at-that-size* twin is injected beside it and the walker has to leave
    it alone.
    """
    content, images = contrast_fixture()
    composition = await compose_cell("stat_grid", Theme.LIGHT, content, images)

    async with laid_out(chromium, composition) as page:
        clean = read_text_nodes(await page.evaluate(TEXT_CONTRAST_JS))
        await page.evaluate(CONTRAST_PROBE_JS)
        probed = read_text_nodes(await page.evaluate(TEXT_CONTRAST_JS))

    before = [c for c in scored_nodes(clean) if not c.passes]
    assert not before, (
        "the page already fails contrast before anything was injected, so this test "
        f"cannot show that the injection is what the walker caught:\n"
        f"{contrast_report(before)}"
    )
    assert len(probed) == len(clean) + 2, (
        f"the injection moved the measured node count from {len(clean)} to "
        f"{len(probed)}, expected +2: the walker is not seeing the probes at all, so "
        "its silence below would mean nothing"
    )

    after = [c for c in scored_nodes(probed) if not c.passes]
    assert len(after) == 1 and {c.node.name for c in after} == {PROBE_VIOLATION}, (
        f"expected exactly {{{PROBE_VIOLATION!r}}} to be reported, got "
        f"{sorted(c.node.name for c in after)}. Both probes measure 3.57:1; the 24px "
        f"one clears the {AA_LARGE}:1 large-text bar and the 13px one does not, so "
        "reporting both means the size split is not being applied and reporting "
        "neither means the walker cannot see an illegible node at all.\n"
        f"{contrast_report(after)}"
    )
    assert round(after[0].ratio, 2) == PROBE_RATIO, (
        f"the injected probe measured {after[0].ratio:.4f}:1, expected "
        f"{PROBE_RATIO}:1 -- #7d7a70 on #eceae2. A ratio that moved means the "
        "scoring arithmetic changed, and it is the arithmetic Test A depends on"
    )


# --- the hero credit, measured against the real photograph -------------------

HERO_FILENAMES: Final = (
    "giant-panda-cub.jpg",
    "giant-panda-eating-bamboo.jpg",
    "giant-panda-full-body.jpg",
    "giant-panda-in-habitat.jpg",
    "giant-panda-portrait.jpg",
)
"""Every hero photograph that ships. ``PANDAS`` covers three of the five and three
other test modules iterate it, so this fence owns its own tuple rather than growing
that one."""


def _read_panda_credits() -> Mapping[str, Mapping[str, object]]:
    """``assets/panda/credits.json`` by filename. The data is authoritative -- prose
    about these licences drifts, and this fence needs the real caption strings at
    their real lengths, because legibility degrades as a caption wraps."""
    raw: object = json.loads((PANDA_DIR / "credits.json").read_text(encoding="utf-8"))
    return MappingProxyType(
        {_text(_fields(entry)["filename"]): _fields(entry) for entry in _rows(raw)}
    )


PANDA_CREDITS: Final = _read_panda_credits()


def real_hero(filename: str) -> ImageAsset:
    """A hero asset built from ``credits.json``, so the caption the browser lays out
    is the string the real pipeline produces at the length it produces it."""
    entry = PANDA_CREDITS[filename]
    return ImageAsset(
        content=PANDA_DIR / filename,
        mime_type=_text(entry["mime_type"]),
        width_px=int(_number(entry["width"])),
        height_px=int(_number(entry["height"])),
        alt_text=_text(entry["alt_text"]),
        credit=ImageCredit(
            license=_text(entry["license"]),
            author=_text(entry["credit"]),
            license_url=_text(entry["license_url"]),
            source=Source(url=_text(entry["source_url"]), title=_text(entry["title"])),
            modified=_flag(entry["modified"]),
        ),
        role=ImageRole.HERO,
    )


def test_every_hero_photograph_that_ships_is_covered_by_the_readback() -> None:
    """A pure constant check, so a new photograph fails here in microseconds rather
    than by quietly not being measured."""
    on_disk = {path.name for path in PANDA_DIR.glob("*.jpg")}

    assert set(HERO_FILENAMES) == on_disk, (
        f"HERO_FILENAMES and assets/panda/*.jpg have diverged: unmeasured "
        f"{sorted(on_disk - set(HERO_FILENAMES))}, stale "
        f"{sorted(set(HERO_FILENAMES) - on_disk)}. A new hero photograph has to be "
        "proven legible under the scrim before it ships -- three of these five were "
        "illegible under the previous gradient, worst 2.87:1"
    )
    assert set(HERO_FILENAMES) <= set(PANDA_CREDITS), (
        f"no credits.json entry for {sorted(set(HERO_FILENAMES) - set(PANDA_CREDITS))}"
        ", so the caption measured would not be the caption that ships"
    )


HERO_CREDIT_READBACK_JS = """
(config) => {
  const lin = config.linear;
  const luminance = (rgb) =>
    0.2126 * lin[Math.round(rgb[0])] +
    0.7152 * lin[Math.round(rgb[1])] +
    0.0722 * lin[Math.round(rgb[2])];
  const ratioTo = (rgb) => {
    const other = luminance(rgb);
    const hi = Math.max(other, config.foregroundLuminance);
    const lo = Math.min(other, config.foregroundLuminance);
    return (hi + 0.05) / (lo + 0.05);
  };
  const parse = (value) => {
    const inner = value.match(/rgba?\\(([^)]+)\\)/);
    if (!inner) return null;
    const parts = inner[1].split(/[,\\s\\/]+/).filter(Boolean).map(Number);
    return [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1];
  };

  const caption = document.querySelector('.hero__credit');
  const img = document.querySelector('.hero img');
  if (!caption) return {error: 'MISSING: .hero__credit'};
  if (!img) return {error: 'MISSING: .hero img'};

  const style = getComputedStyle(caption);
  if (!style.font) return {error: 'NO FONT SHORTHAND'};
  const text = caption.textContent;
  const captionBox = caption.getBoundingClientRect();
  const box = img.getBoundingClientRect();

  // The scrim, read off the page rather than assumed: a gradient that was edited
  // has to change the numbers below, or reverting the fix would still measure clean.
  const raw = style.backgroundImage;
  if (!raw.startsWith('linear-gradient(')) return {error: 'NOT A GRADIENT: ' + raw};
  const pieces = raw.slice(16, raw.lastIndexOf(')')).split(/,(?![^(]*\\))/)
    .map(piece => piece.trim());
  let downward = true;
  if (/^to\\s/.test(pieces[0])) {
    const direction = pieces.shift();
    if (direction === 'to top') downward = false;
    else if (direction !== 'to bottom') return {error: 'UNMODELLED: ' + direction};
  }
  if (pieces.length !== 2) return {error: 'UNMODELLED: ' + pieces.length + ' stops'};
  const height = captionBox.height;
  const stops = pieces.map((piece, index) => {
    const colour = parse(piece);
    const position = piece.slice(piece.lastIndexOf(')') + 1).trim();
    // A stop written without a position sits at the end of the line it is at:
    // chromium serialises `rgba(...) 0, rgb(...) 24px` verbatim but drops the
    // positions of `rgba(...), rgb(...)` entirely.
    const fraction = position === ''
      ? index
      : (position.endsWith('%')
          ? parseFloat(position) / 100
          : parseFloat(position) / height);
    return {rgb: colour.slice(0, 3), alpha: colour[3], at: fraction};
  });
  if (stops.some(stop => !Number.isFinite(stop.at) || !Number.isFinite(stop.alpha))) {
    return {error: 'UNMODELLED STOPS: ' + raw};
  }
  const ink = stops[0].rgb;
  if (stops.some(stop => stop.rgb.some((c, i) => c !== ink[i]))) {
    return {error: 'TWO-COLOUR SCRIM: ' + raw};
  }
  const alphaAt = (pageY) => {
    const t = downward
      ? (pageY - captionBox.top) / height
      : (captionBox.bottom - pageY) / height;
    if (t <= stops[0].at) return stops[0].alpha;
    if (t >= stops[1].at) return stops[1].alpha;
    const span = stops[1].at - stops[0].at;
    const k = span > 0 ? (t - stops[0].at) / span : 1;
    return stops[0].alpha + (stops[1].alpha - stops[0].alpha) * k;
  };

  // The glyph band: the string's ink box on each line it sets, not its line box and
  // not its padding box.
  const range = document.createRange();
  range.selectNodeContents(caption);
  const lines = Array.from(range.getClientRects());
  const probe = document.createElement('canvas').getContext('2d');
  probe.font = style.font;
  probe.letterSpacing = style.letterSpacing;
  const metrics = probe.measureText(text);

  // The photograph as painted: object-fit: cover with the default 50% 50% origin.
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(box.width);
  canvas.height = Math.round(box.height);
  const ctx = canvas.getContext('2d');
  const nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) return {error: 'IMAGE NOT DECODED'};
  const scale = Math.max(canvas.width / nw, canvas.height / nh);
  const sw = canvas.width / scale, sh = canvas.height / scale;
  ctx.drawImage(img, (nw - sw) / 2, (nh - sh) / 2, sw, sh,
                0, 0, canvas.width, canvas.height);
  let pixels;
  try {
    pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  } catch (error) {
    return {error: 'TAINTED: ' + error.name};
  }

  let worst = {ratio: Infinity, rgb: null};
  let darkest = {lum: Infinity, rgb: null};
  let lightest = {lum: -Infinity, rgb: null};
  let band = 0, below = 0, ratioSum = 0;
  let minAlpha = Infinity, maxAlpha = -Infinity;
  for (const line of lines) {
    const baseline = line.top + metrics.fontBoundingBoxAscent;
    const top = baseline - metrics.actualBoundingBoxAscent;
    const bottom = baseline + metrics.actualBoundingBoxDescent;
    for (let y = Math.ceil(top); y < bottom; y += 1) {
      const alpha = alphaAt(y + 0.5);
      const row = Math.floor(y + 0.5 - box.top);
      if (row < 0 || row >= canvas.height) continue;
      minAlpha = Math.min(minAlpha, alpha);
      maxAlpha = Math.max(maxAlpha, alpha);
      for (let x = Math.ceil(line.left); x < line.right; x += 1) {
        const column = Math.floor(x + 0.5 - box.left);
        if (column < 0 || column >= canvas.width) continue;
        const at = (row * canvas.width + column) * 4;
        const out = [
          pixels[at] * (1 - alpha) + ink[0] * alpha,
          pixels[at + 1] * (1 - alpha) + ink[1] * alpha,
          pixels[at + 2] * (1 - alpha) + ink[2] * alpha,
        ];
        const ratio = ratioTo(out);
        const lum = luminance(out);
        band += 1;
        ratioSum += ratio;
        if (ratio < config.threshold) below += 1;
        if (ratio < worst.ratio) worst = {ratio: ratio, rgb: out};
        if (lum < darkest.lum) darkest = {lum: lum, rgb: out};
        if (lum > lightest.lum) lightest = {lum: lum, rgb: out};
      }
    }
  }
  return {
    error: null,
    lines: lines.length,
    bandPixels: band,
    belowThreshold: below,
    meanRatio: band > 0 ? ratioSum / band : 0,
    minAlpha: band > 0 ? minAlpha : 0,
    maxAlpha: band > 0 ? maxAlpha : 0,
    candidates: [worst.rgb, darkest.rgb, lightest.rgb].filter(Boolean),
    foreground: parse(style.color),
    scrim: ink,
    inkAscent: metrics.actualBoundingBoxAscent,
    inkDescent: metrics.actualBoundingBoxDescent,
    borderTopPx: parseFloat(style.borderTopWidth),
  };
}
"""
"""Numbers out, never image data.

Three candidate pixels come back as plain RGB triples -- the worst ratio, the
darkest and the lightest -- and Python recomputes the asserted ratio from them with
its own arithmetic. The browser needs *a* comparison to argmin over 4-7k pixels, so
it gets the two luminance coefficients and a 256-entry linearization table computed
in Python: the sRGB curve, which is the part that is easy to get wrong, exists once.
Returning three candidates rather than one means even a broken argmin cannot hide a
violation behind a comfortable pixel.

``getImageData`` is wrapped even though ``data:`` URIs do not taint a canvas
(confirmed over 96 cells), so a future change to how images are inlined surfaces as
a ``TAINTED:`` failure rather than as a silent skip."""

LINEAR_TABLE: Final = tuple(_linear(channel) for channel in range(256))


@dataclass(frozen=True, slots=True)
class HeroReadback:
    """What the canvas readback measured over one hero photograph."""

    lines: int
    band_pixels: int
    below_threshold: int
    mean_ratio: float
    min_alpha: float
    max_alpha: float
    candidates: tuple[RGB, ...]
    foreground: RGBA
    scrim: RGB
    ink_ascent: float
    ink_descent: float
    border_top_px: float
    """The gradient paints the *padding* box, which is the border box only while this
    is 0: a border would offset every stop from the box top the alpha model measures
    against, silently."""

    @property
    def worst(self) -> float:
        """The lowest ratio over the whole glyph band, recomputed in Python."""
        return min(_ratio(self.foreground[:3], rgb) for rgb in self.candidates)


def read_hero_readback(measured: Mapping[str, object]) -> HeroReadback:
    error = measured["error"]
    assert error is None, f"the hero credit readback could not run: {error!r}"
    candidates = tuple(_rgb(row) for row in _rows(measured["candidates"]))
    return HeroReadback(
        lines=int(_number(measured["lines"])),
        band_pixels=int(_number(measured["bandPixels"])),
        below_threshold=int(_number(measured["belowThreshold"])),
        mean_ratio=_number(measured["meanRatio"]),
        min_alpha=_number(measured["minAlpha"]),
        max_alpha=_number(measured["maxAlpha"]),
        candidates=candidates,
        foreground=_rgba(measured["foreground"]),
        scrim=_rgb(measured["scrim"]),
        ink_ascent=_number(measured["inkAscent"]),
        ink_descent=_number(measured["inkDescent"]),
        border_top_px=_number(measured["borderTopPx"]),
    )


def _rgb(value: object) -> RGB:
    parts = _rows(value)
    assert len(parts) == 3, f"expected an r,g,b triple, measured {value!r}"
    red, green, blue = (_number(part) for part in parts)
    return red, green, blue


MIN_BAND_PIXELS: Final = 1000
"""Measured 4.3k-7.4k glyph-band pixels per cell. The floor matters more than most:
``min`` over an empty band is infinity, so a band that selected nothing would report
a perfect score."""

HERO_CREDIT_FOREGROUND: Final[RGB] = (0xEE, 0xEA, 0xDE)
"""``_chrome.css``'s ``.hero__credit { color: #eeeade }``. Passed into the readback
only so the browser can argmin over the band; the ratio Python asserts is recomputed
from the colour the page itself reports."""

SCRIM_PEAK_ALPHA: Final = 0.78
"""The alpha ``.hero__credit``'s gradient reaches at its stop, pinned because the
ratios this fence asserts are only safe *for this value*.

Asserting the ratios alone is not enough, which is the whole reason this constant
exists: a flat 0.30 peak still measures 6.16:1 over ``giant-panda-portrait.jpg`` and
would pass, and a flat 1.0 passes on every photograph at a constant 16.748:1 -- the
tautology the readback's docstring argues against. Neither is caught by a threshold,
so the level itself is asserted."""


@pytest.mark.parametrize("filename", HERO_FILENAMES)
@BROWSER_LOOP
async def test_the_hero_credit_is_legible_over_every_real_hero_photograph(
    chromium: Browser, filename: str
) -> None:
    """The attribution burned over the hero photograph, measured against the
    photograph's own pixels.

    This is the only honest measure of text on an image and it is what found the
    bug. Under the previous scrim -- ``to top``, from 0.78 down to 0 -- the worst
    pixel over the glyph band read 4.71, 2.88, 2.87, 3.60 and 8.16 across
    ``HERO_FILENAMES`` at the shipped 1200px default, so three of the five
    photographs that ship were illegible in places. At 640px it read 3.34, 2.61,
    2.28, 2.61 and 6.42: four of five, and worse rather than better, because a ramp
    rising from the bottom of the box leaves every wrapped line further up it.

    The shipped scrim ramps *downward* to its 0.78 peak at 24px, which is the
    caption's own padding-top. The topmost ink pixel sits 27.8px below the box top,
    so the ramp never reaches a glyph: every line of every caption sits at the flat
    peak, and wrapping cannot erode it. Measured 9.0-13.6:1 across these five
    photographs, both themes, all three bodies.

    That the alpha is 0.78 rather than 1.0 is deliberate and is what keeps this test
    honest. Compositing ``#eeeade`` over ``alpha x rgb(8,7,4) + (1-alpha) x white``
    -- the worst photograph that can exist -- clears AA at any alpha >= 0.61, so
    0.78 is guaranteed 8.95:1 against *any* hero and still clears AAA. An opaque
    scrim would instead make this measurement the constant 16.748:1 with zero
    variance, which cannot fail on a contrast regression at all -- only on a
    geometry one. At 0.78 the number moves with the photograph, so it keeps biting.

    Worst pixel, never the mean. The mean measured 5.4-17.0 and read green while
    0.8-37% of the band was illegible.

    One body is enough. ``.hero__credit`` is declared once in ``_chrome.css`` now and
    the hero markup is identical in all three bodies; the worst pixel measured
    identical across all three for all five photographs. Paying 3x for the same
    pixels buys nothing, so the de-duplication itself is fenced instead, by
    ``test_the_hero_scrim_is_declared_once_in_the_chrome``.
    """
    content = make_content(facts=make_facts(7))
    config = {
        "linear": list(LINEAR_TABLE),
        "foregroundLuminance": _luminance(HERO_CREDIT_FOREGROUND),
        "threshold": AA_NORMAL,
    }
    hero = real_hero(filename)

    measured: dict[Theme, HeroReadback] = {}
    for theme in THEMES:
        composition = await compose_cell("stat_grid", theme, content, (hero,))
        async with laid_out(chromium, composition) as page:
            raw: dict[str, object] = await page.evaluate(
                HERO_CREDIT_READBACK_JS, config
            )
        measured[theme] = read_hero_readback(raw)

    for theme, readback in measured.items():
        assert readback.band_pixels >= MIN_BAND_PIXELS, (
            f"{filename} in {theme.value}: the glyph band covers only "
            f"{readback.band_pixels} pixels over {readback.lines} lines, under the "
            f"{MIN_BAND_PIXELS} this fence needs. The worst pixel over an empty band "
            "is infinity, so a band that selected nothing scores perfectly"
        )
        assert readback.worst >= AA_NORMAL, (
            f"the hero credit is illegible over {filename} in {theme.value}: worst "
            f"pixel {readback.worst:.2f}:1 against the {AA_NORMAL}:1 bar, over "
            f"{readback.band_pixels} glyph-band pixels on {readback.lines} lines "
            f"({readback.below_threshold} of them, "
            f"{readback.below_threshold / readback.band_pixels:.1%}, below the bar). "
            f"Mean {readback.mean_ratio:.2f}:1 -- which is why the mean is not what "
            f"is asserted. Scrim alpha over the band ran "
            f"{readback.min_alpha:.3f}-{readback.max_alpha:.3f}; anything at or above "
            f"0.61 makes {_hex(readback.foreground[:3])} readable over the worst "
            "photograph that can exist, so an alpha below that is the bug and a "
            "brighter photograph is not"
        )
        assert readback.min_alpha == readback.max_alpha, (
            f"{filename} in {theme.value}: scrim alpha varies across the glyph band, "
            f"{readback.min_alpha:.3f}-{readback.max_alpha:.3f}. The ramp is supposed "
            "to finish before the text starts -- its stop sits at the caption's "
            "padding-top and the topmost ink is 27.8px below the box top -- so every "
            "glyph should sit at one flat peak. A varying alpha means the ramp now "
            "crosses the glyphs, which is exactly the shipped defect this fence was "
            "written for: the gradient ran to top, spending its peak on the padding "
            "below the text. Raise the stop to padding-top or lower padding-top to "
            "meet it; do not compensate by darkening the peak"
        )
        assert readback.min_alpha == SCRIM_PEAK_ALPHA, (
            f"{filename} in {theme.value}: the scrim's peak is "
            f"{readback.min_alpha:.3f}, not the {SCRIM_PEAK_ALPHA} this fence was "
            "derived against. Both directions need an argument this test cannot make "
            "for you. Lower, and the guarantee goes with it: 0.61 is where "
            f"{_hex(readback.foreground[:3])} stops clearing 4.5:1 against a pure "
            "white photograph, and below that the ratios above only stay green "
            "because these five photographs happen to be dark in the right places. "
            "Higher, and at 1.0 the scrim is opaque, every ratio above collapses to "
            "the constant 16.748:1 with zero variance, and this test can no longer "
            "fail on a contrast regression at all -- only on a geometry one. Re-derive "
            "the guarantee, then move this constant."
        )
        assert readback.border_top_px == 0, (
            f"{filename} in {theme.value}: .hero__credit now has a "
            f"{readback.border_top_px}px top border. The gradient paints the padding "
            "box, which has been the same box as the border box only because this rule "
            "declared no border -- with one, every stop shifts down by the border width "
            "while this readback still measures from the border-box top, so the alpha "
            "it reports is wrong by that much and the flatness above can be satisfied "
            "by a model that no longer matches the paint. Either drop the border or "
            "re-derive the alpha model against the padding box."
        )

    worst_by_theme = {theme: readback.worst for theme, readback in measured.items()}
    light, dark = (worst_by_theme[theme] for theme in THEMES)
    assert light == dark, (
        f"{filename} measures {light:.3f}:1 in light and {dark:.3f}:1 in dark. The "
        "scrim and the credit colour are both hardcoded, so the two themes have to "
        "measure identically -- a themed .hero__credit belongs here, measured, rather "
        "than halving this fence's coverage silently"
    )


# --- the scrim is declared once ---------------------------------------------

CSS_DIR: Final = TEMPLATE_DIR / "css"
CHROME_SHEET: Final = "_chrome.css"
BODY_SHEETS: Final = ("stat_grid.css", "process_flow.css", "ranked_list.css")

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def sheet_declarations(name: str) -> str:
    """One stylesheet with both kinds of comment stripped.

    ``_chrome.css`` names ``.hero img`` in prose explaining why that rule stays
    per-body, so counting selectors in the raw text counts the explanation too.
    """
    source = (CSS_DIR / name).read_text(encoding="utf-8")
    return _CSS_COMMENT.sub(" ", _JINJA_COMMENT.sub(" ", source))


def _selector_count(css: str, selector: str) -> int:
    return len(re.findall(re.escape(selector) + r"\s*[,{]", css))


def test_the_hero_scrim_is_declared_once_in_the_chrome() -> None:
    """``.hero__credit`` used to be three character-identical blocks, one per body
    sheet, and the contrast bug was three copies deep. It now lives once in
    ``_chrome.css``.

    This is the stronger successor to "the three blocks are identical": forking one
    body's scrim fails here, and the failure names the duplication rather than the
    symptom. Read as text, the way ``test_chrome_split.py`` reads templates -- a
    second Jinja ``Environment`` to answer a question about four files would be a
    worse answer than the question deserves.
    """
    in_chrome = _selector_count(sheet_declarations(CHROME_SHEET), ".hero__credit")
    assert in_chrome == 1, (
        f"{CHROME_SHEET} declares .hero__credit {in_chrome} times, expected once: it "
        "is the single scrim every body's hero caption is read against"
    )

    per_body = {
        sheet: _selector_count(sheet_declarations(sheet), ".hero__credit")
        for sheet in BODY_SHEETS
    }
    forked = {sheet: count for sheet, count in per_body.items() if count}
    assert not forked, (
        f".hero__credit is declared per-body again in {forked}. That duplication is "
        "the defect: the same illegible scrim shipped three times, and the canvas "
        "readback only measures one body because the rule is shared. Refine it in "
        f"{CHROME_SHEET} or extend the readback to every body"
    )


def test_the_hero_crop_stays_per_body() -> None:
    """``.hero img { object-fit: cover }`` is deliberately three copies: each body
    crops its hero into its own masthead, and hoisting it would be a different
    change from hoisting the scrim. Pinned so the de-duplication above does not
    quietly grow a second victim."""
    for sheet in BODY_SHEETS:
        css = sheet_declarations(sheet)
        assert _selector_count(css, ".hero img") == 1, (
            f"{sheet} no longer declares its own .hero img rule; the crop is per-body "
            "on purpose"
        )
        assert "object-fit: cover" in css, (
            f"{sheet} declares .hero img but has stopped cropping with "
            "object-fit: cover, so the readback's cover reconstruction no longer "
            "models what the page paints"
        )
    assert _selector_count(sheet_declarations(CHROME_SHEET), ".hero img") == 0, (
        f"{CHROME_SHEET} has taken over .hero img. Hoisting the crop is a separate "
        "change from hoisting the scrim -- make it deliberately, and update this test"
    )


# --------------------------------------------------------------------------- #
# 14. A ranking's figures descend with its ranks
# --------------------------------------------------------------------------- #
# Everything above this section compares one element's computed value against an
# expectation. This asserts an *ordering across* elements, which ~11,600 such
# comparisons structurally cannot see: every figure in a ranked list can hold a
# defensible size of its own while the column as a whole says the wrong thing.
#
# It said the wrong thing for a long time. ``--size`` came off the value's character
# count and ``--fit`` off the same count again, so the real ten-fact content rendered
# rank 1 at 35.26px under rank 2 at 50.00px, and rank 3 at 18.55px under rank 4 at
# 50.00px -- four inversions at 640px and two at 1000 and 1200, in both themes, on the
# content the shipped CLI produces. The page still claimed to be a ranking.

RANK_FIGURE_JS: Final = """
() => Array.from(document.querySelectorAll('.rank')).map((row, index) => {
  const value = row.querySelector('.rank__value');
  const ordinal = row.querySelector('.rank__ordinal');
  const label = row.querySelector('.rank__label');
  if (value === null || ordinal === null || label === null) { return null; }
  const style = getComputedStyle(value);
  return {
    position: index + 1,
    ordinal: ordinal.innerText.replace(/\\s+/g, ' ').trim(),
    text: value.innerText.replace(/\\s+/g, ' ').trim(),
    font_px: parseFloat(style.fontSize),
    size: style.getPropertyValue('--size').trim(),
    fit: value.style.getPropertyValue('--fit').trim(),
    label: label.innerText.replace(/\\s+/g, ' ').trim(),
    label_px: parseFloat(getComputedStyle(label).fontSize),
  };
})
"""
"""Every ``.rank`` in document order, with the figure it prints.

Walked off the rows rather than off ``.rank__value`` directly, so a row that has lost
its value, its ordinal or its label comes back as ``null`` instead of shortening the
sequence:
a row the fence cannot see is a row that cannot break the ordering. ``position`` is
the document index and ``ordinal`` is the number the reader sees, and the assertions
below check they agree -- reading order *is* rank order here, and nothing else on the
page encodes it."""


@dataclass(frozen=True, slots=True)
class RankedFigure:
    """One row's display figure, and the two halves that sized it."""

    position: int
    """1-based document order of the ``.rank`` this came from."""
    ordinal: str
    """The rank the row prints, as text: ``layout`` numbers these, not a CSS counter."""
    text: str
    font_px: float
    size: str
    """The computed ``--size`` rung. Empty means no rule matched, whatever the classes
    say -- and with a ``max()`` floor under the figure that failure is *invisible* to
    an ordering check, because every figure lands on the floor and a flat column is
    non-increasing."""
    fit: str
    """The inline ``--fit`` cap, verbatim."""
    label: str
    """The row's own label -- the name of the thing this figure ranks."""
    label_px: float
    """And the size it is set at, which is the other half of the row's hierarchy."""


def read_ranked_figures(rows: Sequence[object]) -> tuple[RankedFigure, ...]:
    """The browser's JSON, with a missing row a failure rather than a gap."""
    figures: list[RankedFigure] = []
    for index, row in enumerate(rows, start=1):
        assert row is not None, (
            f".rank #{index} of {len(rows)} is missing its value, its ordinal or its "
            "label, so it drops out of the ordering entirely -- and a row this fence "
            "cannot see is a row that cannot break it"
        )
        fields = _fields(row)
        figures.append(
            RankedFigure(
                position=int(_number(fields["position"])),
                ordinal=_text(fields["ordinal"]),
                text=_text(fields["text"]),
                font_px=_number(fields["font_px"]),
                size=_text(fields["size"]),
                fit=_text(fields["fit"]),
                label=_text(fields["label"]),
                label_px=_number(fields["label_px"]),
            )
        )
    return tuple(figures)


async def shipped_panda_content() -> ResearchContent:
    """The content the shipped CLI actually composes, from ``assets/panda/facts.json``.

    Through ``PandaResearcher`` rather than by re-reading the JSON here: it is the
    ``Researcher`` the CLI wires up by default, so this is the content the deliverable
    is made of, and a second transcription of the fixture's shape in a test file would
    be a second thing to keep in step. It is also the only set with enough facts to
    reach the tail of ``layout._RANK_LADDER``.

    Ten facts and three narrative sections, against ``make_content()``'s three and one.
    That difference is not cosmetic for anything that measures the page as a whole:
    ``test_palette_fence`` imports this because a three-fact page is 60% masthead and
    therefore legitimately ink-dominant, which is a fact about the fixture rather than
    about any body.
    """
    return await PandaResearcher().research(make_brief())


async def fixture_ranking_content() -> ResearchContent:
    """``make_content()`` unchanged -- three facts, all values the same length."""
    return make_content()


RANKING_CONTENT: Final[Mapping[str, Callable[[], Awaitable[ResearchContent]]]] = (
    MappingProxyType(
        {"real": shipped_panda_content, "fixture": fixture_ranking_content}
    )
)
"""The two content sets this fence runs over, because they fail differently.

The fixture's three values are all four characters long, so the length-derived sizing
this fence exists to prevent produced *no* inversion on it -- it has only two distinct
sizes to get wrong. Every one of the six inversions was on the real ten-fact set. A
fence validated on the fixture alone would have measured nothing at all, which is why
both are here and why the real one is not optional."""

RANKING_CONTENT_IDS: Final = tuple(RANKING_CONTENT)


def test_the_ranking_fence_runs_over_both_content_sets() -> None:
    """Pinned by content, like the three tables above: an emptied axis is a skip.

    ``RANKING_CONTENT_IDS`` feeds a parametrize axis, so emptying the mapping takes
    all twelve ordering cells with it and reports green. Dropping only ``"real"``
    is the subtler half -- six cells survive, none of them able to see the defect.
    """
    assert set(RANKING_CONTENT) == {"real", "fixture"}, (
        f"RANKING_CONTENT covers {sorted(RANKING_CONTENT)}, not both content sets: "
        "without 'real' the ordering fence keeps its cells and stops being able to "
        "fail, because the fixture's three equal-length values never inverted"
    )
    assert len(RANKING_CONTENT_IDS) == len(RANKING_CONTENT), (
        "RANKING_CONTENT_IDS has gone stale against RANKING_CONTENT"
    )


@pytest.mark.parametrize("width_px", [1200, 1000, 640])
@pytest.mark.parametrize("content_set", RANKING_CONTENT_IDS)
@IN_BOTH_THEMES
@BROWSER_LOOP
async def test_a_ranked_figure_never_grows_as_the_rank_falls(
    chromium: Browser, content_set: str, theme: Theme, width_px: int
) -> None:
    """Down a ranked list, the display figures are non-increasing.

    This is the claim ``ranked_list`` makes by being a *ranking*: position is the
    only thing that encodes standing, so a figure that grows as the rank falls is
    the page contradicting itself. Non-increasing rather than strictly decreasing --
    ties are ordinary, because ``min(--size, --fit)`` shares one cap across a run of
    ranks and ``--size`` has a tail rung.

    It holds by construction rather than by tuning: ``layout._RANK_LADDER`` makes
    ``--size`` descend with the ordinal and ``layout._descending_caps`` makes ``--fit``
    a running minimum, and the minimum of two non-increasing sequences is
    non-increasing. So a red cell here means one of those two stopped descending,
    which is why the failure prints both halves next to the rendered size.

    The second claim is that each figure outranks its own label, and it is here rather
    than in a cell of its own because monotonicity alone cannot see the way it was once
    satisfied: shrinking every figure until the column was flat is non-increasing, and
    so is a column whose tail has sunk under the prose beside it. Both assertions walk
    the same rows, so the pair costs one measurement. This one is also structural --
    ``.rank__label`` is a flat 11px and ``.rank__value``'s floor is 19px, so no content
    and no width can invert it -- which is exactly why it is cheap to hold.

    Three widths and both themes, and neither axis is decoration. No theme changes
    any font size on this page, so the two theme cells are expected to agree
    exactly -- and that agreement is worth having, because it is the assumption every
    other size measurement in this suite quietly relies on. Width is where it gets
    interesting: ``--fit`` is measured in ``cqw`` against a column that is a fixed
    *share* of the page, so a basis that lost its proportionality would show up as
    one width behaving differently from the others.
    """
    content = await RANKING_CONTENT[content_set]()
    composition = await compose_cell(
        "ranked_list", theme, content, width_px=width_px
    )

    async with laid_out(chromium, composition) as page:
        measured = await page.evaluate(RANK_FIGURE_JS)
    figures = read_ranked_figures(_rows(measured))

    where = f"ranked_list/{content_set}/{theme.value} at {width_px}px"
    assert len(figures) == len(content.facts), (
        f"{where}: measured {len(figures)} ranked figures for "
        f"{len(content.facts)} facts -- at zero the ordering below runs over nothing "
        "and this cell passes without looking at a single figure"
    )
    assert figures, f"{where}: the ranking is empty, so there is no order to check"

    printed = [figure.ordinal for figure in figures]
    assert printed == [str(figure.position) for figure in figures], (
        f"{where}: the printed ordinals are {printed}, which is not document order. "
        "Reading order is the only thing that encodes rank here, so if the two can "
        "disagree then 'non-increasing in document order' is the wrong claim"
    )

    unsized = [
        (figure.ordinal, figure.size, figure.fit)
        for figure in figures
        if not (figure.size and figure.fit)
    ]
    assert not unsized, (
        f"{where}: {unsized} resolve --size/--fit to nothing, so "
        f"min(var(--size), var(--fit)) is invalid at computed-value time and every "
        f"figure falls back on `.rank__value`'s max() floor. That is a *flat* column, "
        "which is non-increasing -- this fence would pass on a thoroughly broken page "
        "without this assertion"
    )

    inversions = [
        (above, below)
        for above, below in zip(figures, figures[1:], strict=False)
        if below.font_px > above.font_px
    ]
    assert not inversions, (
        f"{where}: a lower rank is set larger than the rank above it:\n"
        + "\n".join(
            f"  #{above.ordinal} {above.text!r} at {above.font_px}px "
            f"(--size {above.size}, --fit {above.fit}) then "
            f"#{below.ordinal} {below.text!r} at {below.font_px}px "
            f"(--size {below.size}, --fit {below.fit})"
            for above, below in inversions
        )
        + "\nDisplay size is how this body expresses rank, so either --size stopped "
        "descending with the ordinal (layout._RANK_LADDER) or --fit stopped being a "
        "running minimum (layout._descending_caps)"
    )

    outranked = [
        figure for figure in figures if figure.font_px <= figure.label_px
    ]
    assert not outranked, (
        f"{where}: a row's figure is set no larger than its own label:\n"
        + "\n".join(
            f"  #{figure.ordinal} figure {figure.text!r} at {figure.font_px}px "
            f"against label {figure.label!r} at {figure.label_px}px "
            f"({figure.font_px / figure.label_px:.2f}x)"
            for figure in outranked
        )
        + "\nThe ordering above can be perfectly monotone and still say the wrong "
        "thing: what a reader sees first in a ranked row has to be the figure that "
        "ranks it, not the name of the thing ranked. This is the assertion that "
        "stops monotonicity being bought by shrinking the tail into the prose -- "
        "measured at 640px before the row learned to stack, all ten figures came "
        "out 21-29% smaller than the 27px display labels beside them"
    )

    lead, last = figures[0], figures[-1]
    assert lead.font_px > last.font_px, (
        f"{where}: the whole column is set at {lead.font_px}px -- rank "
        f"#{lead.ordinal} and rank #{last.ordinal} are the same size, so the ordering "
        "above holds while size expresses no rank at all. A flat column passes every "
        "non-increasing check ever written"
    )
