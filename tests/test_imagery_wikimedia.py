"""Tests for the Wikimedia image sourcer.

Only the transport is faked. A real :class:`httpx.AsyncClient` runs against
:class:`httpx.MockTransport`, so URL building, query parameters, headers, JSON
decoding and error handling are all exercised for real -- and the image bytes are
real JPEGs and PNGs that Pillow genuinely decodes, resizes and re-encodes.

Two things about the fixtures are deliberate:

* Test images are 8x8 random-greyscale patterns scaled up, one pattern per
  ``seed``. A flat-colour image has every pixel equal to its own mean, so all
  flat images share one average hash and would be taken for duplicates of each
  other. Distinct seeds give genuinely distinct fingerprints.
* Pages built with :func:`relevant_page` describe themselves in terms of the
  query, the way real Commons files do. The sourcer drops candidates that do not
  mention enough of the keyword, so a page needs a plausible description
  whenever relevance is not the thing under test.
"""

from __future__ import annotations

import io
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import httpx
import pytest
from PIL import Image

from infographic_generator.core.models import (
    Brief,
    ImageAsset,
    ImageRole,
    ResearchContent,
)
from infographic_generator.core.ports import ImageSourcer
from infographic_generator.imagery import WikimediaImageSourcer, WikimediaSettings
from infographic_generator.imagery.licensing import (
    normalize_license,
    read_credit,
    strip_markup,
)
from infographic_generator.imagery.prepare import (
    fingerprint,
    hamming_distance,
    prepare,
)

ENDPOINT = "https://commons.wikimedia.org/w/api.php"
BLOCKED_TITLE = "File:Panda velká.jpg"


# --------------------------------------------------------------------------- #
# Fixtures: real image bytes                                                  #
# --------------------------------------------------------------------------- #


def pattern_image(width: int, height: int, *, seed: int) -> Image.Image:
    """An 8x8 greyscale pattern from ``seed``, upscaled -- distinct per seed."""
    rng = random.Random(seed)
    small = Image.new("L", (8, 8))
    small.putdata([rng.randrange(256) for _ in range(64)])
    scaled = small.resize((width, height), Image.Resampling.NEAREST).convert("RGB")
    small.close()
    return scaled


