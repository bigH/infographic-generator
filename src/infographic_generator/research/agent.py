"""An LLM-backed :class:`~infographic_generator.core.ports.Researcher`.

One model call inside ``research()``: web search and web fetch ride along as
server-side tools and the output schema rides along in ``output_config["format"]``,
so the model reads the web and returns the finished poster content in one turn.

**Fabricated citations are the one unacceptable failure, so the cross-check is
code, not a prompt instruction.** :func:`retrieved_sources` builds the set of URLs
the *server* actually retrieved; every model-authored ``source_url`` is looked up
there and a miss leaves ``source`` as ``None``. The model authors URL strings and
nothing else -- publisher, title and retrieval time come off the tool results. A
rejected URL appears nowhere in the result, bibliography included, because the
bibliography is built *out of* the surviving
:class:`~infographic_generator.core.models.Source` objects.

What the fence proves is narrow: the *server retrieved* a URL, not that the page
behind it is trustworthy. A hostile page can talk the model into fetching it and is
then verified and citable under whatever title it served.

**This stage raises; it never degrades.** No fallback to
:class:`~infographic_generator.research.panda.PandaResearcher`, no partial or empty
``ResearchContent``: a poster of uncited claims is the artefact this stage exists
to prevent, so a bad run is a loud :class:`ResearchFailedError` to retry.

``client.messages.create``, not the ``client.messages.parse`` of ``CLAUDE.md`` --
:func:`_output_config` has the reason. Validating the reply is therefore ours, and
tolerant: :func:`_draft_from`.

``citations`` on the fetch tool stays off: server-side it is mutually exclusive
with structured output, structured output wins, and we make no second call.

Pydantic is scoped to the schema boundary -- the three ``_``-prefixed models below,
converted to frozen ``core.models`` dataclasses on the spot. ``research()`` returns
``core.models`` types exclusively.

The module never touches ``os.environ``: resolving a key belongs to whoever
constructs the ``AsyncAnthropic``. Live invocations are gated on
``INFOGRAPHIC_LIVE_API=1`` -- intent rather than key presence.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, TypeAlias, TypeVar
from urllib.parse import urlparse

from anthropic import AsyncAnthropic, transform_schema
from anthropic.types import (
    JSONOutputFormatParam,
    Message,
    MessageParam,
    OutputConfigParam,
    TextBlock,
    ToolUnionParam,
    WebFetchBlock,
    WebFetchTool20260209Param,
    WebFetchToolResultBlock,
    WebFetchToolResultErrorBlock,
    WebSearchTool20260209Param,
    WebSearchToolResultBlock,
    WebSearchToolResultError,
)
from pydantic import BaseModel, Field, ValidationError

from infographic_generator.core.models import (
    Brief,
    Fact,
    NarrativeSection,
    ResearchContent,
    Source,
)

if TYPE_CHECKING:
    from infographic_generator.core.ports import Researcher

_LOG: Final = logging.getLogger(__name__)

Effort: TypeAlias = Literal["low", "medium", "high", "xhigh", "max"]
"""Duplicated rather than imported from ``composition/``: another owner's zone, and
an alias is cheaper to duplicate than a seam is to share."""

_SEARCH_TOOL_TYPE: Final[Literal["web_search_20260209"]] = "web_search_20260209"
_FETCH_TOOL_TYPE: Final[Literal["web_fetch_20260209"]] = "web_fetch_20260209"
"""The dated pair ``CLAUDE.md`` documents for Opus 5, and the pair verified live.
Stay away from the ``_20260318`` variants: their ``response_inclusion: "excluded"``
drops the ``server_tool_use``/result block pairs :func:`retrieved_sources` reads,
silently emptying the verified map and therefore every citation."""

_DIRECT_CALLER: Final[Literal["direct"]] = "direct"
"""On the ``_20260209`` tools ``allowed_callers`` defaults server-side to
``["code_execution_20260120"]``: with the field omitted -- and ``code_execution``
never declared -- a live call came back carrying ``code_execution`` server-tool
blocks and was not ZDR-eligible. ``["direct"]`` removed all of it and left the URL
harvest identical, so ``CLAUDE.md``'s "don't also declare ``code_execution``" is
true but not sufficient."""

_UNUSABLE_STOP_REASONS: Final[frozenset[str]] = frozenset(
    {"refusal", "max_tokens", "model_context_window_exceeded", "pause_turn"}
)
"""Four of the seven ``StopReason`` members leave no result to read. ``pause_turn``
is in the set on purpose: the API documents it as "resend the response as-is to
continue", which a single call cannot do. Being able to *see* it is most of why this
module calls ``create`` instead of ``parse``."""

_MAX_TARGET_FACTS: Final = 12
"""Ceiling on what we *ask* for, whatever ``brief.max_facts`` says."""

_MAX_HINT_CHARS: Final = 200
_MAX_TITLE_CHARS: Final = 120

_MAX_PUBLISHER_CHARS: Final = 48
"""``.hostname`` allows 253 characters, and ``layout.py``'s ``.tick`` sets no
``overflow-wrap``, so a long host runs off the row sideways instead of folding."""

_MAX_URL_CHARS: Final = 2048
"""A URL is the one string on the result that no model-side budget bounds, and
dropping one costs a citation -- the last one kills the run -- so the bound sits
where nothing real can reach it: 2048 is what browsers and servers enforce in
practice. Over-long is dropped rather than clipped, because the key must stay
byte-identical to the server's string or the fence stops matching. Layout is *not*
the reason: ``.refs__meta`` carries ``overflow-wrap: anywhere``."""

TONE_KEY: Final = "research.tone"
FOCUS_KEY: Final = "research.focus"
SOURCES_KEY: Final = "research.sources"

_EXTRA_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        TONE_KEY: "Tone",
        FOCUS_KEY: "Angle to favour",
        SOURCES_KEY: "Source preference",
    }
)
"""The only ``research.*`` keys that reach the prompt, and the label each prints
under. Everything else in ``brief.extras`` is ignored and never raised on."""


class ResearchFailedError(RuntimeError):
    """The research call produced nothing we are willing to publish.

    A ``RuntimeError``, which is what ``core.ports.Researcher`` tells callers to
    catch when the backing service fails. There is no degraded path behind it.
    """


@dataclass(frozen=True, slots=True)
class ResearchSettings:
    """Knobs for the research call. Defaults are the shipping configuration."""

    model: str = "claude-opus-5"
    effort: Effort = "high"
    """This stage's accuracy *is* the product. Depth on Opus 5 is
    ``output_config["effort"]`` -- never a token budget, and never
    ``temperature``/``top_p``/``top_k``, which the model rejects outright."""
    timeout_s: float = 300.0
    """End-to-end wall clock around the whole call, server-side searches and fetches
    included. ``agent_composer``'s 90.0 covers one toolless call; this one can run ten
    web round-trips at ``effort="high"`` behind a single ``await``."""
    max_tokens: int = 16_000
    """Thinking is on by default on Opus 5 and shares this budget. The binding ceiling
    is not the model's 128 K output limit: on a non-streaming request with no explicit
    ``timeout=``, the SDK computes ``3600 * max_tokens / 128_000`` and raises
    ``ValueError`` once that exceeds 600 s -- i.e. above **21_333**."""
    max_search_uses: int = 5
    """→ ``max_uses`` on the search tool; ``max_search_uses`` is not an SDK key. Cost
    discipline: a live eight-fact run spent 4 of these 5 searches and **167 081 input
    tokens**, about $3 at Opus rates -- the most expensive stage in the pipeline."""
    max_fetch_uses: int = 5
    """→ ``max_uses`` on the fetch tool; that same run spent 4 of these 5. Only a
    fetched page carries text, so raise this first if the citations come back thin."""
    max_fetch_content_tokens: int = 30_000
    """→ ``max_content_tokens``: the per-page ceiling, so one 300-page PDF cannot
    eat the context window by itself."""
    target_facts: int = 8
    """Asked for, not enforced -- ``min_facts`` and ``brief.max_facts`` do the
    enforcing. 8 is what ``composition/layout.py`` lays out most cleanly, and (8, 3)
    steers ``choose_template`` to ``stat_grid``, the honest default."""
    target_sections: int = 3
    min_facts: int = 5
    """Floor on the **capped** list. ``brief.max_facts`` lowers it rather than
    fighting it; see :func:`_fact_floor`."""
    min_keywords: int = 3
    """``ResearchContent.keywords`` is documented as "3-8 short phrases, most
    important first". These two fields are that bound, enforced."""
    max_keywords: int = 8
    """Raising this above 8 breaks the ``core.models`` contract rather than widening
    it: the image sourcer is promised 3-8 phrases."""
    max_sections: int = 6
    """Sections are the one sequence nothing downstream caps -- ``layout.py`` bounds
    neither sections nor the bibliography. Applied as a prefix."""
    max_sources_per_section: int = 2
    """Two uppercase mono publisher names fit on ``layout.py``'s source line. Applied
    **after** the URL cross-check, so a section whose only real URL was listed third
    still keeps it."""


