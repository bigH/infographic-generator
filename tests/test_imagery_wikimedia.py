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
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import httpx
import pytest
from PIL import Image

from infographic_generator.composition import HtmlComposer
from infographic_generator.core.models import (
    Brief,
    ImageAsset,
    ImageCredit,
    ImageRole,
    ResearchContent,
)
from infographic_generator.imagery import WikimediaImageSourcer, WikimediaSettings
from infographic_generator.imagery.licensing import (
    MAX_LICENSE_URL_CHARS,
    MAX_MARKUP_CHARS,
    image_description,
    normalize_license,
    read_credit,
    strip_markup,
)
from infographic_generator.imagery.prepare import (
    fingerprint,
    hamming_distance,
    prepare,
)
from infographic_generator.imagery.wikimedia import (
    Candidate,
    _by_relevance,
    _query_terms,
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


class UnregisteredQuery(AssertionError):
    """A ``gsrsearch`` value :class:`FakeCommons` holds no pages for.

    Several tests assert an *absence* -- no assets, nothing downloaded -- and are
    only meaningful while the query the sourcer sends is the one the fixture
    stocked candidates under. Answering an unknown key with an empty result set
    would let a rename, a trailing space, or a query the sourcer rewrote turn
    every one of those tests green against a fixture that served nothing. So the
    fixture refuses instead, naming both sides so the drift reads at a glance.

    Deliberately not an :class:`httpx.HTTPError` or a :class:`ValueError`:
    ``WikimediaImageSourcer.search`` swallows both per slot, which would turn
    this back into a silently empty slot.
    """

    def __init__(self, query: str, registered: Iterable[str]) -> None:
        super().__init__(
            f"FakeCommons has no pages for gsrsearch={query!r}; registered: "
            f"{sorted(registered)!r}. Register the query explicitly as [] to "
            "serve an empty result set."
        )


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
        """Serve the pages registered under this ``gsrsearch``, or refuse loudly.

        An unregistered query raises :class:`UnregisteredQuery`. To serve a
        genuinely empty result set -- "Commons found nothing" -- register the
        query explicitly as ``[]``; that still answers with the empty 200 the
        real API sends.
        """
        self.search_requests.append(request)
        query = request.url.params.get("gsrsearch", "")
        pages = self.pages.get(query)
        if pages is None:
            raise UnregisteredQuery(query, self.pages)
        if not pages:
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
    commons = FakeCommons(pages={}, files={})  # nothing registered: any search raises

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
    """Both slots search successfully and Commons has nothing: still ``()``."""
    commons = FakeCommons(pages={"nothing here": [], "also nothing": []}, files={})

    assert await source(commons, ["nothing here", "also nothing"]) == ()
    assert commons.searched == ["nothing here", "also nothing"]


async def test_a_query_the_fixture_never_registered_is_an_error_not_an_absence() -> None:
    """The fixture's own contract, because the absence tests lean on it.

    "Unknown query serves nothing" and "registered-empty query serves nothing"
    are indistinguishable from the sourcer's side, so a test asserting an absence
    would keep passing after its query drifted away from the fixture's key. Only
    the first of the two is an error, and it has to be raised.
    """
    commons = FakeCommons(pages={"registered panda": []}, files={})

    with pytest.raises(UnregisteredQuery, match="'drifted panda'.*'registered panda'"):
        await source(commons, ["drifted panda"])

    assert await source(commons, ["registered panda"]) == ()  # explicitly empty is fine


def test_the_unregistered_query_error_survives_the_sourcers_per_slot_rescue() -> None:
    """``search`` swallows ``(httpx.HTTPError, ValueError)`` so one dud keyword
    cannot fail the run. A fixture misuse must not be able to hide in there."""
    assert not issubclass(UnregisteredQuery, (httpx.HTTPError, ValueError))


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


def _set_metadata(commons: FakeCommons, key: str, value: str) -> None:
    """Poke one extmetadata field on the single page of a single-slot fixture."""
    info = commons.pages["panda"][0]["imageinfo"]
    assert isinstance(info, list)
    info[0]["extmetadata"][key] = {"value": value}


def _decoded_size(asset: ImageAsset) -> tuple[int, int]:
    assert isinstance(asset.content, bytes)
    with Image.open(io.BytesIO(asset.content)) as image:
        return image.size


# --------------------------------------------------------------------------- #
# The licence URL, and the spellings of a denylisted title                     #
# --------------------------------------------------------------------------- #

CLEAN_METADATA: Mapping[str, Mapping[str, str]] = {
    "License": {"value": "cc-by-sa-4.0"},
    "Artist": {"value": "Gzen92"},
}
"""Metadata that yields a credit, so a test can vary exactly one field."""


def credit_for_license_url(url: str) -> ImageCredit:
    """The credit read from clean CC BY-SA metadata carrying ``url``.

    Asserts the candidate survives: a refused licence URL must cost the URL only.
    """
    credit = read_credit(
        {**CLEAN_METADATA, "LicenseUrl": {"value": url}},
        file_page_url="https://commons.wikimedia.org/wiki/File:Fine.jpg",
        title="File:Fine.jpg",
    )
    assert credit is not None
    return credit


def credit_for_title(title: str) -> ImageCredit | None:
    """``read_credit`` over metadata clean apart from which file it names."""
    return read_credit(CLEAN_METADATA, file_page_url="https://x.invalid", title=title)


def test_a_benign_license_url_round_trips_verbatim() -> None:
    """Without this, "always return None" would pass every rejection test below."""
    deed = "https://creativecommons.org/licenses/by-sa/4.0/"

    assert credit_for_license_url(deed).license_url == deed


def test_a_license_url_wrapped_in_markup_is_flattened() -> None:
    """``extmetadata`` values arrive as HTML fragments, licence links included."""
    anchor = '<a rel="license" href="/x">https://creativecommons.org/licenses/by/2.0/</a>'

    url = credit_for_license_url(anchor).license_url

    assert url == "https://creativecommons.org/licenses/by/2.0/"


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("javascript:alert(1)", id="javascript-scheme"),
        pytest.param("data:text/plain,cc-by-sa-4.0", id="data-scheme"),
        pytest.param("ftp://licences.invalid/by-sa", id="ftp-scheme"),
        pytest.param("//creativecommons.org/licenses/by/4.0/", id="no-scheme"),
        pytest.param("https:///licenses/by/4.0/", id="no-hostname"),
        pytest.param("Creative Commons Attribution 4.0", id="prose-not-a-url"),
        pytest.param("https://user:pw@licences.invalid/x", id="user-and-password"),
        pytest.param("https://user@licences.invalid/x", id="user-only"),
        pytest.param("https://:pw@licences.invalid/x", id="password-only"),
    ],
)
def test_an_unusable_license_url_costs_the_url_and_not_the_candidate(url: str) -> None:
    """Credits are rendered as visible text, so a licence URL has to look like one
    -- and userinfo would print a password into the PNG."""
    credit = credit_for_license_url(url)

    assert credit.license_url is None
    assert credit.license == "CC-BY-SA-4.0"  # the file itself is still usable
    assert credit.author == "Gzen92"


