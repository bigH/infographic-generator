"""Contract tests for the LLM-backed :class:`LlmResearcher`.

Only the socket is faked. A real :class:`anthropic.AsyncAnthropic` runs against
:class:`httpx.MockTransport`, so request building, ``output_config`` emission,
JSON-schema transformation and content-block parsing are all exercised for real --
and the things actually under test here *are* SDK behaviours: that
``WebSearchToolResultBlock.content`` discriminates error-first, that a fetched
document's title lives at ``block.content.content.title``, that a narration text
block beside the JSON one is survivable. A duck-typed stub client would let us
assert our beliefs about the SDK rather than the SDK.
``tests/test_imagery_wikimedia.py`` is the precedent one layer down; the deviation
from ``tests/test_agent_composer.py``'s hand-written ``StubClient`` is deliberate.

The through-line is that **nothing the model says can become a citation**.
:func:`retrieved_sources` is the fence, so half of this suite drives it directly --
it is pure and needs no client at all -- and the rest proves that an invented URL
reaches neither ``Fact.source``, nor a ``NarrativeSection``, nor the bibliography.

Four things about the fixtures are deliberate:

* **Two spec families, kept apart.** ``SearchHit``/``Fetched`` are what the *server*
  retrieved; ``DraftFact``/``DraftSection``/``Draft`` are what the *model claimed*.
  The module exists to referee a disagreement between them, so collapsing them
  would make the invented-URL test impossible to write.
* **Every envelope is gated through ``Message.model_validate``** inside
  :func:`envelope`. ``messages.create`` does *no* client-side validation -- it
  constructs leniently -- so a fixture missing ``source.media_type`` silently
  degrades a fetch *success* block into a fetch *error* block and the test then
  passes while proving the opposite of what it claims.
* **Every test builds its client through :meth:`FakeApi.client`.** A real
  ``ANTHROPIC_API_KEY`` is exported in these shells and there is a populated
  ``.env`` at the repo root, so a bare ``AsyncAnthropic()`` is a *live, billed*
  client -- not an unauthenticated one.
* ``_optional`` blanking is asserted with ``is None``, never with ``not``.
  ``unit=""`` is falsy in Jinja yet still perturbs ``layout.py``'s ``--fit`` maths,
  which is the whole reason the collapse exists.

Three shapes under test exist *only* on the lenient ``construct_type`` path that
``messages.create`` actually takes -- a search result carrying no ``url``, one carrying
no ``title``, a fetch result missing ``source.media_type`` -- and
``Message.model_validate`` rejects all three. Rather than loosen the gate for every
fixture, those tests build their message with :func:`lenient_message`, which drives the
body through the real SDK response parser over ``MockTransport``. Still offline, still
the production parser, and the strict gate stays strict for everybody else.

There is no ``tests/conftest.py`` in this repo and this module does not add one, so
the live-gated test has nowhere to hide from an autouse fixture: the key deletion
below therefore checks the gate itself.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import json
import logging
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Final, NoReturn
from urllib.parse import urlparse

import anthropic
import httpx
import pydantic
import pytest
from anthropic import AsyncAnthropic, transform_schema
from anthropic.types import Message, TextBlock

from infographic_generator.core.models import Brief, ResearchContent, Source
from infographic_generator.core.ports import Researcher
from infographic_generator.research import agent
from infographic_generator.research.agent import (
    FOCUS_KEY,
    TONE_KEY,
    LlmResearcher,
    ResearchFailedError,
    ResearchSettings,
    retrieved_sources,
)

API_KEY_ENV: Final = "ANTHROPIC_API_KEY"
AUTH_TOKEN_ENV: Final = "ANTHROPIC_AUTH_TOKEN"
LIVE_GATE: Final = "INFOGRAPHIC_LIVE_API"
"""Defined here, not imported: ``agent.py`` deliberately has no ``API_KEY_ENV`` and
never imports ``os``, because resolving a key belongs to whoever builds the client."""

SEARCH_TOOL_ON_THE_WIRE: Final = "web_search_20260209"
FETCH_TOOL_ON_THE_WIRE: Final = "web_fetch_20260209"
"""Literals, and never ``agent._SEARCH_TOOL_TYPE``. Comparing the request body against
the module's own constant passes for *whatever* that constant holds, including
``web_search_20260318``, whose ``response_inclusion: "excluded"`` drops the result
blocks :func:`retrieved_sources` reads -- silently emptying the verified map and
therefore every citation in the poster. That swap is the one this file must catch, so
the expected value has to be written out here rather than imported."""

DEFAULTS: Final = ResearchSettings()

ZERO_WIDTH: Final = "\u200b"
BIDI_OVERRIDE: Final = "\u202e"
"""Both Unicode category ``Cf``, both invisible in a source file -- hence the names.
``.isspace()`` is ``False`` for both, so ``str.split()`` cannot see them."""


# --------------------------------------------------------------------------- #
# 1. Ground truth: what the server actually retrieved
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ``web_search_result``. A search hit carries no text and no timestamp."""

    url: str
    title: str = "A search hit"


@dataclass(frozen=True, slots=True)
class Fetched:
    """One ``web_fetch_result``. Only a fetch carries a retrieval time."""

    url: str
    title: str | None = "A fetched page"
    retrieved_at: str | None = "2026-07-20T12:00:00Z"


CENSUS: Final = "https://www.worldwildlife.org/species/giant-panda"
REDLIST: Final = "https://iucnredlist.org/species/712/121745669"
GEOGRAPHIC: Final = "https://www.nationalgeographic.com/animals/giant-panda"
BIODIVERSITY: Final = "https://ourworldindata.org/biodiversity"
DIET: Final = "https://pandasinternational.org/bamboo-diet"
SPOTLIGHT: Final = "https://www.si.edu/spotlight/giant-pandas"

SEARCH_HITS: Final = (
    SearchHit(CENSUS, "Giant panda | Species | WWF"),
    SearchHit(REDLIST, "Ailuropoda melanoleuca: IUCN Red List"),
    SearchHit(GEOGRAPHIC, "Giant panda facts"),
    SearchHit(BIODIVERSITY, "Biodiversity - Our World in Data"),
)
FETCHED_PAGES: Final = (
    Fetched(DIET, "Bamboo and the giant panda", "2026-07-20T12:00:00Z"),
    Fetched(SPOTLIGHT, "Giant pandas at the Zoo", "2026-07-20T12:00:00+05:30"),
)
RETRIEVED_URLS: Final = frozenset(
    {hit.url for hit in SEARCH_HITS} | {page.url for page in FETCHED_PAGES}
)

INVENTED: Final = "https://invented.example/never-retrieved"
ALSO_INVENTED: Final = "https://fabricated.example/no-such-page"


# --------------------------------------------------------------------------- #
# 2. The claim: what the model said, pointing into the retrievals
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DraftFact:
    label: str
    value: str
    unit: str | None = None
    detail: str | None = None
    url: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "detail": self.detail,
            "source_url": self.url,
        }


@dataclass(frozen=True, slots=True)
class DraftSection:
    heading: str
    body: str
    urls: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "heading": self.heading,
            "body": self.body,
            "source_urls": list(self.urls),
        }


DRAFT_FACTS: Final = (
    DraftFact("Daily bamboo intake", "12-38", "kg", "WWF range; sources differ", DIET),
    DraftFact("Wild population", "1,864", None, "2014 national survey", CENSUS),
    DraftFact("Cuteness", "maximal", None, None, None),
    DraftFact("Red List status", "Vulnerable", None, "Downlisted in 2016", REDLIST),
    DraftFact("Time spent eating", "14", "hours per day", None, SPOTLIGHT),
    DraftFact("Newborn weight", "100", "g", None, GEOGRAPHIC),
    DraftFact("Tail length", "10-15", "cm", None, None),
    DraftFact("Protected habitat", "1.4", "million hectares", None, BIODIVERSITY),
    DraftFact("Reserves", "67", None, "Across Sichuan, Shaanxi and Gansu", CENSUS),
    DraftFact("Gestation", "95-160", "days", None, DIET),
)
"""Ten facts. ``DRAFT_FACTS[0]`` is attributed -- the attribution floor is checked
on the *capped* list, so ``max_facts=1`` succeeds only because of that -- and
``DRAFT_FACTS[7]`` cites ``BIODIVERSITY``, which **nothing else cites**. Without
that one URL the bibliography-is-not-shrunk test cannot fail: at ``max_facts=7``
every other URL is already covered by an earlier fact or a section."""

DRAFT_SECTIONS: Final = (
    DraftSection(
        "A diet of one plant",
        "Bamboo is more than nine tenths of what a giant panda eats, and it is "
        "poor food: the gut of a carnivore digests little of it, so a panda must "
        "eat for most of its waking hours to stay in credit.",
        (DIET, CENSUS),
    ),
    DraftSection(
        "Off the endangered list",
        "The IUCN moved the giant panda from endangered to vulnerable in 2016 "
        "after two decades of habitat protection in Sichuan, Shaanxi and Gansu "
        "lifted the wild population back above eighteen hundred animals.",
        (REDLIST,),
    ),
    DraftSection(
        "Still a narrow range",
        "The recovery is real but local. Wild pandas live in six mountain ranges "
        "in a handful of Chinese provinces, in forest fragmented by roads and "
        "farmland, which keeps small populations from mixing.",
        (SPOTLIGHT, GEOGRAPHIC),
    ),
)

DRAFT_KEYWORDS: Final = (
    "giant panda eating bamboo",
    "panda cub",
    "bamboo forest sichuan",
    "wild panda habitat",
    "panda research centre",
)


@dataclass(frozen=True, slots=True)
class Draft:
    """What the model returns. One readable line per variation: ``replace(GOOD, ...)``."""

    title: str = "Giant pandas, by the numbers"
    subtitle: str = "What the census and the field studies actually say"
    summary: str = (
        "A giant panda is a bear that gave up meat for a grass. The arithmetic of "
        "that choice runs through everything else about the animal."
    )
    facts: tuple[DraftFact, ...] = DRAFT_FACTS
    sections: tuple[DraftSection, ...] = DRAFT_SECTIONS
    keywords: tuple[str, ...] = DRAFT_KEYWORDS

    def as_json(self) -> str:
        return json.dumps(
            {
                "title": self.title,
                "subtitle": self.subtitle,
                "summary": self.summary,
                "facts": [fact.as_dict() for fact in self.facts],
                "sections": [section.as_dict() for section in self.sections],
                "keywords": list(self.keywords),
            }
        )


GOOD: Final = Draft()
AVAILABLE: Final = len(DRAFT_FACTS)
SECTIONS: Final = len(DRAFT_SECTIONS)
CAP_VALUES: Final = (None, 0, 1, 2, 5, 9, 10, 11, 50, 10_000)

PROMPT: Final = "the giant panda"
NARRATION: Final = "I read the census page and two field summaries before answering."
POSTSCRIPT: Final = "Tell me if you want the underlying tables as well."


