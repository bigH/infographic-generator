"""An LLM-backed :class:`~infographic_generator.core.ports.Composer`.

Two model calls inside one ``compose()`` coroutine, so the port still sees a
single stage:

1. **select** -- the *census* (never the prose) plus every registry entry
   rendered as ``- {id}: {selection_hint}`` goes to the model, which names a
   template. ``selection_hint`` is the only spec field it ever sees, so adding a
   layout to the registry teaches selection about it with no edit here.
2. **map** -- with the template known, the model says which fact leads, in what
   order the sections read, what each step is called, and which image plays
   which role. It answers with **indices into the sequences it was given**; it
   never writes a fact, a source or a line of markup.

The HTML itself is always produced by rendering a human-written registry
template through :class:`~infographic_generator.composition.composer.HtmlComposer`
-- the one Jinja2 environment in the package, with ``autoescape=True``. That is
what keeps escaping, self-containment and visible attribution true on the model
path as much as on the deterministic one.

**Nothing a model does can cost a render.** Every failure -- no API key, a
transport error, a timeout, ``stop_reason == "refusal"``, a schema violation, an
empty or garbled answer, an out-of-range index -- degrades along
``LLM -> rule table -> stat_grid`` and is logged as a warning.

Pydantic lives in exactly one place: the two ``_*Response`` schemas below, which
are the wire format of ``client.messages.parse``. :func:`_ask` binds the parsed
model to a local name and converts it on the spot; every value that leaves this
module's functions is a frozen, slotted dataclass.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal, Protocol, TypeAlias, TypeVar

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, OutputConfigParam
from pydantic import BaseModel, Field

from infographic_generator.composition.composer import HtmlComposer
from infographic_generator.composition.registry import (
    RENDERABLE_TEMPLATE_IDS,
    TEMPLATE_REGISTRY,
    TemplateSpec,
)
from infographic_generator.composition.selection import (
    ContentCensus,
    TemplateChoice,
    TemplateSelector,
    build_census,
    is_renderable,
    resolve_choice,
    template_override_of,
)
from infographic_generator.core.models import (
    Brief,
    Composition,
    ImageAsset,
    ImageRole,
    NarrativeSection,
    ResearchContent,
)

_LOG: Final = logging.getLogger(__name__)

Effort: TypeAlias = Literal["low", "medium", "high", "xhigh", "max"]

RenderableTemplateId: TypeAlias = Literal["stat_grid", "process_flow", "ranked_list"]
"""The ids the selector is *structurally* able to name. Kept in step with
``RENDERABLE_TEMPLATE_IDS`` by ``test_agent_composer``; the returned id is
re-checked against :func:`is_renderable` anyway, because belt and braces."""

RoleName: TypeAlias = Literal["hero", "supporting", "icon", "background"]

DEFAULT_MODEL: Final = "claude-opus-5"
DEFAULT_EFFORT: Final[Effort] = "high"
DEFAULT_TIMEOUT_S: Final = 90.0
MAX_TOKENS: Final = 16000
"""Thinking is on by default on Opus 5 and shares this budget; depth is tuned
with ``output_config={"effort": ...}``, never with a token budget."""

API_KEY_ENV: Final = "ANTHROPIC_API_KEY"

_MAX_CAPTION_CHARS: Final = 120
_MAX_BODY_CHARS: Final = 240


class ModelDeclinedError(RuntimeError):
    """The model refused, or answered with nothing usable. Always a degradation."""


# --------------------------------------------------------------------------- #
# Domain: what a mapping is, once the pydantic is gone
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SectionPlan:
    """One section, addressed by index, optionally re-captioned."""

    index: int
    caption: str | None = None
    """Replaces the section's *heading* only. Bodies, facts and sources are
    never model-authored, so a misbehaving model cannot fabricate a claim."""


@dataclass(frozen=True, slots=True)
class ImagePlan:
    """One image, addressed by index, optionally re-cast into another role."""

    index: int
    role: ImageRole | None = None


@dataclass(frozen=True, slots=True)
class ContentMapping:
    """How the chosen template's slots get filled. Order and roles, never prose."""

    template_id: str
    fact_order: Sequence[int] = ()
    sections: Sequence[SectionPlan] = ()
    images: Sequence[ImagePlan] = ()
    rationale: str = ""