def test_an_empty_userinfo_license_url_is_still_admitted() -> None:
    """``https://@host/x`` leaks nothing -- the research zone admits it too."""
    url = "https://@creativecommons.org/licenses/by-sa/4.0/"

    assert credit_for_license_url(url).license_url == url


@pytest.mark.parametrize(
    ("length", "admitted"),
    [
        pytest.param(MAX_LICENSE_URL_CHARS, True, id="exactly-the-cap"),
        pytest.param(MAX_LICENSE_URL_CHARS + 1, False, id="one-over"),
    ],
)
def test_an_over_long_license_url_is_dropped_not_truncated(
    length: int, admitted: bool
) -> None:
    """A clipped licence URI is a false statement about the licence, so it goes
    entirely -- and a 10 KB one grew a real render by 54% in height."""
    prefix = "https://licences.invalid/"
    url = prefix + "a" * (length - len(prefix))
    assert len(url) == length

    credit = credit_for_license_url(url)

    assert credit.license_url == (url if admitted else None)
    assert credit.license == "CC-BY-SA-4.0"


def test_an_unparseable_license_url_does_not_raise() -> None:
    """``urlparse("https://[::1")`` raises ``Invalid IPv6 URL``; this module
    promises ``None`` for hostile metadata, never an exception."""
    credit = credit_for_license_url("https://[::1")

    assert credit.license_url is None
    assert credit.license == "CC-BY-SA-4.0"


async def test_an_over_long_license_url_never_reaches_a_selected_asset() -> None:
    commons = single_slot_commons()
    _set_metadata(commons, "LicenseUrl", "https://licences.invalid/" + "a" * 10_000)

    (asset,) = await source(commons, ["panda"])

    assert asset.credit.license_url is None
    assert asset.credit.license == "CC-BY-SA-4.0"