def make_brief(max_facts: int | None = None, **overrides: object) -> Brief:
    return Brief(prompt=PROMPT, max_facts=max_facts, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 3. Content blocks, and the envelope they hang in
# --------------------------------------------------------------------------- #


def text_block(text: str) -> dict[str, object]:
    return {"type": "text", "text": text}


def thinking_block() -> dict[str, object]:
    """``signature`` is required; forgetting it fails ``Message.model_validate``."""
    return {
        "type": "thinking",
        "thinking": "Fetch the census page before quoting a population.",
        "signature": "sig",
    }


def server_tool_use(
    name: str = "web_search", tool_use_id: str = "srvtoolu_x"
) -> dict[str, object]:
    return {
        "type": "server_tool_use",
        "id": tool_use_id,
        "name": name,
        "input": {"query": PROMPT},
    }


def search_block(
    hits: Sequence[SearchHit], *, tool_use_id: str = "srvtoolu_s"
) -> dict[str, object]:
    """``title`` and ``encrypted_content`` are both required on a search result."""
    return {
        "type": "web_search_tool_result",
        "tool_use_id": tool_use_id,
        "content": [
            {
                "type": "web_search_result",
                "url": hit.url,
                "title": hit.title,
                "encrypted_content": "E",
            }
            for hit in hits
        ],
    }


def search_error_block(error_code: str = "max_uses_exceeded") -> dict[str, object]:
    """The error branch is **first** in the union, and arrives on an HTTP 200."""
    return {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_se",
        "content": {"type": "web_search_tool_result_error", "error_code": error_code},
    }


def fetch_block(page: Fetched, *, tool_use_id: str = "srvtoolu_f") -> dict[str, object]:
    """``content.source.media_type`` is three levels down and mandatory.

    Omit it and the block silently degrades to a fetch *error* block, because
    ``WebFetchToolResultBlock.content`` is an undiscriminated union.
    """
    return {
        "type": "web_fetch_tool_result",
        "tool_use_id": tool_use_id,
        "content": {
            "type": "web_fetch_result",
            "url": page.url,
            "retrieved_at": page.retrieved_at,
            "content": {
                "type": "document",
                "title": page.title,
                "source": {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": "Bamboo is a grass.",
                },
            },
        },
    }


def fetch_error_block(error_code: str = "url_not_accessible") -> dict[str, object]:
    return {
        "type": "web_fetch_tool_result",
        "tool_use_id": "srvtoolu_fe",
        "content": {"type": "web_fetch_tool_result_error", "error_code": error_code},
    }


def bare_search_block(
    results: Sequence[Mapping[str, object]], *, tool_use_id: str = "srvtoolu_bare"
) -> dict[str, object]:
    """A search result batch with no field required, for the lenient path only."""
    return {
        "type": "web_search_tool_result",
        "tool_use_id": tool_use_id,
        "content": list(results),
    }


def search_result(
    url: str | None = None, title: str | None = None
) -> dict[str, object]:
    """One ``web_search_result`` element with either field simply absent."""
    element: dict[str, object] = {"type": "web_search_result", "encrypted_content": "E"}
    if url is not None:
        element["url"] = url
    if title is not None:
        element["title"] = title
    return element


def malformed_fetch_block(url: str = DIET) -> dict[str, object]:
    """A fetch *success* block missing ``content.source.media_type``.

    The lenient union resolves it to a ``WebFetchToolResultErrorBlock`` whose
    ``error_code`` is ``None`` -- which is why the log line says "malformed result
    block" rather than "None".
    """
    return {
        "type": "web_fetch_tool_result",
        "tool_use_id": "srvtoolu_bad",
        "content": {
            "type": "web_fetch_result",
            "url": url,
            "retrieved_at": "2026-07-20T12:00:00Z",
            "content": {
                "type": "document",
                "title": "Bamboo",
                "source": {"type": "text", "data": "Bamboo is a grass."},
            },
        },
    }


ABSENT: Final[object] = object()
"""``content`` not present on a result block at all -- a different wire shape from
``content: null``, and ``None`` is itself one of the shapes under test, so the marker
for "the key is missing" cannot be ``None``."""


def resultless_search_block(content: object = ABSENT) -> dict[str, object]:
    """A ``web_search_tool_result`` whose ``content`` is neither the error nor a list.

    Measured on ``anthropic`` 0.120.2: every shape the table below passes here still
    constructs as a real ``WebSearchToolResultBlock``, so it clears the
    ``WebSearchToolResultError`` check and lands on ``for result in results:``.
    """
    block: dict[str, object] = {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_noresults",
    }
    if content is not ABSENT:
        block["content"] = content
    return block


def documentless_fetch_block(content: object = ABSENT) -> dict[str, object]:
    """A ``web_fetch_tool_result`` whose ``content`` is no ``WebFetchBlock``.

    A *list* is in the table because it is the shape that looks most like a success:
    its elements really do construct as ``WebFetchToolResultErrorBlock``, so only the
    container gives it away -- and ``document.url`` on a list is an ``AttributeError``.
    """
    block: dict[str, object] = {
        "type": "web_fetch_tool_result",
        "tool_use_id": "srvtoolu_nodocument",
    }
    if content is not ABSENT:
        block["content"] = content
    return block


def envelope(
    draft: Draft | None = GOOD,
    *,
    searched: Sequence[SearchHit] = SEARCH_HITS,
    fetched: Sequence[Fetched] = FETCHED_PAGES,
    stop_reason: str | None = "end_turn",
    texts: Sequence[str] | None = None,
    extra_blocks: Sequence[Mapping[str, object]] = (),
    thinking: bool = True,
    search_first: bool = True,
    trailing_tool_use: bool = True,
) -> dict[str, object]:
    """A whole assistant turn, strictly validated before it is ever served.

    The default shape is the one that kills ``messages.parse``: a ``thinking``
    block, narration **before** the JSON, a postscript **after** it, and a
    ``server_tool_use`` block last -- so any implementation reading ``content[-1]``
    or trusting a single text block fails here.
    """
    if texts is None:
        if draft is None:
            raise AssertionError("give envelope() a draft or an explicit texts=")
        texts = (NARRATION, draft.as_json(), POSTSCRIPT)

    searches: list[Mapping[str, object]] = [
        server_tool_use("web_search", "srvtoolu_s"),
        search_block(searched),
    ]
    fetches: list[Mapping[str, object]] = []
    for index, page in enumerate(fetched):
        fetches.append(server_tool_use("web_fetch", f"srvtoolu_f{index}"))
        fetches.append(fetch_block(page, tool_use_id=f"srvtoolu_f{index}"))

    content: list[Mapping[str, object]] = []
    if thinking:
        content.append(thinking_block())
    content.extend(searches + fetches if search_first else fetches + searches)
    content.extend(extra_blocks)
    content.extend(text_block(text) for text in texts)
    if trailing_tool_use:
        content.append(server_tool_use("web_search", "srvtoolu_last"))

    body: dict[str, object] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "stop_reason": stop_reason,
        "content": content,
        "usage": {"input_tokens": 41_000, "output_tokens": 2_100},
    }
    Message.model_validate(body)
    return body


@dataclass(slots=True)
class FakeApi:
    """Serves one canned assistant turn through a ``MockTransport``, recording asks."""

    body: Mapping[str, object] = field(default_factory=envelope)
    delay_s: float = 0.0
    requests: list[httpx.Request] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return httpx.Response(200, json=dict(self.body))

    def client(self, *, api_key: str | None = "test") -> AsyncAnthropic:
        """``max_retries=0`` -- exactly one HTTP attempt, so call counts are safe."""
        return AsyncAnthropic(
            api_key=api_key,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )

    @property
    def sent(self) -> list[dict[str, object]]:
        bodies: list[dict[str, object]] = [
            json.loads(request.content) for request in self.requests
        ]
        return bodies


async def research(
    fake: FakeApi | None = None,
    brief: Brief | None = None,
    settings: ResearchSettings = DEFAULTS,
) -> ResearchContent:
    api = FakeApi() if fake is None else fake
    async with api.client() as client:
        return await LlmResearcher(client, settings).research(brief or make_brief())


FUTURE_BLOCK: Final[Mapping[str, object]] = {
    "type": "some_future_block_2027",
    "payload": {"a": 1},
}
"""An unrecognised block type. On the lenient path it constructs as ``TextBlock`` with
``text=None`` -- verified below, not assumed -- which is the shape ``_draft_from`` skips
structurally rather than by catching whatever pydantic raises for it this year."""


def spliced(
    body: Mapping[str, object],
    blocks: Sequence[Mapping[str, object]],
    *,
    first: bool = False,
) -> dict[str, object]:
    """Append content blocks *after* ``envelope()``'s strict gate has already run.

    The gate stays: it is what stops a typo'd fixture proving the opposite of what it
    claims. But it cannot express a block the API can genuinely return and
    ``Message.model_validate`` refuses, so those arrive here -- visibly, one call site
    at a time -- rather than by weakening ``envelope()`` for all sixty other tests.
    """
    content = body["content"]
    assert isinstance(content, list)
    ordered = [*blocks, *content] if first else [*content, *blocks]
    return {**body, "content": ordered}


async def lenient_message(body: Mapping[str, object]) -> Message:
    """The message exactly as ``messages.create`` builds it: leniently, and for real.

    Offline throughout -- ``MockTransport`` serves ``body`` and the SDK's own response
    parser does the construction, so what comes back is the object the module reads in
    production rather than our belief about it.
    """
    api = FakeApi(body)
    async with api.client() as client:
        return await client.messages.create(
            model="claude-opus-5",
            max_tokens=16,
            messages=[{"role": "user", "content": PROMPT}],
        )


async def lenient_harvest(*blocks: Mapping[str, object]) -> Mapping[str, Source]:
    """:func:`harvest`, for the blocks only the lenient path can carry."""
    return retrieved_sources(await lenient_message(spliced(envelope(), blocks)))


def harvest(
    *,
    searched: Sequence[SearchHit] = SEARCH_HITS,
    fetched: Sequence[Fetched] = FETCHED_PAGES,
    extra_blocks: Sequence[Mapping[str, object]] = (),
    search_first: bool = True,
) -> Mapping[str, Source]:
    """``retrieved_sources`` is pure, so most of the fence needs no client at all."""
    message = Message.model_validate(
        envelope(
            searched=searched,
            fetched=fetched,
            extra_blocks=extra_blocks,
            search_first=search_first,
        )
    )
    return retrieved_sources(message)


def fact_sources(content: ResearchContent) -> tuple[Source, ...]:
    return tuple(fact.source for fact in content.facts if fact.source is not None)


def section_sources(content: ResearchContent) -> tuple[Source, ...]:
    return tuple(
        source for section in content.sections for source in section.sources
    )


def every_source(content: ResearchContent) -> tuple[Source, ...]:
    return (*content.sources, *fact_sources(content), *section_sources(content))


def every_url(content: ResearchContent) -> set[str]:
    return {source.url for source in every_source(content)}


# --------------------------------------------------------------------------- #
# 4. The gate. Key presence is not consent; only INFOGRAPHIC_LIVE_API=1 is.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def offline_unless_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """No credentials in the environment for any test here -- unless the run is live.

    Deliberately *not* named ``offline``: ``test_agent_composer.py`` owns that name
    for an unconditional deletion, and the two conventions must stay
    distinguishable. Unconditional here would hand the live test a 401 that looks
    exactly like an ``--env-file`` problem, and there is no ``conftest.py`` for it
    to hide in.
    """
    if not os.environ.get(LIVE_GATE):
        monkeypatch.delenv(API_KEY_ENV, raising=False)
        monkeypatch.delenv(AUTH_TOKEN_ENV, raising=False)


@pytest.fixture
def live_api_or_skip() -> AsyncAnthropic:
    """Gated on intent, never on key presence -- a real key is exported in our shells."""
    if os.environ.get(LIVE_GATE) != "1":
        pytest.skip(f"set {LIVE_GATE}=1 to spend money on a real API call")
    key = os.environ.get(API_KEY_ENV)
    if not key:
        pytest.fail(f"{LIVE_GATE}=1 but {API_KEY_ENV} is unset")
    return AsyncAnthropic(api_key=key)


@pytest.fixture
def kolkata(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A non-UTC process timezone. Without it the naive-timestamp bug is invisible."""
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# --------------------------------------------------------------------------- #
# 5. Fixture-invariant guards: three of the tables below are only honest if these hold
# --------------------------------------------------------------------------- #


