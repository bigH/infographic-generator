"""Contract tests for the composition stage (``core.ports.Composer``).

Everything here is an outcome the ``Composer`` docstring promises: a
self-contained document, escaped untrusted text, honoured render options,
visible attribution, and every fact on the page. The HTML is parsed with the
stdlib rather than pattern-matched, and the escaping test is confirmed a second
time inside a real chromium DOM.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import pytest

from infographic_generator.composition import HtmlComposer
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
        self._open: list[str] = []

    def handle_decl(self, decl: str) -> None:
        self.doctypes.append(decl)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, {name: value or "" for name, value in attrs}))
        if tag not in VOID_ELEMENTS:
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self._open:
            return
        while self._open and self._open.pop() != tag:
            pass

    def handle_data(self, data: str) -> None:
        match self._open[-1] if self._open else "":
            case "style":
                self.style_chunks.append(data)
            case "script":
                pass
            case "title":
                self.title_chunks.append(data)
            case _:
                self.text_chunks.append(data)


@dataclass(frozen=True, slots=True)
class ParsedHtml:
    """A document reduced to what the assertions below care about."""

    tags: tuple[tuple[str, Mapping[str, str]], ...]
    doctypes: tuple[str, ...]
    text: str
    css: str
    titles: tuple[str, ...]

    @property
    def title_count(self) -> int:
        return sum(1 for tag, _ in self.tags if tag == "title")

    def tagged(self, name: str) -> tuple[Mapping[str, str], ...]:
        return tuple(attrs for tag, attrs in self.tags if tag == name)

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
    )


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
    return tuple(
        Fact(
            label=f"Bamboo metric {index}",
            value=f"{index}7.5",
            unit="kg",
            detail=f"Measured across {index} reserves",
            source=make_source(url=f"https://example.org/study-{index}"),
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

    def as_asset(self, role: ImageRole = ImageRole.SUPPORTING) -> ImageAsset:
        return ImageAsset(
            content=PANDA_DIR / self.filename,
            mime_type="image/jpeg",
            width_px=1600,
            height_px=1066,
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


async def test_payloads_are_inert_text_in_a_real_browser_dom() -> None:
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - playwright is a declared dependency
        pytest.skip("playwright is not installed")

    content, images = hostile_inputs()
    composition = await HtmlComposer().compose(make_brief(), content, images)

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except PlaywrightError as error:  # pragma: no cover - browser not installed
            pytest.skip(f"chromium is unavailable: {error}")
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(composition.html, wait_until="load")

            scripts: int = await page.evaluate(
                "document.querySelectorAll('script').length"
            )
            on_error: int = await page.evaluate(
                "document.querySelectorAll('[onerror]').length"
            )
            injected_img: int = await page.evaluate(
                "document.querySelectorAll('img[src=\"x\"]').length"
            )
            body_text: str = await page.evaluate("document.body.innerText")
        finally:
            await browser.close()

    assert scripts == 0, "a <script> element made it into the DOM"
    assert on_error == 0, "an element with an onerror handler made it into the DOM"
    assert injected_img == 0, "the injected <img src=x> made it into the DOM"

    for payload in MARKUP_PAYLOADS:
        assert payload in body_text, f"payload should be visible as text: {payload!r}"


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