class ContentMapper(Protocol):
    """Anything that can map content into a known template's slots."""

    async def map_content(
        self,
        spec: TemplateSpec,
        census: ContentCensus,
        content: ResearchContent,
        images: Sequence[ImageAsset],
    ) -> ContentMapping: ...


# --------------------------------------------------------------------------- #
# Applying a mapping: select, order, re-caption. Never invent.
# --------------------------------------------------------------------------- #


def apply_mapping(
    content: ResearchContent,
    images: Sequence[ImageAsset],
    mapping: ContentMapping | None,
) -> tuple[ResearchContent, tuple[ImageAsset, ...]]:
    """Reorder and re-cast what we were given; drop every out-of-range index.

    Nothing is *removed*: an index the mapping never mentions keeps its original
    relative position at the end. ``core.ports.Composer`` promises every fact is
    rendered -- capping is the researcher's job -- so a mapping reorders facts,
    it does not select a subset of them.
    """
    if mapping is None:
        return content, tuple(images)
    facts = _reordered(content.facts, mapping.fact_order, "fact")
    sections = _sections(content.sections, mapping.sections)
    mapped_images = _images(images, mapping.images)
    return replace(content, facts=facts, sections=sections), mapped_images


def _sections(
    sections: Sequence[NarrativeSection], plans: Sequence[SectionPlan]
) -> tuple[NarrativeSection, ...]:
    captions = {
        plan.index: _caption(plan.caption)
        for plan in plans
        if 0 <= plan.index < len(sections) and _caption(plan.caption) is not None
    }
    order = _order(len(sections), [plan.index for plan in plans], "section")
    return tuple(
        _recaptioned(sections[index], captions.get(index)) for index in order
    )


def _recaptioned(
    section: NarrativeSection, caption: str | None
) -> NarrativeSection:
    return section if caption is None else replace(section, heading=caption)


def _caption(caption: str | None) -> str | None:
    if caption is None:
        return None
    trimmed = caption.strip()
    return trimmed[:_MAX_CAPTION_CHARS] if trimmed else None


def _images(
    images: Sequence[ImageAsset], plans: Sequence[ImagePlan]
) -> tuple[ImageAsset, ...]:
    roles = {
        plan.index: plan.role
        for plan in plans
        if 0 <= plan.index < len(images) and plan.role is not None
    }
    order = _order(len(images), [plan.index for plan in plans], "image")
    return tuple(_recast(images[index], roles.get(index)) for index in order)


def _recast(asset: ImageAsset, role: ImageRole | None) -> ImageAsset:
    return asset if role is None else replace(asset, role=role)


_ItemT = TypeVar("_ItemT")


def _reordered(
    items: Sequence[_ItemT], indices: Sequence[int], what: str
) -> tuple[_ItemT, ...]:
    return tuple(items[index] for index in _order(len(items), indices, what))


def _order(count: int, indices: Sequence[int], what: str) -> tuple[int, ...]:
    """Requested indices first, then everything the mapping did not mention.

    An index outside ``range(count)`` -- or a repeat -- is dropped and logged:
    the model cannot address something it was not given.
    """
    chosen: list[int] = []
    dropped = 0
    for index in indices:
        if 0 <= index < count and index not in chosen:
            chosen.append(index)
        else:
            dropped += 1
    if dropped:
        _LOG.warning("dropped %d unusable %s index/indices from the mapping", dropped, what)
    chosen.extend(index for index in range(count) if index not in chosen)
    return tuple(chosen)


# --------------------------------------------------------------------------- #
# Prompts: census in, ids out
# --------------------------------------------------------------------------- #

SELECTION_SYSTEM: Final = (
    "You choose the page layout for an infographic. You are given the *shape* of "
    "the content -- counts, flags, geometry, and the title -- and a catalogue of "
    "layouts, each with a hint describing the content shape it suits. Pick the "
    "one whose hint best matches the shape. Answer only in the requested "
    "structure: a template id, a confidence between 0 and 1, and one line of "
    "rationale. Report low confidence honestly when the shape is ambiguous; a "
    "hedged answer is handled correctly, an overconfident wrong one is not."
)

MAPPING_SYSTEM: Final = (
    "You lay researched content into a chosen layout's slots. You may only "
    "select, order and re-caption what you are given: address every fact, "
    "section and image by its zero-based index into the lists below. Never write "
    "a fact, a statistic, a URL, a source or any HTML -- a human-written "
    "template renders the page, and text you invent has no attribution. The one "
    "place you may write prose is a section caption, which replaces that "
    "section's heading and must stay faithful to its body."
)