def test_the_fixture_can_exercise_the_fact_floor() -> None:
    assert AVAILABLE > DEFAULTS.min_facts
    assert SECTIONS >= 3
    assert DEFAULTS.min_keywords <= len(DRAFT_KEYWORDS) <= DEFAULTS.max_keywords


def test_the_cap_values_bracket_the_fixture() -> None:
    assert {AVAILABLE - 1, AVAILABLE, AVAILABLE + 1} <= set(CAP_VALUES)


def test_the_leading_fact_is_attributed() -> None:
    """``max_facts=1`` survives the attribution floor only because of this.

    Membership in ``RETRIEVED_URLS``, not merely ``is not None``: an invented URL
    would satisfy the weaker check and then fail the cross-check, collapsing the
    whole ``max_facts=1`` row while this guard still passed.
    """
    assert DRAFT_FACTS[0].url in RETRIEVED_URLS


def test_a_late_fact_cites_a_url_nothing_else_does() -> None:
    """Without this, ``capping_does_not_shrink_the_bibliography`` cannot fail."""
    late = {fact.url for fact in DRAFT_FACTS[1:] if fact.url}
    covered = {url for s in DRAFT_SECTIONS for url in s.urls} | {DRAFT_FACTS[0].url}

    assert late - covered, "no cap value can distinguish a pre-cap bibliography"


def test_the_fixture_claims_some_urls_more_than_once() -> None:
    """Without a repeat, the bibliography-dedupe test cannot fail."""
    claimed = [fact.url for fact in DRAFT_FACTS if fact.url]
    claimed += [url for section in DRAFT_SECTIONS for url in section.urls]

    assert len(claimed) > len(set(claimed))


def test_the_fixture_spreads_its_facts_over_several_publishers() -> None:
    """Without this, ``_warn_if_single_sourced`` would fire on the happy path."""
    hosts = {urlparse(url).hostname for fact in DRAFT_FACTS if (url := fact.url)}

    assert len(hosts) > 1


def test_every_draft_url_was_really_retrieved() -> None:
    """The happy path must not accidentally rely on the fence rejecting something."""
    claimed = {fact.url for fact in DRAFT_FACTS if fact.url}
    claimed |= {url for section in DRAFT_SECTIONS for url in section.urls}

    assert claimed <= RETRIEVED_URLS


def test_the_default_envelope_hides_the_json_between_other_blocks() -> None:
    """The exact shape that kills ``messages.parse`` -- and the reason for ``create``."""
    content = envelope()["content"]
    assert isinstance(content, list)

    kinds = [block["type"] for block in content]
    texts = [index for index, kind in enumerate(kinds) if kind == "text"]

    assert kinds[0] == "thinking"
    assert kinds[-1] == "server_tool_use", "content[-1] must never be the JSON"
    assert len(texts) == 3, "narration must sit both before and after the JSON"


# --------------------------------------------------------------------------- #
# 6. Happy shape: the stub suite's contract, held against the real agent
# --------------------------------------------------------------------------- #


async def test_research_returns_a_fully_populated_document() -> None:
    content = await research()

    assert content.title == GOOD.title
    assert content.subtitle == GOOD.subtitle
    assert content.summary == GOOD.summary
    assert len(content.facts) == AVAILABLE
    assert len(content.sections) == SECTIONS
    assert 3 <= len(content.keywords) <= 8
    assert all(keyword.strip() for keyword in content.keywords)
    assert content.sources


async def test_every_fact_and_section_carries_its_own_text() -> None:
    content = await research()

    assert all(fact.label and fact.value for fact in content.facts)
    assert all(section.heading and section.body for section in content.sections)


async def test_sequence_fields_are_tuples() -> None:
    content = await research()
    assert len(content.sections) == SECTIONS, "an empty sections makes the last clause vacuous"

    assert isinstance(content.facts, tuple)
    assert isinstance(content.sections, tuple)
    assert isinstance(content.keywords, tuple)
    assert isinstance(content.sources, tuple)
    assert all(isinstance(section.sources, tuple) for section in content.sections)


async def test_the_returned_content_is_frozen() -> None:
    content = await research()

    with pytest.raises(dataclasses.FrozenInstanceError):
        content.title = "other"  # type: ignore[misc]

    with pytest.raises(dataclasses.FrozenInstanceError):
        content.facts[0].value = "other"  # type: ignore[misc]


async def test_llm_researcher_satisfies_the_researcher_port() -> None:
    fake = FakeApi()
    async with fake.client() as client:
        researcher: Researcher = LlmResearcher(client)

        content = await researcher.research(make_brief())

    assert content.title == GOOD.title
    assert content.subtitle == GOOD.subtitle


async def test_the_same_response_parses_the_same_way() -> None:
    """Replaces the stub's determinism test; still catches accumulating state."""
    fake = FakeApi()

    assert await research(fake) == await research(fake)
    assert len(fake.requests) == 2


async def test_research_makes_exactly_one_api_call() -> None:
    fake = FakeApi()

    await research(fake)

    assert len(fake.requests) == 1


def tools_by_type(body: Mapping[str, object]) -> Mapping[str, Mapping[str, object]]:
    tools = body["tools"]
    assert isinstance(tools, list)
    return {str(tool["type"]): tool for tool in tools}


async def test_the_wire_call_carries_the_right_knobs() -> None:
    """Every knob is overridden to a value that is *not* the default.

    ``body["model"] == settings.model`` would pass whatever either side held, so the
    expected values are literals here and the shipping defaults are pinned separately,
    by :func:`test_the_shipping_defaults_are_pinned_to_literals`.
    """
    settings = ResearchSettings(
        model="claude-haiku-5",
        max_tokens=4_096,
        effort="medium",
        max_search_uses=3,
        max_fetch_uses=7,
        max_fetch_content_tokens=999,
    )
    fake = FakeApi()

    await research(fake, settings=settings)

    body = fake.sent[0]
    assert body["model"] == "claude-haiku-5"
    assert body["max_tokens"] == 4_096
    # Not de-tautologised: the alternative is a 500-line prompt snapshot. What the
    # prompt has to *say* is pinned by the phrase tests in section 15 instead; this
    # asserts only that it arrives on the wire unabridged.
    assert body["system"] == agent._RESEARCH_SYSTEM
    assert "stream" not in body, "this module is non-streaming"
    output_config = body["output_config"]
    assert isinstance(output_config, dict)
    assert output_config["effort"] == "medium"
    assert output_config["format"] == {
        "schema": transform_schema(agent._ResearchResponse),
        "type": "json_schema",
    }
    by_type = tools_by_type(body)
    assert set(by_type) == {SEARCH_TOOL_ON_THE_WIRE, FETCH_TOOL_ON_THE_WIRE}
    assert all(tool["allowed_callers"] == ["direct"] for tool in by_type.values())
    assert by_type[SEARCH_TOOL_ON_THE_WIRE]["max_uses"] == 3
    assert by_type[FETCH_TOOL_ON_THE_WIRE]["max_uses"] == 7
    assert by_type[FETCH_TOOL_ON_THE_WIRE]["max_content_tokens"] == 999
    for rejected in ("temperature", "top_p", "top_k", "thinking"):
        assert rejected not in body, f"Opus 5 rejects {rejected}"


def test_the_shipping_defaults_are_pinned_to_literals() -> None:
    """Every default written out, deliberately as literals rather than as a comparison.

    ``asdict(ResearchSettings()) == {field: getattr(DEFAULTS, field) ...}`` restates the
    dataclass and therefore passes for any value it holds: ``model`` could go to Haiku,
    ``effort`` to ``"low"`` and ``max_search_uses`` to 50 with the suite still green.
    These are the shipping configuration, they cost real money and real accuracy, and
    changing one should be a visible edit to this table.
    """
    assert dataclasses.asdict(ResearchSettings()) == {
        "model": "claude-opus-5",
        "effort": "high",
        "timeout_s": 300.0,
        "max_tokens": 16_000,
        "max_search_uses": 5,
        "max_fetch_uses": 5,
        "max_fetch_content_tokens": 30_000,
        "target_facts": 8,
        "target_sections": 3,
        "min_facts": 5,
        "min_keywords": 3,
        "max_keywords": 8,
        "max_sections": 6,
        "max_sources_per_section": 2,
    }


async def test_the_default_settings_are_what_reaches_the_wire() -> None:
    """The complement of the table above: the defaults are also *sent*, not just held."""
    fake = FakeApi()

    await research(fake)

    body = fake.sent[0]
    output_config = body["output_config"]
    assert isinstance(output_config, dict)
    assert body["model"] == "claude-opus-5"
    assert body["max_tokens"] == 16_000
    assert output_config["effort"] == "high"
    by_type = tools_by_type(body)
    assert by_type[SEARCH_TOOL_ON_THE_WIRE]["max_uses"] == 5
    assert by_type[FETCH_TOOL_ON_THE_WIRE]["max_uses"] == 5
    assert by_type[FETCH_TOOL_ON_THE_WIRE]["max_content_tokens"] == 30_000


async def test_the_declared_tools_are_the_pair_that_returns_result_blocks() -> None:
    """The ``_20260318`` swap is invisible to every other assertion in this file.

    Its ``response_inclusion: "excluded"`` returns no ``web_search_result`` blocks, so
    ``retrieved_sources`` verifies nothing, every citation is dropped and the failure
    surfaces only as a live run that suddenly cannot attribute a single fact.
    """
    fake = FakeApi()

    await research(fake)

    assert set(tools_by_type(fake.sent[0])) == {
        "web_search_20260209",
        "web_fetch_20260209",
    }


async def test_the_schema_on_the_wire_is_the_transformed_one() -> None:
    """``transform_schema`` is a *stricter* request than ``model_json_schema()``."""
    fake = FakeApi()

    await research(fake)

    output_config = fake.sent[0]["output_config"]
    assert isinstance(output_config, dict)
    schema = output_config["format"]["schema"]
    assert schema != agent._ResearchResponse.model_json_schema()
    assert schema["additionalProperties"] is False


async def test_code_execution_is_never_declared() -> None:
    fake = FakeApi()

    await research(fake)

    tools = fake.sent[0]["tools"]
    assert isinstance(tools, list)
    assert not any(str(tool["type"]).startswith("code_execution") for tool in tools)
    assert not any("citations" in tool for tool in tools)


async def test_nothing_returned_is_a_pydantic_model() -> None:
    content = await research()

    for item in (content, *content.facts, *content.sections, *every_source(content)):
        assert not isinstance(item, pydantic.BaseModel), item
        params = type(item).__dataclass_params__  # type: ignore[attr-defined]
        assert params.frozen, item


def test_the_modules_entire_pydantic_inventory_is_private() -> None:
    """Pins the boundary: three schemas, all private, all defined here.

    Filtered to models *declared* in the module -- the imported SDK response types
    are pydantic too, and counting them would make the assertion a list of the
    SDK's inventory rather than of ours.
    """
    declared = {
        name
        for name, value in vars(agent).items()
        if isinstance(value, type)
        and issubclass(value, pydantic.BaseModel)
        and value.__module__ == agent.__name__
    }

    assert declared == {"_Fact", "_Section", "_ResearchResponse"}, (
        f"unexpected pydantic declared in the module: {sorted(declared)}"
    )
    assert all(name.startswith("_") for name in declared)