def jpeg_bytes(width: int, height: int, *, seed: int = 1, quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    with pattern_image(width, height, seed=seed) as image:
        image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def png_bytes(width: int, height: int, *, seed: int = 1, alpha: int = 255) -> bytes:
    buffer = io.BytesIO()
    with pattern_image(width, height, seed=seed) as image:
        rgba = image.convert("RGBA")
        rgba.putalpha(alpha)
        rgba.save(buffer, format="PNG")
        rgba.close()
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Fixtures: a fake Commons                                                    #
# --------------------------------------------------------------------------- #


def commons_page(
    page_id: int,
    title: str,
    *,
    download_url: str,
    index: int = 1,
    license_id: str | None = "cc-by-sa-4.0",
    short_name: str | None = None,
    artist: str | None = "Gzen92",
    attribution_required: str = "true",
    restrictions: str | None = None,
    description: str | None = None,
    mime_type: str = "image/jpeg",
) -> dict[str, object]:
    """One page as ``generator=search`` + ``prop=imageinfo`` reports it."""
    metadata: dict[str, dict[str, str]] = {}
    if license_id is not None:
        metadata["License"] = {"value": license_id}
    if short_name is not None:
        metadata["LicenseShortName"] = {"value": short_name}
    if artist is not None:
        metadata["Artist"] = {"value": artist}
    if restrictions is not None:
        metadata["Restrictions"] = {"value": restrictions}
    if description is not None:
        metadata["ImageDescription"] = {"value": description}
    metadata["AttributionRequired"] = {"value": attribution_required}
    metadata["LicenseUrl"] = {"value": "https://creativecommons.org/licenses/by-sa/4.0/"}

    quoted = title.replace(" ", "_")
    return {
        "pageid": page_id,
        "ns": 6,
        "title": title,
        "index": index,
        "imageinfo": [
            {
                "url": f"https://upload.wikimedia.org/original/{quoted}",
                "thumburl": download_url,
                "thumbwidth": 2000,
                "mime": mime_type,
                "width": 4000,
                "height": 2000,
                "descriptionurl": f"https://commons.wikimedia.org/wiki/{quoted}",
                "extmetadata": metadata,
            }
        ],
    }


def relevant_page(
    query: str, page_id: int, title: str, *, download_url: str, **overrides: object
) -> dict[str, object]:
    """A page that plainly matches its query, for tests not about relevance."""
    overrides.setdefault("description", f"A photograph of {query}.")
    return commons_page(page_id, title, download_url=download_url, **overrides)  # type: ignore[arg-type]


@dataclass(slots=True)
class FakeCommons:
    """Serves canned search results and image bytes through a MockTransport."""

    pages: Mapping[str, Sequence[Mapping[str, object]]]
    files: Mapping[str, bytes]
    search_requests: list[httpx.Request] = field(default_factory=list)
    download_requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(ENDPOINT):
            return self._search(request)
        return self._download(request)

    def _search(self, request: httpx.Request) -> httpx.Response:
        self.search_requests.append(request)
        query = request.url.params.get("gsrsearch", "")
        pages = self.pages.get(query)
        if pages is None:
            return httpx.Response(200, json={"batchcomplete": True})
        return httpx.Response(200, json={"query": {"pages": list(pages)}})

    def _download(self, request: httpx.Request) -> httpx.Response:
        self.download_requests.append(request)
        payload = self.files.get(str(request.url))
        if payload is None:
            return httpx.Response(404)
        return httpx.Response(200, content=payload, headers={"content-type": "image/jpeg"})

    @property
    def searched(self) -> list[str]:
        return [r.url.params.get("gsrsearch", "") for r in self.search_requests]

    @property
    def downloaded(self) -> list[str]:
        return [str(r.url) for r in self.download_requests]


async def source(
    commons: FakeCommons, keywords: Sequence[str], **overrides: object
) -> Sequence[ImageAsset]:
    settings = WikimediaSettings(**overrides)  # type: ignore[arg-type]
    client = httpx.AsyncClient(transport=httpx.MockTransport(commons.handle))
    async with client:
        return await WikimediaImageSourcer(client, settings).source_images(
            Brief(prompt="the giant panda"),
            ResearchContent(
                title="Giant panda",
                subtitle="Ailuropoda",
                summary="s",
                keywords=tuple(keywords),
            ),
        )


def selected_titles(assets: Sequence[ImageAsset]) -> list[str]:
    """Which Commons files came back, by file page title."""
    return [a.credit.source.title for a in assets if a.credit.source is not None]


def single_slot_commons(**page_kwargs: object) -> FakeCommons:
    """One keyword ("panda"), one candidate, one 4000x2000 JPEG behind it."""
    url = "https://upload.wikimedia.org/thumb/one.jpg"
    page_kwargs.setdefault("description", "A photograph of a panda.")
    page = commons_page(1, "File:Panda one.jpg", download_url=url, **page_kwargs)  # type: ignore[arg-type]
    return FakeCommons(pages={"panda": [page]}, files={url: jpeg_bytes(4000, 2000, seed=1)})


# --------------------------------------------------------------------------- #
# Ordering, capping, and the empty cases                                      #
# --------------------------------------------------------------------------- #


async def test_keyword_order_is_preserved() -> None:
    keywords = ["panda portrait", "panda eating bamboo", "panda cub"]
    urls = [f"https://files.invalid/{i}.jpg" for i in range(3)]
    commons = FakeCommons(
        pages={
            keyword: [relevant_page(keyword, i + 10, f"File:Slot{i}.jpg", download_url=url)]
            for i, (keyword, url) in enumerate(zip(keywords, urls, strict=True))
        },
        files={url: jpeg_bytes(900, 600, seed=i) for i, url in enumerate(urls)},
    )

    assets = await source(commons, keywords)

    assert selected_titles(assets) == ["File:Slot0.jpg", "File:Slot1.jpg", "File:Slot2.jpg"]
    assert commons.searched == keywords


async def test_more_than_six_keywords_takes_the_first_six() -> None:
    keywords = [f"panda number {i}" for i in range(9)]
    urls = [f"https://files.invalid/{i}.jpg" for i in range(9)]
    commons = FakeCommons(
        pages={
            keyword: [relevant_page(keyword, i + 1, f"File:F{i}.jpg", download_url=url)]
            for i, (keyword, url) in enumerate(zip(keywords, urls, strict=True))
        },
        files={url: jpeg_bytes(600, 400, seed=i) for i, url in enumerate(urls)},
    )

    assets = await source(commons, keywords)

    assert len(assets) == 6
    assert commons.searched == keywords[:6]  # no wasted searches on dropped slots


async def test_no_keywords_means_no_assets_and_no_requests() -> None:
    commons = FakeCommons(pages={}, files={})

    assert await source(commons, []) == ()
    assert commons.search_requests == []
    assert commons.download_requests == []


async def test_search_sends_a_user_agent_and_one_request_per_slot() -> None:
    commons = single_slot_commons()

    await source(commons, ["panda", "panda"])

    assert len(commons.search_requests) == 2
    request = commons.search_requests[0]
    assert "infographic-generator" in request.headers["user-agent"]
    assert request.url.params["gsrnamespace"] == "6"
    assert request.url.params["iiprop"] == "url|size|mime|extmetadata"


# --------------------------------------------------------------------------- #
# Never pad: a bad slot is skipped, its neighbours survive                     #
# --------------------------------------------------------------------------- #


async def test_unlicensable_slot_is_skipped_and_other_slots_still_return() -> None:
    good_url = "https://files.invalid/good.jpg"
    bad_url = "https://files.invalid/bad.jpg"
    commons = FakeCommons(
        pages={
            "unlicensed panda": [
                relevant_page(
                    "unlicensed panda", 1, "File:Bad.jpg", download_url=bad_url, license_id=None
                )
            ],
            "licensed panda": [
                relevant_page("licensed panda", 2, "File:Good.jpg", download_url=good_url)
            ],
        },
        files={good_url: jpeg_bytes(900, 600, seed=2), bad_url: jpeg_bytes(900, 600, seed=3)},
    )

    assets = await source(commons, ["unlicensed panda", "licensed panda"])

    assert selected_titles(assets) == ["File:Good.jpg"]
    assert bad_url not in commons.downloaded


async def test_nothing_usable_anywhere_returns_empty_not_junk() -> None:
    commons = FakeCommons(pages={}, files={})

    assert await source(commons, ["nothing here", "also nothing"]) == ()


async def test_a_failed_search_does_not_fail_the_run() -> None:
    url = "https://files.invalid/ok.jpg"
    good = relevant_page("fine", 1, "File:Fine.jpg", download_url=url)

    def handle(request: httpx.Request) -> httpx.Response:
        if "boom" in str(request.url):
            return httpx.Response(500)
        if str(request.url).startswith(ENDPOINT):
            return httpx.Response(200, json={"query": {"pages": [good]}})
        return httpx.Response(200, content=jpeg_bytes(800, 600, seed=4))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    async with client:
        assets = await WikimediaImageSourcer(client).source_images(
            Brief(prompt="p"),
            ResearchContent(title="t", subtitle="s", summary="m", keywords=("boom", "fine")),
        )

    assert len(assets) == 1


async def test_malformed_json_is_treated_as_no_candidates() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    async with client:
        assets = await WikimediaImageSourcer(client).source_images(
            Brief(prompt="p"),
            ResearchContent(title="t", subtitle="s", summary="m", keywords=("panda",)),
        )

    assert assets == ()


async def test_undownloadable_candidate_falls_through_to_the_next_one() -> None:
    missing = "https://files.invalid/missing.jpg"
    present = "https://files.invalid/present.jpg"
    commons = FakeCommons(
        pages={
            "panda": [
                relevant_page("panda", 1, "File:Missing.jpg", download_url=missing, index=1),
                relevant_page("panda", 2, "File:Present.jpg", download_url=present, index=2),
            ]
        },
        files={present: jpeg_bytes(900, 600, seed=5)},
    )

    assets = await source(commons, ["panda"])

    assert selected_titles(assets) == ["File:Present.jpg"]


async def test_undecodable_bytes_are_rejected() -> None:
    url = "https://files.invalid/lying.jpg"
    commons = FakeCommons(
        pages={"panda": [relevant_page("panda", 1, "File:Lying.jpg", download_url=url)]},
        files={url: b"GIF89a this is not a jpeg at all"},
    )

    assert await source(commons, ["panda"]) == ()


async def test_oversized_download_is_refused() -> None:
    commons = single_slot_commons()

    assert await source(commons, ["panda"], max_download_bytes=64) == ()


# --------------------------------------------------------------------------- #
# Licence handling                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("license_id", "expected"),
    [
        ("cc0", "CC0-1.0"),
        ("pd", "public-domain"),
        ("pd-old-70", "public-domain"),
        ("cc-by-2.0", "CC-BY-2.0"),
        ("cc-by-sa-4.0", "CC-BY-SA-4.0"),
    ],
)
async def test_free_licences_are_accepted(license_id: str, expected: str) -> None:
    commons = single_slot_commons(license_id=license_id)

    assets = await source(commons, ["panda"])

    assert [a.credit.license for a in assets] == [expected]