@pytest.mark.parametrize(
    "title",
    [
        pytest.param(BLOCKED_TITLE, id="canonical-nfc"),
        pytest.param("File:Panda velka\u0301.jpg", id="nfd-combining-acute"),
        pytest.param("File:Panda_velk\u00e1.jpg", id="underscored-as-a-page-url-spells-it"),
        pytest.param("  FILE:Panda_Velka\u0301.JPG  ", id="all-of-the-above-at-once"),
    ],
)
def test_every_spelling_of_the_poisoned_title_is_refused(title: str) -> None:
    """The denylist exists because the EXIF credits naturepl.com/WWF. A caller
    holding a page-URL title, or a decomposed one, must not slip past it."""
    assert credit_for_title(title) is None


@pytest.mark.parametrize(
    "title",
    [
        pytest.param("File:Panda one.jpg", id="another-panda-file"),
        pytest.param("File:Panda velk\u00fd.jpg", id="a-near-miss-adjective"),
        pytest.param("File:Panda_velka.jpg", id="unaccented-and-underscored"),
    ],
)
def test_normalising_the_title_does_not_deny_innocent_files(title: str) -> None:
    credit = credit_for_title(title)

    assert credit is not None
    assert credit.license == "CC-BY-SA-4.0"


# --------------------------------------------------------------------------- #
# Relevance off the Latin alphabet: the junk filter must not switch itself off  #
# --------------------------------------------------------------------------- #

ARABIC_QUERY = "الباندا العملاقة"
""""The giant panda". Two whitespace-separated terms, so the floor is
``ceil(2 * 0.75) == 2`` and filtering genuinely has work to do."""

CHINESE_QUERY = "大熊猫吃竹子"
""""Giant panda eating bamboo", written without spaces between the words."""

FRENCH_QUERY = "panda géant"

DEVANAGARI_QUERY = "विशाल पांडा"
""""The giant panda" in Hindi. ``पांडा`` is प + ा + ं + ड + ा -- five characters, of
which only three are letters -- so a term scanner that stops at a combining mark
finds nothing three characters long here and the relevance guard switches itself
off. Two terms, so the floor is ``ceil(2 * 0.75) == 2``, same as Arabic."""


def junk_and_one_match(query: str, panda_description: str) -> FakeCommons:
    """Commons' real top hits for a "giant panda" search, plus one true match.

    Saturn and the Galapagos tortoise are ranked first and carry no description,
    so they mention nothing the query asked for. The panda page is ranked last and
    described by the caller, which is how a keyword in one script meets a file
    described in another.
    """
    saturn = "https://files.invalid/ml-saturn.jpg"
    tortoise = "https://files.invalid/ml-tortoise.jpg"
    panda = "https://files.invalid/ml-panda.jpg"
    return FakeCommons(
        pages={
            query: [
                commons_page(
                    1, "File:Latest Saturn Portrait.jpg", download_url=saturn, index=1
                ),
                commons_page(
                    2, "File:Galapagos Giant Tortoise.jpg", download_url=tortoise, index=2
                ),
                commons_page(
                    3,
                    "File:Giant panda.jpg",
                    download_url=panda,
                    index=3,
                    description=panda_description,
                ),
            ]
        },
        files={
            saturn: jpeg_bytes(900, 600, seed=50),
            tortoise: jpeg_bytes(900, 600, seed=51),
            panda: jpeg_bytes(900, 600, seed=52),
        },
    )


def arabic_commons() -> FakeCommons:
    return junk_and_one_match(ARABIC_QUERY, f"صورة لـ {ARABIC_QUERY}.")


async def test_an_arabic_keyword_drops_the_candidates_that_match_nothing() -> None:
    """The researcher writes keywords in ``brief.locale``, so an ASCII-only term
    class saw no terms at all here and returned all three candidates unranked."""
    commons = arabic_commons()

    assets = await source(commons, [ARABIC_QUERY])

    assert selected_titles(assets) == ["File:Giant panda.jpg"]


async def test_an_arabic_keyword_does_not_put_saturn_in_the_hero_slot() -> None:
    commons = arabic_commons()

    assets = await source(commons, [ARABIC_QUERY])

    titles = selected_titles(assets)
    assert "File:Latest Saturn Portrait.jpg" not in titles
    assert "File:Galapagos Giant Tortoise.jpg" not in titles
    assert assets[0].role is ImageRole.HERO
    assert assets[0].credit.source is not None
    assert assets[0].credit.source.title == "File:Giant panda.jpg"
    assert commons.downloaded == ["https://files.invalid/ml-panda.jpg"]