def test_the_module_never_imports_os() -> None:
    """Walk the AST, never grep: ``from os import environ`` defeats a substring test."""
    tree = ast.parse(inspect.getsource(agent))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "os" not in imported, "key resolution belongs to whoever builds the client"


def test_the_client_is_required() -> None:
    """No default client, so the module cannot conjure a live one from the shell."""
    with pytest.raises(TypeError):
        LlmResearcher()  # type: ignore[call-arg]


def test_the_schema_gives_the_model_nowhere_to_author_a_source() -> None:
    """Goes red the day somebody helpfully adds ``source_title``."""
    assert set(agent._Fact.model_fields) == {
        "label",
        "value",
        "unit",
        "detail",
        "source_url",
    }
    assert set(agent._Section.model_fields) == {"heading", "body", "source_urls"}
    assert "sources" not in agent._ResearchResponse.model_fields


async def test_an_unusual_brief_is_tolerated() -> None:
    brief = Brief(
        prompt=PROMPT,
        audience="curious eight-year-olds",
        locale="ar-EG",
        extras={TONE_KEY: "playful", "wholly.unknown": "NEEDLE-OUTSIDE-NAMESPACE"},
    )

    content = await research(brief=brief)

    assert content.title and content.facts


async def test_unknown_extras_are_ignored_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Asserting only that the *key* is absent passes while the value leaks."""
    brief = Brief(
        prompt=PROMPT,
        audience="curious eight-year-olds",
        locale="ar-EG",
        extras={
            TONE_KEY: "playful",
            "research.bogus": "NEEDLE-UNKNOWN-RESEARCH-KEY",
            "wholly.unknown": "NEEDLE-OUTSIDE-NAMESPACE",
        },
    )
    fake = FakeApi()

    with caplog.at_level(logging.DEBUG, logger=agent.__name__):
        await research(fake, brief=brief)

    dumped = json.dumps(fake.sent[0])
    assert "playful" in dumped
    assert "curious eight-year-olds" in dumped
    assert "ar-EG" in dumped
    assert "NEEDLE-" not in dumped, "an unsupported extra's value reached the prompt"
    assert "research.bogus" in caplog.text


def test_the_prompt_asks_for_the_counts_the_settings_name() -> None:
    settings = ResearchSettings(target_sections=4, min_keywords=2, max_keywords=6)
    brief = make_brief(audience="marine biologists", locale="pt-BR")
    brief = replace(brief, extras={FOCUS_KEY: "the 2016 downlisting"})

    prompt = agent._research_prompt(brief, settings, target_facts=9)

    assert "9 facts" in prompt
    assert "4 narrative sections" in prompt
    assert "2-6 image-search keywords" in prompt
    assert "marine biologists" in prompt
    assert "pt-BR" in prompt
    assert "the 2016 downlisting" in prompt


def test_the_prompt_names_the_per_section_source_cap() -> None:
    """``max_sources_per_section`` is enforced in Python; the model has to hear it too.

    Otherwise it offers four URLs per section, three get silently dropped and the
    setting reads as a bug report about missing sources.
    """
    settings = ResearchSettings(max_sources_per_section=4)

    prompt = agent._research_prompt(make_brief(), settings, target_facts=8)

    assert "at most 4 URLs" in prompt
    assert "at most 2 URLs" not in prompt


def test_a_long_stage_hint_is_clipped_to_two_hundred_characters() -> None:
    """The hint keys are the values most likely to arrive pasted in bulk.

    200 is written out rather than read off ``agent._MAX_HINT_CHARS``, so widening the
    clip is a visible edit here; the topic itself is deliberately uncapped.
    """
    hint = "".join(str(index % 10) for index in range(260))
    brief = replace(make_brief(), extras={FOCUS_KEY: hint})

    prompt = agent._research_prompt(brief, DEFAULTS, target_facts=8)

    assert hint[:200] in prompt
    assert hint[:201] not in prompt, "the 201st character of a hint reached the prompt"


@pytest.mark.parametrize(
    "audience",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="spaces"),
        pytest.param("\n\t", id="whitespace"),
    ],
)
def test_a_blank_audience_falls_back_to_the_general_reader(audience: str | None) -> None:
    """Interpolating an unset ``audience`` would tell the model to write for "None"."""
    brief = Brief(prompt=PROMPT, audience=audience)

    prompt = agent._research_prompt(brief, DEFAULTS, target_facts=8)

    assert "Reader: a curious general adult audience." in prompt
    assert "None" not in prompt


@pytest.mark.parametrize(
    ("max_facts", "expected"),
    [(None, 8), (0, 1), (1, 1), (3, 3), (12, 12), (13, 12), (10_000, 12)],
)
def test_the_requested_fact_count_is_bounded_by_the_cap_and_the_ceiling(
    max_facts: int | None, expected: int
) -> None:
    assert agent._target_facts(make_brief(max_facts), DEFAULTS) == expected


@pytest.mark.parametrize(
    ("max_facts", "expected"), [(None, 5), (0, 0), (1, 1), (4, 4), (5, 5), (99, 5)]
)
def test_max_facts_lowers_the_fact_floor_but_never_raises_it(
    max_facts: int | None, expected: int
) -> None:
    assert agent._fact_floor(make_brief(max_facts), DEFAULTS) == expected


# --------------------------------------------------------------------------- #
# 7. Narration, thinking, and the block the module must never read
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "texts",
    [
        pytest.param((GOOD.as_json(),), id="json-alone"),
        pytest.param((NARRATION, GOOD.as_json()), id="narration-before"),
        pytest.param((GOOD.as_json(), POSTSCRIPT), id="narration-after"),
        pytest.param((NARRATION, GOOD.as_json(), POSTSCRIPT), id="narration-both"),
        pytest.param(("{not json", GOOD.as_json()), id="garbled-then-good"),
    ],
)
async def test_narration_around_the_json_block_still_parses(
    texts: tuple[str, ...],
) -> None:
    """Every one of these orders raises inside ``messages.parse``. ``create`` survives."""
    content = await research(FakeApi(envelope(texts=texts)))

    assert content.title == GOOD.title
    assert len(content.facts) == AVAILABLE


async def test_a_thinking_block_is_not_mistaken_for_the_answer() -> None:
    fake = FakeApi(envelope(thinking=True, texts=(NARRATION, GOOD.as_json())))

    content = await research(fake)

    assert content.title == GOOD.title


@pytest.mark.parametrize(
    "texts",
    [
        pytest.param(("Here are some interesting panda facts.",), id="prose-only"),
        pytest.param(("{not json",), id="garbled"),
        pytest.param(('{"title": "half a doc"}',), id="missing-required-fields"),
        pytest.param((), id="no-text-block-at-all"),
    ],
)
async def test_a_reply_no_text_block_validates_is_a_failure(
    texts: tuple[str, ...],
) -> None:
    with pytest.raises(ResearchFailedError):
        await research(FakeApi(envelope(texts=texts)))


async def test_an_unparseable_reply_chains_the_first_validation_error() -> None:
    """``prose`` and ``omitted subtitle`` call for opposite responses; keep them apart."""
    with pytest.raises(ResearchFailedError) as raised:
        await research(FakeApi(envelope(texts=("prose", "{not json"))))

    assert isinstance(raised.value.__cause__, pydantic.ValidationError)


async def test_an_empty_text_block_beside_the_json_is_skipped() -> None:
    content = await research(FakeApi(envelope(texts=("", GOOD.as_json()))))

    assert content.title == GOOD.title


async def test_a_block_with_no_text_at_all_is_skipped_not_validated() -> None:
    """An unrecognised block type constructs as ``TextBlock(text=None)``, and the reply
    still has to parse around it."""
    message = await lenient_message(spliced(envelope(), (FUTURE_BLOCK,), first=True))
    assert any(
        isinstance(block, TextBlock) and block.text is None for block in message.content
    ), "the fixture no longer produces a text-less block; the skip is untested"

    assert agent._draft_from(message).title == GOOD.title


async def test_a_text_less_block_never_becomes_the_reported_failure() -> None:
    """The chained cause has to be a report about what the *model* wrote.

    ``pydantic`` currently answers ``model_validate_json(None)`` with a ``ValidationError``
    about JSON input types, so without the structural skip that error -- about a block
    the model never wrote -- is the one kept as ``first`` and logged. "the model omitted
    ``subtitle``" and "a block had no text" call for opposite responses.
    """
    half = '{"title": "half a doc"}'
    message = await lenient_message(
        spliced(envelope(texts=(half,)), (FUTURE_BLOCK,), first=True)
    )

    with pytest.raises(ResearchFailedError) as raised:
        agent._draft_from(message)

    cause = raised.value.__cause__
    assert isinstance(cause, pydantic.ValidationError)
    assert "subtitle" in str(cause)


# --------------------------------------------------------------------------- #
# 8. max_facts: the cap, the two floors, and the bibliography
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("max_facts", CAP_VALUES)
async def test_max_facts_caps_the_fact_count(max_facts: int | None) -> None:
    facts = (await research(brief=make_brief(max_facts))).facts

    expected = AVAILABLE if max_facts is None else min(max_facts, AVAILABLE)
    assert len(facts) == expected


@pytest.mark.parametrize("max_facts", range(0, 13))
async def test_capping_keeps_the_most_significant_facts_in_order(
    max_facts: int,
) -> None:
    uncapped = await research()

    capped = await research(brief=make_brief(max_facts))

    assert tuple(capped.facts) == tuple(uncapped.facts)[:max_facts]


@pytest.mark.parametrize("max_facts", [0, 1, 3, 7])
async def test_capping_facts_does_not_shrink_the_bibliography(max_facts: int) -> None:
    uncapped = await research()

    capped = await research(brief=make_brief(max_facts))

    assert tuple(capped.sources) == tuple(uncapped.sources)


@pytest.mark.parametrize("max_facts", [-1, -10])
async def test_negative_max_facts_is_rejected_before_any_request(
    max_facts: int,
) -> None:
    fake = FakeApi()

    with pytest.raises(ValueError, match=f"max_facts cannot be negative: {max_facts}"):
        await research(fake, brief=make_brief(max_facts))

    assert fake.requests == []


@pytest.mark.parametrize("prompt", ["", " ", "\n\t "])
async def test_a_blank_prompt_is_rejected_before_any_request(prompt: str) -> None:
    fake = FakeApi()

    with pytest.raises(ValueError, match="prompt cannot be empty"):
        await research(fake, brief=Brief(prompt=prompt))

    assert fake.requests == []


@pytest.mark.parametrize("kept", [0, 1, 4])
async def test_too_few_facts_is_a_failure(kept: int) -> None:
    thin = replace(GOOD, facts=GOOD.facts[:kept])

    with pytest.raises(ResearchFailedError, match="usable facts"):
        await research(FakeApi(envelope(thin)))


async def test_the_fact_floor_is_relative_to_the_caller() -> None:
    """A three-fact answer *raises* uncapped and *succeeds* at ``max_facts=3``.

    Surprising, and deliberate: a caller who asks for three facts is asking for
    three, not for a failure. Do not "fix" this.
    """
    thin = FakeApi(envelope(replace(GOOD, facts=GOOD.facts[:3])))

    with pytest.raises(ResearchFailedError):
        await research(thin)

    assert len((await research(thin, brief=make_brief(3))).facts) == 3


async def test_facts_with_blank_text_are_dropped() -> None:
    """Survivors stay above ``min_facts``, so only the drop can explain the count."""
    facts = list(GOOD.facts)
    facts[2] = replace(facts[2], label="   ")
    facts[5] = replace(facts[5], value="")
    facts[6] = replace(facts[6], label="\n\t")
    expected = tuple(
        fact.label for index, fact in enumerate(GOOD.facts) if index not in {2, 5, 6}
    )

    content = await research(FakeApi(envelope(replace(GOOD, facts=tuple(facts)))))

    assert len(content.facts) == AVAILABLE - 3
    assert tuple(fact.label for fact in content.facts) == expected


async def test_facts_are_dropped_before_the_floor_is_counted() -> None:
    """Ten raw facts clear the floor; the four that survive do not."""
    facts = tuple(
        replace(fact, label="  ") if index >= 4 else fact
        for index, fact in enumerate(GOOD.facts)
    )

    with pytest.raises(ResearchFailedError, match="only 4 usable facts"):
        await research(FakeApi(envelope(replace(GOOD, facts=facts))))


async def test_a_capped_set_with_no_attribution_is_a_failure() -> None:
    """Gated on the cap: ``max_facts=0`` has nothing to attribute and must succeed."""
    unattributed = replace(
        GOOD,
        facts=tuple(replace(fact, url=None) for fact in GOOD.facts),
        sections=tuple(replace(section, urls=()) for section in GOOD.sections),
    )
    fake = FakeApi(envelope(unattributed))

    with pytest.raises(ResearchFailedError, match="no fact carries a verified source"):
        await research(fake)

    assert (await research(fake, brief=make_brief(0))).facts == ()


@pytest.mark.parametrize("max_facts", [None, 1, 2, 5, AVAILABLE])
async def test_at_least_one_fact_is_attributed_whenever_the_cap_allows_one(
    max_facts: int | None,
) -> None:
    content = await research(brief=make_brief(max_facts))

    assert fact_sources(content), "an uncited poster is what this stage prevents"


async def test_an_unattributed_leading_fact_is_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At a low ``max_facts`` that warning is the difference between a run and a failure."""
    facts = (replace(GOOD.facts[0], url=None), *GOOD.facts[1:])
    fake = FakeApi(envelope(replace(GOOD, facts=facts)))

    with caplog.at_level(logging.WARNING, logger=agent.__name__):
        content = await research(fake)

    assert content.facts[0].source is None
    assert "carries no verified source" in caplog.text

    with pytest.raises(ResearchFailedError):
        await research(fake, brief=make_brief(1))