def selection_prompt(census: ContentCensus) -> str:
    """The census, the catalogue, and the ids that are actually renderable."""
    catalogue = "\n".join(
        f"- {spec.id}: {spec.selection_hint}" for spec in TEMPLATE_REGISTRY.values()
    )
    allowed = ", ".join(sorted(RENDERABLE_TEMPLATE_IDS))
    return (
        "Content shape:\n"
        f"- facts: {census.fact_count}\n"
        f"- narrative sections: {census.section_count}\n"
        f"- images: {census.image_count}\n"
        f"- has a hero image: {census.has_hero}\n"
        f"- audience: {census.audience or 'unspecified'}\n"
        f"- locale: {census.locale}\n"
        f"- aspect ratio: {'full page' if census.aspect is None else f'{census.aspect:.3f}'}\n"
        f"- learning preference: {census.learning_preference.value}\n"
        f"- title: {census.title}\n"
        f"- subtitle: {census.subtitle}\n"
        f"- summary: {census.summary}\n"
        "\nLayout catalogue:\n"
        f"{catalogue}\n"
        f"\nChoose exactly one of these ids: {allowed}. "
        "No other id can be rendered."
    )


def mapping_prompt(
    spec: TemplateSpec,
    census: ContentCensus,
    content: ResearchContent,
    images: Sequence[ImageAsset],
) -> str:
    """The chosen layout, its hint and slots, and everything indexed."""
    roles = ", ".join(role.value for role in spec.image_roles) or "none"
    return (
        f"Chosen layout: {spec.id}\n"
        f"What it suits: {spec.selection_hint}\n"
        f"Image roles it has somewhere to put: {roles}\n"
        f"\nTitle: {census.title}\nSubtitle: {census.subtitle}\n"
        f"Summary: {census.summary}\n"
        f"\n{_fact_lines(content)}"
        f"\n{_section_lines(content)}"
        f"\n{_image_lines(images)}"
        "\nAnswer with:\n"
        "- fact_order: every fact index, most important first (the first is the "
        "headline). Indices only.\n"
        "- sections: the section indices in reading order, each with an optional "
        "caption replacing its heading.\n"
        "- images: the image indices you want placed, each with the role it "
        "should play, most significant first.\n"
        "- rationale: one line."
    )


def _fact_lines(content: ResearchContent) -> str:
    if not content.facts:
        return "Facts: none.\n"
    lines = "\n".join(
        f"[{index}] {fact.label}: {fact.value}{' ' + fact.unit if fact.unit else ''}"
        f"{' -- ' + _clip(fact.detail) if fact.detail else ''}"
        for index, fact in enumerate(content.facts)
    )
    return f"Facts (index: label: value):\n{lines}\n"


def _section_lines(content: ResearchContent) -> str:
    if not content.sections:
        return "Sections: none.\n"
    lines = "\n".join(
        f"[{index}] {section.heading}: {_clip(section.body)}"
        for index, section in enumerate(content.sections)
    )
    return f"Sections (index: heading: body):\n{lines}\n"


def _image_lines(images: Sequence[ImageAsset]) -> str:
    if not images:
        return "Images: none.\n"
    lines = "\n".join(
        f"[{index}] role={asset.role.value} {asset.width_px}x{asset.height_px} "
        f"alt={_clip(asset.alt_text)}"
        for index, asset in enumerate(images)
    )
    return f"Images (index: current role: size: alt text):\n{lines}\n"


