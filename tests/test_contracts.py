"""Smoke tests for the core contracts: defaults, immutability, encoding, ports.

No stage implementations and no domain content -- this file only asserts what
:mod:`infographic_generator.core` promises its callers.
"""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Sequence
from pathlib import Path

import pytest

from infographic_generator.core.encoding import data_uri, image_bytes, to_data_uri
from infographic_generator.core.models import (
    Brief,
    Composition,
    Fact,
    ImageAsset,
    ImageCredit,
    ImageRole,
    NarrativeSection,
    RenderOptions,
    RenderResult,
    ResearchContent,
    Source,
    Theme,
)
from infographic_generator.core.ports import Composer, ImageSourcer, Renderer, Researcher

PAYLOAD = b"\x89PNG\r\n\x1a\n"


def make_asset(content: bytes | Path) -> ImageAsset:
    return ImageAsset(
        content=content,
        mime_type="image/png",
        width_px=1,
        height_px=1,
        alt_text="a pixel",
        credit=ImageCredit(license="CC0-1.0"),
    )


def test_required_fields_are_enough_to_construct_every_model() -> None:
    source = Source(url="https://example.invalid/a")
    assert (source.title, source.publisher, source.retrieved_at) == (None, None, None)

    fact = Fact(label="label", value="1")
    assert (fact.unit, fact.detail, fact.source) == (None, None, None)

    section = NarrativeSection(heading="h", body="b")
    assert section.sources == ()

    content = ResearchContent(title="t", subtitle="s", summary="m")
    assert (content.facts, content.sections, content.keywords, content.sources) == (
        (),
        (),
        (),
        (),
    )

    result = RenderResult(
        output_path=Path("/tmp/out.png"), width_px=2, height_px=2, bytes_written=1
    )
    assert result.bytes_written == 1


def test_documented_defaults() -> None:
    options = RenderOptions()
    assert options.width_px == 1200
    assert options.height_px is None
    assert options.theme is Theme.LIGHT
    assert options.device_scale_factor == 2.0

    brief = Brief(prompt="p")
    assert brief.options == options
    assert brief.locale == "en-US"
    assert brief.extras == {}
    assert brief.max_facts is None

    credit = ImageCredit(license="CC-BY-4.0")
    assert credit.modified is False
    assert (credit.author, credit.license_url, credit.source) == (None, None, None)

    assert make_asset(PAYLOAD).role is ImageRole.SUPPORTING

    composition = Composition(html="<main></main>", width_px=1200)
    assert composition.height_px is None
    assert composition.device_scale_factor == 2.0
    assert composition.title == ""


def test_models_are_frozen() -> None:
    brief = Brief(prompt="p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        brief.prompt = "other"  # type: ignore[misc]


def test_to_data_uri_round_trips_both_content_variants(tmp_path: Path) -> None:
    path = tmp_path / "pixel.png"
    path.write_bytes(PAYLOAD)

    from_bytes = to_data_uri(make_asset(PAYLOAD))
    from_path = to_data_uri(make_asset(path))

    assert from_bytes == from_path
    prefix = "data:image/png;base64,"
    assert from_bytes.startswith(prefix)
    assert base64.b64decode(from_bytes.removeprefix(prefix)) == PAYLOAD


@pytest.mark.parametrize(
    "mime_type",
    ['image/png" onerror="alert(1)', "image/png;charset=utf-8", "image/png<script>"],
)
def test_data_uri_rejects_unsafe_mime_types(mime_type: str) -> None:
    with pytest.raises(ValueError, match="unsafe mime type"):
        data_uri(mime_type, PAYLOAD)


def test_image_bytes_raises_for_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        image_bytes(make_asset(tmp_path / "absent.png"))


class NullStage:
    """Minimal structural implementation of all four ports."""

    async def research(self, brief: Brief) -> ResearchContent:
        return ResearchContent(title="", subtitle="", summary="")

    async def source_images(
        self, brief: Brief, content: ResearchContent
    ) -> Sequence[ImageAsset]:
        return ()

    async def compose(
        self, brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
    ) -> Composition:
        return Composition(html="", width_px=brief.options.width_px)

    async def render(
        self, composition: Composition, output_path: Path
    ) -> RenderResult:
        return RenderResult(
            output_path=output_path, width_px=0, height_px=0, bytes_written=0
        )


async def test_a_trivial_stage_satisfies_every_port() -> None:
    stage = NullStage()
    researcher: Researcher = stage
    sourcer: ImageSourcer = stage
    composer: Composer = stage
    renderer: Renderer = stage

    brief = Brief(prompt="p")
    content = await researcher.research(brief)
    images = await sourcer.source_images(brief, content)
    composition = await composer.compose(brief, content, images)
    result = await renderer.render(composition, Path("/tmp/out.png"))

    assert composition.width_px == brief.options.width_px
    assert result.output_path == Path("/tmp/out.png")
