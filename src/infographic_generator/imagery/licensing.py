"""Reading a usable :class:`ImageCredit` out of Wikimedia ``extmetadata``.

Pure functions: no network, no image library, no logging. The one rule that
matters is that failure is expressed as ``None`` -- a file whose licence we
cannot state precisely is not a candidate, rather than a candidate with a vague
credit. ``ImageCredit.license`` is mandatory on the model and gets rendered
verbatim into the output, so "unknown" is not a value we may invent.

Only free licences are accepted: CC0, public domain, CC BY and CC BY-SA.
NonCommercial and NoDerivatives are refused deliberately -- the imagery stage
resamples every image it returns, which is exactly the adaptation ND forbids,
and NC cannot be cleared for output we may publish.
"""

from __future__ import annotations

import dataclasses
import html
import re
import unicodedata
from collections.abc import Mapping
from typing import Final
from urllib.parse import urlparse

from infographic_generator.core.models import ImageCredit, Source

ExtMetadata = Mapping[str, Mapping[str, str]]
"""Shape of the API's ``extmetadata`` block: ``{key: {"value": ..., ...}}``."""

FREE_LICENSE_IDS: Final[Mapping[str, str]] = {
    "cc-zero": "CC0-1.0",
    "pd": "public-domain",
    "no-restrictions": "public-domain",
}
"""Licence slugs with nothing to parse, keyed by their hyphenated form. Anything
starting ``pd-`` (``pd-old``, ``pd-us``, ``pd-art``) or ``public-domain`` is
treated as public domain too."""

BLOCKED_FILE_TITLES: Final[frozenset[str]] = frozenset(
    {
        # Tagged CC BY-SA 4.0 "own work" while its EXIF credits
        # "naturepl.com / LYNN M. STONE / WWF". See the gotchas in CLAUDE.md: it
        # is the best-looking forest shot in the pool, so it is precisely what a
        # relevance-ranked search will surface first.
        "file:panda velká.jpg",
    }
)

MAX_AUTHOR_CHARS: Final = 160
"""Credits are rendered *visibly* into the PNG, so an author field has to be
short enough to lay out. Some uploaders put a whole licence essay in ``Artist``
instead of a name -- see :func:`_read_author`."""

MAX_ALT_CHARS: Final = 300
"""Descriptions run to paragraphs; alt text only needs to describe the picture,
and every extra character is inlined into the composition's HTML."""

MAX_LICENSE_URL_CHARS: Final = 200
"""A licence URI is one unbreakable token rendered *visibly* into the PNG, so an
absurd one is a layout bug: a 10 KB ``LicenseUrl`` grew a render from 2400x7380
to 2400x11338. Every deed URL that exists is under 60 characters, so the cap sits
where nothing real reaches it -- see :func:`_read_license_url`."""

PLACEHOLDER_AUTHORS: Final[frozenset[str]] = frozenset(
    {
        "own work",
        "self",
        "self-photographed",
        "unknown",
        "unknown author",
        "anonymous",
        "none",
        "n/a",
        "see below",
        "see source",
        "not specified",
        "no machine-readable author provided",
    }
)
"""Values that occupy an author field without naming anybody. ``Own work`` is
rife on Commons: true on the file page, useless as the credit line in a PNG,
where there is no surrounding page to say whose work it is."""

_CC_LICENSE: Final = re.compile(r"^cc-by(-sa)?-(\d+\.\d+)$")
_CC_ZERO: Final = re.compile(r"^cc0(-1\.0)?$")
_UNFREE_TERMS: Final = re.compile(r"\b(nc|nd)\b|noncommercial|noderiv")
_TAG: Final = re.compile(r"<[^>]+>")
_WHITESPACE: Final = re.compile(r"\s+")
_LETTER: Final = re.compile(r"[^\W\d_]", re.UNICODE)
_MIN_DESCRIPTION_CHARS: Final = 4