@pytest.mark.parametrize(
    "license_id",
    ["cc-by-nc-2.0", "cc-by-nd-4.0", "cc-by-nc-sa-3.0", "gfdl", "fairuse", "", None],
)
async def test_unusable_licences_are_rejected(license_id: str | None) -> None:
    commons = single_slot_commons(license_id=license_id)

    assert await source(commons, ["panda"]) == ()


async def test_human_readable_short_name_is_understood() -> None:
    commons = single_slot_commons(license_id=None, short_name="CC BY-SA 4.0")

    assets = await source(commons, ["panda"])

    assert [a.credit.license for a in assets] == ["CC-BY-SA-4.0"]


async def test_attribution_required_without_an_author_is_rejected() -> None:
    commons = single_slot_commons(artist=None, attribution_required="true")

    assert await source(commons, ["panda"]) == ()


async def test_cc_by_needs_an_author_even_when_the_metadata_flag_says_otherwise() -> None:
    """The licence obliges us to credit somebody; a missing flag is not permission."""
    commons = single_slot_commons(
        license_id="cc-by-4.0", artist=None, attribution_required="false"
    )

    assert await source(commons, ["panda"]) == ()


@pytest.mark.parametrize("license_id", ["cc0", "pd"])
async def test_public_domain_needs_nobody_named(license_id: str) -> None:
    commons = single_slot_commons(
        license_id=license_id, artist=None, attribution_required="false"
    )

    (asset,) = await source(commons, ["panda"])

    assert asset.credit.author is None
    assert asset.credit.license in {"CC0-1.0", "public-domain"}


