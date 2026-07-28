from pathlib import Path

import pytest

from infographic_agent.contracts.content import ContentPayload
from infographic_agent.contracts.image_manifest import ImageManifest

CONTENT_FIXTURES = sorted((Path(__file__).parent / "fixtures" / "content").glob("*.json"))
IMAGE_FIXTURES = sorted((Path(__file__).parent / "fixtures" / "images").glob("*.json"))


@pytest.mark.parametrize("path", CONTENT_FIXTURES, ids=lambda p: p.stem)
def test_content_fixture_parses(path: Path) -> None:
    ContentPayload.model_validate_json(path.read_text())


@pytest.mark.parametrize("path", IMAGE_FIXTURES, ids=lambda p: p.stem)
def test_image_fixture_parses(path: Path) -> None:
    ImageManifest.model_validate_json(path.read_text())
