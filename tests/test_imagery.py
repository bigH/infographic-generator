"""Contract tests for the image sourcing stage.

These assert what :class:`~infographic_generator.core.ports.ImageSourcer` promises
its callers -- licensing, honest metadata, bounded dimensions, stable
significance-first ordering -- against the real bytes on disk. No mocks, no
network, and deliberately no image library: the JPEG dimensions are re-derived
here by an independent parser so that a wrong ``width_px`` cannot agree with
itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from infographic_generator.core.models import (
    Brief,
    Fact,
    ImageAsset,
    ImageCredit,
    ImageRole,
    ResearchContent,
    Source,
)
from infographic_generator.core.ports import ImageSourcer
from infographic_generator.imagery import (
    MAX_DIMENSION_PX,
    MAX_IMAGES,
    PandaImageSourcer,
)

ACCEPTED_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/webp"}
)

BRIEF: Final[Brief] = Brief(prompt="an infographic about the giant panda", max_facts=6)

CONTENT: Final[ResearchContent] = ResearchContent(
    title="The Giant Panda",
    subtitle="A bamboo specialist back from the brink",
    summary="Giant pandas eat almost nothing but bamboo and live in the mountain "
    "forests of central China.",
    facts=(
        Fact(
            label="Wild population",
            value="1,864",
            detail="Last full range-wide survey.",
            source=Source(url="https://example.invalid/panda-survey", title="Survey"),
        ),
        Fact(label="Daily bamboo intake", value="12-38", unit="kg"),
    ),
    keywords=("giant panda", "panda eating bamboo", "panda cub", "panda habitat"),
)


# --- an independent JPEG reader -------------------------------------------------
# Deliberately not the implementation's parser: a shared parser would let a bug
# agree with itself and the dimension test would prove nothing.

SOF_MARKERS: Final[frozenset[int]] = frozenset(
    set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
)
STANDALONE_MARKERS: Final[frozenset[int]] = frozenset({0x01, *range(0xD0, 0xDA)})
START_OF_SCAN: Final[int] = 0xDA


@dataclass(frozen=True, slots=True)
class PixelSize:
    width: int
    height: int


def jpeg_size(data: bytes) -> PixelSize:
    """Width and height from the first start-of-frame marker."""
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("not a JPEG: missing SOI marker")
    at = 2
    while at + 1 < len(data):
        if data[at] != 0xFF:
            raise ValueError(f"lost marker alignment at byte {at}")
        marker = data[at + 1]
        if marker == 0xFF:
            at += 1
        elif marker in SOF_MARKERS:
            return PixelSize(
                width=int.from_bytes(data[at + 7 : at + 9], "big"),
                height=int.from_bytes(data[at + 5 : at + 7], "big"),
            )
        elif marker == START_OF_SCAN:
            break
        elif marker in STANDALONE_MARKERS:
            at += 2
        else:
            at += 2 + int.from_bytes(data[at + 2 : at + 4], "big")
    raise ValueError("no start-of-frame marker before the scan data")


def sniff_mime(data: bytes) -> str | None:
    """The media type the bytes themselves claim, ignoring any declaration."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# --- reading assets without an ``Any`` in sight ---------------------------------


def path_of(asset: ImageAsset) -> Path | None:
    content = asset.content
    return content if isinstance(content, Path) else None


def bytes_of(asset: ImageAsset) -> bytes:
    content = asset.content
    return content if isinstance(content, bytes) else content.read_bytes()


def image_paths(assets: Sequence[ImageAsset]) -> Sequence[Path]:
    return [path for path in map(path_of, assets) if path is not None]


def label(index: int, asset: ImageAsset) -> str:
    path = path_of(asset)
    return f"{index}:{path.name if path else asset.mime_type}"


def labelled(assets: Sequence[ImageAsset]) -> Sequence[tuple[str, ImageAsset]]:
    """Pair each asset with a stable, unique name so failures read as a diff."""
    return [(label(index, asset), asset) for index, asset in enumerate(assets)]


def requires_attribution(license_id: str) -> bool:
    """True for CC BY / CC BY-SA in any spelling; false for CC0 and public domain."""
    return "cc by" in license_id.strip().lower().replace("-", " ").replace("_", " ")


def missing_attribution_fields(credit: ImageCredit) -> tuple[str, ...]:
    source = credit.source
    required: Sequence[tuple[str, str | None]] = (
        ("author", credit.author),
        ("license_url", credit.license_url),
        ("source.url", source.url if source else None),
        ("source.title", source.title if source else None),
    )
    return tuple(name for name, value in required if not (value or "").strip())


@pytest.fixture(scope="module")
def sourced_images() -> Sequence[ImageAsset]:
    return asyncio.run(PandaImageSourcer().source_images(BRIEF, CONTENT))


# --- the port ------------------------------------------------------------------


def test_the_panda_sourcer_structurally_satisfies_the_image_sourcer_port() -> None:
    sourcer: ImageSourcer = PandaImageSourcer()
    assert callable(sourcer.source_images)


async def test_source_images_returns_a_materialised_sequence() -> None:
    images = await PandaImageSourcer().source_images(BRIEF, CONTENT)

    assert isinstance(images, Sequence)
    assert not isinstance(images, Iterator)
    assert list(images) == list(images)