async def test_an_unsegmentable_chinese_keyword_returns_nothing_not_junk() -> None:
    """The documented trade, asserted rather than hoped for.

    No regex word-segments 大熊猫吃竹子, so it stays one six-character term, no
    Latin Commons title or description contains that phrase, and the slot empties.
    Change this only alongside ``_by_relevance``'s second "Known limit": a
    segmenter that makes it pass must update the prose that says it cannot.
    """
    commons = junk_and_one_match(
        CHINESE_QUERY, "A giant panda eating bamboo shoots in Sichuan."
    )
    assert len(commons.pages[CHINESE_QUERY]) == 3  # three candidates were on offer

    assets = await source(commons, [CHINESE_QUERY])

    assert commons.searched == [CHINESE_QUERY]  # and the sourcer asked for them
    assert assets == ()
    assert selected_titles(assets) == []
    assert commons.download_requests == []  # not even fetched, let alone returned


async def test_an_accented_keyword_no_longer_matches_on_a_split_stem() -> None:
    """A *red* panda is not a giant panda, and used to win this slot anyway.

    While the term class was ASCII, "panda géant" tokenised to ``{"panda",
    "ant"}`` -- the accent split "géant" and left the garbage term "ant", which
    "mangeant" contains. Two of two terms matched, so the file cleared the floor.
    """
    red = "https://files.invalid/panda-roux.jpg"
    commons = FakeCommons(
        pages={
            FRENCH_QUERY: [
                commons_page(
                    1,
                    "File:Panda roux.jpg",
                    download_url=red,
                    description="Un panda roux mangeant des pousses de bambou.",
                )
            ]
        },
        files={red: jpeg_bytes(900, 600, seed=53)},
    )
    assert len(commons.pages[FRENCH_QUERY]) == 1  # a candidate was on offer

    assert await source(commons, [FRENCH_QUERY]) == ()
    assert commons.searched == [FRENCH_QUERY]  # and the sourcer asked for it
    assert commons.download_requests == []


async def test_a_candidate_naming_the_number_beats_one_that_only_names_the_noun() -> None:
    """Digits are terms too: without them every numbered keyword would match on
    its noun alone, which is one term short of the floor's whole point."""
    counted = "https://files.invalid/counted.jpg"
    uncounted = "https://files.invalid/uncounted.jpg"
    commons = FakeCommons(
        pages={
            "1000 pandas": [
                commons_page(
                    1,
                    "File:Wild pandas.jpg",
                    download_url=uncounted,
                    index=1,
                    description="Wild pandas in Sichuan.",
                ),
                commons_page(
                    2,
                    "File:1000 wild pandas counted.jpg",
                    download_url=counted,
                    index=2,
                    description="A chart of 1000 wild pandas.",
                ),
            ]
        },
        files={
            uncounted: jpeg_bytes(900, 600, seed=54),
            counted: jpeg_bytes(900, 600, seed=55),
        },
    )

    assets = await source(commons, ["1000 pandas"])

    assert selected_titles(assets) == ["File:1000 wild pandas counted.jpg"]
    assert uncounted not in commons.downloaded


async def test_a_keyword_with_no_matchable_term_still_admits_its_candidates() -> None:
    """The empty-term fallback narrowed but did not close. "3D +/-" has no run of
    three letters or digits, so there is nothing to rank on and the slot keeps the
    API's own ordering rather than dropping everything on no evidence."""
    url = "https://files.invalid/no-terms.jpg"
    commons = FakeCommons(
        pages={"3D +/-": [commons_page(1, "File:Something Else.jpg", download_url=url)]},
        files={url: jpeg_bytes(900, 600, seed=56)},
    )

    assert selected_titles(await source(commons, ["3D +/-"])) == ["File:Something Else.jpg"]