def _clip(text: str, limit: int = _MAX_BODY_CHARS) -> str:
    stripped = " ".join(text.split())
    return stripped if len(stripped) <= limit else stripped[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# The pydantic boundary. Nothing below leaves this section as a pydantic model.
# --------------------------------------------------------------------------- #


class _SelectionResponse(BaseModel):
    """Wire format of call 1. A blocked id is not even expressible."""

    template_id: RenderableTemplateId
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class _SectionResponse(BaseModel):
    index: int
    caption: str | None = None


class _ImageResponse(BaseModel):
    index: int
    role: RoleName | None = None


class _MappingResponse(BaseModel):
    """Wire format of call 2. Indices and captions; no facts, no markup."""

    fact_order: list[int] = Field(default_factory=list)
    sections: list[_SectionResponse] = Field(default_factory=list)
    images: list[_ImageResponse] = Field(default_factory=list)
    rationale: str = ""


def _choice_from(parsed: _SelectionResponse) -> TemplateChoice:
    return TemplateChoice(
        template_id=parsed.template_id,
        confidence=float(parsed.confidence),
        rationale=parsed.rationale.strip() or "no rationale given",
    )


def _mapping_from(parsed: _MappingResponse, template_id: str) -> ContentMapping:
    return ContentMapping(
        template_id=template_id,
        fact_order=tuple(parsed.fact_order),
        sections=tuple(
            SectionPlan(index=item.index, caption=item.caption)
            for item in parsed.sections
        ),
        images=tuple(
            ImagePlan(index=item.index, role=_role(item.role)) for item in parsed.images
        ),
        rationale=parsed.rationale.strip(),
    )


def _role(name: str | None) -> ImageRole | None:
    if name is None:
        return None
    try:
        return ImageRole(name)
    except ValueError:
        _LOG.warning("ignoring unknown image role %r from the mapping", name)
        return None


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_ResultT = TypeVar("_ResultT")


async def _ask(
    *,
    client: AsyncAnthropic,
    model: str,
    effort: Effort,
    timeout_s: float,
    system: str,
    prompt: str,
    output_format: type[_ModelT],
    convert: Callable[[_ModelT], _ResultT],
    what: str,
) -> _ResultT:
    """One structured call. The parsed model is converted before it can escape.

    ``stop_reason`` is checked *before* ``parsed_output``, which reads
    ``content``: a refusal is a degradation path, not an exception to leak.
    ``temperature``/``top_p``/``top_k`` are rejected on Opus 5 -- steering is
    prompting, and depth is ``output_config["effort"]``.
    """
    config: OutputConfigParam = {"effort": effort}
    messages: list[MessageParam] = [{"role": "user", "content": prompt}]
    response = await asyncio.wait_for(
        client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            output_config=config,
            output_format=output_format,
            messages=messages,
        ),
        timeout=timeout_s,
    )
    if response.stop_reason == "refusal":
        raise ModelDeclinedError(f"the model declined the {what} request")
    parsed = response.parsed_output
    if parsed is None:
        raise ModelDeclinedError(f"no structured {what} in the model's reply")
    return convert(parsed)


# --------------------------------------------------------------------------- #
# The two model-backed stages
# --------------------------------------------------------------------------- #


class LlmTemplateSelector:
    """Call 1: census plus catalogue in, :class:`TemplateChoice` out."""

    __slots__ = ("_client", "_effort", "_model", "_timeout_s")

    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        model: str = DEFAULT_MODEL,
        effort: Effort = DEFAULT_EFFORT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._timeout_s = timeout_s

    async def select(self, census: ContentCensus) -> TemplateChoice:
        return await _ask(
            client=self._client,
            model=self._model,
            effort=self._effort,
            timeout_s=self._timeout_s,
            system=SELECTION_SYSTEM,
            prompt=selection_prompt(census),
            output_format=_SelectionResponse,
            convert=_choice_from,
            what="template selection",
        )


class LlmContentMapper:
    """Call 2: the chosen template plus indexed content in, indices out."""

    __slots__ = ("_client", "_effort", "_model", "_timeout_s")

    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        model: str = DEFAULT_MODEL,
        effort: Effort = DEFAULT_EFFORT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._timeout_s = timeout_s

    async def map_content(
        self,
        spec: TemplateSpec,
        census: ContentCensus,
        content: ResearchContent,
        images: Sequence[ImageAsset],
    ) -> ContentMapping:
        return await _ask(
            client=self._client,
            model=self._model,
            effort=self._effort,
            timeout_s=self._timeout_s,
            system=MAPPING_SYSTEM,
            prompt=mapping_prompt(spec, census, content, images),
            output_format=_MappingResponse,
            convert=lambda parsed: _mapping_from(parsed, spec.id),
            what="content mapping",
        )


def default_client() -> AsyncAnthropic | None:
    """An async client, or ``None`` when there is no key to use it with.

    Deliberately keyed on the environment variable rather than the SDK's full
    credential chain: a test run and a CI run must take the deterministic path
    without reaching the network, and "no key" has to be a degradation the suite
    can assert on rather than a request that quietly succeeds.
    """
    if not os.environ.get(API_KEY_ENV):
        _LOG.warning(
            "%s is unset; AgentComposer will use the deterministic layout path",
            API_KEY_ENV,
        )
        return None
    try:
        return AsyncAnthropic()
    except Exception as error:  # noqa: BLE001 - construction must never cost a render
        _LOG.warning("no Anthropic client (%s); using the deterministic path", error)
        return None


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