async def test_restricted_file_is_rejected_even_with_a_free_licence() -> None:
    commons = single_slot_commons(license_id="cc0", restrictions="trademarked")

    assert await source(commons, ["panda"]) == ()


async def test_credit_is_fully_populated() -> None:
    commons = single_slot_commons(
        license_id="cc-by-sa-4.0",
        artist='<a href="/wiki/User:Gzen92" title="User:Gzen92">Gzen92</a>',
    )

    (asset,) = await source(commons, ["panda"])
    credit = asset.credit

    assert credit.license == "CC-BY-SA-4.0"
    assert credit.author == "Gzen92"  # HTML stripped to the plain text models promise
    assert credit.license_url == "https://creativecommons.org/licenses/by-sa/4.0/"
    assert credit.source is not None
    assert credit.source.url == "https://commons.wikimedia.org/wiki/File:Panda_one.jpg"
    assert credit.source.title == "File:Panda one.jpg"
    assert credit.source.publisher == "Wikimedia Commons"


async def test_the_poisoned_panda_file_is_never_selected() -> None:
    """CLAUDE.md: tagged CC BY-SA 4.0 "own work", EXIF credits naturepl.com/WWF."""
    blocked_url = "https://files.invalid/blocked.jpg"
    other_url = "https://files.invalid/other.jpg"
    query = "panda forest"
    commons = FakeCommons(
        pages={
            query: [
                relevant_page(query, 1, BLOCKED_TITLE, download_url=blocked_url, index=1),
                relevant_page(query, 2, "File:Innocent.jpg", download_url=other_url, index=2),
            ]
        },
        files={
            blocked_url: jpeg_bytes(900, 600, seed=6),
            other_url: jpeg_bytes(900, 600, seed=7),
        },
    )

    assets = await source(commons, [query])

    assert selected_titles(assets) == ["File:Innocent.jpg"]
    assert blocked_url not in commons.downloaded


