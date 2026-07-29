"""Contract tests for the two-call :class:`AgentComposer`.

No network, no API key, no mock library. The LLM seams are Protocols
(``TemplateSelector``, ``ContentMapper``), so every path here is driven by a real
in-process stub object -- including the SDK-shaped path, which is exercised with
a stub client whose ``messages.parse`` returns the same pydantic response models
the wire would.

The through-line is that nothing a model does can cost a render, and nothing a
model says can become a fact: selection degrades to the rule table, mapping
degrades to the deterministic body, indices are validated against the input, and
the document always comes out of a human-written template.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final, get_args

import pydantic
import pytest

from infographic_generator.composition import agent_composer
from infographic_generator.composition.agent_composer import (
    API_KEY_ENV,
    DEFAULT_MODEL,
    AgentComposer,
    ContentMapping,
    ImagePlan,
    ModelDeclinedError,
    RenderableTemplateId,
    SectionPlan,
    apply_mapping,
    mapping_prompt,
    selection_prompt,
)
from infographic_generator.composition.composer import HtmlComposer
from infographic_generator.composition.registry import (
    RENDERABLE_TEMPLATE_IDS,
    TEMPLATE_REGISTRY,
    TemplateSpec,
)
from infographic_generator.composition.selection import (
    MIN_CONFIDENCE,
    TEMPLATE_EXTRA_KEY,
    ContentCensus,
    TemplateChoice,
    build_census,
    choose_template,
)
from infographic_generator.core.models import (
    Brief,
    Composition,
    Fact,
    ImageAsset,
    ImageRole,
    NarrativeSection,
    RenderOptions,
    ResearchContent,
    Source,
)
from infographic_generator.core.ports import Composer
from tests.test_composition import (
    PANDAS,
    REMOTE_SCHEMES,
    assert_structurally_valid,
    make_brief,
    make_content,
    make_facts,
    parse,
)

RENDERABLE: Final = tuple(sorted(RENDERABLE_TEMPLATE_IDS))
BLOCKED: Final = ("timeline", "comparison", "quote_spotlight")
_URL = re.compile(r"https?://[^\s<>\"')]+")


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key in the environment for any test in this module.

    Belt and braces on top of the injected stubs: even a composer built with no
    arguments at all cannot reach the network from here.
    """
    monkeypatch.delenv(API_KEY_ENV, raising=False)


# --------------------------------------------------------------------------- #
# Stubs: real objects, no mock library
# --------------------------------------------------------------------------- #


class StubSelector:
    """Returns a canned answer and counts how often it was asked."""

    def __init__(self, choice: TemplateChoice | None) -> None:
        self.choice = choice
        self.calls = 0
        self.censuses: list[ContentCensus] = []

    async def select(self, census: ContentCensus) -> TemplateChoice | None:
        self.calls += 1
        self.censuses.append(census)
        return self.choice