# --------------------------------------------------------------------------- #
# Provenance: what the server actually retrieved, as opposed to what was claimed
# --------------------------------------------------------------------------- #


_BlockT = TypeVar("_BlockT")


def _blocks(message: Message, kind: type[_BlockT]) -> Iterator[_BlockT]:
    """The response's content blocks of one kind, in arrival order."""
    return (block for block in message.content if isinstance(block, kind))


def retrieved_sources(message: Message) -> Mapping[str, Source]:
    """The pages the *server* retrieved, keyed by their exact URL string.

    Pure and public on purpose: this is the fence every citation has to get through,
    so a test can prove the negative -- that an invented URL is absent.

    Fetches are harvested first, so a fetched page outranks a search hit for the same
    URL whatever order the blocks arrived in; within a pass the first mention wins.
    Only a fetch carries a retrieval time and a document title, which is the whole of
    the precedence rule.
    """
    found: dict[str, Source] = {}
    for fetch in _blocks(message, WebFetchToolResultBlock):
        _admit_fetch(found, fetch)
    for search in _blocks(message, WebSearchToolResultBlock):
        _admit_search(found, search)
    return found


def _admit_search(into: dict[str, Source], block: WebSearchToolResultBlock) -> None:
    """Search success is a *list* of results; failure is a single error object.

    ``.content`` is ``WebSearchToolResultError | list[WebSearchResultBlock]`` with the
    **error first**. Server-tool failures such as ``max_uses_exceeded`` arrive as
    blocks on an HTTP 200, so no ``try/except`` can see them -- only that guard can.
    A scalar ``.content`` is neither, and iterating it raises ``TypeError`` outside
    both of ``_run``'s ``try`` blocks -- hence the list guard.

    The elements need a second guard, on their **fields**. That union is
    **undiscriminated**, so the lenient path degrades *per block*, first-member-wins:
    a clean batch really is ``WebSearchResultBlock`` elements, but *one* element
    missing ``url``, ``title`` or ``encrypted_content`` turns the whole batch into
    ``WebSearchToolResultError``s keeping the real values as pydantic extras (measured
    on ``anthropic`` 0.120.2). Narrowing elements would not break a healthy search; it
    would silently lose a whole batch's citations to one bad element. Hence
    :func:`_str_field`: no ``url`` skips it, no ``title`` costs only ``Source.title``.
    """
    results = block.content
    if isinstance(results, WebSearchToolResultError):
        # Logged, not swallowed: if every search comes back `max_uses_exceeded` the
        # run dies further down on "no fact carries a verified source", which sends an
        # operator to tune the prompt when the fix is to raise `max_search_uses`.
        _LOG.warning("a web search failed: %s", results.error_code)
        return
    if not isinstance(results, list):
        _LOG.warning("a web search returned no result list: %r", results)
        return
    for result in results:
        url = _str_field(result, "url")
        if url is None:
            _LOG.debug("skipping a search result carrying no url: %r", result)
            continue
        _admit(into, url, title=_str_field(result, "title"), retrieved_at=None)