# --------------------------------------------------------------------------- #
# 9. Content validation: blanks, keywords, sections
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field_name", ["title", "subtitle", "summary"])
@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
async def test_a_blank_heading_field_is_a_failure(field_name: str, blank: str) -> None:
    draft = replace(GOOD, **{field_name: blank})  # type: ignore[arg-type]

    with pytest.raises(ResearchFailedError, match=f"empty {field_name}"):
        await research(FakeApi(envelope(draft)))


async def test_a_blank_title_is_reported_before_fact_poverty() -> None:
    """A draft that is *both* blank-titled and fact-poor reports the title.

    Deliberate precedence, not Python's argument-evaluation order: the three heading
    checks run above ``_check_facts``. "only 2 usable facts" would send an operator to
    raise ``max_fetch_uses`` when the model in fact returned no headline at all.
    """
    draft = replace(GOOD, title="   ", facts=GOOD.facts[:2])

    with pytest.raises(ResearchFailedError, match="the model returned an empty title"):
        await research(FakeApi(envelope(draft)))


async def test_the_cap_counts_only_usable_facts() -> None:
    """A blank inside the first three must not cost the caller a fact.

    Placed at index 1 on purpose: a cap-then-drop implementation returns two facts
    here, which is the whole point of dropping before counting.
    """
    facts = (GOOD.facts[0], replace(GOOD.facts[1], value="   "), *GOOD.facts[2:])

    content = await research(
        FakeApi(envelope(replace(GOOD, facts=facts))), brief=make_brief(3)
    )

    assert len(content.facts) == 3
    assert tuple(fact.label for fact in content.facts) == (
        GOOD.facts[0].label,
        GOOD.facts[2].label,
        GOOD.facts[3].label,
    )


@pytest.mark.parametrize("field_name", ["unit", "detail"])
@pytest.mark.parametrize("blank", ["", "  ", "\n"])
async def test_optional_fact_text_is_never_the_empty_string(
    field_name: str, blank: str
) -> None:
    """``is None``, not falsiness: ``unit=""`` still perturbs ``layout.py``'s fit maths."""
    facts = (replace(GOOD.facts[0], **{field_name: blank}), *GOOD.facts[1:])

    content = await research(FakeApi(envelope(replace(GOOD, facts=facts))))

    assert getattr(content.facts[0], field_name) is None


async def test_fact_text_is_whitespace_collapsed_not_merely_stripped() -> None:
    facts = (
        replace(GOOD.facts[0], label="  Daily   bamboo\nintake  ", unit=" kg "),
        *GOOD.facts[1:],
    )

    content = await research(FakeApi(envelope(replace(GOOD, facts=facts))))

    assert content.facts[0].label == "Daily bamboo intake"
    assert content.facts[0].unit == "kg"


async def test_keywords_are_stripped_deduped_and_capped() -> None:
    """The survivor is the **first-seen spelling**, stripped but never folded."""
    draft = replace(
        GOOD,
        keywords=(
            "Bamboo",
            "bamboo",
            "  BAMBOO  ",
            "giant  panda",
            "Giant Panda",
            "panda cub",
            "  ",
            "sichuan",
        ),
    )

    content = await research(FakeApi(envelope(draft)))

    assert content.keywords == ("Bamboo", "giant panda", "panda cub", "sichuan")


async def test_the_keyword_dedupe_folds_case_the_unicode_way() -> None:
    """``casefold`` rather than ``lower``: ``brief.locale`` makes these strings
    locale-driven, and ``"Straße"``/``"STRASSE"`` are one query to a photo archive that
    ``.lower()`` cannot see. Four raw keywords fold to three, which still clears
    ``min_keywords=3`` -- so this asserts the fold and not the floor.
    """
    draft = replace(GOOD, keywords=("Straße", "STRASSE", "bamboo", "panda"))

    content = await research(FakeApi(envelope(draft)))

    assert content.keywords == ("Straße", "bamboo", "panda")


async def test_two_different_phrases_are_not_folded_together() -> None:
    """``["Giant Panda", "PANDA"]`` are different phrases; the key is the whole phrase."""
    draft = replace(GOOD, keywords=("Giant Panda", "PANDA", "bamboo"))

    content = await research(FakeApi(envelope(draft)))

    assert content.keywords == ("Giant Panda", "PANDA", "bamboo")


async def test_keywords_are_capped_as_a_prefix() -> None:
    twelve = tuple(f"keyword {index}" for index in range(12))

    content = await research(FakeApi(envelope(replace(GOOD, keywords=twelve))))

    assert content.keywords == twelve[: DEFAULTS.max_keywords]


@pytest.mark.parametrize(
    "keywords",
    [
        pytest.param(("panda", "bamboo"), id="two-distinct"),
        pytest.param(
            ("Panda", "panda", "PANDA", "bamboo", " bamboo "), id="five-folding-to-two"
        ),
        pytest.param(("panda", "  ", ""), id="blanks-do-not-count"),
    ],
)
async def test_too_few_keywords_is_a_failure(keywords: tuple[str, ...]) -> None:
    with pytest.raises(ResearchFailedError, match="distinct keywords"):
        await research(FakeApi(envelope(replace(GOOD, keywords=keywords))))


async def test_the_keyword_floor_is_checked_before_the_cap() -> None:
    """Four distinct keywords must clear ``min_keywords=3`` even when only two survive."""
    draft = replace(GOOD, keywords=("panda", "bamboo", "sichuan", "cub"))

    content = await research(
        FakeApi(envelope(draft)), settings=ResearchSettings(max_keywords=2)
    )

    assert content.keywords == ("panda", "bamboo")


async def test_sections_with_blank_text_are_dropped() -> None:
    sections = list(GOOD.sections)
    sections[1] = replace(sections[1], heading="   ")

    content = await research(FakeApi(envelope(replace(GOOD, sections=tuple(sections)))))

    assert tuple(section.heading for section in content.sections) == (
        GOOD.sections[0].heading,
        GOOD.sections[2].heading,
    )


async def test_no_usable_section_is_a_failure() -> None:
    blank = tuple(replace(section, body=" ") for section in GOOD.sections)

    with pytest.raises(ResearchFailedError, match="no usable narrative sections"):
        await research(FakeApi(envelope(replace(GOOD, sections=blank))))


async def test_sections_are_capped_as_a_prefix() -> None:
    content = await research(settings=ResearchSettings(max_sections=2))

    assert tuple(section.heading for section in content.sections) == (
        GOOD.sections[0].heading,
        GOOD.sections[1].heading,
    )


@pytest.mark.parametrize("cap", [1, 2, 3])
async def test_a_section_keeps_only_its_first_verified_sources(cap: int) -> None:
    sections = (replace(GOOD.sections[0], urls=(DIET, CENSUS, REDLIST)), *GOOD.sections[1:])
    fake = FakeApi(envelope(replace(GOOD, sections=sections)))

    content = await research(fake, settings=ResearchSettings(max_sources_per_section=cap))

    kept = tuple(source.url for source in content.sections[0].sources)
    assert kept == (DIET, CENSUS, REDLIST)[:cap]


async def test_a_section_verifies_before_it_caps() -> None:
    """Cap-then-verify keeps zero sources here, which is the bug this test exists for."""
    sections = (
        replace(GOOD.sections[0], urls=(INVENTED, ALSO_INVENTED, CENSUS)),
        *GOOD.sections[1:],
    )
    fake = FakeApi(envelope(replace(GOOD, sections=sections)))

    content = await research(fake, settings=ResearchSettings(max_sources_per_section=1))

    assert tuple(s.url for s in content.sections[0].sources) == (CENSUS,)


# --------------------------------------------------------------------------- #
# 10. ★ The citation fence. The point of the module.
# --------------------------------------------------------------------------- #


async def test_an_invented_url_leaves_the_fact_unattributed() -> None:
    facts = (GOOD.facts[0], replace(GOOD.facts[1], url=INVENTED), *GOOD.facts[2:])

    content = await research(FakeApi(envelope(replace(GOOD, facts=facts))))

    assert content.facts[1].source is None
    assert content.facts[0].source is not None, "siblings keep theirs"
    assert content.facts[0].source.url == DIET
    assert content.facts[3].source is not None
    assert content.facts[3].source.url == REDLIST


async def test_an_invented_url_reaches_no_part_of_the_document() -> None:
    """Not the fact, not the bibliography, not a section. Nowhere."""
    facts = (GOOD.facts[0], replace(GOOD.facts[1], url=INVENTED), *GOOD.facts[2:])
    sections = (replace(GOOD.sections[1], urls=(ALSO_INVENTED,)),)
    draft = replace(GOOD, facts=facts, sections=(GOOD.sections[0], *sections))

    content = await research(FakeApi(envelope(draft)))

    assert INVENTED not in {source.url for source in content.sources}
    assert ALSO_INVENTED not in {source.url for source in content.sources}
    assert INVENTED not in every_url(content)
    assert ALSO_INVENTED not in every_url(content)
    assert every_url(content) <= RETRIEVED_URLS