class ExplodingSelector:
    """The transport failed, the schema failed, who knows. It raised."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def select(self, census: ContentCensus) -> TemplateChoice:
        self.calls += 1
        raise self.error


class SlowSelector:
    """Hangs, then times out exactly the way ``asyncio.wait_for`` does."""

    def __init__(self) -> None:
        self.calls = 0

    async def select(self, census: ContentCensus) -> TemplateChoice:
        self.calls += 1
        await asyncio.wait_for(asyncio.sleep(30), timeout=0.01)
        raise AssertionError("unreachable: the sleep must time out")


class StubMapper:
    """Returns a canned mapping."""

    def __init__(self, mapping: ContentMapping | None) -> None:
        self.mapping = mapping
        self.calls = 0
        self.specs: list[TemplateSpec] = []

    async def map_content(
        self,
        spec: TemplateSpec,
        census: ContentCensus,
        content: ResearchContent,
        images: Sequence[ImageAsset],
    ) -> ContentMapping | None:
        self.calls += 1
        self.specs.append(spec)
        return self.mapping


class ExplodingMapper:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def map_content(
        self,
        spec: TemplateSpec,
        census: ContentCensus,
        content: ResearchContent,
        images: Sequence[ImageAsset],
    ) -> ContentMapping:
        raise self.error


@dataclass(slots=True)
class StubResponse:
    """What ``messages.parse`` hands back, minus everything unused."""

    stop_reason: str | None
    payload: pydantic.BaseModel | None = None

    @property
    def parsed_output(self) -> pydantic.BaseModel | None:
        """Reading this reads ``content``; a refusal must never get here."""
        if self.stop_reason == "refusal":
            raise AssertionError("parsed_output read on a refusal")
        return self.payload


class StubMessages:
    def __init__(self, responses: dict[str, StubResponse]) -> None:
        self._responses = responses
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> StubResponse:
        self.kwargs.append(kwargs)
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        self.prompts.append(str(messages[0]["content"]))
        output_format = kwargs["output_format"]
        assert isinstance(output_format, type)
        return self._responses[output_format.__name__]


class StubClient:
    """Stands in for ``AsyncAnthropic`` at the one attribute this module uses."""

    def __init__(self, responses: dict[str, StubResponse]) -> None:
        self.messages = StubMessages(responses)


# --------------------------------------------------------------------------- #
# Content fixtures
# --------------------------------------------------------------------------- #


def fact_heavy() -> ResearchContent:
    """Many facts, one section: the rule table calls this a ``ranked_list``."""
    return make_content(
        facts=make_facts(8),
        sections=(NarrativeSection(heading="Diet", body="Bamboo, mostly."),),
    )


def section_heavy() -> ResearchContent:
    """Sections outnumbering facts: the rule table calls this a ``process_flow``."""
    return ResearchContent(
        title="How a Panda Eats",
        subtitle="Four steps",
        summary="Sit, select, peel, chew.",
        facts=(Fact(label="Hours a day", value="14"),),
        sections=tuple(
            NarrativeSection(
                heading=f"Step {index}",
                body=f"Body of step {index}.",
                sources=(Source(url=f"https://example.org/step-{index}"),),
            )
            for index in range(4)
        ),
        keywords=("panda",),
        sources=(Source(url="https://example.org/steps", title="Steps"),),
    )


def panda_images() -> tuple[ImageAsset, ...]:
    return (
        PANDAS[0].as_asset(ImageRole.HERO),
        PANDAS[1].as_asset(ImageRole.SUPPORTING),
        PANDAS[2].as_asset(ImageRole.SUPPORTING),
    )


def ruled_id(brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]) -> str:
    return choose_template(build_census(brief, content, images)).template_id


async def deterministic_html(
    template_id: str,
    brief: Brief,
    content: ResearchContent,
    images: Sequence[ImageAsset],
) -> str:
    return (await HtmlComposer(template_id=template_id).compose(brief, content, images)).html


def input_urls(content: ResearchContent, images: Sequence[ImageAsset]) -> set[str]:
    urls = {source.url for source in content.sources}
    urls |= {fact.source.url for fact in content.facts if fact.source is not None}
    urls |= {
        source.url for section in content.sections for source in section.sources
    }
    for asset in images:
        credit = asset.credit
        if credit.license_url:
            urls.add(credit.license_url)
        if credit.source is not None:
            urls.add(credit.source.url)
    return urls


def rendered_urls(html: str) -> Iterator[str]:
    yield from _URL.findall(parse(html).text)


def _input_strings(
    brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
) -> list[str]:
    """Every string a page is allowed to be built out of."""
    strings = [brief.prompt, content.title, content.subtitle, content.summary]
    strings.extend(content.keywords)
    for fact in content.facts:
        strings.extend(part for part in (fact.label, fact.value, fact.unit, fact.detail) if part)
    for section in content.sections:
        strings.extend((section.heading, section.body))
    strings.extend(sorted(input_urls(content, images)))
    for asset in images:
        credit = asset.credit
        strings.extend(part for part in (asset.alt_text, credit.license, credit.author) if part)
        if credit.source is not None and credit.source.title:
            strings.append(credit.source.title)
    for source in content.sources:
        strings.extend(part for part in (source.title, source.publisher) if part)
    return strings


def _numbers(strings: Sequence[str]) -> set[str]:
    return {match for text in strings for match in re.findall(r"\d[\d,.]*", text)}


# --------------------------------------------------------------------------- #
# 1. A trusted choice picks that template, and only delegates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("template_id", RENDERABLE)
async def test_trusted_selection_produces_that_templates_document(
    template_id: str,
) -> None:
    brief, content, images = make_brief(), make_content(), panda_images()
    selector = StubSelector(TemplateChoice(template_id, 0.95, "sure"))

    composition = await AgentComposer(selector=selector).compose(brief, content, images)

    assert composition.html == await deterministic_html(
        template_id, brief, content, images
    )
    assert selector.calls == 1
    assert_structurally_valid(composition.html)


async def test_the_census_is_what_the_selector_sees() -> None:
    brief, content, images = make_brief(), fact_heavy(), panda_images()
    selector = StubSelector(TemplateChoice("stat_grid", 0.9, "sure"))

    await AgentComposer(selector=selector).compose(brief, content, images)

    assert selector.censuses == [build_census(brief, content, images)]


# --------------------------------------------------------------------------- #
# 2. Untrusted answers degrade to the rule table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "choice",
    [
        pytest.param(TemplateChoice("timeline", 1.0, "blocked"), id="blocked-timeline"),
        pytest.param(TemplateChoice("comparison", 1.0, "blocked"), id="blocked-comparison"),
        pytest.param(
            TemplateChoice("quote_spotlight", 1.0, "blocked"), id="blocked-quote"
        ),
        pytest.param(TemplateChoice("made_up", 1.0, "unregistered"), id="unregistered"),
        pytest.param(TemplateChoice("", 1.0, "empty id"), id="empty-id"),
        pytest.param(TemplateChoice("process_flow", 0.0, "no idea"), id="zero-confidence"),
        pytest.param(
            TemplateChoice("process_flow", MIN_CONFIDENCE - 0.01, "hedging"),
            id="just-below-threshold",
        ),
        pytest.param(None, id="nothing-at-all"),
    ],
)
async def test_untrusted_choices_fall_through_to_the_rule_table(
    choice: TemplateChoice | None,
) -> None:
    brief, content, images = make_brief(), fact_heavy(), ()
    expected = ruled_id(brief, content, images)
    assert expected == "ranked_list", "fixture should exercise a non-default rule"

    composition = await AgentComposer(selector=StubSelector(choice)).compose(
        brief, content, images
    )

    assert composition.html == await deterministic_html(expected, brief, content, images)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("transport exploded"), id="transport"),
        pytest.param(ModelDeclinedError("the model declined"), id="refusal"),
        pytest.param(TimeoutError("took too long"), id="timeout"),
        pytest.param(ValueError("schema violation"), id="validation"),
    ],
)
async def test_a_raising_selector_degrades_instead_of_propagating(
    error: Exception,
) -> None:
    brief, content = make_brief(), section_heavy()
    expected = ruled_id(brief, content, ())
    assert expected == "process_flow", "fixture should exercise a non-default rule"
    selector = ExplodingSelector(error)

    composition = await AgentComposer(selector=selector).compose(brief, content, ())

    assert selector.calls == 1
    assert composition.html == await deterministic_html(expected, brief, content, ())
    assert_structurally_valid(composition.html)


async def test_a_hanging_selector_times_out_and_degrades() -> None:
    brief, content = make_brief(), fact_heavy()
    selector = SlowSelector()

    composition = await AgentComposer(selector=selector).compose(brief, content, ())

    assert selector.calls == 1
    assert composition.html == await deterministic_html(
        ruled_id(brief, content, ()), brief, content, ()
    )


async def test_a_raising_mapper_leaves_the_deterministic_body_intact() -> None:
    brief, content, images = make_brief(), fact_heavy(), panda_images()
    composer = AgentComposer(
        selector=StubSelector(TemplateChoice("ranked_list", 0.9, "sure")),
        mapper=ExplodingMapper(ModelDeclinedError("declined the mapping")),
    )

    composition = await composer.compose(brief, content, images)

    assert composition.html == await deterministic_html(
        "ranked_list", brief, content, images
    )


async def test_everything_failing_still_yields_the_floor_template() -> None:
    brief = make_brief()
    content = ResearchContent(title="Bare", subtitle="", summary="")
    composer = AgentComposer(
        selector=ExplodingSelector(RuntimeError("no")),
        mapper=ExplodingMapper(RuntimeError("also no")),
    )

    composition = await composer.compose(brief, content, ())

    assert composition.html == await deterministic_html("stat_grid", brief, content, ())


# --------------------------------------------------------------------------- #
# 3. The SDK-shaped path, offline: refusal, emptiness, and a real conversion
# --------------------------------------------------------------------------- #


def selection_payload(template_id: str, confidence: float) -> pydantic.BaseModel:
    return agent_composer._SelectionResponse(
        template_id=template_id,  # type: ignore[arg-type]
        confidence=confidence,
        rationale="the shape reads as a sequence",
    )


def mapping_payload() -> pydantic.BaseModel:
    return agent_composer._MappingResponse(
        fact_order=[3, 0],
        sections=[agent_composer._SectionResponse(index=1, caption="Peel it")],
        images=[agent_composer._ImageResponse(index=2, role="hero")],
        rationale="lead with the biggest number",
    )


async def test_a_refusal_degrades_without_ever_reading_the_content() -> None:
    """``stop_reason`` is checked first; the stub asserts nothing read ``content``."""
    brief, content = make_brief(), fact_heavy()
    client = StubClient(
        {
            "_SelectionResponse": StubResponse(stop_reason="refusal"),
            "_MappingResponse": StubResponse(stop_reason="refusal"),
        }
    )

    composition = await AgentComposer(client=client).compose(brief, content, ())

    assert composition.html == await deterministic_html(
        ruled_id(brief, content, ()), brief, content, ()
    )
    assert_structurally_valid(composition.html)


async def test_an_empty_structured_answer_degrades() -> None:
    brief, content = make_brief(), fact_heavy()
    client = StubClient(
        {
            "_SelectionResponse": StubResponse(stop_reason="end_turn", payload=None),
            "_MappingResponse": StubResponse(stop_reason="end_turn", payload=None),
        }
    )

    composition = await AgentComposer(client=client).compose(brief, content, ())

    assert composition.html == await deterministic_html(
        ruled_id(brief, content, ()), brief, content, ()
    )


async def test_the_sdk_path_converts_both_answers_and_composes() -> None:
    brief, content, images = make_brief(), section_heavy(), panda_images()
    client = StubClient(
        {
            "_SelectionResponse": StubResponse(
                stop_reason="end_turn", payload=selection_payload("ranked_list", 0.91)
            ),
            "_MappingResponse": StubResponse(
                stop_reason="end_turn", payload=mapping_payload()
            ),
        }
    )

    composition = await AgentComposer(client=client).compose(brief, content, images)

    parsed = assert_structurally_valid(composition.html)
    assert "Peel it" in parsed.text, "the section caption should be honoured"
    assert composition.html != await deterministic_html(
        "ranked_list", brief, content, images
    ), "the mapping should have changed the page"
    assert [kwargs["model"] for kwargs in client.messages.kwargs] == [
        DEFAULT_MODEL,
        DEFAULT_MODEL,
    ]
    for kwargs in client.messages.kwargs:
        assert kwargs["output_config"] == {"effort": "high"}
        assert "temperature" not in kwargs
        assert "top_p" not in kwargs
        assert "top_k" not in kwargs
        assert "thinking" not in kwargs


# --------------------------------------------------------------------------- #
# 4. The brief's override outranks the model
# --------------------------------------------------------------------------- #


async def test_the_extras_override_beats_a_confident_selection() -> None:
    brief = Brief(
        prompt="a panda", extras={TEMPLATE_EXTRA_KEY: "process_flow"}
    )
    content, images = fact_heavy(), panda_images()
    selector = StubSelector(TemplateChoice("ranked_list", 1.0, "very sure"))

    composition = await AgentComposer(selector=selector).compose(brief, content, images)

    assert composition.html == await deterministic_html(
        "process_flow", brief, content, images
    )
    assert selector.calls == 0, "a settled answer should not cost a model call"


# --------------------------------------------------------------------------- #
# 5. No API key: the most important one
# --------------------------------------------------------------------------- #


async def test_without_an_api_key_compose_still_returns_a_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    brief, content, images = make_brief(), fact_heavy(), panda_images()

    composition = await AgentComposer().compose(brief, content, images)

    assert isinstance(composition, Composition)
    assert_structurally_valid(composition.html)
    assert composition.html == await deterministic_html(
        ruled_id(brief, content, images), brief, content, images
    )


async def test_without_an_api_key_no_client_is_built() -> None:
    assert agent_composer.default_client() is None


# --------------------------------------------------------------------------- #
# 6. Mapping validation: indices are checked, prose is not accepted
# --------------------------------------------------------------------------- #


async def test_out_of_range_indices_are_dropped_and_nothing_is_lost() -> None:
    brief, content, images = make_brief(), section_heavy(), panda_images()
    mapping = ContentMapping(
        template_id="process_flow",
        fact_order=(99, -1, 0, 0),
        sections=(
            SectionPlan(index=42, caption="ghost step"),
            SectionPlan(index=-3),
            SectionPlan(index=2, caption="Third first"),
        ),
        images=(ImagePlan(index=17, role=ImageRole.HERO), ImagePlan(index=0)),
    )
    composer = AgentComposer(
        selector=StubSelector(TemplateChoice("process_flow", 0.9, "sure")),
        mapper=StubMapper(mapping),
    )

    composition = await composer.compose(brief, content, images)
    parsed = assert_structurally_valid(composition.html)

    assert "ghost step" not in parsed.text, "a caption for a missing section leaked"
    assert "Third first" in parsed.text
    for section in content.sections:
        assert section.body in parsed.text, "an out-of-range index cost us a section"
    for fact in content.facts:
        assert fact.label in parsed.text and fact.value in parsed.text


async def test_a_mapping_that_references_nothing_still_renders() -> None:
    brief, content, images = make_brief(), fact_heavy(), panda_images()
    composer = AgentComposer(
        selector=StubSelector(TemplateChoice("ranked_list", 0.9, "sure")),
        mapper=StubMapper(ContentMapping(template_id="ranked_list")),
    )

    composition = await composer.compose(brief, content, images)

    assert composition.html == await deterministic_html(
        "ranked_list", brief, content, images
    )


async def test_no_fact_or_source_outside_the_input_reaches_the_page() -> None:
    brief, content, images = make_brief(), section_heavy(), panda_images()
    mapping = ContentMapping(
        template_id="process_flow",
        fact_order=(0,),
        sections=(SectionPlan(index=0, caption="A re-worded step"),),
        images=(ImagePlan(index=1, role=ImageRole.HERO),),
    )
    composer = AgentComposer(
        selector=StubSelector(TemplateChoice("process_flow", 0.9, "sure")),
        mapper=StubMapper(mapping),
    )

    composition = await composer.compose(brief, content, images)
    parsed = parse(composition.html)
    allowed = input_urls(content, images)

    for url in rendered_urls(composition.html):
        assert url.rstrip(".,") in allowed, f"a URL nobody researched: {url!r}"
    allowed_numbers = _numbers(_input_strings(brief, content, images))
    allowed_numbers |= {str(index) for index in range(len(content.sections) + 1)}
    invented = _numbers([parsed.text]) - allowed_numbers
    assert not invented, f"numbers on the page that came from nowhere: {invented}"


def test_the_mapping_schema_has_nowhere_to_put_a_fact() -> None:
    """Fabrication is structurally impossible: the wire carries indices."""
    fields = set(agent_composer._MappingResponse.model_fields)
    assert fields == {"fact_order", "sections", "images", "rationale"}
    assert set(agent_composer._SectionResponse.model_fields) == {"index", "caption"}
    assert set(agent_composer._ImageResponse.model_fields) == {"index", "role"}
    assert {field.name for field in dataclasses.fields(SectionPlan)} == {
        "index",
        "caption",
    }
    assert {field.name for field in dataclasses.fields(ImagePlan)} == {"index", "role"}


def test_apply_mapping_is_a_permutation_of_its_input() -> None:
    content, images = section_heavy(), panda_images()
    mapping = ContentMapping(
        template_id="process_flow",
        fact_order=(0, 0, 0),
        sections=(SectionPlan(index=3), SectionPlan(index=900)),
        images=(ImagePlan(index=2, role=ImageRole.ICON),),
    )

    mapped, mapped_images = apply_mapping(content, images, mapping)

    assert sorted(f.value for f in mapped.facts) == sorted(
        f.value for f in content.facts
    )
    assert sorted(s.body for s in mapped.sections) == sorted(
        s.body for s in content.sections
    )
    assert mapped.sections[0].body == content.sections[3].body
    assert len(mapped_images) == len(images)
    assert mapped_images[0].role is ImageRole.ICON


def test_apply_mapping_without_a_mapping_changes_nothing() -> None:
    content, images = section_heavy(), panda_images()

    assert apply_mapping(content, images, None) == (content, tuple(images))


def test_an_unknown_role_name_is_ignored_rather_than_raising() -> None:
    mapping = agent_composer._mapping_from(
        agent_composer._MappingResponse(
            images=[agent_composer._ImageResponse(index=0, role="hero")]
        ),
        "stat_grid",
    )
    assert mapping.images[0].role is ImageRole.HERO
    assert agent_composer._role("not-a-role") is None
    assert agent_composer._role(None) is None


# --------------------------------------------------------------------------- #
# 7. Self-containment and attribution hold on the model path too
# --------------------------------------------------------------------------- #


async def test_the_llm_path_is_still_self_contained_and_still_credits() -> None:
    brief, content, images = make_brief(), section_heavy(), panda_images()
    composer = AgentComposer(
        selector=StubSelector(TemplateChoice("process_flow", 0.99, "sure")),
        mapper=StubMapper(
            ContentMapping(
                template_id="process_flow",
                fact_order=(0,),
                sections=(SectionPlan(index=1, caption="Chew"),),
                images=(
                    ImagePlan(index=2, role=ImageRole.HERO),
                    ImagePlan(index=0, role=ImageRole.ICON),
                ),
            )
        ),
    )

    composition = await composer.compose(brief, content, images)
    parsed = assert_structurally_valid(composition.html)

    for url in parsed.fetchable_urls:
        assert not url.lower().startswith(REMOTE_SCHEMES), f"remote fetch: {url!r}"
    for url in parsed.css_urls:
        assert url.startswith("data:"), f"CSS url() must be a data URI: {url!r}"
    assert not parsed.tagged("link")
    assert "@import" not in composition.html.lower()
    assert any(url.startswith("data:") for url in parsed.fetchable_urls)

    for panda in PANDAS:
        assert panda.license_text in parsed.text, f"no licence for {panda.filename}"
        assert panda.author in parsed.text, f"no author for {panda.filename}"


async def test_render_options_survive_the_model_path() -> None:
    options = RenderOptions(width_px=880, height_px=5000, device_scale_factor=1.25)
    brief = make_brief(options=options)
    composer = AgentComposer(selector=StubSelector(TemplateChoice("stat_grid", 0.9, "y")))

    composition = await composer.compose(brief, make_content(), ())

    assert composition.width_px == options.width_px
    assert composition.height_px == options.height_px
    assert composition.device_scale_factor == options.device_scale_factor
    assert composition.title


# --------------------------------------------------------------------------- #
# 8. Prompt shape: hints in, no spec internals, no image paths
# --------------------------------------------------------------------------- #


def a_census() -> ContentCensus:
    return build_census(make_brief(), section_heavy(), panda_images())


def test_the_selection_prompt_is_the_registry_and_the_census() -> None:
    prompt = selection_prompt(a_census())

    for spec in TEMPLATE_REGISTRY.values():
        assert f"- {spec.id}: {spec.selection_hint}" in prompt
    for template_id in RENDERABLE:
        assert template_id in prompt
    for spec in TEMPLATE_REGISTRY.values():
        if spec.blocked_on is not None:
            assert spec.blocked_on not in prompt, "blocked_on is not for the model"
    assert "Bamboo" not in prompt, "the prose is not what selection reads"


def test_the_mapping_prompt_addresses_everything_by_index() -> None:
    content, images = section_heavy(), panda_images()
    spec = TEMPLATE_REGISTRY["process_flow"]

    prompt = mapping_prompt(spec, a_census(), content, images)

    assert spec.selection_hint in prompt
    for index in range(len(content.facts)):
        assert f"[{index}]" in prompt
    for section in content.sections:
        assert section.heading in prompt
    for asset in images:
        assert str(asset.content) not in prompt, "an image path reached the model"
        assert asset.alt_text in prompt
    assert "data:" not in prompt


def test_the_selectable_ids_cannot_drift_from_the_registry() -> None:
    assert set(get_args(RenderableTemplateId)) == RENDERABLE_TEMPLATE_IDS


# --------------------------------------------------------------------------- #
# 9. The port, and the pydantic boundary
# --------------------------------------------------------------------------- #


async def test_agent_composer_satisfies_the_composer_port() -> None:
    composer: Composer = AgentComposer(
        selector=StubSelector(TemplateChoice("stat_grid", 0.9, "sure")),
        mapper=StubMapper(None),
    )

    composition = await composer.compose(make_brief(), make_content(), ())

    assert isinstance(composition, Composition)


@pytest.mark.parametrize(
    "cls", [ContentMapping, SectionPlan, ImagePlan], ids=lambda c: c.__name__
)
def test_the_domain_types_are_frozen_and_slotted(cls: type) -> None:
    assert dataclasses.is_dataclass(cls)
    assert cls.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert "__slots__" in cls.__dict__
    assert not issubclass(cls, pydantic.BaseModel)


def test_the_modules_entire_pydantic_inventory_is_private() -> None:
    """Pins the boundary: four private schemas, plus the imported base class."""
    names = {
        name
        for name, value in vars(agent_composer).items()
        if isinstance(value, type) and issubclass(value, pydantic.BaseModel)
    }

    assert names == {
        "BaseModel",
        "_SelectionResponse",
        "_SectionResponse",
        "_ImageResponse",
        "_MappingResponse",
    }, f"unexpected pydantic in the module: {sorted(names)}"


def test_the_boundary_converters_return_frozen_dataclasses() -> None:
    choice = agent_composer._choice_from(selection_payload("stat_grid", 0.7))
    mapping = agent_composer._mapping_from(mapping_payload(), "ranked_list")

    for value in (choice, mapping, *mapping.sections, *mapping.images):
        assert dataclasses.is_dataclass(value)
        assert not isinstance(value, pydantic.BaseModel)
    assert isinstance(choice, TemplateChoice)
    assert isinstance(mapping, ContentMapping)


async def test_nothing_downstream_of_the_parse_is_pydantic() -> None:
    """Everything the mapping touches on its way to the template is a dataclass."""
    content, images = section_heavy(), panda_images()
    mapping = agent_composer._mapping_from(mapping_payload(), "process_flow")

    mapped, mapped_images = apply_mapping(content, images, mapping)

    for value in (mapped, *mapped.facts, *mapped.sections, *mapped_images):
        assert not isinstance(value, pydantic.BaseModel)
        assert dataclasses.is_dataclass(value)

    composition = await AgentComposer(
        selector=StubSelector(TemplateChoice("process_flow", 0.9, "sure")),
        mapper=StubMapper(mapping),
    ).compose(make_brief(), content, images)
    assert not isinstance(composition, pydantic.BaseModel)
