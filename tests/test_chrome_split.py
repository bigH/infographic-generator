"""Guards on the chrome/body template split.

``_base.html.j2`` carries the document shell; each layout extends it. These tests
pin the invariants that split is only safe under: the default composer and the
registry's ``stat_grid`` name are the same page, the base is a partial rather than
a page, and the split did not smuggle in a second Jinja2 environment or an escape
bypass along the way.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from infographic_generator.composition import HtmlComposer
from infographic_generator.composition.composer import TEMPLATE_DIR, TEMPLATE_NAME
from infographic_generator.composition.registry import TEMPLATE_REGISTRY
from infographic_generator.core.models import Brief, ImageAsset, ResearchContent

from tests.test_composition import (
    PANDAS,
    make_brief,
    make_content,
    make_facts,
    parse,
)

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
STAT_GRID_TEMPLATE = "stat_grid.html.j2"
BASE_TEMPLATE = "_base.html.j2"

ESCAPE_BYPASSES = ("|safe", "| safe", "Markup", "autoescape false", "autoescape False")
REMOTE_SCHEMES = ("http://", "https://", "//")


def python_sources() -> Iterator[Path]:
    return SRC_ROOT.rglob("*.py")


def template_sources() -> Iterator[Path]:
    yield from TEMPLATE_DIR.rglob("*.j2")
    yield from TEMPLATE_DIR.rglob("*.css")


def full_inputs() -> tuple[Brief, ResearchContent, tuple[ImageAsset, ...]]:
    """Enough content to exercise every region of both templates."""
    content = make_content(facts=make_facts(8))
    images = tuple(panda.as_asset() for panda in PANDAS)
    return make_brief(), content, images


# --------------------------------------------------------------------------- #
# 1. The default composer and the registry name are the same page
# --------------------------------------------------------------------------- #


async def test_default_template_is_the_registry_stat_grid_name() -> None:
    assert TEMPLATE_NAME == TEMPLATE_REGISTRY["stat_grid"].template_name


async def test_default_and_explicit_stat_grid_render_identical_html() -> None:
    brief, content, images = full_inputs()

    default = await HtmlComposer().compose(brief, content, images)
    explicit = await HtmlComposer(template_name=STAT_GRID_TEMPLATE).compose(
        brief, content, images
    )

    assert default.html == explicit.html
    assert default.title == explicit.title


async def test_stat_grid_renders_without_images_too() -> None:
    composition = await HtmlComposer(template_name=STAT_GRID_TEMPLATE).compose(
        make_brief(), make_content(), ()
    )

    assert composition.html.lstrip().lower().startswith("<!doctype html")


# --------------------------------------------------------------------------- #
# 2. The base is a partial, not a page
# --------------------------------------------------------------------------- #


def test_the_base_template_is_marked_as_a_partial_and_registered_nowhere() -> None:
    assert (TEMPLATE_DIR / BASE_TEMPLATE).is_file()
    assert BASE_TEMPLATE.startswith("_"), "a partial is named with a leading underscore"
    registered = {spec.template_name for spec in TEMPLATE_REGISTRY.values()}
    assert BASE_TEMPLATE not in registered
    assert not any(name.startswith("_") for name in registered), (
        "no registry entry may point at a partial"
    )


async def test_the_base_template_alone_is_not_a_usable_page() -> None:
    """Rendered directly it drops the entire body -- so nothing should do that."""
    brief, content, images = full_inputs()

    chrome_only = await HtmlComposer(template_name=BASE_TEMPLATE).compose(
        brief, content, images
    )
    whole_page = await HtmlComposer().compose(brief, content, images)

    chrome_text = parse(chrome_only.html).text
    for fact in content.facts:
        assert fact.value not in chrome_text, "the base rendered body content"
    for section in content.sections:
        assert section.body not in chrome_text, "the base rendered body content"
    assert content.summary not in chrome_text, "the base rendered the lede"
    assert len(chrome_only.html) < len(whole_page.html)


# --------------------------------------------------------------------------- #
# 3. One environment, no escape bypass
# --------------------------------------------------------------------------- #


def test_exactly_one_jinja_environment_is_constructed_under_src() -> None:
    constructions = [
        (path, line)
        for path in python_sources()
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"(?<!`)\bEnvironment\(", line)
    ]

    assert len(constructions) == 1, f"expected one Environment(...), got {constructions}"
    path, _ = constructions[0]
    assert path.name == "composer.py"


def test_the_one_environment_still_autoescapes_strictly() -> None:
    source = (SRC_ROOT / "infographic_generator" / "composition" / "composer.py").read_text(
        encoding="utf-8"
    )

    for setting in (
        "autoescape=True",
        "undefined=StrictUndefined",
        "trim_blocks=True",
        "lstrip_blocks=True",
        "keep_trailing_newline=True",
    ):
        assert setting in source, f"the environment lost {setting}"


@pytest.mark.parametrize("bypass", ESCAPE_BYPASSES)
def test_no_escape_bypass_anywhere_under_src(bypass: str) -> None:
    scanned = (*python_sources(), *template_sources())
    assert len(scanned) >= 28, f"the walk collapsed: {len(scanned)} files, 32 today"
    assert {"composer.py", "_base.html.j2", "_chrome.css"} <= {p.name for p in scanned}, (
        f"a glob resolved to nothing: {sorted(path.name for path in scanned)}"
    )

    offenders = [path for path in scanned if bypass in path.read_text(encoding="utf-8")]

    assert not offenders, f"{bypass!r} appears in {offenders}"


# --------------------------------------------------------------------------- #
# 4. Still one self-contained document
# --------------------------------------------------------------------------- #


async def test_the_split_document_has_no_external_subresources() -> None:
    brief, content, images = full_inputs()

    composition = await HtmlComposer().compose(brief, content, images)
    parsed = parse(composition.html)

    assert not parsed.tagged("link"), "<link> pulls in an external resource"
    assert not parsed.tagged("script"), "the document has no scripts at all"
    for url in parsed.fetchable_urls:
        assert not url.lower().startswith(REMOTE_SCHEMES), f"remote URL: {url!r}"
    for url in parsed.css_urls:
        assert url.startswith("data:"), f"CSS url() must be a data URI, got {url!r}"
    assert "@import" not in composition.html.lower()
    assert any(url.startswith("data:") for url in parsed.fetchable_urls)


async def test_the_stylesheet_is_one_inline_style_element() -> None:
    brief, content, images = full_inputs()

    composition = await HtmlComposer().compose(brief, content, images)
    parsed = parse(composition.html)

    assert len(parsed.tagged("style")) == 1, "chrome and body CSS must share one <style>"
    for marker in (":root {", ".masthead {", ".ledger {", ".colophon {"):
        assert marker in parsed.css, f"missing CSS from the split: {marker}"


# --------------------------------------------------------------------------- #
# 5. Attribution follows what the body displays
# --------------------------------------------------------------------------- #


async def test_credits_track_displayed_figures_without_duplicates() -> None:
    brief, content, images = full_inputs()

    composition = await HtmlComposer().compose(brief, content, images)
    parsed = parse(composition.html)

    licences = [panda.license_text for panda in PANDAS]
    assert licences, "no licences to walk makes the loop below vacuous"
    assert len(parsed.classed("credit")) == len(images), (
        "one colophon row per displayed figure -- the general relation is "
        "min(len(images), 1 + _BAND_CAPACITY) = min(len(images), 4), which equals "
        f"len(images) only while full_inputs() stays small (3 today, {len(images)} here)"
    )
    for licence in licences:
        assert parsed.text_in("colophon").count(licence) == 1, (
            f"the colophon must credit {licence} exactly once"
        )
        assert parsed.text.count(licence) == 2, (
            f"{licence} belongs in one figure caption and one colophon row, nowhere else"
        )
