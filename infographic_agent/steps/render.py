from infographic_agent.contracts.content_mapping import ContentMappingResult
from infographic_agent.contracts.image_manifest import ImageManifest
from infographic_agent.rendering.html_builder import build_html
from infographic_agent.rendering.screenshot import render_to_png
from infographic_agent.steps.resolve_images import resolve_images
from infographic_agent.templates.registry import TEMPLATE_REGISTRY


def render(mapping_result: ContentMappingResult, manifest: ImageManifest) -> bytes:
    template_spec = TEMPLATE_REGISTRY[mapping_result.template_id]
    image_srcs = resolve_images(mapping_result.slots, template_spec.image_slot_fields, manifest)
    html = build_html(template_spec, mapping_result.slots, image_srcs)
    return render_to_png(html)