async def test_an_invented_section_url_does_not_displace_a_real_one() -> None:
    """It must not eat a ``max_sources_per_section`` slot on the way to being dropped."""
    sections = (
        *GOOD.sections[:2],
        replace(GOOD.sections[2], urls=(INVENTED, SPOTLIGHT, GEOGRAPHIC)),
    )

    content = await research(FakeApi(envelope(replace(GOOD, sections=sections))))

    kept = tuple(source.url for source in content.sections[2].sources)
    assert kept == (SPOTLIGHT, GEOGRAPHIC)


async def test_the_bibliography_lists_each_url_exactly_once() -> None:
    """``layout.py::_references`` caps nothing, so a duplicate is a duplicate row."""
    content = await research()

    urls = [source.url for source in content.sources]

    assert len(urls) == len(set(urls))


async def test_the_bibliography_is_in_first_appearance_order() -> None:
    """Facts first, in significance order, then sections in reading order.

    Written out rather than derived, so a ``set`` anywhere in the chain goes red here.
    The default draft cannot distinguish *which* pass runs first, though --
    :func:`test_a_facts_citation_precedes_a_sections_in_the_bibliography` does that.
    """
    content = await research()

    assert tuple(source.url for source in content.sources) == (
        DIET,
        CENSUS,
        REDLIST,
        SPOTLIGHT,
        GEOGRAPHIC,
        BIODIVERSITY,
    )


async def test_a_facts_citation_precedes_a_sections_in_the_bibliography() -> None:
    """The default draft hides the pass order: its first section cites its first fact's
    URL first, so swapping the two passes in ``_bibliography`` produces the identical
    list. This draft breaks that tie -- section one now leads with ``CENSUS``, which is
    the *second* fact's page -- so facts-first is the only order that yields this.
    """
    sections = (replace(GOOD.sections[0], urls=(CENSUS, DIET)), *GOOD.sections[1:])

    content = await research(FakeApi(envelope(replace(GOOD, sections=sections))))

    assert tuple(source.url for source in content.sources)[:2] == (DIET, CENSUS)


async def test_every_fact_source_appears_in_the_bibliography() -> None:
    content = await research()

    bibliography = {source.url for source in content.sources}

    assert {source.url for source in fact_sources(content)} <= bibliography
    assert {source.url for source in section_sources(content)} <= bibliography


async def test_fact_sources_are_well_formed_https_urls() -> None:
    sources = every_source(await research())
    assert sources, "the fixture must attribute something for this to mean anything"

    for source in sources:
        parsed = urlparse(source.url)
        assert parsed.scheme == "https", source.url
        assert parsed.netloc, source.url
        assert source.publisher, source.url
        assert source.title, source.url


async def test_no_retrieval_means_no_citations() -> None:
    """Every claimed URL is now an invention, so the attribution floor must bite."""
    fake = FakeApi(envelope(searched=(), fetched=()))

    with pytest.raises(ResearchFailedError, match="no fact carries a verified source"):
        await research(fake)

    assert (await research(fake, brief=make_brief(0))).sources == ()


async def test_a_url_the_server_malformed_does_not_escape_as_a_value_error() -> None:
    """``ports.py`` reserves ``ValueError`` for a brief we cannot act on.

    ``urlparse("https://[::1")`` raises ``ValueError``, and the harvest runs outside
    both of ``_run``'s ``try`` blocks, so an unguarded raise here would blame the
    caller for a search engine's malformed result.
    """
    malformed = "https://[::1"
    facts = tuple(replace(fact, url=malformed) for fact in GOOD.facts)
    sections = tuple(replace(section, urls=(malformed,)) for section in GOOD.sections)
    fake = FakeApi(
        envelope(
            replace(GOOD, facts=facts, sections=sections),
            searched=(SearchHit(malformed, "Malformed"),),
            fetched=(),
        )
    )

    with pytest.raises(ResearchFailedError):
        await research(fake)


async def research_or_declared_failure(fake: FakeApi) -> ResearchContent | None:
    """``research()`` held to the exception set ``core.ports`` permits.

    ``None`` means it declined the job with a ``ResearchFailedError``, which the port
    allows. The four named below it does not, and they are the ones a block shape the
    harvest cannot read produces: ``retrieved_sources`` is called from ``_run`` outside
    every ``try`` -- the one enclosing handler catches ``TimeoutError`` alone -- so such
    an exception reaches a caller written against ``ports.py``, which has no handler for
    it. Caught and named here rather than left to surface raw, so the failure report says
    *which* type got out.
    """
    try:
        return await research(fake)
    except ResearchFailedError:
        return None
    except (TypeError, AttributeError, KeyError, ValueError) as escaped:
        pytest.fail(f"{type(escaped).__name__} escaped research(): {escaped}")


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(7, id="a-scalar-int"),
        pytest.param(None, id="null"),
        pytest.param(ABSENT, id="the-key-absent"),
    ],
)
async def test_a_search_block_carrying_no_result_list_does_not_escape_research(
    content: object,
) -> None:
    """None of these is either branch of the union, and ``for result in 7`` raises.

    ``TypeError`` is nowhere in ``ports.py``'s permitted set, so without the list guard
    it leaves ``research()`` naked. The block is spliced onto an otherwise healthy
    envelope, which is why the run is expected to *survive*: one unreadable candidate
    costs nothing that was really retrieved, and the equality below is what says so. A
    ``ResearchFailedError`` would satisfy the port here as well -- the helper above
    accepts it -- but dropping the candidate is the behaviour worth pinning.
    """
    fake = FakeApi(spliced(envelope(), (resultless_search_block(content),)))

    surviving = await research_or_declared_failure(fake)

    assert surviving is not None
    assert surviving.title == GOOD.title
    assert len(surviving.facts) == AVAILABLE
    assert every_url(surviving) == RETRIEVED_URLS


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("oops", id="a-bare-string"),
        pytest.param(7, id="a-scalar-int"),
        pytest.param(
            [{"type": "web_fetch_result", "url": INVENTED}], id="a-list-of-results"
        ),
        pytest.param(None, id="null"),
        pytest.param(ABSENT, id="the-key-absent"),
    ],
)
async def test_a_fetch_block_carrying_no_document_does_not_escape_research(
    content: object,
) -> None:
    """The sibling of the search case, and it raises ``AttributeError`` rather than
    ``TypeError``: ``WebFetchToolResultBlock.content`` is read for ``.url``, not iterated.

    ``ports.py`` permits neither. The list row carries a URL nothing retrieved, so the
    equality also says the guard returned *before* admitting it -- a container narrowed
    negatively, on the error branch alone, would have let its elements through.
    """
    fake = FakeApi(spliced(envelope(), (documentless_fetch_block(content),)))

    surviving = await research_or_declared_failure(fake)

    assert surviving is not None
    assert surviving.title == GOOD.title
    assert len(surviving.facts) == AVAILABLE
    assert every_url(surviving) == RETRIEVED_URLS


# --------------------------------------------------------------------------- #
# 11. retrieved_sources: the harvest, unit-tested. Pure, so no client at all.
# --------------------------------------------------------------------------- #


def test_the_harvest_finds_exactly_what_the_server_retrieved() -> None:
    verified = harvest()

    assert set(verified) == RETRIEVED_URLS


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://a.example/x", id="not-https"),
        pytest.param("https:///no-host", id="no-host"),
        pytest.param("ftp://a.example/x", id="ftp"),
        pytest.param("not a url", id="not-a-url"),
        pytest.param("//a.example/x", id="scheme-relative"),
        pytest.param("https://[::1", id="malformed-ipv6"),
    ],
)
def test_only_https_urls_with_a_host_are_verified(url: str) -> None:
    """``//a.example/x`` is the second witness: a good host with no scheme."""
    assert harvest(searched=(SearchHit(url),), fetched=()) == {}
    assert harvest(searched=(), fetched=(Fetched(url),)) == {}


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://a.example/x", id="not-https"),
        pytest.param("https://[::1", id="malformed-ipv6"),
    ],
)
async def test_a_fact_citing_an_unverifiable_url_is_unattributed(url: str) -> None:
    facts = (GOOD.facts[0], replace(GOOD.facts[1], url=url), *GOOD.facts[2:])

    content = await research(FakeApi(envelope(replace(GOOD, facts=facts))))

    assert content.facts[1].source is None
    assert url not in every_url(content)


def test_an_over_long_url_is_dropped_not_clipped() -> None:
    """Clipping would break the verbatim key that *is* the fence."""
    long_url = "https://a.example/" + "a" * agent._MAX_URL_CHARS
    assert len(long_url) > agent._MAX_URL_CHARS

    verified = harvest(searched=(SearchHit(long_url), SearchHit(CENSUS)), fetched=())

    assert set(verified) == {CENSUS}
    assert not any(key.startswith("https://a.example/") for key in verified)


@pytest.mark.parametrize(
    ("length", "admitted"),
    [
        pytest.param(2_047, True, id="under"),
        pytest.param(2_048, True, id="exactly-the-cap"),
        pytest.param(2_049, False, id="over"),
    ],
)
def test_the_url_cap_is_the_practical_browser_limit(length: int, admitted: bool) -> None:
    """2048 as a literal: the cap sits where nothing real reaches it, and dropping a
    URL costs a citation, so tightening it would quietly cost citations."""
    url = "https://a.example/" + "a" * (length - len("https://a.example/"))
    assert len(url) == length

    verified = harvest(searched=(SearchHit(url),), fetched=())

    assert (url in verified) is admitted


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://user:pw@secret.example/x", id="user-and-password"),
        pytest.param("https://user@secret.example/x", id="user-only"),
        pytest.param("https://:pw@secret.example/x", id="password-only"),
    ],
)
def test_a_url_carrying_userinfo_is_refused_and_never_rewritten(url: str) -> None:
    """``Source.url`` is rendered as visible bibliography text, so admitting this
    would print a password into the PNG -- and stripping the userinfo would break the
    byte-exact key the fence depends on. The sibling hit proves the batch survives and
    that no ``https://secret.example/x`` was admitted in its place.
    """
    verified = harvest(searched=(SearchHit(url), SearchHit(CENSUS)), fetched=())

    assert set(verified) == {CENSUS}


def test_an_empty_userinfo_url_is_still_admitted() -> None:
    """``https://@host/x`` leaks nothing, and refusing it would cost a real citation."""
    verified = harvest(searched=(SearchHit("https://@a.example/x"),), fetched=())

    assert set(verified) == {"https://@a.example/x"}
    assert verified["https://@a.example/x"].publisher == "a.example"


async def test_no_source_url_anywhere_in_a_result_carries_userinfo() -> None:
    """The end-to-end statement of the same rule: nothing with an ``@`` gets published."""
    secret = "https://reader:hunter2@secret.example/leak"
    facts = (GOOD.facts[0], replace(GOOD.facts[1], url=secret), *GOOD.facts[2:])
    sections = (replace(GOOD.sections[0], urls=(secret, DIET)), *GOOD.sections[1:])
    fake = FakeApi(
        envelope(
            replace(GOOD, facts=facts, sections=sections),
            searched=(SearchHit(secret, "Leaky"), *SEARCH_HITS),
        )
    )

    content = await research(fake)

    assert content.facts[1].source is None
    assert secret not in every_url(content)
    assert "https://secret.example/leak" not in every_url(content)
    assert not any("@" in source.url for source in every_source(content))


