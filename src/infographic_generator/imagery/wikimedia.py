"""The real image sourcer: Wikimedia Commons behind ``core.ports.ImageSourcer``.

One search per entry in ``ResearchContent.keywords``, which the researcher hands
over already ordered by significance. Each keyword is a *slot*; a slot yields at
most one image and is skipped outright when nothing licensable and on-topic turns
up. We never pad the result to look fuller than the evidence -- the port
explicitly allows returning ``()`` and the composer copes with it.

Searching fans out; downloading does not. The candidate walk is sequential and in
slot order so that de-duplication decisions -- and therefore the returned set --
do not depend on which HTTP response happened to land first.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import httpx

from infographic_generator.core.models import (
    Brief,
    ImageAsset,
    ImageCredit,
    ImageRole,
    ResearchContent,
)
from infographic_generator.imagery.licensing import (
    ExtMetadata,
    image_description,
    read_credit,
    with_modified,
)
from infographic_generator.imagery.prepare import (
    PreparedImage,
    fingerprint,
    hamming_distance,
    prepare,
)

if TYPE_CHECKING:
    from infographic_generator.core.ports import ImageSourcer

_ACCEPTED_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)
"""Pre-filter on the API's declared type. The authority on what we actually
return is :func:`prepare`, which re-derives it from the decoded bytes."""

# TODO: a regex cannot word-segment Chinese, Japanese, Thai or Khmer, so a keyword
# in one of those scripts collapses to a single unmatchable run and its slot comes
# back empty -- see the second "Known limit" in :func:`_by_relevance`. The fix is
# the vision pass CLAUDE.md anticipates, not a wider character class.
_TERM: Final = re.compile(r"[^\W_]{3,}")
"""Runs of three or more letters or digits, in any script.