QUERY_TERM_CASES = [
    pytest.param(
        "giant panda eating bamboo",
        frozenset({"giant", "panda", "eating", "bamboo"}),
        id="english-must-not-drift",
    ),
    pytest.param(
        "the panda and its bamboo",
        frozenset({"panda", "bamboo"}),
        id="english-stopwords-still-filtered",
    ),
    pytest.param(FRENCH_QUERY, frozenset({"panda", "géant"}), id="french-accented"),
    pytest.param("большая панда", frozenset({"большая", "панда"}), id="russian-cyrillic"),
    pytest.param(ARABIC_QUERY, frozenset({"الباندا", "العملاقة"}), id="arabic"),
    pytest.param(
        DEVANAGARI_QUERY, frozenset({"विशाल", "पांडा"}), id="devanagari-vowel-signs-kept"
    ),
    pytest.param(
        "বিশাল পান্ডা", frozenset({"বিশাল", "পান্ডা"}), id="bengali-vowel-signs-kept"
    ),
    pytest.param(
        "ராட்சத பாண்டா", frozenset({"ராட்சத", "பாண்டா"}), id="tamil-vowel-signs-kept"
    ),
    pytest.param(
        "ខ្លាឃ្មុំផេនដា", frozenset({"ខ្លាឃ្មុំផេនដា"}), id="khmer-one-unsegmented-run"
    ),
    pytest.param(
        CHINESE_QUERY, frozenset({CHINESE_QUERY}), id="chinese-one-unsegmented-run"
    ),
    pytest.param(
        "ジャイアントパンダ",
        frozenset({"ジャイアントパンダ"}),
        id="japanese-one-unsegmented-run",
    ),
    pytest.param(
        "ยักษ์แพนด้า", frozenset({"ยักษ์แพนด้า"}), id="thai-one-unsegmented-run"
    ),
    pytest.param("1000 pandas", frozenset({"1000", "pandas"}), id="digits-are-terms"),
    pytest.param(
        "snake_case term",
        frozenset({"snake", "case", "term"}),
        id="underscore-is-not-a-term-character",
    ),
    pytest.param("3D +/-", frozenset(), id="nothing-long-enough-to-match-on"),
]
"""Every script the term scanner is claimed to read, with its exact term set."""

QUERY_TERM_CASE_IDS = frozenset(str(case.id) for case in QUERY_TERM_CASES)

assert len(QUERY_TERM_CASES) >= 15, "the table lost rows; 'every script' is a promise"
assert {
    "devanagari-vowel-signs-kept",
    "bengali-vowel-signs-kept",
    "tamil-vowel-signs-kept",
    "khmer-one-unsegmented-run",
} <= QUERY_TERM_CASE_IDS, "the mark-bearing scripts are the whole point of the scanner"
# Module scope on purpose: an empty or shrunken parametrize axis *skips*, so the
# same assertions inside the test below could never fail.


@pytest.mark.parametrize(("query", "expected"), QUERY_TERM_CASES)
def test_query_terms_reads_every_script(query: str, expected: frozenset[str]) -> None:
    """The exact term set, not membership: the English rows are a regression pin so
    a later widening of :data:`_TERM_CHAR` cannot quietly alter Latin behaviour, the
    mark-bearing rows pin the syllables the old class cut in half, and the
    unsegmented rows record what word-segmentation-free scanning genuinely cannot do.
    """
    assert _query_terms(query) == expected


def relevance_candidate(page_id: int, title: str, description: str | None = None) -> Candidate:
    """A licence-passed candidate, carrying only what :func:`_by_relevance` reads."""
    return Candidate(
        page_id=page_id,
        title=title,
        download_url=f"https://files.invalid/rank-{page_id}.jpg",
        mime_type="image/jpeg",
        credit=ImageCredit(license="CC-BY-SA-4.0"),
        description=description,
    )


def test_a_devanagari_keyword_drops_the_impostors_instead_of_admitting_all_three() -> None:
    """The guard is genuinely back on: the list shrinks, three candidates to one.

    With no terms, ``_by_relevance`` returns every candidate in the API's order --
    Saturn first, in the hero slot. With ``{"विशाल", "पांडा"}`` the floor is 2, the
    two impostors match zero terms and the panda page matches both.
    """
    saturn = relevance_candidate(1, "File:Latest Saturn Portrait.jpg")
    tortoise = relevance_candidate(2, "File:Galapagos Giant Tortoise.jpg")
    panda = relevance_candidate(3, "File:Giant panda.jpg", f"{DEVANAGARI_QUERY} की तस्वीर।")
    candidates = [saturn, tortoise, panda]

    kept = _by_relevance(candidates, DEVANAGARI_QUERY, min_ratio=0.75)

    assert len(candidates) == 3
    assert len(kept) == 1
    assert len(kept) < len(candidates)
    assert kept == [panda]
    assert saturn not in kept
    assert tortoise not in kept


async def test_a_devanagari_keyword_does_not_put_saturn_in_the_hero_slot() -> None:
    """End to end, through a real search response: the impostor never downloads."""
    commons = junk_and_one_match(DEVANAGARI_QUERY, f"{DEVANAGARI_QUERY} की तस्वीर।")

    assets = await source(commons, [DEVANAGARI_QUERY])

    assert len(commons.pages[DEVANAGARI_QUERY]) == 3
    assert selected_titles(assets) == ["File:Giant panda.jpg"]
    assert assets[0].role is ImageRole.HERO
    assert commons.downloaded == ["https://files.invalid/ml-panda.jpg"]


# --------------------------------------------------------------------------- #
# Hostile metadata *lengths*: the quadratic tag regex                         #
# --------------------------------------------------------------------------- #