def test_an_uppercase_scheme_is_admitted_under_its_original_spelling() -> None:
    """A model that "tidies" the URL misses the lookup and loses that citation."""
    verified = harvest(searched=(SearchHit("HTTPS://A.Example/x"),), fetched=())

    assert set(verified) == {"HTTPS://A.Example/x"}
    assert verified["HTTPS://A.Example/x"].publisher == "a.example"
    assert "https://a.example/x" not in verified


@pytest.mark.parametrize(
    ("url", "publisher"),
    [
        ("https://www.worldwildlife.org/x", "worldwildlife.org"),
        ("https://iucnredlist.org/x", "iucnredlist.org"),
        ("https://WWW.Example.COM/x", "example.com"),
        ("https://wwwf.example/x", "wwwf.example"),
        ("https://sub.www.example/x", "sub.www.example"),
        ("https://www.example:8443/x", "example"),
        ("https://www./x", "www."),
    ],
)
def test_the_publisher_is_the_host_without_www(url: str, publisher: str) -> None:
    """``.netloc`` would publish the port -- and once, ``user:pw@example`` -- into the PNG.

    The userinfo row that used to live in this table is gone on purpose: such a URL is
    now refused at admission, so there is no ``Source`` left to read a publisher off.
    :func:`test_a_url_carrying_userinfo_is_refused_and_never_rewritten` is where it went.
    """
    verified = harvest(searched=(SearchHit(url),), fetched=())

    assert verified[url].publisher == publisher
    assert verified[url].publisher != "", "nothing on a Source we build is ever empty"


@pytest.mark.parametrize(
    ("hostname", "publisher"),
    [
        pytest.param("www.wwf.org.uk", "wwf.org.uk", id="www-stripped"),
        pytest.param("www.", "www.", id="the-legal-host-www-dot"),
        pytest.param(f"www.a{ZERO_WIDTH}b.example", "ab.example", id="zero-width-dropped"),
        pytest.param("a" * 60 + ".example", "a" * 48, id="clipped-to-48"),
    ],
)
def test_a_publisher_is_visible_characters_only_and_fits_the_source_line(
    hostname: str, publisher: str
) -> None:
    """48 as a literal: ``layout.py``'s ``.tick`` sets no ``overflow-wrap``, so a longer
    publisher runs off the row sideways instead of folding. The ``www.`` row is the one
    host whose stripped form is empty, and it must stay structurally present."""
    assert agent._publisher_of(hostname) == publisher


def test_a_zero_width_host_still_keys_the_source_verbatim() -> None:
    """The publisher loses the invisible character; the URL key must not."""
    url = f"https://a{ZERO_WIDTH}b.example/x"

    verified = harvest(searched=(SearchHit(url),), fetched=())

    assert set(verified) == {url}
    assert verified[url].publisher == "ab.example"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(f"A{BIDI_OVERRIDE}B", "AB", id="bidi-override-dropped"),
        pytest.param(ZERO_WIDTH * 2, None, id="zero-width-only-is-none"),
        pytest.param(f"  Real{ZERO_WIDTH}  title ", "Real title", id="stripped-and-collapsed"),
        pytest.param("", None, id="empty"),
        pytest.param(None, None, id="absent"),
    ],
)
def test_a_title_keeps_only_its_visible_characters(
    raw: str | None, expected: str | None
) -> None:
    """A RIGHT-TO-LEFT OVERRIDE visually reverses the rest of a rendered title, and
    zero-width characters answer ``False`` to ``.isspace()`` -- so ``str.split()`` leaves
    an invisible one-character bibliography line behind unless they go first."""
    assert agent._clean_title(raw) == expected


def test_a_bidi_spoofed_document_title_is_cleaned_before_it_reaches_the_png() -> None:
    verified = harvest(searched=(), fetched=(Fetched(DIET, f"Bamboo{BIDI_OVERRIDE}/moc.live"),))

    assert verified[DIET].title == "Bamboo/moc.live"


@pytest.mark.parametrize(
    "error_code", ["max_uses_exceeded", "too_many_requests", "unavailable"]
)
def test_a_search_error_block_does_not_stop_the_harvest(error_code: str) -> None:
    """Server-tool failures arrive as blocks on an HTTP 200; no try/except sees them."""
    verified = harvest(extra_blocks=(search_error_block(error_code),))

    assert set(verified) == RETRIEVED_URLS


@pytest.mark.parametrize(
    "error_code", ["url_not_accessible", "unsupported_content_type", "max_uses_exceeded"]
)
def test_a_fetch_error_block_does_not_stop_the_harvest(error_code: str) -> None:
    verified = harvest(extra_blocks=(fetch_error_block(error_code),))

    assert set(verified) == RETRIEVED_URLS


def test_a_failed_search_is_logged_with_its_error_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Otherwise an all-``max_uses_exceeded`` run looks like a prompting problem."""
    with caplog.at_level(logging.WARNING, logger=agent.__name__):
        harvest(extra_blocks=(search_error_block(), fetch_error_block()))

    assert "a web search failed: max_uses_exceeded" in caplog.text
    assert "a web fetch failed: url_not_accessible" in caplog.text


async def test_a_malformed_fetch_result_is_logged_as_malformed_not_as_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fetch *success* missing ``media_type`` degrades to an error block with no code.

    "a web fetch failed: None" reads like a bug in this module rather than a report
    about the block that arrived, and sends an operator looking in the wrong file.
    """
    message = await lenient_message(spliced(envelope(), (malformed_fetch_block(),)))

    with caplog.at_level(logging.WARNING, logger=agent.__name__):
        verified = retrieved_sources(message)

    assert "a web fetch failed: malformed result block" in caplog.text
    assert "a web fetch failed: None" not in caplog.text
    assert set(verified) == RETRIEVED_URLS, "the rest of the harvest is untouched"


async def test_a_search_result_with_no_url_is_skipped_and_the_batch_survives() -> None:
    """``isinstance(result, WebSearchResultBlock)`` matches *nothing* on this path.

    Every element of a successful search arrives as a ``WebSearchToolResultError``
    carrying the real fields as pydantic extras, so the guard has to be on the fields:
    a missing ``url`` must skip one element, not discard the batch -- and must not raise
    ``AttributeError`` from a harvest that runs outside both of ``_run``'s ``try`` blocks.
    """
    keeper = "https://kept.example/page"

    verified = await lenient_harvest(
        bare_search_block(
            [
                search_result(title="No URL at all"),
                search_result(url=keeper, title="Kept"),
            ]
        )
    )

    assert set(verified) == RETRIEVED_URLS | {keeper}
    assert verified[keeper].title == "Kept"


async def test_a_search_result_with_no_title_is_admitted_without_one() -> None:
    """A missing ``title`` costs only ``Source.title``, which is already optional."""
    untitled = "https://untitled.example/page"

    verified = await lenient_harvest(
        bare_search_block([search_result(url=untitled)])
    )

    assert untitled in verified
    assert verified[untitled].title is None
    assert verified[untitled].publisher == "untitled.example"


@pytest.mark.parametrize("search_first", [True, False])
def test_a_fetched_page_outranks_a_search_hit_for_the_same_url(
    search_first: bool,
) -> None:
    """Precedence is the harvest order, not the block order: only a fetch has a time."""
    verified = harvest(
        searched=(SearchHit(DIET, "The search engine's title"),),
        fetched=(Fetched(DIET, "The fetched document's title", "2026-07-20T12:00:00Z"),),
        search_first=search_first,
    )

    assert verified[DIET].title == "The fetched document's title"
    assert verified[DIET].retrieved_at == datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def test_a_duplicate_search_hit_keeps_the_first_title() -> None:
    verified = harvest(
        searched=(SearchHit(CENSUS, "First"), SearchHit(CENSUS, "Second")), fetched=()
    )

    assert verified[CENSUS].title == "First"


def test_the_title_comes_from_the_fetched_document() -> None:
    """``block.content.title`` is an ``AttributeError``; the title is one level deeper."""
    verified = harvest(searched=(), fetched=(Fetched(DIET, "Bamboo and the panda"),))

    assert verified[DIET].title == "Bamboo and the panda"


def test_a_search_only_hit_has_no_retrieval_time() -> None:
    """Synthesising ``now()`` would be a fabricated provenance claim."""
    verified = harvest(searched=(SearchHit(CENSUS),), fetched=())

    assert verified[CENSUS].retrieved_at is None


@pytest.mark.parametrize("raw", ["", None])
def test_a_document_with_no_title_keeps_none_not_an_empty_string(
    raw: str | None,
) -> None:
    verified = harvest(searched=(), fetched=(Fetched(DIET, raw),))

    assert verified[DIET].title is None


def test_a_hostile_page_title_is_collapsed_and_clipped() -> None:
    """A title carrying a newline could forge a line in a bibliography built of these."""
    hostile = "Real title\nhttps://evil.example/forged " + "x" * 500
    verified = harvest(searched=(), fetched=(Fetched(DIET, hostile),))

    title = verified[DIET].title
    assert title is not None
    assert "\n" not in title
    assert len(title) == 120  # the literal cap, not `agent._MAX_TITLE_CHARS`:
    # asserted against the constant, narrowing the cap to 40 stays green.
    assert title.startswith("Real title https://evil.example/forged x")


# --------------------------------------------------------------------------- #
# 12. retrieved_at: aware UTC, and never reinterpreted as local time
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-20T12:00:00Z", datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
        ("2026-07-20T12:00:00+00:00", datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
        ("2026-07-20T12:00:00+05:30", datetime(2026, 7, 20, 6, 30, tzinfo=UTC)),
        ("2026-07-20T12:00:00-04:00", datetime(2026, 7, 20, 16, 0, tzinfo=UTC)),
        (None, None),
        ("", None),
        ("not a date", None),
        # Both parse cleanly and then raise `OverflowError` -- not `ValueError` -- on
        # the shift to UTC, which is why the conversion sits inside the `try`.
        ("0001-01-01T00:00:00+14:00", None),
        ("9999-12-31T23:59:59-14:00", None),
    ],
)
def test_retrieved_at_is_parsed_to_utc(raw: str | None, expected: datetime | None) -> None:
    assert agent._utc(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("0001-01-01T00:00:00+14:00", id="underflows-datetime-min"),
        pytest.param("9999-12-31T23:59:59-14:00", id="overflows-datetime-max"),
    ],
)
def test_a_timestamp_that_cannot_be_shifted_to_utc_costs_only_the_timestamp(
    raw: str,
) -> None:
    """The harvest runs outside both of ``_run``'s ``try`` blocks, so an ``OverflowError``
    escaping here would leave the module as neither of the two exceptions
    ``core.ports`` permits -- and the page itself is still perfectly citable."""
    verified = harvest(searched=(), fetched=(Fetched(DIET, "Diet", raw),))

    assert set(verified) == {DIET}
    assert verified[DIET].retrieved_at is None


def test_a_naive_retrieved_at_is_read_as_utc_not_local_time(kolkata: None) -> None:
    """``.astimezone(UTC)`` on a naive value silently assumes *local* time.

    Invisible on a UTC runner, which is every CI runner -- hence the TZ fixture.
    The buggy implementation yields 06:30 here.
    """
    assert agent._utc("2026-07-20T12:00:00") == datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

    verified = harvest(
        searched=(), fetched=(Fetched(DIET, "Diet", "2026-07-20T12:00:00"),)
    )

    assert verified[DIET].retrieved_at == datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