def _str_field(block: object, name: str) -> str | None:
    """A string field the lenient construct path may simply have left off a model.

    Typed rather than ``getattr`` at the call site so no ``Any`` leaks into the
    harvest; ``None`` covers both "absent" and "present but not a string"."""
    value = getattr(block, name, None)
    return value if isinstance(value, str) else None


def _admit_fetch(into: dict[str, Source], block: WebFetchToolResultBlock) -> None:
    """Fetch success is one document, and only a fetch carries a retrieval time.

    The title is ``block.content.content.title`` -- ``DocumentBlock.title``.
    ``WebFetchBlock`` has no ``.title``, so ``block.content.title`` raises and
    ``getattr(..., "title", None)`` is a silent always-``None``.

    A ``.content`` that is neither branch -- a scalar, or a list of search results --
    would raise ``AttributeError`` on ``.url``, so the container is narrowed
    *positively*: a mapping payload really does construct as its declared type, which
    is what a ``True`` here means and what does not hold of the elements
    :func:`_admit_search` deliberately guards field by field.
    """
    document = block.content
    if isinstance(document, WebFetchToolResultErrorBlock):
        # A malformed result block degrades to `error_code=None` on the lenient
        # construct path, and "a web fetch failed: None" reads like a bug here.
        _LOG.warning(
            "a web fetch failed: %s", document.error_code or "malformed result block"
        )
        return
    if not isinstance(document, WebFetchBlock):
        _LOG.warning("a web fetch returned no document: %r", document)
        return
    _admit(
        into,
        document.url,
        title=document.content.title,
        retrieved_at=_utc(document.retrieved_at),
    )


def _admit(
    into: dict[str, Source],
    url: str,
    *,
    title: str | None,
    retrieved_at: datetime | None,
) -> None:
    """The admission gate: ``https`` with a host, or it never existed.

    That is what makes "every fact's source URL is https with a non-empty netloc"
    structurally true rather than hopefully true. The key is the server's URL string
    **verbatim, mixed case and all** -- not normalising it is precisely what the model
    is being tested on. ``urlparse`` lowercases only ``.scheme``, so
    ``HTTPS://A.Example/x`` is admitted (publisher ``a.example``) and keyed under its
    original spelling; a model that "tidies" it misses the lookup and loses that
    citation.

    Userinfo is refused, not stripped: ``Source.url`` is rendered as visible
    bibliography text, so ``https://user:pw@host/x`` would print a password into the
    PNG, and rewriting it would break the byte-exact key the fence depends on.

    Absent-only: the caller establishes precedence by harvest order. Every rejection
    is logged, so "the model cited six URLs and four verified" stays distinguishable
    from "the harvest quietly dropped two".
    """
    # TODO: the top hardening candidate is a fetch-only fence -- admit *fact*
    # citations only from fetch results, keeping the union for the bibliography, since
    # a search hit is a title and a URL with no text and so provably a page the model
    # never read. The seam is the two harvest passes in `retrieved_sources`: keep them
    # in separate maps. Held back because the attribution floor would turn a merely
    # shallow citation into a hard run failure.
    if len(url) > _MAX_URL_CHARS:
        _LOG.debug(
            "dropping a %d-character URL, over the %d cap", len(url), _MAX_URL_CHARS
        )
        return
    try:
        parsed = urlparse(url)
    except ValueError:
        # `urlparse("https://[::1")` raises `Invalid IPv6 URL`. The harvest runs
        # outside both of `_run`'s `try` blocks, so an unguarded raise here would
        # leave the module as a bare `ValueError` -- which `core.ports` defines as "a
        # brief it cannot act on", blaming the caller for a search engine's malformed
        # result. `layout.py::_host` guards `urlsplit` the same way.
        _LOG.debug("dropping an unparseable URL: %r", url)
        return
    if parsed.scheme != "https" or not parsed.hostname:
        _LOG.debug("dropping a URL that is not https with a host: %r", url)
        return
    if parsed.username or parsed.password:
        _LOG.debug(
            "dropping a URL carrying userinfo: %s://%s", parsed.scheme, parsed.hostname
        )
        return
    if url in into:
        return
    into[url] = Source(
        url=url,
        title=_clean_title(title),
        publisher=_publisher_of(parsed.hostname),
        retrieved_at=retrieved_at,
    )


