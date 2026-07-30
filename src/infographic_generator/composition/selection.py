"""Choosing a layout from the shape of the content.

Two halves. The *census* reduces a brief, its research and its images to a dozen
scalars -- counts, flags, geometry -- which is small enough to hand a model in a
prompt and cheap enough to test exhaustively. The *rule table* turns a census
into a :class:`TemplateChoice`, purely and synchronously.

The rule table is the deterministic floor, not a placeholder. A later LLM
selector implements :class:`TemplateSelector` and its answer is only trusted when
it names a renderable template and clears :data:`MIN_CONFIDENCE`; otherwise
:func:`resolve_choice` falls back here, and below here to ``stat_grid``. Nothing
in this module raises: a bad hint, an unknown id, a blocked id or a page size
with no area degrades to a documented default, because nothing a requester can
type should cost a render.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from infographic_generator.composition.registry import (
    RENDERABLE_TEMPLATE_IDS,
    TEMPLATE_REGISTRY,
    TemplateSpec,
)
from infographic_generator.core.models import (
    Brief,
    ImageAsset,
    ImageRole,
    ResearchContent,
)


class LearningPreference(StrEnum):
    """How the requester would rather absorb the piece: prose, pictures, either."""

    TEXT_HEAVY = "text_heavy"
    IMAGE_HEAVY = "image_heavy"
    BALANCED = "balanced"


TEMPLATE_EXTRA_KEY: Final = "composition.template"
"""``brief.extras`` key holding a manual template id -- the override, so no CLI
flag is needed."""

LEARNING_PREFERENCE_EXTRA_KEY: Final = "composition.learning_preference"

MIN_CONFIDENCE: Final = 0.55
"""Below this, a selector's answer is not trusted and the rule table decides."""

_DEFAULT_TEMPLATE_ID: Final = "stat_grid"
"""The floor. Today's layout, and the one that always works."""

_MANY_FACTS: Final = 5
_FEW_SECTIONS: Final = 2
_MANY_SECTIONS: Final = 3


@dataclass(frozen=True, slots=True)
class ContentCensus:
    """The *shape* of the content, not its prose. Cheap enough to put in a prompt."""

    fact_count: int
    section_count: int
    image_count: int
    has_hero: bool
    audience: str | None
    locale: str
    aspect: float | None
    """width/height, always positive; ``None`` when the brief asked for no usable
    height (full-page) -- which includes a zero or negative one."""
    title: str
    subtitle: str
    summary: str
    learning_preference: LearningPreference


@dataclass(frozen=True, slots=True)
class TemplateChoice:
    """A selector's answer: which layout, how sure, and why."""

    template_id: str
    confidence: float
    rationale: str


class TemplateSelector(Protocol):
    """Anything that can pick a template id from a census."""

    async def select(self, census: ContentCensus) -> TemplateChoice: ...


def build_census(
    brief: Brief, content: ResearchContent, images: Sequence[ImageAsset]
) -> ContentCensus:
    """Reduce the pipeline's payload to the scalars selection actually reads."""
    options = brief.options
    return ContentCensus(
        fact_count=len(content.facts),
        section_count=len(content.sections),
        image_count=len(images),
        has_hero=any(asset.role is ImageRole.HERO for asset in images),
        audience=brief.audience,
        locale=brief.locale,
        aspect=_aspect_of(options.width_px, options.height_px),
        title=content.title,
        subtitle=content.subtitle,
        summary=content.summary,
        learning_preference=learning_preference_of(brief),
    )


def _aspect_of(width_px: int, height_px: int | None) -> float | None:
    """The page's width/height, or ``None`` when it was never usefully asked for.

    ``RenderOptions`` validates nothing and ``--height 0`` / ``--height -5`` reach
    here intact, so a dimension can arrive non-positive: a box with no area, which
    describes no geometry at all. That is exactly what ``None`` already means, so
    it degrades to full-page rather than dividing by zero or handing a selector a
    ratio of ``-240.000`` as if it were the shape of the page.
    """
    if height_px is None or height_px <= 0 or width_px <= 0:
        return None
    return width_px / height_px


