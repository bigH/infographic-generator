from pathlib import Path

from pydantic import BaseModel

from infographic_agent.templates.comparison.schema import ComparisonSlots
from infographic_agent.templates.process_flow.schema import ProcessFlowSlots
from infographic_agent.templates.quote_spotlight.schema import QuoteSpotlightSlots
from infographic_agent.templates.ranked_list.schema import RankedListSlots
from infographic_agent.templates.stat_grid.schema import StatGridSlots
from infographic_agent.templates.timeline.schema import TimelineSlots

_TEMPLATES_DIR = Path(__file__).parent


class TemplateSpec(BaseModel):
    id: str
    display_name: str
    description: str  # fed into the template-selection LLM prompt
    slot_model: type[BaseModel]
    html_path: Path
    css_path: Path
    # names of ImageSlot-valued fields on slot_model, used by the renderer to resolve images
    image_slot_fields: list[str] = []

    model_config = {"arbitrary_types_allowed": True}


TEMPLATE_REGISTRY: dict[str, TemplateSpec] = {
    "stat_grid": TemplateSpec(
        id="stat_grid",
        display_name="Stat Grid",
        description=(
            "Best when content has 3-6 standalone numeric facts with no inherent order "
            "or chronology (market sizes, percentages, counts). Pick when `facts` is rich "
            "and timeline/comparison content is sparse."
        ),
        slot_model=StatGridSlots,
        html_path=_TEMPLATES_DIR / "stat_grid" / "template.html.j2",
        css_path=_TEMPLATES_DIR / "stat_grid" / "style.css",
        image_slot_fields=["hero_image"],
    ),
    "timeline": TemplateSpec(
        id="timeline",
        display_name="Timeline",
        description=(
            "Best when content has clear chronological/dated events (history, roadmap, "
            "milestones). Pick when `timeline_events` is populated."
        ),
        slot_model=TimelineSlots,
        html_path=_TEMPLATES_DIR / "timeline" / "template.html.j2",
        css_path=_TEMPLATES_DIR / "timeline" / "style.css",
        image_slot_fields=["hero_image"],
    ),
    "comparison": TemplateSpec(
        id="comparison",
        display_name="Comparison",
        description=(
            "Best when content contrasts exactly two things (A vs B, before/after, product "
            "vs competitor). Pick when `comparisons` is populated or the content is inherently "
            "dichotomous."
        ),
        slot_model=ComparisonSlots,
        html_path=_TEMPLATES_DIR / "comparison" / "template.html.j2",
        css_path=_TEMPLATES_DIR / "comparison" / "style.css",
        image_slot_fields=["left_image", "right_image"],
    ),
    "process_flow": TemplateSpec(
        id="process_flow",
        display_name="Process Flow",
        description=(
            "Best when content describes an ordered how-to/procedure/pipeline with no dates, "
            "just sequential steps. Pick when `steps` is populated and `timeline_events` is not."
        ),
        slot_model=ProcessFlowSlots,
        html_path=_TEMPLATES_DIR / "process_flow" / "template.html.j2",
        css_path=_TEMPLATES_DIR / "process_flow" / "style.css",
        image_slot_fields=["hero_image"],
    ),
    "quote_spotlight": TemplateSpec(
        id="quote_spotlight",
        display_name="Quote Spotlight",
        description=(
            "Best when the single most compelling asset is one strong quote or claim, with "
            "everything else secondary. Pick when `quotes` has a standout entry and facts/steps "
            "are thin."
        ),
        slot_model=QuoteSpotlightSlots,
        html_path=_TEMPLATES_DIR / "quote_spotlight" / "template.html.j2",
        css_path=_TEMPLATES_DIR / "quote_spotlight" / "style.css",
        image_slot_fields=["background_image"],
    ),
    "ranked_list": TemplateSpec(
        id="ranked_list",
        display_name="Ranked List",
        description=(
            "Best when content is a ranked/prioritized list of items (top 5 reasons, biggest "
            "risks) with an implicit priority order but no natural pairwise comparison or "
            "chronology. Pick when `key_points` is the richest field."
        ),
        slot_model=RankedListSlots,
        html_path=_TEMPLATES_DIR / "ranked_list" / "template.html.j2",
        css_path=_TEMPLATES_DIR / "ranked_list" / "style.css",
        image_slot_fields=["hero_image"],
    ),
}
