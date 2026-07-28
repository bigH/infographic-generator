from infographic_agent.contracts.content import ContentPayload
from infographic_agent.contracts.image_manifest import ImageManifest
from infographic_agent.steps.map_content import map_content
from infographic_agent.steps.render import render
from infographic_agent.steps.select_template import select_template


def run(content: ContentPayload, manifest: ImageManifest) -> bytes:
    selection = select_template(content)
    mapping_result = map_content(content, selection.template_id, manifest)
    return render(mapping_result, manifest)