HOSTILE_LENGTHS = (25_000, 200_000, 800_000)
"""Counts of unterminated ``<`` to feed the fields that get markup stripped.

``_TAG`` is ``<[^>]+>``, which backtracks to the end of the string from every
``<``. Measured before the fix: 0.12 s, 8.4 s and 137 s respectively -- a single
Commons file could therefore spend over two minutes of CPU. After it: 0.00 s.
"""

REDOS_BUDGET_SECONDS = 1.0
"""Well over the ~0 s a bounded read costs, far under the 8.4 s that 200_000
unterminated ``<`` cost before the fix, so removing a length guard fails loudly
and a loaded machine cannot make this flaky.

Which row proves what, measured against unfixed code: the *timing* proof is
carried by :data:`COSTLY_HOSTILE_LENGTHS` alone, because 25_000 ``<`` cost only
0.13 s unfixed -- inside this budget. At 25_000 only ``Artist`` fails unfixed, and
on *output* rather than on the clock: 160 ``<`` characters arrive as the credit
line, where an unfixed ``LicenseUrl`` and ``ImageDescription`` both already read
as ``None`` (no ``>``, so no tag matches, and what survives has no letter in it).
So ``Artist`` runs every length and the other two run only the costly ones.
"""

COSTLY_HOSTILE_LENGTHS = HOSTILE_LENGTHS[1:]
"""The lengths whose unfixed cost -- 8.5 s and 137 s -- clears the budget outright.

Three orders of magnitude of margin, so dropping 25_000 from the two fields it
cannot prove anything about loses no coverage of the quadratic path.
"""

assert len(HOSTILE_LENGTHS) == 3, "the timing rows are the point of this section"
assert min(HOSTILE_LENGTHS) > MAX_MARKUP_CHARS, "every row must clear the guard"
assert len(COSTLY_HOSTILE_LENGTHS) == 2, "both over-budget rows, or the clock proves nothing"
assert min(COSTLY_HOSTILE_LENGTHS) >= 200_000, "under 200_000 the unfixed cost is under a second"

hostile_lengths = pytest.mark.parametrize(
    "length", HOSTILE_LENGTHS, ids=lambda n: f"{n // 1000}k-opening-tags"
)

costly_hostile_lengths = pytest.mark.parametrize(
    "length", COSTLY_HOSTILE_LENGTHS, ids=lambda n: f"{n // 1000}k-opening-tags"
)


def hostile_markup(length: int) -> str:
    """``length`` unterminated ``<``: the input the tag regex is quadratic on."""
    return "<" * length


def artist_markup_of_length(length: int) -> str:
    """``<a title="ppp...">Gzen92</a>`` padded to exactly ``length`` characters.

    Real markup around a real name, so its *stripped* length stays 6 however long
    the raw is -- which is what makes it a probe of where the guard is applied.
    """
    prefix, suffix = '<a title="', '">Gzen92</a>'
    padded = prefix + "p" * (length - len(prefix) - len(suffix)) + suffix
    assert len(padded) == length
    return padded


def credit_for_artist(artist: str) -> ImageCredit | None:
    """``read_credit`` over CC BY-SA metadata carrying ``artist`` and nothing else.

    CC BY-SA obliges us to name somebody, so an unusable ``Artist`` costs the
    whole candidate: ``None`` here means "refused", not "credited anonymously".
    """
    return read_credit(
        {"License": {"value": "cc-by-sa-4.0"}, "Artist": {"value": artist}},
        file_page_url="https://commons.wikimedia.org/wiki/File:Fine.jpg",
        title="File:Fine.jpg",
    )


@costly_hostile_lengths
def test_a_hostile_license_url_is_refused_in_bounded_time(length: int) -> None:
    """Wall clock, not a proxy: the cap has to be reached before the regex is.

    The clock is the whole assertion here -- unfixed, ``license_url`` reads ``None``
    at every length -- so only the over-budget lengths run. That the cap applies to
    the *raw* value is pinned separately, by the test below.
    """
    start = time.perf_counter()
    credit = credit_for_license_url(hostile_markup(length))
    elapsed = time.perf_counter() - start

    assert credit.license_url is None
    assert credit.license == "CC-BY-SA-4.0"  # the file itself is still usable
    assert elapsed < REDOS_BUDGET_SECONDS, f"{length} tags took {elapsed:.3f}s"