def test_documented_limits_match_the_stage_contract() -> None:
    assert MAX_DIMENSION_PX == 2000
    assert MAX_IMAGES == 6


# --- how many, and in what order -----------------------------------------------


def test_asset_count_stays_within_the_documented_bound(
    sourced_images: Sequence[ImageAsset],
) -> None:
    assert 0 <= len(sourced_images) <= MAX_IMAGES


def test_the_panda_stub_actually_yields_images(
    sourced_images: Sequence[ImageAsset],
) -> None:
    assert len(sourced_images) > 0


def test_the_lead_asset_is_the_hero(sourced_images: Sequence[ImageAsset]) -> None:
    assert sourced_images[0].role is ImageRole.HERO


def test_at_most_one_asset_claims_the_hero_role(
    sourced_images: Sequence[ImageAsset],
) -> None:
    assert sourced_images, "no assets to walk makes the bound below vacuous"

    heroes = [
        name
        for name, asset in labelled(sourced_images)
        if asset.role is ImageRole.HERO
    ]
    assert len(heroes) <= 1


def test_every_role_is_a_real_image_role(sourced_images: Sequence[ImageAsset]) -> None:
    strays = {
        name: asset.role
        for name, asset in labelled(sourced_images)
        if not isinstance(asset.role, ImageRole)
    }
    assert strays == {}


async def test_ordering_is_stable_across_calls() -> None:
    first = tuple(await PandaImageSourcer().source_images(BRIEF, CONTENT))
    second = tuple(await PandaImageSourcer().source_images(BRIEF, CONTENT))

    assert first == second


# --- content the composer can actually open -------------------------------------


def test_path_backed_content_is_absolute_and_points_at_real_bytes(
    sourced_images: Sequence[ImageAsset],
) -> None:
    paths = image_paths(sourced_images)
    assert paths, "the panda stub is expected to hand back local files"

    assert [str(path) for path in paths if not path.is_absolute()] == []
    sizes = {
        str(path): path.stat().st_size if path.is_file() else None for path in paths
    }
    assert [name for name, size in sizes.items() if not size] == []


async def test_paths_still_resolve_from_a_different_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = tuple(await PandaImageSourcer().source_images(BRIEF, CONTENT))

    monkeypatch.chdir(tmp_path)
    relocated = tuple(await PandaImageSourcer().source_images(BRIEF, CONTENT))

    assert relocated == baseline
    paths = image_paths(relocated)
    assert paths
    assert [str(path) for path in paths if not path.is_absolute()] == []
    assert [str(path) for path in paths if not path.is_file()] == []


# --- metadata that describes the real bytes -------------------------------------


def test_every_asset_carries_a_licence_and_alt_text(
    sourced_images: Sequence[ImageAsset],
) -> None:
    blanks = {
        name: (asset.credit.license, asset.alt_text)
        for name, asset in labelled(sourced_images)
        if not asset.credit.license.strip() or not asset.alt_text.strip()
    }
    assert blanks == {}


def test_declared_dimensions_match_the_encoded_image(
    sourced_images: Sequence[ImageAsset],
) -> None:
    declared = {
        name: PixelSize(asset.width_px, asset.height_px)
        for name, asset in labelled(sourced_images)
    }
    actual = {
        name: jpeg_size(bytes_of(asset)) for name, asset in labelled(sourced_images)
    }
    assert declared == actual


def test_no_asset_exceeds_the_maximum_dimension(
    sourced_images: Sequence[ImageAsset],
) -> None:
    oversized = {
        name: (asset.width_px, asset.height_px)
        for name, asset in labelled(sourced_images)
        if max(asset.width_px, asset.height_px) > MAX_DIMENSION_PX
    }
    assert oversized == {}


def test_mime_type_matches_the_magic_number_of_the_real_bytes(
    sourced_images: Sequence[ImageAsset],
) -> None:
    declared = {name: asset.mime_type for name, asset in labelled(sourced_images)}
    sniffed = {
        name: sniff_mime(bytes_of(asset)) for name, asset in labelled(sourced_images)
    }
    assert declared == sniffed


def test_only_accepted_raster_types_are_offered(
    sourced_images: Sequence[ImageAsset],
) -> None:
    rejected = {
        name: asset.mime_type
        for name, asset in labelled(sourced_images)
        if asset.mime_type not in ACCEPTED_MIME_TYPES
    }
    assert rejected == {}


def test_svg_is_never_offered(sourced_images: Sequence[ImageAsset]) -> None:
    svg_like = {
        name: asset.mime_type
        for name, asset in labelled(sourced_images)
        if "svg" in asset.mime_type.lower() or bytes_of(asset).lstrip()[:1] == b"<"
    }
    assert svg_like == {}


# --- attribution ----------------------------------------------------------------


def test_attribution_licences_carry_everything_attribution_requires(
    sourced_images: Sequence[ImageAsset],
) -> None:
    attributed = [
        (name, asset.credit)
        for name, asset in labelled(sourced_images)
        if requires_attribution(asset.credit.license)
    ]
    assert attributed, "the panda pool should exercise CC BY / CC BY-SA attribution"

    gaps = {name: missing_attribution_fields(credit) for name, credit in attributed}
    assert {name: missing for name, missing in gaps.items() if missing} == {}
