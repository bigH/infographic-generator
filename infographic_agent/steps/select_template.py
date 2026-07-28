from pathlib import Path

from infographic_agent.contracts.content import ContentPayload
from infographic_agent.contracts.template_selection import TemplateSelectionOutput
from infographic_agent.llm.client import structured_call
from infographic_agent.templates.registry import TEMPLATE_REGISTRY

_PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "select_template.md"


def select_template(content: ContentPayload) -> TemplateSelectionOutput:
    template_options = "\n".join(
        f"- {spec.id}: {spec.description}" for spec in TEMPLATE_REGISTRY.values()
    )
    prompt = _PROMPT_PATH.read_text().format(
        topic=content.topic,
        audience=content.audience,
        learning_preference=content.learning_preference,
        summary=content.summary,
        facts_count=len(content.facts),
        key_points_count=len(content.key_points),
        steps_count=len(content.steps),
        comparisons_count=len(content.comparisons),
        quotes_count=len(content.quotes),
        timeline_events_count=len(content.timeline_events),
        template_options=template_options,
    )
    return structured_call(prompt, TemplateSelectionOutput)