def _visible(text: str) -> str:
    """Drop Unicode ``Cf`` characters -- zero-width spaces and the bidi overrides.

    They answer ``False`` to ``.isspace()``, so ``str.split()`` leaves them in place,
    and a RIGHT-TO-LEFT OVERRIDE visually reverses the rest of a rendered title.
    Autoescaping holds: visual spoofing, not injection."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _publisher_of(hostname: str) -> str:
    """``www.wwf.org.uk`` -> ``wwf.org.uk``, and ``wwwf.example`` keeps its host.

    Built from ``.hostname``, never ``.netloc``, which keeps the port and the case.
    The ``or hostname`` fallback catches the legal host ``www.``, whose stripped form
    is ``""`` -- falsy everywhere downstream yet structurally present. It stays
    non-empty because ``_admit`` already refused an empty host.
    """
    return _visible(hostname.removeprefix("www."))[:_MAX_PUBLISHER_CHARS] or hostname


def _clean_title(raw: str | None) -> str | None:
    """Collapse whitespace and clip: a retrieved page's title lands in the PNG.

    ``layout.py``'s bibliography caps nothing, and a title carrying a newline could
    forge a line in any list built out of these. Collapsing makes "one source is
    exactly one line" true by construction. ``_visible`` runs *before* the split, so a
    title of nothing but zero-width spaces comes back ``None`` rather than as an
    invisible one-character line.
    """
    return " ".join(_visible(raw or "").split())[:_MAX_TITLE_CHARS] or None


def _utc(raw: str | None) -> datetime | None:
    """An ISO timestamp as aware UTC, or ``None``. Never a synthesised "now".

    The trap is the naive case: ``.astimezone(UTC)`` on a naive value silently
    reinterprets it as **local** time, so a naive string must be *stamped* rather than
    converted. A search-only hit has no retrieval time and correctly yields ``None``;
    inventing ``datetime.now()`` would be fabricated provenance.

    The conversion sits **inside** the ``try`` because that is where the second
    failure lives: ``"0001-01-01T00:00:00+14:00"`` parses cleanly and then raises
    ``OverflowError`` -- not ``ValueError`` -- on the shift to UTC.
    """
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
        return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    except (ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------- #
# Prompts: a brief in, a research job and a shape to pour it into out
# Private, both of them: nothing in this design can inject a prompt, so a public name
# would advertise an extension point that does not exist. Steering goes through
# `ResearchSettings` and the three `research.*` keys above.
# --------------------------------------------------------------------------- #


_RESEARCH_SYSTEM: Final = (
    "You are the research stage of an infographic pipeline. Find out what is true "
    "about the topic, then return the finished content of one poster: headline, "
    "standfirst, headline statistics, short narrative sections, image keywords.\n"
    "\n"
    "Use web_search to find candidate pages, then web_fetch to actually read the "
    "ones you intend to rely on. A search result is a title and a URL, not "
    "evidence: it carries no text, so a number you have not fetched is a number "
    "you are remembering rather than reading. Prefer primary and institutional "
    "sources.\n"
    "\n"
    "Spread the facts across pages. A poster whose statistics all come from one "
    "document is a summary of that document, not research, and it inherits every "
    "one of that document's errors. Fetch and cite at least three different "
    "publishers, and when you want one more number, go to a page you have not used "
    "yet before mining another one out of a page you have.\n"
    "\n"
    "Every URL you write must be copied character for character out of the tool "
    "results in this conversation. Each one is checked against the pages the tools "
    "actually retrieved, and a URL that is not among them is deleted along with the "
    "citation it carried -- so inventing, completing or tidying a URL cannot help "
    "you and can only cost you a source. Where you cannot tell which page a number "
    "came from, leave the source out. An uncited fact is acceptable; a "
    "misattributed one is the one unacceptable failure.\n"
    "\n"
    "A fact carries one URL, so everything in that fact has to come off that one "
    "page -- the detail clause included. If qualifying a number means reaching for a "
    "second document, drop the clause or make it a fact of its own with its own "
    "source; do not attach it to a page that does not say it. Year figures are where "
    "this goes wrong: last year's number usually lives in last year's edition. And "
    "name a quantity the way its own source names it, rather than restating it in "
    "words you find clearer.\n"
    "\n"
    "Split every statistic into value and unit. The layout sizes the value off its "
    "character count alone, so a unit baked into it costs a font size: value "
    '"12-38" with unit "kg" sets large, value "12-38 kg" sets small. Where there is '
    'no unit -- "1,864", "Vulnerable" -- use null, never an empty string.\n'
    "\n"
    "Order facts and keywords by significance. A caller may keep only the first "
    "few, so lead with your strongest, and make the first fact one you can cite.\n"
    "\n"
    "Keywords are image-search queries pasted verbatim into a photo archive: short "
    'concrete noun phrases naming something a camera can see. "giant panda eating '
    'bamboo" works; "conservation success" returns nothing.\n'
    "\n"
    "Never state a publisher or a retrieval date. Both are recorded from the tool "
    "results, not from you. Do your reasoning in thinking, and reply with the "
    "requested JSON object."
)


def _research_prompt(
    brief: Brief, settings: ResearchSettings, *, target_facts: int
) -> str:
    """The topic, the reader, and the shape the findings have to fit.

    ``target_facts`` stays a separate argument because it is brief-derived, not a
    setting. Pure: :func:`_log_ignored_extras` is called once from ``_run`` instead,
    because a prompt builder documented as pure that also logs is a small lie.
    """
    return (
        f"Topic: {brief.prompt.strip()}\n"
        f"Reader: {_reader(brief)}. Pitch the reading level and vocabulary there.\n"
        f"Language of the finished poster: {brief.locale}. Search in whichever "
        "language finds the best evidence, but write every string the reader will "
        "see in that language and format numbers by its conventions.\n"
        f"{_hints(brief)}"
        "\nProduce:\n"
        "- a title (the poster's headline), a subtitle (one line saying what the "
        "reader is looking at), and a summary of 1-3 sentences that opens the "
        "piece.\n"
        f"- {target_facts} facts, most significant first, the first one cited. Add "
        "a detail clause wherever a number needs qualifying: its measurement "
        "basis, its year, or that reputable sources disagree.\n"
        f"- {settings.target_sections} narrative sections of 40-80 words, in "
        f"reading order, each with at most {settings.max_sources_per_section} URLs "
        "of pages you fetched. A section's source prints as a short name in "
        "capitals, never a link, so prefer a page whose publisher name is short: "
        "IUCNREDLIST.ORG reads, ENGLISH.WWW.GOV.CN does not.\n"
        f"- {settings.min_keywords}-{settings.max_keywords} image-search keywords, "
        "most important first.\n"
        "\nFetch every page you intend to cite."
    )


def _reader(brief: Brief) -> str:
    """``audience`` is free text and optional; an absent one is not "None"."""
    return (brief.audience or "").strip() or "a curious general adult audience"


def _hints(brief: Brief) -> str:
    """The three supported ``research.*`` keys; blank and unknown keys vanish.

    Each value is clipped to bound the *stage hints*, the values most likely to arrive
    pasted in bulk from a caller who did not mean to spend a context window. The
    prompt as a whole stays unbounded: ``brief.prompt``, ``audience`` and ``locale``
    all interpolate uncapped, and capping the topic would be wrong.
    """
    return "".join(
        f"{label}: {hint[:_MAX_HINT_CHARS]}\n"
        for key, label in _EXTRA_LABELS.items()
        if (hint := brief.extras.get(key, "").strip())
    )


def _log_ignored_extras(brief: Brief) -> None:
    """A typo'd hint should be findable, never fatal. ``core.models`` forbids raising."""
    ignored = sorted(
        key
        for key in brief.extras
        if key.startswith("research.") and key not in _EXTRA_LABELS
    )
    if ignored:
        _LOG.debug("ignoring unsupported research extras: %s", ", ".join(ignored))