async def test_retrieval_timestamps_are_timezone_aware_utc() -> None:
    stamped = [
        source.retrieved_at
        for source in every_source(await research())
        if source.retrieved_at is not None
    ]
    assert stamped, "the fixture must fetch something for this to mean anything"

    for retrieved_at in stamped:
        assert retrieved_at.tzinfo is not None
        assert retrieved_at.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------- #
# 13. Model and transport failures. This stage raises; it never degrades.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "stop_reason",
    ["refusal", "max_tokens", "model_context_window_exceeded", "pause_turn"],
)
async def test_a_bad_stop_reason_is_a_failure(stop_reason: str) -> None:
    """The reply still carries perfectly valid JSON, so the guard is load-bearing.

    ``pause_turn`` is in the set because a single call cannot resend the response
    to continue -- and being able to *see* it is most of why this module calls
    ``create`` rather than ``parse``.
    """
    fake = FakeApi(envelope(stop_reason=stop_reason))

    with pytest.raises(ResearchFailedError, match=stop_reason):
        await research(fake)


@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence", "tool_use", None])
async def test_a_benign_stop_reason_is_accepted(stop_reason: str | None) -> None:
    """The negative control, with the benign set named rather than guessed at."""
    content = await research(FakeApi(envelope(stop_reason=stop_reason)))

    assert content.title == GOOD.title


async def test_a_slow_response_is_a_failure_that_is_not_an_os_error() -> None:
    """Bounded on purpose: a real hang would wedge a suite with no ``pytest-timeout``.

    ``asyncio.TimeoutError`` *is* ``OSError``, and ``ports.py`` already uses
    ``OSError`` for "unwritable path", so leaking one past the wrap would slip by
    every handler that file tells callers to write.
    """
    fake = FakeApi(delay_s=5.0)

    with pytest.raises(ResearchFailedError) as raised:
        await research(fake, settings=ResearchSettings(timeout_s=0.05))

    assert isinstance(raised.value, RuntimeError)
    assert not isinstance(raised.value, OSError)
    assert isinstance(raised.value.__cause__, TimeoutError)


async def test_a_client_with_no_credentials_fails_as_a_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport raises if reached, so this cannot quietly become a real 401.

    A real key is exported in these shells, so ``AsyncAnthropic(api_key=None)``
    alone authenticates fine and bills a call. Belt and braces on top of the
    autouse deletion, explicit at the point where the absence *is* the subject.
    """
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.delenv(AUTH_TOKEN_ENV, raising=False)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a request escaped to {request.url}")

    transport = httpx.MockTransport(unreachable)
    async with AsyncAnthropic(
        api_key=None, max_retries=0, http_client=httpx.AsyncClient(transport=transport)
    ) as client:
        with pytest.raises(ResearchFailedError) as raised:
            await LlmResearcher(client).research(make_brief())

    assert isinstance(raised.value, RuntimeError)
    assert isinstance(raised.value.__cause__, TypeError)


async def test_a_transport_failure_reaches_the_caller_unwrapped() -> None:
    """``ports.py`` permits a transport error; wrapping one discards its status code."""

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    transport = httpx.MockTransport(explode)
    async with AsyncAnthropic(
        api_key="test", max_retries=0, http_client=httpx.AsyncClient(transport=transport)
    ) as client:
        with pytest.raises(anthropic.APIConnectionError) as raised:
            await LlmResearcher(client).research(make_brief())

    assert not isinstance(raised.value, RuntimeError)
    assert not isinstance(raised.value, ResearchFailedError)


async def test_the_retrieved_count_is_logged_before_validation_can_reject_the_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reply we reject still has to tell us what the call actually retrieved."""
    fake = FakeApi(envelope(texts=("prose only",)))

    with caplog.at_level(logging.INFO, logger=agent.__name__):
        with pytest.raises(ResearchFailedError):
            await research(fake)

    assert f"retrieved {len(RETRIEVED_URLS)} verified sources" in caplog.text


# --------------------------------------------------------------------------- #
# 14. The TypeError boundary: exactly one TypeError may be relabelled
# --------------------------------------------------------------------------- #


async def test_a_programming_type_error_is_not_relabelled_as_a_credentials_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_tools`` and ``_output_config`` are locals *above* the ``try`` for this reason.

    As argument expressions they were evaluated inside it, so the day
    ``transform_schema`` raises ``TypeError`` over a changed model the operator is told
    the problem is unresolvable credentials and goes hunting for a key that is fine.
    """

    def explode(effort: agent.Effort) -> NoReturn:
        raise TypeError("transform_schema cannot handle this annotation")

    monkeypatch.setattr(agent, "_output_config", explode)
    fake = FakeApi()

    with pytest.raises(TypeError) as raised:
        await research(fake)

    assert not isinstance(raised.value, ResearchFailedError)
    assert str(raised.value) == "transform_schema cannot handle this annotation"
    assert fake.requests == [], "the call must not have been attempted"


async def test_a_type_error_from_the_call_itself_is_still_a_research_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complement, and the case the ``except`` is actually for.

    ``AsyncAnthropic()`` with no resolvable credentials constructs fine and fails at
    request time inside ``_validate_headers`` with a plain ``TypeError``, which the port
    does not permit. Narrowing on the message would be brittle, so the wrap is broad --
    which is precisely why the two argument expressions had to move out of the ``try``.
    """
    fake = FakeApi()

    async with fake.client() as client:

        async def explode(**kwargs: object) -> Message:
            raise TypeError("Could not resolve authentication method")

        monkeypatch.setattr(client.messages, "create", explode)

        with pytest.raises(ResearchFailedError, match="unresolvable credentials"):
            await LlmResearcher(client).research(make_brief())


# --------------------------------------------------------------------------- #
# 15. What the live audit found. Zero fabricated URLs and zero fabricated numbers,
# but 4 of 8 detail clauses citing a document that did not contain them and 8 of 8
# facts on one publisher. Prompt instructions for the first, a warning for the second.
# The table below has since grown past the instructions the audit *added*, to also pin
# the three the audit found were carrying it: URL fidelity, the value/unit split, and
# keywords a camera can see. Deleting any one of those left this suite fully green.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "instruction",
    [
        pytest.param("Spread the facts across pages", id="source-diversity"),
        pytest.param("at least three different", id="three-publishers-minimum"),
        pytest.param("go to a page you have not used", id="new-page-before-re-mining"),
        pytest.param("the detail clause included", id="detail-shares-the-value-s-page"),
        pytest.param("does not say it", id="do-not-attach-to-the-wrong-page"),
        pytest.param(
            "name a quantity the way its own source names it", id="source-terminology"
        ),
        pytest.param("copied character for character", id="url-copied-verbatim"),
        pytest.param("Split every statistic into value and unit", id="value-unit-split"),
        pytest.param("something a camera can see", id="keywords-a-camera-can-see"),
    ],
)
async def test_the_system_prompt_carries_the_instruction_the_audit_added(
    instruction: str,
) -> None:
    """Asserted on the wire and per phrase, not as a snapshot of the whole prompt.

    A snapshot goes red on every rewording and gets updated without being read; a
    phrase goes red only when the instruction itself is deleted, which is the event
    worth catching. These are the only lever this module has over prose: a ``detail``
    clause cannot be code-checked against the page it cites.
    """
    fake = FakeApi()

    await research(fake)

    assert instruction in str(fake.sent[0]["system"])


async def test_the_detail_field_schema_binds_the_clause_to_the_value_s_page() -> None:
    """The schema is the other half of the instruction, and it is also on the wire."""
    fake = FakeApi()

    await research(fake)

    assert "supported by the same page as the value" in json.dumps(fake.sent[0])


async def test_facts_all_citing_one_publisher_are_warned_about_but_still_ship(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warn, never fail: ``core.ports`` says nothing about source diversity and a
    genuinely single-source topic is legitimate. Two distinct URLs on one host, so the
    check is on the publisher rather than on the URL.
    """
    sibling = "https://pandasinternational.org/gestation"
    facts = tuple(
        replace(fact, url=sibling if index % 2 else DIET)
        for index, fact in enumerate(GOOD.facts)
    )
    fake = FakeApi(
        envelope(
            replace(GOOD, facts=facts),
            searched=(*SEARCH_HITS, SearchHit(sibling, "Gestation")),
        )
    )

    with caplog.at_level(logging.WARNING, logger=agent.__name__):
        content = await research(fake)

    assert len(content.facts) == AVAILABLE, "a warning is not a failure"
    assert "the poster is a summary of one source" in caplog.text
    assert "pandasinternational.org" in caplog.text


async def test_a_multi_publisher_result_is_not_called_single_sourced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The negative control. A warning on the happy path is a warning nobody reads."""
    with caplog.at_level(logging.WARNING, logger=agent.__name__):
        await research()

    assert "summary of one source" not in caplog.text
    assert caplog.text == "", "the default fixture must warn about nothing at all"


async def test_a_single_attributed_fact_is_not_called_single_sourced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One attributed fact trivially has one publisher; saying so would be noise."""
    facts = (GOOD.facts[0], *(replace(fact, url=None) for fact in GOOD.facts[1:]))
    sections = tuple(replace(section, urls=()) for section in GOOD.sections)
    fake = FakeApi(envelope(replace(GOOD, facts=facts, sections=sections)))

    with caplog.at_level(logging.WARNING, logger=agent.__name__):
        content = await research(fake)

    assert len(fact_sources(content)) == 1
    assert "summary of one source" not in caplog.text


async def test_the_single_source_warning_reads_the_shipped_facts_not_the_draft(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It is what the poster *prints* that is a summary of one page.

    The draft here spans five publishers; only the two that survive ``max_facts=2``
    share one, so the uncapped run must stay silent and the capped one must not.
    """
    facts = (GOOD.facts[0], replace(GOOD.facts[1], url=DIET), *GOOD.facts[2:])
    fake = FakeApi(envelope(replace(GOOD, facts=facts)))

    with caplog.at_level(logging.WARNING, logger=agent.__name__):
        await research(fake)
    assert "summary of one source" not in caplog.text

    with caplog.at_level(logging.WARNING, logger=agent.__name__):
        await research(fake, brief=make_brief(2))
    assert "all 2 attributed facts cite pandasinternational.org" in caplog.text


# --------------------------------------------------------------------------- #
# 16. The one live test. Gated on intent; `pytest -k live` selects exactly it.
# --------------------------------------------------------------------------- #


async def test_live_research_returns_only_real_citations(
    live_api_or_skip: AsyncAnthropic,
) -> None:
    """A real billed call on a non-panda topic. Properties only, never content.

    ``max_facts=None`` on purpose: it is the only value that exercises the shipping
    defaults end to end -- ``target_facts=8`` reaching the prompt and the ungated
    ``min_facts=5`` floor actually biting.
    """
    brief = Brief(prompt="the Hoover Dam", max_facts=None)

    async with live_api_or_skip as client:
        content = await LlmResearcher(client).research(brief)

    assert content.title and content.subtitle and content.summary
    assert DEFAULTS.min_facts <= len(content.facts) <= agent._MAX_TARGET_FACTS
    assert content.sections
    assert 3 <= len(content.keywords) <= 8
    cited = fact_sources(content)
    assert cited, "a live run that attributes nothing is a failure, not a pass"
    bibliography = {source.url for source in content.sources}
    assert {source.url for source in cited} <= bibliography
    for source in every_source(content):
        parsed = urlparse(source.url)
        assert parsed.scheme == "https", source.url
        assert parsed.hostname, source.url
        if source.retrieved_at is not None:
            assert source.retrieved_at.utcoffset() == timedelta(0)