class AgentComposer:
    """A ``Composer`` that picks a template, then maps content into its slots.

    Composes *by choosing and delegating*: it emits no markup of its own, and
    every document it returns comes out of :class:`HtmlComposer`. Both model
    calls happen inside one ``compose()``, and every one of them is optional --
    with no selector and no mapper this is exactly today's deterministic
    behaviour, which is what makes it safe as the offline path too.
    """

    __slots__ = ("_composers", "_mapper", "_selector")

    def __init__(
        self,
        *,
        selector: TemplateSelector | None = None,
        mapper: ContentMapper | None = None,
        client: AsyncAnthropic | None = None,
        model: str = DEFAULT_MODEL,
        effort: Effort = DEFAULT_EFFORT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        """Inject the seams you want; the rest come from the environment.

        Passing ``selector``/``mapper`` replaces that call entirely -- that is
        the seam the tests use, so no test ever needs a mocked SDK. Passing
        ``client`` builds the real pair against it. With neither, a client is
        built only if :data:`API_KEY_ENV` is set.
        """
        resolved = client if client is not None else _maybe_client(selector, mapper)
        self._selector: TemplateSelector | None = selector
        if self._selector is None and resolved is not None:
            self._selector = LlmTemplateSelector(
                resolved, model=model, effort=effort, timeout_s=timeout_s
            )
        self._mapper: ContentMapper | None = mapper
        if self._mapper is None and resolved is not None:
            self._mapper = LlmContentMapper(
                resolved, model=model, effort=effort, timeout_s=timeout_s
            )
        self._composers: dict[str, HtmlComposer] = {}

    async def compose(
        self, brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
    ) -> Composition:
        """Select, map, delegate. Never raises because a model misbehaved."""
        census = build_census(brief, content, images)
        override = template_override_of(brief)
        if override is not None:
            _LOG.info("brief requested template %s; skipping selection", override)
        choice = None if override is not None else await self._select(census)
        spec = resolve_choice(brief, census, choice)
        mapping = await self._map(spec, census, content, images)
        mapped_content, mapped_images = apply_mapping(content, images, mapping)
        return await self._composer_for(spec.id).compose(
            brief, mapped_content, mapped_images
        )

    async def _select(self, census: ContentCensus) -> TemplateChoice | None:
        """``None`` means "let ``resolve_choice`` fall through to the rules"."""
        if self._selector is None:
            return None
        try:
            choice = await self._selector.select(census)
        except Exception as error:  # noqa: BLE001 - every failure is a degradation
            _LOG.warning("template selection failed (%s); using the rule table", error)
            return None
        if not isinstance(choice, TemplateChoice):
            _LOG.warning("selector returned %r, not a TemplateChoice", choice)
            return None
        if not is_renderable(choice.template_id):
            _LOG.warning(
                "selector named unrenderable template %r; using the rule table",
                choice.template_id,
            )
            return None
        return choice

    async def _map(
        self,
        spec: TemplateSpec,
        census: ContentCensus,
        content: ResearchContent,
        images: Sequence[ImageAsset],
    ) -> ContentMapping | None:
        """``None`` means "use the deterministic body builder as-is"."""
        if self._mapper is None:
            return None
        try:
            mapping = await self._mapper.map_content(spec, census, content, images)
        except Exception as error:  # noqa: BLE001 - every failure is a degradation
            _LOG.warning(
                "content mapping failed (%s); using the %s body as given",
                error,
                spec.id,
            )
            return None
        if not isinstance(mapping, ContentMapping):
            _LOG.warning("mapper returned %r, not a ContentMapping", mapping)
            return None
        return mapping

    def _composer_for(self, template_id: str) -> HtmlComposer:
        """One ``HtmlComposer`` per layout: one Jinja2 environment, reused."""
        composer = self._composers.get(template_id)
        if composer is None:
            composer = HtmlComposer(template_id=template_id)
            self._composers[template_id] = composer
        return composer


def _maybe_client(
    selector: TemplateSelector | None, mapper: ContentMapper | None
) -> AsyncAnthropic | None:
    """Only reach for credentials if there is a call left to make."""
    if selector is not None and mapper is not None:
        return None
    return default_client()