# --------------------------------------------------------------------------- #
# The pydantic boundary. Nothing below leaves this section as a pydantic model.
# --------------------------------------------------------------------------- #


class _Fact(BaseModel):
    """One headline statistic, sized for a stat card."""

    label: str = Field(
        description="What the number measures, 2-5 words: 'Daily bamboo intake'."
    )
    value: str = Field(
        description=(
            "The figure alone, no unit inside it: '12-38', '1,864', '~99%', "
            "'Vulnerable'. Shorter sets larger."
        )
    )
    unit: str | None = Field(
        default=None,
        description=(
            "The unit, set small beside the value: 'kg', 'per day', 'km2'. null "
            "when the value is a plain count, a percentage or a category. Never an "
            "empty string."
        ),
    )
    detail: str | None = Field(
        default=None,
        description=(
            "One clause qualifying the number -- its measurement basis, its year, "
            "or that sources disagree. It must be supported by the same page as the "
            "value: if it needs a second document, drop it or make it its own fact. "
            "null when label and value say enough."
        ),
    )
    source_url: str | None = Field(
        default=None,
        description=(
            "Exact URL, copied character for character, of the retrieved page this "
            "number is printed on. null if you cannot point to one; a URL the tools "
            "did not retrieve is discarded along with this citation."
        ),
    )


class _Section(BaseModel):
    """A titled paragraph supporting the poster's story."""

    heading: str = Field(description="3-6 words, sentence case.")
    body: str = Field(description="40-80 words of plain prose. No markup, no lists.")
    source_urls: list[str] = Field(
        default_factory=list,
        # No number here on purpose: a `Field(description=...)` is evaluated at
        # import, before any settings object exists, so baking 2 in would tell the
        # model 2 forever even to a caller who set 1. The Python cap is authoritative
        # and `_research_prompt` carries the live number.
        description=(
            "Only URLs of pages the tools retrieved, behind this paragraph. Printed "
            "as short names in capitals, so prefer short publishers. The caller "
            "keeps only the first few. Empty is fine."
        ),
    )


# TODO: the docstring below is *on the wire* -- `transform_schema` copies it into the
# schema's top-level `description`, so it is sent to the model and billed as input on
# every call. Wrong in kind: a note to maintainers, not an instruction to the model.
# Move the reasoning to a comment and leave a one-line docstring. Not done here
# because trimming it changes the request payload, which the tests assert on.
class _ResearchResponse(BaseModel):
    """The whole poster's content. The only object the model returns.

    Constraints such as ``min_length``/``max_items`` are deliberately absent: the
    emitted JSON Schema strips them into a terse ``{maxItems: 8}`` hint in the
    description, so they buy nothing on the wire and cost a client-side
    ``ValidationError`` that would discard a call worth 167 000 input tokens. Every
    such rule is enforced in Python instead, where it also produces a better
    message.
    """

    title: str = Field(description="The poster's headline. Under 60 characters.")
    subtitle: str = Field(
        description="One clarifying line saying what the reader is looking at."
    )
    summary: str = Field(description="1-3 sentences of standfirst opening the piece.")
    facts: list[_Fact] = Field(
        default_factory=list,
        description="Most significant first. The first one must carry a source_url.",
    )
    sections: list[_Section] = Field(
        default_factory=list, description="In reading order."
    )
    keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Image-search queries pasted verbatim into a photo archive, most "
            "important first: short concrete noun phrases a photograph can "
            "satisfy, e.g. 'giant panda eating bamboo'."
        ),
    )


# --------------------------------------------------------------------------- #
# Conversion and validation: pure, and where every rule actually lives
# --------------------------------------------------------------------------- #


def _content_from(
    draft: _ResearchResponse,
    verified: Mapping[str, Source],
    brief: Brief,
    settings: ResearchSettings,
) -> ResearchContent:
    """Everything the model does not get to decide. Every sequence is a tuple.

    Facts are dropped before they are counted and counted after they are capped; the
    bibliography is built from the **pre-cap** list, so capping never shrinks it and it
    is still populated at ``max_facts=0``.

    The three ``_required`` calls run above ``_check_facts`` so that a draft which is
    both blank-titled and fact-poor reports the blank title -- a decision, not Python's
    argument-evaluation order.
    """
    title = _required(draft.title, "title")
    subtitle = _required(draft.subtitle, "subtitle")
    summary = _required(draft.summary, "summary")
    facts = _facts_from(draft, verified)
    capped = facts if brief.max_facts is None else facts[: brief.max_facts]
    _check_facts(capped, facts, brief, settings)
    sections = _sections_from(draft, verified, settings)
    return ResearchContent(
        title=title,
        subtitle=subtitle,
        summary=summary,
        facts=capped,
        sections=sections,
        keywords=_keywords_from(draft, settings),
        sources=_bibliography(facts, sections),
    )