def extmetadata_value(meta: ExtMetadata, key: str) -> str | None:
    """Pull one ``extmetadata`` value, or ``None`` if absent or blank."""
    entry = meta.get(key)
    if entry is None:
        return None
    value = entry.get("value")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def strip_markup(raw: str) -> str:
    """Flatten an HTML metadata value to the plain text the models promise.

    ``Artist`` and ``ImageDescription`` arrive as HTML fragments -- typically an
    anchor around a username. The core models document every string as plain
    text, never markup, so tags come out and entities get unescaped.
    """
    return _WHITESPACE.sub(" ", html.unescape(_TAG.sub(" ", raw))).strip()


def normalize_license(raw: str) -> str | None:
    """Map a Commons licence slug to an SPDX-style id, or ``None`` if unusable.

    Handles both forms the API reports: the machine-readable ``License``
    (``cc-by-sa-4.0``) and the human ``LicenseShortName`` (``CC BY-SA 4.0``),
    which differ only by separator. ``cc0`` -> ``CC0-1.0``, ``pd-old`` ->
    ``public-domain``. Returns ``None`` for NC, ND, and anything unrecognised --
    including free-but-unhandled licences like GFDL, which we would rather skip
    than mis-state.
    """
    slug = _WHITESPACE.sub("-", raw.strip().lower())
    if not slug:
        return None
    if _UNFREE_TERMS.search(slug):
        return None
    if slug in FREE_LICENSE_IDS:
        return FREE_LICENSE_IDS[slug]
    if slug.startswith(("pd-", "public-domain")):
        return "public-domain"
    if _CC_ZERO.fullmatch(slug):
        return "CC0-1.0"
    match = _CC_LICENSE.fullmatch(slug)
    if match is None:
        return None
    share_alike, version = match.groups()
    return f"CC-BY{'-SA' if share_alike else ''}-{version}"


def truncate(text: str, limit: int) -> str:
    """Shorten to ``limit`` characters on a word boundary, marking the cut."""
    if len(text) <= limit:
        return text
    kept = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return f"{kept or text[: limit - 1]}…"


def image_description(meta: ExtMetadata) -> str | None:
    """The file's description as plain text, if it has a usable one.

    Commons descriptions include junk: one panda photo's is the single character
    ``0``. Anything without real words is treated as absent so the caller can
    build a better description than "0".
    """
    raw = extmetadata_value(meta, "ImageDescription")
    if raw is None:
        return None
    description = strip_markup(raw)
    if len(description) < _MIN_DESCRIPTION_CHARS or not _LETTER.search(description):
        return None
    return truncate(description, MAX_ALT_CHARS)


def read_credit(
    meta: ExtMetadata, *, file_page_url: str, title: str
) -> ImageCredit | None:
    """Build a fully-populated credit, or ``None`` if the file is not usable.

    Rejects, in order: a licence that will not normalise; any file carrying
    ``Restrictions`` (trademark, personality rights), which we cannot evaluate;
    and a file we would be unable to attribute -- every CC BY and CC BY-SA work
    needs a named author whatever the ``AttributionRequired`` field happens to
    say, because the licence requires it and the metadata flag is not always
    populated. CC0 and public-domain files need nobody named.

    Known gap, accepted for v1: Wikimedia's licence metadata is *self-declared*
    by the uploader on the file description page. Nothing here proves the
    uploader held the rights they claim -- ``File:Panda velká.jpg`` is tagged
    CC BY-SA 4.0 "own work" while its EXIF credits ``naturepl.com / LYNN M.
    STONE / WWF``. Trusting ``extmetadata`` is acceptable for now; a real fix
    cross-checks the EXIF credit line against the declared author and does
    reverse-image provenance. Until then the only defence is
    :data:`BLOCKED_FILE_TITLES`, which is a denylist of one and does not
    generalise.
    """
    if _is_blocked(title):
        return None
    if extmetadata_value(meta, "Restrictions") is not None:
        return None

    license_id = _read_license_id(meta)
    if license_id is None:
        return None

    author = _read_author(meta)
    if author is None and _requires_attribution(license_id, meta):
        return None

    return ImageCredit(
        license=license_id,
        author=author,
        license_url=_read_license_url(meta),
        source=Source(url=file_page_url, title=title, publisher="Wikimedia Commons"),
        modified=False,
    )


