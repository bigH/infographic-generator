"""The catalogue of layouts the composer can be asked for.

One entry per layout, keyed by a stable id. ``selection_hint`` is the only field
a selector -- human or model -- ever reads: it describes the *shape* of content
the layout suits, so the choice can be made from a census rather than from the
prose itself.

Three entries are registered but not renderable. They are the layouts we want
and cannot build, because each needs a field ``core/models.py`` does not have and
that is not ours to add. Registering them keeps the reasoning in one place --
``blocked_on`` names the missing field -- while ``RENDERABLE_TEMPLATE_IDS`` keeps
them out of every code path that would try to render one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from infographic_generator.core.models import ImageRole


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    """A layout the composer knows how to ask for."""

    id: str
    display_name: str
    selection_hint: str
    """Prose describing the content shape this layout suits -- the only field a
    selector sees. Written for a model to read, not for the UI."""
    template_name: str
    """Filename under ``composition/templates/``."""
    image_roles: Sequence[ImageRole] = ()
    """Roles this layout is designed around. Advisory only: it is read in one
    place, ``agent_composer``'s selection prompt, and no builder filters on it --
    every asset handed to a body is placed whatever role it carries. ``fafec26``
    removed the readability filter that used to qualify that sentence: an asset
    that cannot be read now raises ``OSError`` out of every body rather than being
    dropped."""
    blocked_on: str | None = None
    """``None`` means renderable. Otherwise: why it is not, naming the missing
    core field. Never dispatch to a spec with this set."""


_SPECS: Final[tuple[TemplateSpec, ...]] = (
    TemplateSpec(
        id="stat_grid",
        display_name="Stat Grid",
        selection_hint=(
            "Best when content has 3-6 standalone numeric facts with no inherent "
            "order or chronology (market sizes, percentages, counts). Pick when "
            "`facts` is rich and no chronological or paired structure is present."
        ),
        template_name="stat_grid.html.j2",
        image_roles=(ImageRole.HERO, ImageRole.SUPPORTING),
    ),
    TemplateSpec(
        id="timeline",
        display_name="Timeline",
        selection_hint=(
            "Best when content has clear chronological/dated events (history, "
            "roadmap, milestones). Pick when the facts carry dates."
        ),
        template_name="timeline.html.j2",
        image_roles=(ImageRole.HERO,),
        blocked_on=(
            "needs `Fact.when: str | None` -- there is nowhere to put an event "
            "date; `Source.retrieved_at` is when we read the page, not when the "
            "event happened."
        ),
    ),
    TemplateSpec(
        id="comparison",
        display_name="Comparison",
        selection_hint=(
            "Best when content contrasts exactly two things (A vs B, "
            "before/after, product vs competitor). Pick when the content is "
            "inherently dichotomous."
        ),
        template_name="comparison.html.j2",
        image_roles=(ImageRole.SUPPORTING,),
        blocked_on=(
            "needs a `ComparisonPair` type and `ResearchContent.comparisons`; two "
            "`Fact`s side by side lose the shared dimension that makes it a "
            "comparison."
        ),
    ),
    TemplateSpec(
        id="process_flow",
        display_name="Process Flow",
        selection_hint=(
            "Best when content describes an ordered how-to/procedure/pipeline "
            "with no dates, just sequential steps. Pick when `sections` reads as "
            "an ordered procedure and no dates are present."
        ),
        template_name="process_flow.html.j2",
        image_roles=(ImageRole.HERO, ImageRole.ICON),
    ),
    TemplateSpec(
        id="quote_spotlight",
        display_name="Quote Spotlight",
        selection_hint=(
            "Best when the single most compelling asset is one strong quote or "
            "claim, with everything else secondary. Pick when a standout "
            "quotation carries the piece and facts are thin."
        ),
        template_name="quote_spotlight.html.j2",
        image_roles=(ImageRole.BACKGROUND,),
        blocked_on=(
            "needs a `Quote` type and `ResearchContent.quotes`; "
            "`Fact.label`/`value` splits a sentence in the wrong place and "
            "`Source` is the page, not the speaker."
        ),
    ),
    TemplateSpec(
        id="ranked_list",
        display_name="Ranked List",
        selection_hint=(
            "Best when content is a ranked/prioritized list of items (top 5 "
            "reasons, biggest risks) with an implicit priority order but no "
            "natural pairwise comparison or chronology. Pick when `facts` is the "
            "richest field and reads as a priority order."
        ),
        template_name="ranked_list.html.j2",
        image_roles=(ImageRole.HERO,),
    ),
)

TEMPLATE_REGISTRY: Final[Mapping[str, TemplateSpec]] = MappingProxyType(
    {spec.id: spec for spec in _SPECS}
)
"""Every known layout, in catalogue order. Read-only: adding a layout is a code
change, not a runtime one."""

RENDERABLE_TEMPLATE_IDS: Final[frozenset[str]] = frozenset(
    spec.id for spec in _SPECS if spec.blocked_on is None
)
"""The ids a selector is allowed to return today."""