``\\W`` is the Unicode-aware complement of ``\\w`` for ``str`` patterns, so
``[^\\W_]`` is "word character but not underscore" -- letters and digits. The
class this replaced was ``[0-9a-z]``, which saw ``panda géant`` as ``{"panda",
"ant"}`` and ``الباندا العملاقة`` as nothing at all. Digits stay in because
:func:`_matched_terms` has to be able to match a term like ``1000`` against a
title; underscore stays out because :func:`_readable_title` spells underscores as
spaces, so a term carrying one could never match the haystack."""

_STOPWORDS: Final[frozenset[str]] = frozenset(
    {"the", "and", "for", "with", "from", "its", "was", "are", "this", "that"}
)

JsonObject = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class WikimediaSettings:
    """Knobs for the Commons client. Defaults are the shipping configuration."""

    endpoint: str = "https://commons.wikimedia.org/w/api.php"
    user_agent: str = (
        "infographic-generator/0.1 (+https://github.com/bigH/infographic-generator)"
    )
    """Wikimedia's API etiquette requires a descriptive User-Agent; requests
    without one get throttled or refused."""
    candidates_per_slot: int = 8
    max_images: int = 6
    max_dimension_px: int = 2000
    max_encoded_bytes: int = 1_000_000
    max_download_bytes: int = 12_000_000
    """Commons originals reach hundreds of megabytes; refuse to pull one."""
    near_duplicate_distance: int = 6
    min_term_match_ratio: float = 0.75
    """Fraction of a keyword's terms a file must mention to be considered at all.
    Tuned against live Commons results for "giant panda ..." keywords: 0.75 and
    1.0 returned the same six images, while 0.5 additionally let in a photo of
    Saturn. Raising it returns fewer, safer images; lowering it lets
    keyword-shaped search noise through."""
    timeout_s: float = 20.0


@dataclass(frozen=True, slots=True)
class Candidate:
    """A Commons file that has already passed licence verification."""

    page_id: int
    title: str
    download_url: str
    mime_type: str
    credit: ImageCredit
    description: str | None


class WikimediaImageSourcer:
    """Sources licensed imagery from Wikimedia Commons.

    Implements :class:`infographic_generator.core.ports.ImageSourcer`. The client
    is injected so that timeouts, proxies and transports stay the caller's
    business -- and so tests can drive real request/response plumbing through a
    fake transport.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: WikimediaSettings = WikimediaSettings(),
    ) -> None:
        self._client = client
        self._settings = settings

    async def source_images(
        self, brief: Brief, content: ResearchContent
    ) -> Sequence[ImageAsset]:
        """Return 0-6 display-ready assets, one per keyword, significance-first."""
        slots = tuple(content.keywords)[: self._settings.max_images]
        if not slots:
            return ()
        per_slot = await asyncio.gather(*(self.search(slot) for slot in slots))
        return await self._select(tuple(zip(slots, per_slot, strict=True)))

    async def search(self, query: str) -> list[Candidate]:
        """Run one Commons search and return its licensable candidates, ranked.

        A single request does double duty: ``generator=search`` feeds the search
        hits straight into ``prop=imageinfo``, so licence metadata for every
        candidate arrives with the search results rather than costing a round
        trip each. Transport and API failures yield no candidates for this slot
        instead of failing the whole run -- one dud keyword should not cost the
        infographic its other five images.
        """
        try:
            response = await self._client.get(
                self._settings.endpoint,
                params=self._search_params(query),
                headers={"User-Agent": self._settings.user_agent},
                timeout=self._settings.timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        candidates = [
            candidate
            for page in _ranked_pages(payload)
            if (candidate := self._candidate(page)) is not None
        ]
        return _by_relevance(
            candidates, query, min_ratio=self._settings.min_term_match_ratio
        )

    def _search_params(self, query: str) -> Mapping[str, str | int]:
        return {
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,  # File:
            "gsrlimit": self._settings.candidates_per_slot,
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": self._settings.max_dimension_px,
        }

    def _candidate(self, page: JsonObject) -> Candidate | None:
        """Turn one API page into a candidate, or drop it."""
        page_id = page.get("pageid")
        title = page.get("title")
        info = page.get("imageinfo")
        if not isinstance(page_id, int) or not isinstance(title, str):
            return None
        if not isinstance(info, list) or not info or not isinstance(info[0], dict):
            return None
        first: JsonObject = info[0]

        mime_type = first.get("mime")
        file_page_url = first.get("descriptionurl")
        if not isinstance(mime_type, str) or mime_type not in _ACCEPTED_MIME_TYPES:
            return None
        if not isinstance(file_page_url, str):
            return None

        metadata = _extmetadata(first)
        credit = read_credit(metadata, file_page_url=file_page_url, title=title)
        if credit is None:
            return None
        download_url = _download_url(first)
        if download_url is None:
            return None

        return Candidate(
            page_id=page_id,
            title=title,
            download_url=download_url,
            mime_type=mime_type,
            credit=credit,
            description=image_description(metadata),
        )

    async def _select(
        self, slots: Sequence[tuple[str, Sequence[Candidate]]]
    ) -> tuple[ImageAsset, ...]:
        """Walk slots in order, taking the first usable candidate from each."""
        assets: list[ImageAsset] = []
        seen_pages: set[int] = set()
        seen_hashes: list[int] = []

        for query, candidates in slots:
            chosen = await self._first_usable(candidates, seen_pages, seen_hashes)
            if chosen is None:
                continue  # Nothing licensable and distinct here: skip, never pad.
            candidate, image, image_hash = chosen
            seen_pages.add(candidate.page_id)
            seen_hashes.append(image_hash)
            role = ImageRole.HERO if not assets else ImageRole.SUPPORTING
            assets.append(self._asset(candidate, image, query, role))
        return tuple(assets)

    async def _first_usable(
        self,
        candidates: Sequence[Candidate],
        seen_pages: set[int],
        seen_hashes: Sequence[int],
    ) -> tuple[Candidate, PreparedImage, int] | None:
        """Download candidates in rank order until one survives preparation."""
        for candidate in candidates:
            if candidate.page_id in seen_pages:
                continue
            payload = await self._download(candidate.download_url)
            if payload is None:
                continue
            image = prepare(
                payload,
                max_dimension_px=self._settings.max_dimension_px,
                max_encoded_bytes=self._settings.max_encoded_bytes,
            )
            if image is None:
                continue
            image_hash = fingerprint(image.payload)
            if self._is_near_duplicate(image_hash, seen_hashes):
                continue
            return candidate, image, image_hash
        return None

    def _is_near_duplicate(self, candidate_hash: int, seen_hashes: Sequence[int]) -> bool:
        """True when we already have this picture, resized or re-encoded."""
        return any(
            hamming_distance(candidate_hash, seen)
            <= self._settings.near_duplicate_distance
            for seen in seen_hashes
        )

    async def _download(self, url: str) -> bytes | None:
        """Fetch image bytes, refusing anything implausibly large."""
        try:
            response = await self._client.get(
                url,
                headers={"User-Agent": self._settings.user_agent},
                timeout=self._settings.timeout_s,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        if _declared_length(response) > self._settings.max_download_bytes:
            return None
        payload = response.content
        if not payload or len(payload) > self._settings.max_download_bytes:
            return None
        return payload

    def _asset(
        self,
        candidate: Candidate,
        image: PreparedImage,
        query: str,
        role: ImageRole,
    ) -> ImageAsset:
        """Assemble the asset, stating adaptation whenever we changed the bytes."""
        return ImageAsset(
            content=image.payload,
            mime_type=image.mime_type,
            width_px=image.width_px,
            height_px=image.height_px,
            alt_text=_alt_text(candidate, query),
            credit=with_modified(candidate.credit, modified=image.resampled),
            role=role,
        )


def _ranked_pages(payload: JsonObject) -> list[JsonObject]:
    """Pages in search-relevance order.

    ``generator=search`` stamps each page with its search rank in ``index``;
    the surrounding object order is not guaranteed, so sort on it and fall back
    to arrival order when it is missing.
    """
    query = payload.get("query")
    if not isinstance(query, dict):
        return []
    pages = query.get("pages")
    if not isinstance(pages, list):
        return []
    objects = [page for page in pages if isinstance(page, dict)]
    return sorted(
        objects,
        key=lambda page: _as_int(page.get("index"), default=len(objects)),
    )


def _by_relevance(
    candidates: Sequence[Candidate], query: str, *, min_ratio: float
) -> list[Candidate]:
    """Rank by how much of the keyword a file actually mentions, dropping the rest.

    Commons' full-text relevance is keyword-shaped, and its top hits for "giant
    panda portrait" are a Galapagos *giant* tortoise and a Saturn *portrait* --
    matching a word each, with no panda in sight. Counting matched query terms
    against the file's own title and description both re-ranks the genuine
    matches to the top and identifies the impostors, which are dropped: an empty
    slot is a documented outcome, but a Saturn photo in a panda infographic is
    the "junk" the stage is required never to pad with.

    Known limit: term counting has no notion of which word carries the subject.
    ESA's Saturn caption calls it a "gas *giant*", which scores it two of three
    on "giant panda portrait" -- hence a floor of 0.75 rather than a majority.
    The robust version of this is the vision pass CLAUDE.md anticipates, where a
    model looks at the candidates and says which ones are pandas.

    Known limit, scripts written without spaces between words: Chinese, Japanese,
    Thai and Khmer keywords tokenise as one long run, because :data:`_TERM` cannot
    word-segment them. Measured: "大熊猫吃竹子" ("giant panda eating bamboo") yields
    the single term ``大熊猫吃竹子`` and a floor of 1, and Commons titles and
    descriptions are overwhelmingly Latin, so nothing contains that phrase --
    every candidate is dropped and the slot comes back empty. That is the trade we
    took: before :data:`_TERM` understood non-ASCII these locales produced *no*
    terms, which took this guard off entirely and handed the hero slot to whatever
    Commons ranked first, Saturn included. An empty slot costs the poster a
    picture; an unfiltered one presents the wrong picture as evidence.

    Ties keep the API's ordering. Terms are matched as substrings, so "panda"
    still matches "pandas" and "Panda's".

    The empty-term fallback below is now reached only by a query with no run of
    three letters or digits anywhere in it -- "3D", punctuation, two-letter tokens
    -- where there is nothing to rank on and dropping everything would be
    arbitrary rather than safe.
    """
    terms = _query_terms(query)
    if not terms:
        return list(candidates)
    floor = _match_floor(len(terms), min_ratio)
    ranked = sorted(
        ((_matched_terms(c, terms), position, c) for position, c in enumerate(candidates)),
        key=lambda scored: (-scored[0], scored[1]),
    )
    return [candidate for matched, _, candidate in ranked if matched >= floor]


def _match_floor(term_count: int, min_ratio: float) -> int:
    """How many terms must match: at least one, else the ratio rounded up."""
    return max(1, math.ceil(term_count * min_ratio))


def _query_terms(query: str) -> frozenset[str]:
    """The words worth matching on: three or more characters, not filler."""
    return frozenset(_TERM.findall(query.lower())) - _STOPWORDS


def _matched_terms(candidate: Candidate, terms: frozenset[str]) -> int:
    haystack = f"{_readable_title(candidate.title)} {candidate.description or ''}".lower()
    return sum(1 for term in terms if term in haystack)


def _extmetadata(info: JsonObject) -> ExtMetadata:
    """The ``extmetadata`` block, narrowed to the shape licensing expects."""
    metadata = info.get("extmetadata")
    if not isinstance(metadata, dict):
        return {}
    return {
        key: value
        for key, value in metadata.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _download_url(info: JsonObject) -> str | None:
    """Prefer the server-side thumbnail: already scaled, far cheaper to fetch."""
    for key in ("thumburl", "url"):
        value = info.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _alt_text(candidate: Candidate, query: str) -> str:
    """Describe the image in plain text; never empty.

    The file's own description is best. Failing that, the search term plus the
    filename beats nothing at all -- alt text is required by the model and is the
    only description a screen reader of the final PNG would ever get.
    """
    if candidate.description:
        return candidate.description
    return f"{query} - {_readable_title(candidate.title)}"


def _readable_title(title: str) -> str:
    """``File:Giant_panda_eating_bamboo.jpg`` -> ``Giant panda eating bamboo``."""
    without_namespace = title.split(":", 1)[-1]
    stem = without_namespace.rsplit(".", 1)[0]
    return stem.replace("_", " ").strip() or without_namespace


def _declared_length(response: httpx.Response) -> int:
    return _as_int(response.headers.get("content-length"), default=0)


def _as_int(value: object, *, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


if TYPE_CHECKING:
    _conforms: ImageSourcer = WikimediaImageSourcer(httpx.AsyncClient())
