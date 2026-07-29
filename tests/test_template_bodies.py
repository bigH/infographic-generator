"""Contract tests for the two newer template bodies and the dispatch that picks one.

``process_flow`` and ``ranked_list`` owe their callers everything ``stat_grid``
already owes them -- a self-contained, escaped, attributed document that renders
every fact and every section it is handed -- plus two things it does not: a
coherent text-only page when there is no image for a slot, and a completed render
when an asset's bytes cannot be read. The parsing helpers and hostile payloads are
reused from :mod:`tests.test_composition` so both paths are held to one standard.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

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
    MARKUP_PAYLOADS,
    PANDAS,
    REMOTE_SCHEMES,
    assert_structurally_valid,
    hostile_inputs,
    make_brief,
    make_content,
    make_facts,
    parse,
)

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
    """A ``Path``-backed asset whose bytes will never arrive, uniquely credited."""
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
# 3. Graceful image degradation
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


@pytest.mark.parametrize("template_id", NEW_TEMPLATE_IDS)
async def test_unreadable_asset_is_skipped_rather_than_failing_the_render(
    template_id: str, tmp_path: Path
) -> None:
    broken = unreadable_asset(tmp_path / "absent.png")
    good = panda_assets(hero=False)

    html = await render(template_id, images=(broken, *good))
    parsed = assert_structurally_valid(html)

    assert broken.credit.license not in parsed.rendered_strings, (
        "an image that was never displayed must not be credited"
    )
    assert broken.credit.author is not None
    assert broken.credit.author not in parsed.rendered_strings
    assert broken.alt_text not in parsed.rendered_strings

    embedded = set(parsed.fetchable_urls)
    for asset in good:
        assert to_data_uri(asset) in embedded, "a readable asset was dropped too"
        assert asset.credit.license in parsed.text, "a displayed asset lost its credit"


async def test_stat_grid_still_propagates_oserror_for_the_same_input(
    tmp_path: Path,
) -> None:
    """The asymmetry is deliberate -- see the note on ``build_page``."""
    broken = unreadable_asset(tmp_path / "absent.png")

    with pytest.raises(OSError):
        await HtmlComposer().compose(make_brief(), make_content(), (broken,))


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
    """Guards the test above against silently testing nothing."""
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