def test_read_credit_refuses_the_blocked_title_directly() -> None:
    clean = {
        "License": {"value": "cc-by-sa-4.0"},
        "Artist": {"value": "Somebody"},
    }
    assert read_credit(clean, file_page_url="https://x.invalid", title=BLOCKED_TITLE) is None
    assert read_credit(clean, file_page_url="https://x.invalid", title="File:Fine.jpg") is not None


def test_strip_markup_flattens_html_metadata() -> None:
    assert strip_markup("<a href='/x'>Kevin  Dooley</a> &amp; friends") == "Kevin Dooley & friends"
    assert strip_markup("<span>\n  spaced\n</span>") == "spaced"


async def test_a_prose_artist_field_does_not_become_a_500_character_credit() -> None:
    """A real CC BY-SA panda photo fills Artist with an essay, not a name."""
    essay = (
        "Another one of my pictures: This photograph was taken by Medium69 "
        "(William Crochot) and released under the license stated below. You are "
        "free to use it for any purpose as long as you credit the author, the "
        "Source (Wikimedia Commons) and the license in close relation to the image."
    )
    commons = single_slot_commons(artist=essay)

    (asset,) = await source(commons, ["panda"])

    assert asset.credit.author is not None
    assert len(asset.credit.author) <= 160
    assert asset.credit.author.endswith("…")
    assert "William Crochot" in asset.credit.author  # the name survives the cut


async def test_a_short_credit_is_preferred_over_a_prose_artist() -> None:
    commons = single_slot_commons(artist="x " * 200)
    _set_metadata(commons, "Credit", "Gzen92")

    (asset,) = await source(commons, ["panda"])

    assert asset.credit.author == "Gzen92"


async def test_own_work_is_not_treated_as_an_author() -> None:
    """"Own work" is true on the file page and useless as a credit line in a PNG."""
    commons = single_slot_commons(artist="Own work.")

    assert await source(commons, ["panda"]) == ()  # attribution required, nobody named


async def test_a_placeholder_credit_does_not_beat_a_real_artist() -> None:
    commons = single_slot_commons(artist="William Crochot")
    _set_metadata(commons, "Credit", "Own work")

    (asset,) = await source(commons, ["panda"])

    assert asset.credit.author == "William Crochot"


async def test_a_junk_description_falls_back_instead_of_becoming_alt_text() -> None:
    """One real panda photo's entire Commons description is the character "0"."""
    commons = single_slot_commons(description="0")

    (asset,) = await source(commons, ["panda"])

    assert asset.alt_text == "panda - Panda one"


async def test_a_paragraph_description_is_trimmed_for_alt_text() -> None:
    commons = single_slot_commons(description="A panda in a forest. " * 40)

    (asset,) = await source(commons, ["panda"])

    assert len(asset.alt_text) <= 300
    assert asset.alt_text.startswith("A panda in a forest.")


def test_normalize_license_rejects_the_unversioned_unknown() -> None:
    assert normalize_license("cc-by") is None
    assert normalize_license("some bespoke permission") is None


# --------------------------------------------------------------------------- #
# Image preparation                                                           #
# --------------------------------------------------------------------------- #


async def test_oversized_image_is_downscaled_and_marked_as_adapted() -> None:
    commons = single_slot_commons()  # source is 4000x2000

    (asset,) = await source(commons, ["panda"])

    assert max(asset.width_px, asset.height_px) == 2000
    assert (asset.width_px, asset.height_px) == (2000, 1000)  # aspect ratio kept
    assert asset.credit.modified is True
    assert _decoded_size(asset) == (asset.width_px, asset.height_px)


async def test_small_enough_image_is_passed_through_unmodified() -> None:
    url = "https://files.invalid/small.jpg"
    payload = jpeg_bytes(800, 600, seed=8)
    commons = FakeCommons(
        pages={"panda": [relevant_page("panda", 1, "File:Small.jpg", download_url=url)]},
        files={url: payload},
    )

    (asset,) = await source(commons, ["panda"])

    assert (asset.width_px, asset.height_px) == (800, 600)
    assert asset.content == payload
    assert asset.credit.modified is False  # we did not adapt it, so we do not say so


async def test_recompressed_image_is_marked_as_adapted() -> None:
    """Under 2000px but over the byte ceiling: re-encoding is still an adaptation."""
    commons = single_slot_commons()

    (asset,) = await source(commons, ["panda"], max_dimension_px=4000, max_encoded_bytes=2_000)

    assert (asset.width_px, asset.height_px) == (4000, 2000)
    assert asset.credit.modified is True