def with_modified(credit: ImageCredit, *, modified: bool) -> ImageCredit:
    """Return a copy carrying the adaptation flag -- models are frozen."""
    return dataclasses.replace(credit, modified=modified)


def _is_blocked(title: str) -> bool:
    """Whether ``title`` names a denylisted file, in any of its spellings.

    :data:`BLOCKED_FILE_TITLES` holds the canonical form the ``query`` API
    reports -- NFC, space separated -- so the *input* is folded back to it. A
    title lifted out of a file page URL uses underscores, and a decomposed
    ``velká`` (``a`` + U+0301) compares unequal to the composed one character for
    character, so either spelling would otherwise walk straight past a denylist
    whose whole job is to be unmissable.
    """
    canonical = unicodedata.normalize("NFC", title).replace("_", " ")
    return canonical.strip().lower() in BLOCKED_FILE_TITLES


def _read_license_id(meta: ExtMetadata) -> str | None:
    """Prefer the machine-readable ``License`` slug, fall back to the short name."""
    for key in ("License", "LicenseShortName"):
        raw = extmetadata_value(meta, key)
        if raw is None:
            continue
        license_id = normalize_license(strip_markup(raw))
        if license_id is not None:
            return license_id
    return None


def _read_license_url(meta: ExtMetadata) -> str | None:
    """The deed URL, or ``None`` if the value is not plainly a web address.

    Unlike the author field this never gets :func:`truncate`d: a clipped licence
    URI is a false statement about the licence, so an over-long one is dropped
    whole. ``license_url`` is optional on the model and a bad one only costs the
    URL -- an unusable ``license`` still drops the candidate.

    ``http`` and ``https`` with a host, and nothing else. Userinfo is refused
    rather than stripped, because the credit is rendered as visible text and
    ``https://user:pw@host/`` would print a password into the PNG; an empty
    userinfo (``https://@host/``) leaks nothing and stays admitted, matching the
    research zone's admission gate.
    """
    raw = extmetadata_value(meta, "LicenseUrl")
    if raw is None:
        return None
    url = strip_markup(raw)
    if not url or len(url) > MAX_LICENSE_URL_CHARS:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        # `urlparse("https://[::1")` raises `Invalid IPv6 URL`, and this module
        # promises a `None` for unusable metadata, never an exception.
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return url


def _read_author(meta: ExtMetadata) -> str | None:
    """The person CC BY/BY-SA attribution has to name.

    ``Artist`` is free text and not always a name: one CC BY-SA panda photo fills
    it with a 500-character essay about how the uploader wishes to be credited.
    So prefer the first field that reads like a name, and only fall back to
    truncating the prose -- which keeps the credit rendered and legible, at the
    cost of possibly cutting detail the uploader asked for. Curating the worst
    offenders by hand is the real answer.
    """
    candidates = [
        author
        for key in ("Artist", "Attribution", "Credit")
        if (raw := extmetadata_value(meta, key)) and (author := strip_markup(raw))
        if not _is_placeholder(author)
    ]
    if not candidates:
        return None
    for author in candidates:
        if len(author) <= MAX_AUTHOR_CHARS:
            return author
    return truncate(candidates[0], MAX_AUTHOR_CHARS)


def _is_placeholder(author: str) -> bool:
    return author.lower().rstrip(" .").strip() in PLACEHOLDER_AUTHORS


def _requires_attribution(license_id: str, meta: ExtMetadata) -> bool:
    """Whether publishing this file obliges us to name somebody.

    The licence itself is the authority: every CC BY variant requires credit. The
    metadata flag is only ever additional evidence, never permission to skip it.
    """
    return license_id.startswith("CC-BY") or _attribution_flag(meta)


def _attribution_flag(meta: ExtMetadata) -> bool:
    """``AttributionRequired`` is a stringly-typed boolean; absent means no."""
    return (extmetadata_value(meta, "AttributionRequired") or "").lower() == "true"