def _facts_from(
    draft: _ResearchResponse, verified: Mapping[str, Source]
) -> tuple[Fact, ...]:
    """Drop the unusable, keep significance order, attribute only what checks out."""
    return tuple(
        Fact(
            label=label,
            value=value,
            unit=_optional(claim.unit),
            detail=_optional(claim.detail),
            source=_verified_source(claim.source_url, verified),
        )
        for claim in draft.facts
        if (label := _optional(claim.label)) and (value := _optional(claim.value))
    )


def _verified_source(url: str | None, verified: Mapping[str, Source]) -> Source | None:
    """★ The fence. A URL the server did not retrieve buys nothing: ``None``.

    **Do not normalise either side.** Both must be the server's byte-exact string;
    every normalisation you add is a class of fabricated URL that now matches.

    Never ``verified.get(url) or Source(url=url)`` -- that one-line "improvement" is
    exactly the fabricated citation ``core.ports`` calls the one unacceptable failure.

    What the fence proves is narrower than it looks: *the server retrieved this URL*,
    not *this page supports this sentence*. A ``label``/``value``/``unit`` triple can
    be hand-checked against the cited page; a ``detail`` clause and a section's
    ``body`` cannot, and a live audit of eight facts found four detail clauses citing
    the wrong one of two genuine documents. Prose here is prompt-managed, not
    code-enforced. Do not claim otherwise.
    """
    # TODO: close that residual with a second, cheap call -- hand the model each claim
    # next to the text of the page it cites and drop the ones it says the page does not
    # support. Not done here: this module is one call by design.
    return None if url is None else verified.get(url)


def _check_facts(
    capped: Sequence[Fact],
    all_facts: Sequence[Fact],
    brief: Brief,
    settings: ResearchSettings,
) -> None:
    """The two floors, both checked against the **capped** list.

    ★ The attribution floor is the one place this module deliberately exceeds the
    port; ``research()``'s docstring says so and why.
    """
    floor = _fact_floor(brief, settings)
    if len(capped) < floor:
        raise ResearchFailedError(
            f"only {len(capped)} usable facts, need at least {floor}"
        )
    if all_facts and all_facts[0].source is None:
        _LOG.warning(
            "the leading fact %r carries no verified source; at a low max_facts that "
            "is the difference between a run and a failure",
            all_facts[0].label,
        )
    if capped and all(fact.source is None for fact in capped):
        raise ResearchFailedError("no fact carries a verified source")
    _warn_if_single_sourced(capped)


def _warn_if_single_sourced(facts: Sequence[Fact]) -> None:
    """Every shipped fact on one publisher is a summary, not research -- a live run put
    8 of 8 there while 31 URLs verified. A warning and never a failure: a genuinely
    single-source topic is legitimate. One attributed fact trivially has one
    publisher, so nothing is said below two."""
    attributed = [fact.source for fact in facts if fact.source is not None]
    publishers = {source.publisher for source in attributed}
    if len(attributed) > 1 and len(publishers) == 1:
        _LOG.warning(
            "all %d attributed facts cite %s; the poster is a summary of one source",
            len(attributed),
            next(iter(publishers)),
        )


def _fact_floor(brief: Brief, settings: ResearchSettings) -> int:
    """``brief.max_facts`` lowers the floor; it never raises it. A caller who asks for
    two facts is asking for two, not for a failure, so the floor gates on the model
    being *unable* to answer rather than on the caller being modest."""
    if brief.max_facts is None:
        return settings.min_facts
    return min(brief.max_facts, settings.min_facts)


def _sections_from(
    draft: _ResearchResponse, verified: Mapping[str, Source], settings: ResearchSettings
) -> tuple[NarrativeSection, ...]:
    """Drop the blank, require one, then cap as a prefix."""
    sections = tuple(
        NarrativeSection(
            heading=heading,
            body=body,
            sources=_section_sources(item.source_urls, verified, settings),
        )
        for item in draft.sections
        if (heading := _optional(item.heading)) and (body := _optional(item.body))
    )
    if not sections:
        raise ResearchFailedError("the model returned no usable narrative sections")
    return sections[: settings.max_sections]


def _section_sources(
    urls: Sequence[str], verified: Mapping[str, Source], settings: ResearchSettings
) -> tuple[Source, ...]:
    """Verify, *then* cap: cap-then-verify would silently drop a section's only real
    URL because it was listed third."""
    kept = tuple(
        source
        for url in urls
        if (source := _verified_source(url, verified)) is not None
    )
    return kept[: settings.max_sources_per_section]


def _keywords_from(
    draft: _ResearchResponse, settings: ResearchSettings
) -> tuple[str, ...]:
    """strip -> drop blanks -> dedupe -> floor -> cap. Two of those orderings show.

    The dedupe key is exactly ``" ".join(raw.split()).casefold()`` and all three parts
    are load-bearing: ``casefold`` rather than ``lower`` because these strings are
    locale-driven by ``brief.locale``; the re-join collapses *interior* runs; and the
    emitted spelling is the **first seen**, stripped but not folded, because we never
    emit a string the model did not write.
    """
    seen: dict[str, str] = {}
    for raw in draft.keywords:
        keyword = _optional(raw)
        if keyword is not None:
            seen.setdefault(keyword.casefold(), keyword)
    if len(seen) < settings.min_keywords:
        raise ResearchFailedError(
            f"only {len(seen)} distinct keywords, need at least "
            f"{settings.min_keywords}"
        )
    return tuple(seen.values())[: settings.max_keywords]