async def test_assets_respect_the_encoded_byte_ceiling() -> None:
    url = "https://files.invalid/big.jpg"
    commons = FakeCommons(
        pages={"panda": [relevant_page("panda", 1, "File:Big.jpg", download_url=url)]},
        files={url: jpeg_bytes(3000, 3000, seed=9, quality=100)},
    )

    (asset,) = await source(commons, ["panda"])

    assert len(asset.content) <= 1_000_000


async def test_transparent_png_stays_a_png() -> None:
    url = "https://files.invalid/logo.png"
    commons = FakeCommons(
        pages={
            "panda": [
                relevant_page(
                    "panda", 1, "File:Panda logo.png", download_url=url, mime_type="image/png"
                )
            ]
        },
        files={url: png_bytes(2600, 2600, seed=10, alpha=128)},
    )

    (asset,) = await source(commons, ["panda"])

    assert asset.mime_type == "image/png"
    assert max(asset.width_px, asset.height_px) == 2000


async def test_svg_candidates_are_never_considered() -> None:
    url = "https://files.invalid/diagram.svg"
    commons = FakeCommons(
        pages={
            "panda": [
                relevant_page(
                    "panda", 1, "File:Panda range.svg", download_url=url, mime_type="image/svg+xml"
                )
            ]
        },
        files={url: b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"},
    )

    assert await source(commons, ["panda"]) == ()
    assert commons.download_requests == []


def test_prepare_rejects_bytes_that_are_not_an_image() -> None:
    assert prepare(b"", max_dimension_px=2000, max_encoded_bytes=1_000) is None
    assert prepare(b"nope", max_dimension_px=2000, max_encoded_bytes=1_000) is None


def test_prepare_reports_the_real_decoded_dimensions() -> None:
    prepared = prepare(
        jpeg_bytes(2500, 500, seed=11), max_dimension_px=1000, max_encoded_bytes=10**7
    )

    assert prepared is not None
    assert (prepared.width_px, prepared.height_px) == (1000, 200)
    with Image.open(io.BytesIO(prepared.payload)) as image:
        assert image.size == (1000, 200)


# --------------------------------------------------------------------------- #
# Relevance: rank the real matches up, drop the impostors                      #
# --------------------------------------------------------------------------- #


async def test_a_better_term_match_outranks_the_api_ordering() -> None:
    """Both candidates clear the floor; the fuller match still wins the slot."""
    partial = "https://files.invalid/grass.jpg"
    full = "https://files.invalid/bamboo.jpg"
    commons = FakeCommons(
        pages={
            "giant panda eating bamboo": [
                commons_page(
                    1,
                    "File:Giant panda eating grass.jpg",
                    download_url=partial,
                    index=1,
                    description="A giant panda eating grass on a lawn.",
                ),
                commons_page(
                    2,
                    "File:Panda with shoots.jpg",
                    download_url=full,
                    index=2,
                    description="A giant panda eating bamboo shoots.",
                ),
            ]
        },
        files={
            partial: jpeg_bytes(900, 600, seed=30),
            full: jpeg_bytes(900, 600, seed=31),
        },
    )

    assets = await source(commons, ["giant panda eating bamboo"])

    assert selected_titles(assets) == ["File:Panda with shoots.jpg"]
    assert partial not in commons.downloaded


async def test_a_slot_matching_too_little_of_its_keyword_is_skipped() -> None:
    """Saturn matches only "portrait", the tortoise only "giant": neither is a panda."""
    saturn = "https://files.invalid/saturn.jpg"
    tortoise = "https://files.invalid/tortoise.jpg"
    commons = FakeCommons(
        pages={
            "giant panda portrait": [
                commons_page(1, "File:Latest Saturn Portrait.jpg", download_url=saturn, index=1),
                commons_page(
                    2, "File:Galapagos Giant Tortoise.jpg", download_url=tortoise, index=2
                ),
            ]
        },
        files={
            saturn: jpeg_bytes(900, 600, seed=32),
            tortoise: jpeg_bytes(900, 600, seed=33),
        },
    )

    assets = await source(commons, ["giant panda portrait"])

    assert assets == ()
    assert commons.download_requests == []  # not even fetched, let alone returned


async def test_a_weak_slot_does_not_cost_the_strong_slots() -> None:
    saturn = "https://files.invalid/saturn.jpg"
    panda = "https://files.invalid/panda.jpg"
    commons = FakeCommons(
        pages={
            "giant panda portrait": [
                commons_page(1, "File:Latest Saturn Portrait.jpg", download_url=saturn)
            ],
            "panda eating bamboo": [
                relevant_page(
                    "panda eating bamboo", 2, "File:Panda eating bamboo.jpg", download_url=panda
                )
            ],
        },
        files={saturn: jpeg_bytes(900, 600, seed=34), panda: jpeg_bytes(900, 600, seed=35)},
    )

    assets = await source(commons, ["giant panda portrait", "panda eating bamboo"])

    assert selected_titles(assets) == ["File:Panda eating bamboo.jpg"]
    assert assets[0].role is ImageRole.HERO  # the surviving slot leads


async def test_a_single_term_keyword_must_still_be_mentioned() -> None:
    url = "https://files.invalid/unrelated.jpg"
    commons = FakeCommons(
        pages={"panda": [commons_page(1, "File:Something Else.jpg", download_url=url)]},
        files={url: jpeg_bytes(900, 600, seed=36)},
    )

    assert await source(commons, ["panda"]) == ()


async def test_equal_matches_keep_the_api_ordering() -> None:
    first = "https://files.invalid/a.jpg"
    second = "https://files.invalid/b.jpg"
    commons = FakeCommons(
        pages={
            "panda": [
                relevant_page("panda", 1, "File:Panda A.jpg", download_url=first, index=1),
                relevant_page("panda", 2, "File:Panda B.jpg", download_url=second, index=2),
            ]
        },
        files={first: jpeg_bytes(900, 600, seed=37), second: jpeg_bytes(900, 600, seed=38)},
    )

    assets = await source(commons, ["panda"])

    assert selected_titles(assets) == ["File:Panda A.jpg"]


async def test_pages_are_read_in_search_rank_not_object_order() -> None:
    low = "https://files.invalid/low.jpg"
    high = "https://files.invalid/high.jpg"
    commons = FakeCommons(
        pages={
            "panda": [
                relevant_page("panda", 1, "File:Panda low.jpg", download_url=low, index=9),
                relevant_page("panda", 2, "File:Panda high.jpg", download_url=high, index=1),
            ]
        },
        files={low: jpeg_bytes(900, 600, seed=39), high: jpeg_bytes(900, 600, seed=40)},
    )

    assets = await source(commons, ["panda"])

    assert selected_titles(assets) == ["File:Panda high.jpg"]


async def test_relevance_floor_is_tunable() -> None:
    """Lowering the ratio lets a one-in-three match back through."""
    saturn = "https://files.invalid/saturn.jpg"
    commons = FakeCommons(
        pages={
            "giant panda portrait": [
                commons_page(1, "File:Latest Saturn Portrait.jpg", download_url=saturn)
            ]
        },
        files={saturn: jpeg_bytes(900, 600, seed=41)},
    )

    assert await source(commons, ["giant panda portrait"]) == ()
    assert len(await source(commons, ["giant panda portrait"], min_term_match_ratio=0.1)) == 1


# --------------------------------------------------------------------------- #
# De-duplication                                                              #
# --------------------------------------------------------------------------- #


async def test_the_same_file_across_two_slots_yields_one_asset() -> None:
    url = "https://files.invalid/shared.jpg"
    page = relevant_page("giant panda", 1, "File:Shared panda.jpg", download_url=url)
    commons = FakeCommons(
        pages={"panda": [page], "giant panda": [page]},
        files={url: jpeg_bytes(900, 600, seed=12)},
    )

    assets = await source(commons, ["panda", "giant panda"])

    assert len(assets) == 1


async def test_near_identical_files_across_slots_yield_one_asset() -> None:
    """Same picture, different resolution and quality, different page: still one."""
    first = "https://files.invalid/big-copy.jpg"
    second = "https://files.invalid/small-copy.jpg"
    commons = FakeCommons(
        pages={
            "panda": [
                relevant_page("giant panda", 1, "File:Panda big copy.jpg", download_url=first)
            ],
            "giant panda": [
                relevant_page("giant panda", 2, "File:Panda small copy.jpg", download_url=second)
            ],
        },
        files={
            first: jpeg_bytes(1600, 1200, seed=13, quality=95),
            second: jpeg_bytes(800, 600, seed=13, quality=60),
        },
    )

    assets = await source(commons, ["panda", "giant panda"])

    assert selected_titles(assets) == ["File:Panda big copy.jpg"]


async def test_a_near_duplicate_does_not_cost_the_slot_its_next_candidate() -> None:
    shared = "https://files.invalid/dupe.jpg"
    fresh = "https://files.invalid/fresh.jpg"
    commons = FakeCommons(
        pages={
            "panda": [relevant_page("giant panda", 1, "File:Panda first.jpg", download_url=shared)],
            "giant panda": [
                relevant_page(
                    "giant panda", 2, "File:Panda dupe.jpg", download_url=shared, index=1
                ),
                relevant_page(
                    "giant panda", 3, "File:Panda fresh.jpg", download_url=fresh, index=2
                ),
            ],
        },
        files={
            shared: jpeg_bytes(1200, 900, seed=14),
            fresh: jpeg_bytes(1200, 900, seed=15),
        },
    )

    assets = await source(commons, ["panda", "giant panda"])

    assert selected_titles(assets) == ["File:Panda first.jpg", "File:Panda fresh.jpg"]


def test_distinct_pictures_are_not_near_duplicates() -> None:
    left = fingerprint(jpeg_bytes(800, 600, seed=16))
    right = fingerprint(jpeg_bytes(800, 600, seed=17))

    assert hamming_distance(left, right) > 6


def test_resized_copies_share_a_fingerprint() -> None:
    left = fingerprint(jpeg_bytes(1600, 1200, seed=18, quality=95))
    right = fingerprint(jpeg_bytes(400, 300, seed=18, quality=60))

    assert hamming_distance(left, right) <= 6


# --------------------------------------------------------------------------- #
# Contract-level guarantees                                                   #
# --------------------------------------------------------------------------- #


async def test_roles_put_the_lead_image_first() -> None:
    keywords = ["panda one", "panda two", "panda three"]
    urls = [f"https://files.invalid/r{i}.jpg" for i in range(3)]
    commons = FakeCommons(
        pages={
            keyword: [relevant_page(keyword, i + 1, f"File:R{i}.jpg", download_url=url)]
            for i, (keyword, url) in enumerate(zip(keywords, urls, strict=True))
        },
        files={url: jpeg_bytes(700, 500, seed=20 + i) for i, url in enumerate(urls)},
    )

    assets = await source(commons, keywords)

    assert [a.role for a in assets] == [
        ImageRole.HERO,
        ImageRole.SUPPORTING,
        ImageRole.SUPPORTING,
    ]


async def test_every_asset_satisfies_the_port_promises() -> None:
    commons = single_slot_commons(description="A panda eating bamboo in a forest clearing.")

    (asset,) = await source(commons, ["panda"])

    assert isinstance(asset.content, bytes)  # never a remote URL for someone else to fetch
    assert asset.mime_type in {"image/jpeg", "image/png", "image/webp"}
    assert asset.alt_text == "A panda eating bamboo in a forest clearing."
    assert asset.credit.license
    assert max(asset.width_px, asset.height_px) <= 2000


async def test_alt_text_falls_back_to_the_query_and_filename() -> None:
    commons = single_slot_commons(description=None)

    (asset,) = await source(commons, ["panda"])

    assert asset.alt_text == "panda - Panda one"


def test_the_sourcer_satisfies_the_image_sourcer_protocol() -> None:
    sourcer: ImageSourcer = WikimediaImageSourcer(httpx.AsyncClient())

    assert sourcer is not None


def _set_metadata(commons: FakeCommons, key: str, value: str) -> None:
    """Poke one extmetadata field on the single page of a single-slot fixture."""
    info = commons.pages["panda"][0]["imageinfo"]
    assert isinstance(info, list)
    info[0]["extmetadata"][key] = {"value": value}


def _decoded_size(asset: ImageAsset) -> tuple[int, int]:
    assert isinstance(asset.content, bytes)
    with Image.open(io.BytesIO(asset.content)) as image:
        return image.size