@hostile_lengths
def test_a_hostile_artist_is_refused_in_bounded_time(length: int) -> None:
    """``Artist`` reaches the same regex, and CC BY-SA cannot go unattributed.

    The one field where every length earns its row: unfixed, ``credit`` comes back
    naming 160 ``<`` characters, so ``credit is None`` fails at 25_000 on output
    even though 25_000 clears the clock.
    """
    start = time.perf_counter()
    credit = credit_for_artist(hostile_markup(length))
    elapsed = time.perf_counter() - start

    assert credit is None
    assert elapsed < REDOS_BUDGET_SECONDS, f"{length} tags took {elapsed:.3f}s"


@costly_hostile_lengths
def test_a_hostile_description_is_refused_in_bounded_time(length: int) -> None:
    """A refused description is absent, and the caller writes its own alt text.

    Absent unfixed too -- ``<`` is not a letter, so the letter check rejects it
    anyway -- which leaves the clock as the only assertion, and only the
    over-budget lengths worth running.
    """
    start = time.perf_counter()
    description = image_description({"ImageDescription": {"value": hostile_markup(length)}})
    elapsed = time.perf_counter() - start

    assert description is None
    assert elapsed < REDOS_BUDGET_SECONDS, f"{length} tags took {elapsed:.3f}s"


def test_a_raw_license_url_over_the_cap_is_refused_before_it_is_stripped() -> None:
    """The cap is measured on the raw value: an anchor big enough to hurt goes,
    even though the URL it wraps would have been fine on its own."""
    deed = "https://creativecommons.org/licenses/by/4.0/"
    raw = f'<a rel="license" title="{"p" * MAX_LICENSE_URL_CHARS}" href="/x">{deed}</a>'
    assert len(raw) > MAX_LICENSE_URL_CHARS
    assert strip_markup(raw) == deed  # short enough *after* stripping, and refused anyway

    assert credit_for_license_url(raw).license_url is None


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        pytest.param(MAX_MARKUP_CHARS, "Gzen92", id="exactly-the-bound"),
        pytest.param(MAX_MARKUP_CHARS + 1, None, id="one-over"),
    ],
)
def test_an_artist_field_past_the_markup_bound_drops_the_candidate(
    length: int, expected: str | None
) -> None:
    """Dropped whole, not clipped: half a tag is not half a name. Both rows strip
    to the same six characters, so only the raw length decides."""
    credit = credit_for_artist(artist_markup_of_length(length))

    assert (credit.author if credit is not None else None) == expected


def test_the_markup_bound_leaves_real_metadata_exactly_as_it_was() -> None:
    """The regression floor: everything the guard must not touch, pinned.

    The essay is the ``Artist``-full-of-prose case :func:`_read_author` documents
    -- 504 characters, so it truncates to a legible credit rather than vanishing.
    """
    deed = "https://creativecommons.org/licenses/by-sa/4.0/"
    essay = "Photograph by Jane Q. Photographer. " * 14
    assert len(essay) == 504

    credit = credit_for_artist(f'<a href="/x">{essay}</a>')
    assert credit is not None

    assert credit_for_license_url(deed).license_url == deed
    assert credit.author == "Photograph by Jane Q. Photographer. " * 4 + "Photograph by…"
    assert image_description({"ImageDescription": {"value": f"<p>{essay}</p>"}}) == (
        "Photograph by Jane Q. Photographer. " * 8 + "Photograph…"
    )


# --------------------------------------------------------------------------- #
# The file page URL: the fifth attribution door into the rendered page         #
# --------------------------------------------------------------------------- #

PASSWORD = "hunter2"
"""What must never reach a pixel. ``descriptionurl`` comes from the API just like
``LicenseUrl`` does, and the composer renders ``source.url`` into the same
``.credit__url`` element, so userinfo there prints a password into the PNG."""

POISONED_PAGE_URL = f"https://user:{PASSWORD}@commons.wikimedia.org/wiki/File:X.jpg"
CLEAN_PAGE_URL = "https://commons.wikimedia.org/wiki/File:Fine.jpg"


def credit_for_file_page_url(url: str) -> ImageCredit | None:
    """``read_credit`` over clean CC BY-SA metadata, varying only the file page.

    ``None`` here means the whole candidate is refused -- unlike a refused
    ``license_url``, which costs the URL alone.
    """
    return read_credit(CLEAN_METADATA, file_page_url=url, title="File:Fine.jpg")


def credit_text(credit: ImageCredit) -> str:
    """Every string a credit could print, flattened.

    Deliberately not accepting ``None``: a helper that answered ``""`` for a
    refused credit turned ``PASSWORD not in credit_text(credit)`` into a
    tautology wherever it followed ``assert credit is None``.
    """
    parts = [credit.license, credit.author, credit.license_url]
    if credit.source is not None:
        parts += [credit.source.url, credit.source.title, credit.source.publisher]
    return " ".join(part for part in parts if part)