def learning_preference_of(brief: Brief) -> LearningPreference:
    """Read the hint from ``extras``, case- and whitespace-insensitively.

    Absent, empty or unrecognised means ``BALANCED``. Never raises -- ``extras``
    is an open bag and an unknown value there is not an error.
    """
    raw = brief.extras.get(LEARNING_PREFERENCE_EXTRA_KEY, "").strip().lower()
    try:
        return LearningPreference(raw)
    except ValueError:
        return LearningPreference.BALANCED


def template_override_of(brief: Brief) -> str | None:
    """The manually requested template id, if it is one we can actually render.

    An unknown or blocked id is silently ignored: the override is a convenience,
    and a typo should cost the requester their preference, not the render.
    """
    requested = brief.extras.get(TEMPLATE_EXTRA_KEY, "").strip()
    return requested if is_renderable(requested) else None


def is_renderable(template_id: str) -> bool:
    """True when the id is registered and not blocked on a missing core field."""
    return template_id in RENDERABLE_TEMPLATE_IDS


def choose_template(census: ContentCensus) -> TemplateChoice:
    """The rule table: content shape decides, in this order.

    1. Many facts and few sections -- a long fact list reads as a priority order
       and the stat grid stops fitting: ``ranked_list``.
    2. Sections outnumbering facts -- ordered prose steps: ``process_flow``.
    3. Otherwise ``stat_grid``, which is also the floor.

    ``learning_preference`` is a **tiebreaker only, never a driver**. It is read
    solely in the third branch, where the shape said nothing: ``IMAGE_HEAVY``
    with imagery in hand keeps the hero-bearing grid but with more conviction,
    and ``TEXT_HEAVY`` with any sections tips to ``process_flow``. When a shape
    rule fires the preference is not consulted at all, so it cannot overturn a
    clear signal.

    Structurally incapable of naming a blocked template: every branch returns a
    literal from ``RENDERABLE_TEMPLATE_IDS``.
    """
    if census.fact_count >= _MANY_FACTS and census.section_count <= _FEW_SECTIONS:
        return TemplateChoice(
            template_id="ranked_list",
            confidence=0.85,
            rationale=(
                f"{census.fact_count} facts and only {census.section_count} "
                "sections reads as a priority order"
            ),
        )
    if (
        census.section_count >= _MANY_SECTIONS
        and census.section_count > census.fact_count
    ):
        return TemplateChoice(
            template_id="process_flow",
            confidence=0.8,
            rationale=(
                f"{census.section_count} sections outnumber "
                f"{census.fact_count} facts, so the story is a sequence"
            ),
        )
    return _tiebreak(census)


def _tiebreak(census: ContentCensus) -> TemplateChoice:
    """Shape said nothing; let the learning preference nudge, weakly."""
    if census.learning_preference is LearningPreference.TEXT_HEAVY and (
        census.section_count >= 1
    ):
        return TemplateChoice(
            template_id="process_flow",
            confidence=0.6,
            rationale="shape is ambiguous; text-heavy preference favours prose steps",
        )
    if census.learning_preference is LearningPreference.IMAGE_HEAVY and (
        census.image_count >= 1
    ):
        return TemplateChoice(
            template_id=_DEFAULT_TEMPLATE_ID,
            confidence=0.6,
            rationale="shape is ambiguous; image-heavy preference favours the hero grid",
        )
    return TemplateChoice(
        template_id=_DEFAULT_TEMPLATE_ID,
        confidence=0.5,
        rationale="no clear shape signal; falling back to the stat grid",
    )


def resolve_choice(
    brief: Brief, census: ContentCensus, choice: TemplateChoice | None
) -> TemplateSpec:
    """Settle on a spec. Override, then trusted choice, then rules, then the floor.

    Never raises and never returns a blocked spec, so callers can hand it any
    selector's output -- including a confident answer naming a template we cannot
    render, or a NaN confidence, both of which simply fail the trust check.
    """
    override = template_override_of(brief)
    if override is not None:
        return TEMPLATE_REGISTRY[override]
    if choice is not None and _is_trusted(choice):
        return TEMPLATE_REGISTRY[choice.template_id]
    ruled = choose_template(census)
    return TEMPLATE_REGISTRY.get(
        ruled.template_id, TEMPLATE_REGISTRY[_DEFAULT_TEMPLATE_ID]
    )


def _is_trusted(choice: TemplateChoice) -> bool:
    """A selector's answer counts only if it is renderable and sure enough."""
    return is_renderable(choice.template_id) and choice.confidence >= MIN_CONFIDENCE
