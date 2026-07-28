from pathlib import Path

from infographic_agent.contracts.content import ContentPayload
from infographic_agent.contracts.content_mapping import ContentMappingResult
from infographic_agent.contracts.image_manifest import ImageManifest
from infographic_agent.llm.client import structured_call
from infographic_agent.templates.registry import TEMPLATE_REGISTRY

_PROMPT_PATH = Path(__file__).parent.parent / "llm" / "prompts" / "map_content.md"


def map_content(content: ContentPayload, template_id: str, manifest: ImageManifest) -> ContentMappingResult:
    template_spec = TEMPLATE_REGISTRY[template_id]

    image_candidates = "\n".join(
        f"- id={c.id}, alt_text={c.alt_text!r}, tags={c.tags}" for c in manifest.candidates
    ) or "(none available)"

    prompt = _PROMPT_PATH.read_text().format(
        topic=content.topic,
        audience=content.audience,
        learning_preference=content.learning_preference,
        headline=content.headline or "(none provided)",
        summary=content.summary,
        facts=content.facts,
        key_points=content.key_points,
        steps=content.steps,
        comparisons=content.comparisons,
        quotes=content.quotes,
        timeline_events=content.timeline_events,
        template_id=template_spec.id,
        template_description=template_spec.description,
        image_candidates=image_candidates,
    )
    slots = structured_call(prompt, template_spec.slot_model, max_tokens=4096)
    return ContentMappingResult(template_id=template_id, slots=slots)