def test_a_benign_file_page_url_round_trips_verbatim() -> None:
    """Without this, "always refuse" would pass every rejection test below."""
    credit = credit_for_file_page_url(CLEAN_PAGE_URL)

    assert credit is not None
    assert credit.source is not None
    assert credit.source.url == CLEAN_PAGE_URL
    assert credit.source.title == "File:Fine.jpg"


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(POISONED_PAGE_URL, id="user-and-password"),
        pytest.param("https://user@commons.wikimedia.org/wiki/File:X.jpg", id="user-only"),
        pytest.param(
            f"https://:{PASSWORD}@commons.wikimedia.org/wiki/File:X.jpg", id="password-only"
        ),
        pytest.param("javascript:alert(1)", id="javascript-scheme"),
        pytest.param("data:text/html,File:X.jpg", id="data-scheme"),
        pytest.param("ftp://commons.wikimedia.org/wiki/File:X.jpg", id="ftp-scheme"),
        pytest.param("//commons.wikimedia.org/wiki/File:X.jpg", id="no-scheme"),
        pytest.param("https:///wiki/File:X.jpg", id="no-hostname"),
        pytest.param("https://[::1", id="unparseable-ipv6-does-not-raise"),
        pytest.param("", id="empty"),
    ],
)
def test_an_unusable_file_page_url_drops_the_whole_candidate(url: str) -> None:
    """``source`` carries the page URL *and* title CC BY-SA attribution has to
    show, so a file page we cannot print is a file we cannot publish."""
    assert credit_for_file_page_url(url) is None


def test_an_empty_userinfo_file_page_url_is_still_admitted() -> None:
    """``https://@host/x`` leaks nothing -- the licence URL admits it too."""
    url = "https://@commons.wikimedia.org/wiki/File:Fine.jpg"

    credit = credit_for_file_page_url(url)

    assert credit is not None
    assert credit.source is not None
    assert credit.source.url == url


POISONED_QUERY = "panda"
CLEAN_QUERY = "bamboo"


def set_description_url(page: Mapping[str, object], url: str) -> None:
    """Poke one page's ``descriptionurl``: :func:`commons_page` derives it from the
    title, and a poisoned one is the whole point here."""
    info = page["imageinfo"]
    assert isinstance(info, list)
    info[0]["descriptionurl"] = url


def poisoned_and_clean_commons() -> FakeCommons:
    """Two slots: one file page URL carrying a password, one wholly clean file.

    The clean slot is what keeps the assertions below honest -- "no asset prints
    the password" is free if nothing is selected at all.
    """
    poisoned_file = "https://files.invalid/poisoned.jpg"
    clean_file = "https://files.invalid/clean.jpg"
    poisoned = relevant_page(
        POISONED_QUERY, 1, "File:Poisoned panda.jpg", download_url=poisoned_file
    )
    set_description_url(poisoned, POISONED_PAGE_URL)
    clean = relevant_page(CLEAN_QUERY, 2, "File:Clean bamboo.jpg", download_url=clean_file)
    return FakeCommons(
        pages={POISONED_QUERY: [poisoned], CLEAN_QUERY: [clean]},
        files={
            poisoned_file: jpeg_bytes(900, 600, seed=71),
            clean_file: jpeg_bytes(900, 600, seed=72),
        },
    )


async def test_a_poisoned_file_page_url_never_reaches_a_selected_asset() -> None:
    commons = poisoned_and_clean_commons()

    assets = await source(commons, [POISONED_QUERY, CLEAN_QUERY])

    assert selected_titles(assets) == ["File:Clean bamboo.jpg"]
    for asset in assets:
        assert PASSWORD not in asset.alt_text
        assert PASSWORD not in credit_text(asset.credit)


async def test_a_poisoned_file_page_url_never_reaches_the_rendered_html() -> None:
    """The end of the road: what the screenshot would actually show a reader."""
    assets = await source(poisoned_and_clean_commons(), [POISONED_QUERY, CLEAN_QUERY])
    content = ResearchContent(
        title="Giant panda",
        subtitle="Ailuropoda",
        summary="Giant pandas spend most of their waking hours eating bamboo.",
        keywords=(POISONED_QUERY, CLEAN_QUERY),
    )

    brief = Brief(prompt="the giant panda")

    composition = await HtmlComposer().compose(brief, content, assets)

    assert PASSWORD not in composition.html
    assert "File:Clean bamboo.jpg" in composition.html  # a credit did render