def _bibliography(
    all_facts: Sequence[Fact], sections: Sequence[NarrativeSection]
) -> tuple[Source, ...]:
    """Cited-only, first-appearance order, deduped by exact URL string.

    ``all_facts`` is always the **pre-cap** list; the whole invariant hangs on that
    name. Cited-only rather than "every page we retrieved" because
    ``layout.py::_references`` caps nothing: five searches at ten hits each would
    render fifty rows into the PNG. The exact-string dedupe is safe because every URL
    in play came out of :func:`retrieved_sources`.
    """
    cited = [fact.source for fact in all_facts if fact.source is not None]
    cited.extend(source for section in sections for source in section.sources)
    unique: dict[str, Source] = {}
    for source in cited:
        unique.setdefault(source.url, source)
    return tuple(unique.values())


def _required(raw: str, field: str) -> str:
    """The port promises title, subtitle and summary are always populated."""
    text = _optional(raw)
    if text is None:
        raise ResearchFailedError(f"the model returned an empty {field}")
    return text


def _optional(raw: str | None) -> str | None:
    """Collapse whitespace; blank becomes ``None``, **never** ``""``.

    ``unit=""`` is the real trap: falsy in Jinja so no span renders, yet
    ``layout.py::_unit_width("")`` still returns 0.5 and perturbs the ``--fit`` maths.
    For ``detail`` it breaks equality between two semantically identical results.
    """
    return " ".join((raw or "").split()) or None


def _validated(brief: Brief) -> None:
    """The two ``ValueError``s, both raised before any API call is made."""
    if brief.max_facts is not None and brief.max_facts < 0:
        raise ValueError(f"max_facts cannot be negative: {brief.max_facts}")
    if not brief.prompt.strip():
        raise ValueError("prompt cannot be empty")


def _target_facts(brief: Brief, settings: ResearchSettings) -> int:
    """``max_facts`` is the target as well as the cap.

    ``max_facts=0`` still asks for one: the run is worth making for the title, summary
    and sections, and the cap then discards the fact. Negatives are already refused by
    :func:`_validated`.
    """
    if brief.max_facts is None:
        return settings.target_facts
    return max(1, min(brief.max_facts, _MAX_TARGET_FACTS))


# --------------------------------------------------------------------------- #
# The one model-backed call, and what we read off it
# --------------------------------------------------------------------------- #


def _tools(settings: ResearchSettings) -> list[ToolUnionParam]:
    """Search and fetch, annotated so ``mypy --strict`` checks the keys.

    Both live in the **non-beta** ``anthropic.types.ToolUnionParam``: no
    ``anthropic-beta`` header, no ``client.beta``. The per-tool budget key is
    ``max_uses``; ``max_search_uses``/``max_fetch_uses`` are our setting names, not SDK
    keys. Search has ``user_location`` and no ``citations``, fetch the reverse -- we
    set neither, and ``allowed_domains``/``blocked_domains`` are mutually exclusive
    (both -> 400), so neither of those either. ``code_execution`` is never declared;
    :data:`_DIRECT_CALLER` says why that alone is not enough.
    """
    search: WebSearchTool20260209Param = {
        "type": _SEARCH_TOOL_TYPE,
        "name": "web_search",
        "max_uses": settings.max_search_uses,
        "allowed_callers": [_DIRECT_CALLER],
    }
    fetch: WebFetchTool20260209Param = {
        "type": _FETCH_TOOL_TYPE,
        "name": "web_fetch",
        "max_uses": settings.max_fetch_uses,
        "max_content_tokens": settings.max_fetch_content_tokens,
        "allowed_callers": [_DIRECT_CALLER],
    }
    return [search, fetch]


def _output_config(effort: Effort) -> OutputConfigParam:
    """Effort plus the schema, assembled the way ``messages.parse`` assembles it.

    Deliberately **not** ``client.messages.parse(..., output_format=...)``, the
    convention ``CLAUDE.md`` documents. ``anthropic/lib/_parse/_response.py`` runs
    ``TypeAdapter(...).validate_json`` over **every** ``text`` block with no
    ``try/except``, eagerly inside the ``await``: a narration block, a ``refusal``
    carrying prose, or a ``max_tokens`` truncation therefore raises
    ``pydantic_core.ValidationError`` before ``parse`` returns, and the response object
    never exists -- so ``stop_reason``, ``usage`` and the tool-result blocks of a call
    that may have burned 167 000 input tokens are unreachable, not merely unread. With
    ``create`` we hold the message. The wire request is byte-identical either way
    (diffed; only the ``x-stainless-helper`` telemetry header differs).

    ``transform_schema`` is public and not optional: raw ``model_json_schema()`` is a
    *looser* request, because the helper forces ``additionalProperties: false`` on
    every object and folds ``"default": null`` into the description string.
    """
    schema: JSONOutputFormatParam = {
        "schema": transform_schema(_ResearchResponse),
        "type": "json_schema",
    }
    return {"effort": effort, "format": schema}


def _check_usable(message: Message) -> None:
    """Read ``stop_reason`` before ``content``, exactly as ``CLAUDE.md`` asks."""
    if message.stop_reason in _UNUSABLE_STOP_REASONS:
        raise ResearchFailedError(
            f"the research call stopped on {message.stop_reason!r}"
        )


