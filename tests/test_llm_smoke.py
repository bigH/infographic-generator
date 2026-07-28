from pathlib import Path

import pytest

from infographic_agent.contracts.content import ContentPayload
from infographic_agent.contracts.image_manifest import ImageManifest
from infographic_agent.pipeline import run
from infographic_agent.steps.map_content import map_content
from infographic_agent.steps.select_template import select_template
from infographic_agent.templates.registry import TEMPLATE_REGISTRY

_CONTENT_DIR = Path(__file__).parent / "fixtures" / "content"
_IMAGES_DIR = Path(__file__).parent / "fixtures" / "images"
_OUT = Path(__file__).parent / "fixtures" / "renders"


def _load(name: str) -> tuple[ContentPayload, ImageManifest]:
    content = ContentPayload.model_validate_json((_CONTENT_DIR / f"{name}.json").read_text())
    manifest = ImageManifest.model_validate_json((_IMAGES_DIR / f"{name}.json").read_text())
    return content, manifest


@pytest.mark.llm
@pytest.mark.parametrize("name", ["stat_grid", "timeline", "comparison"])
def test_select_template_picks_a_valid_template(name):
    content, _ = _load(name)
    selection = select_template(content)
    assert selection.template_id in TEMPLATE_REGISTRY
    assert 0 <= selection.confidence <= 1
    assert selection.rationale


@pytest.mark.llm
def test_map_content_fills_the_selected_templates_schema():
    content, manifest = _load("timeline")
    selection = select_template(content)
    mapping_result = map_content(content, selection.template_id, manifest)

    expected_model = TEMPLATE_REGISTRY[selection.template_id].slot_model
    assert isinstance(mapping_result.slots, expected_model)
    assert mapping_result.template_id == selection.template_id


@pytest.mark.llm
def test_full_pipeline_produces_a_real_png():
    content, manifest = _load("quote_spotlight")
    png_bytes = run(content, manifest)

    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    _OUT.mkdir(parents=True, exist_ok=True)
    (_OUT / "llm_smoke_quote_spotlight.png").write_bytes(png_bytes)