def _draft_from(message: Message) -> _ResearchResponse:
    """Tolerant validation: try every ``text`` block, first one that parses wins.

    Never ``content[-1]``: with a truncation or a pause the last block is a
    ``server_tool_use``, and narration is not reliably at the tail either.

    The empty-text skip is structural: an unrecognised block type constructs as
    ``TextBlock(text=None)`` on the lenient path, and letting
    ``model_validate_json(None)`` raise would pin our correctness to pydantic's error
    taxonomy staying exactly as it is.
    """
    first: ValidationError | None = None
    for block in _blocks(message, TextBlock):
        if not block.text:
            continue
        try:
            return _ResearchResponse.model_validate_json(block.text)
        except ValidationError as exc:
            # Keep the first failure: in a log with no traceback, "the model wrote
            # prose" and "the model omitted `subtitle`" are otherwise indistinguishable
            # and call for opposite responses.
            first = first or exc
    raise ResearchFailedError(
        "no text block in the reply validated against the schema"
    ) from first


# --------------------------------------------------------------------------- #
# The researcher
# --------------------------------------------------------------------------- #


class LlmResearcher:
    """A ``Researcher`` that reads the web and cites only what it read.

    Implements :class:`infographic_generator.core.ports.Researcher`. The client is
    injected and required, so timeouts, proxies, credentials and transports stay the
    caller's business -- and so tests can drive real SDK plumbing through a fake
    transport rather than asserting their own beliefs about the SDK.
    """

    __slots__ = ("_client", "_settings")

    def __init__(
        self,
        client: AsyncAnthropic,
        settings: ResearchSettings = ResearchSettings(),
    ) -> None:
        self._client = client
        self._settings = settings

    async def research(self, brief: Brief) -> ResearchContent:
        """Research the brief and return content nothing invented can reach.

        At most ``brief.max_facts`` facts, most significant first; the
        bibliography is built before that cap, so it never shrinks with it.

        ★ **This deliberately exceeds the port in one place.** ``core.ports`` reads
        *"unattributed facts leave ``source`` as ``None`` rather than inventing a
        URL"*, which permits an all-unattributed result; we refuse it and raise. At
        ``max_facts=1`` that makes success depend on the model's *ordering* -- the
        leading fact has to be the attributable one -- which is why an unattributed
        lead fact is warned about even when the run succeeds. Checking the floor on
        the pre-cap set instead would remove the brittleness but permit shipping one
        uncited statistic above a full bibliography, i.e. attribution by proximity.

        Raises ``ValueError`` for a brief it cannot act on (a blank prompt, a negative
        ``max_facts``); :class:`ResearchFailedError` -- a ``RuntimeError`` -- for any
        failure of the call or of the content; and ``anthropic.APIError`` unwrapped,
        because wrapping it would discard the status code the caller needs.
        """
        _validated(brief)
        try:
            return await asyncio.wait_for(
                self._run(brief), timeout=self._settings.timeout_s
            )
        except TimeoutError as exc:
            # A timeout is the backing service failing, and the port says service
            # failure is a `RuntimeError`. Python's builtin `TimeoutError` is an
            # `OSError`, so unwrapped it slips past every handler `ports.py` tells
            # callers to write -- and `OSError` already means "unwritable path" in that
            # same file. `anthropic.APITimeoutError` is disjoint and still passes
            # through untouched.
            raise ResearchFailedError(
                f"research timed out after {self._settings.timeout_s:g}s"
            ) from exc

    async def _run(self, brief: Brief) -> ResearchContent:
        """Ask, check, harvest, validate, convert -- strictly in that order.

        The stop-reason check comes first because a refusal has no content worth
        reading. The harvest comes before validation so that a reply we end up
        rejecting still tells us what the call actually retrieved -- hence the log
        line.
        """
        _log_ignored_extras(brief)
        message = await self._ask(brief)
        _check_usable(message)
        verified = retrieved_sources(message)
        _LOG.info("the research call retrieved %d verified sources", len(verified))
        draft = _draft_from(message)
        return _content_from(draft, verified, brief, self._settings)

    async def _ask(self, brief: Brief) -> Message:
        """One call, with the tools and the schema on it together.

        ``create`` rather than ``parse``: see :func:`_output_config`. No
        ``thinking`` block (it is on by default and tuned with ``effort``), no
        ``temperature``/``top_p``/``top_k`` (rejected on Opus 5), no ``citations``,
        and ``code_execution`` never declared.
        """
        settings = self._settings
        prompt = _research_prompt(
            brief, settings, target_facts=_target_facts(brief, settings)
        )
        messages: list[MessageParam] = [{"role": "user", "content": prompt}]
        tools = _tools(settings)
        output_config = _output_config(settings.effort)
        try:
            # The `try` hugs exactly this one `await` and nothing of ours -- `tools`
            # and `output_config` are built above it so that a `transform_schema`
            # `TypeError` can never be relabelled as a credentials failure. The case
            # this catches: `AsyncAnthropic()` constructs fine with no key and only
            # fails at request time, inside `_validate_headers`, with a plain
            # `TypeError`, which the port does not permit. Narrowing on the message is
            # brittle, and probing `client.api_key` false-positives on a client
            # authenticated by `auth_token`. Since `mypy --strict` covers `src`, a
            # `TypeError` escaping the SDK here is overwhelmingly likely to be that one.
            return await self._client.messages.create(
                model=settings.model,
                max_tokens=settings.max_tokens,
                system=_RESEARCH_SYSTEM,
                messages=messages,
                tools=tools,
                output_config=output_config,
            )
        except TypeError as exc:
            raise ResearchFailedError(
                "the Anthropic client rejected the research call, most likely "
                f"unresolvable credentials: {exc}"
            ) from exc


if TYPE_CHECKING:
    _conforms: Researcher = LlmResearcher(AsyncAnthropic())
