import logging

from pydantic import BaseModel

from infographic_agent.contracts.image_manifest import ImageManifest
from infographic_agent.images.encode import file_to_data_uri
from infographic_agent.images.placeholder import placeholder_data_uri

logger = logging.getLogger(__name__)


def resolve_images(
    slots: BaseModel, image_slot_fields: list[str], manifest: ImageManifest
) -> dict[str, str]:
    """Resolve each ImageSlot-valued field on `slots` to a data URI, keyed as
    `"{field}_src"` (matching the Jinja variable names templates expect).

    A slot with `image_id is None` is skipped entirely (no image for that slot;
    templates render a graceful no-image layout). A slot that references an id
    not resolvable to a valid local image file falls back to a placeholder
    rather than failing the render.
    """
    candidates_by_id = {c.id: c for c in manifest.candidates}
    resolved: dict[str, str] = {}

    for field in image_slot_fields:
        slot = getattr(slots, field, None)
        if slot is None or slot.image_id is None:
            continue

        candidate = candidates_by_id.get(slot.image_id)
        if candidate is None:
            logger.warning("image_id %r not found in manifest; using placeholder", slot.image_id)
            resolved[f"{field}_src"] = placeholder_data_uri()
            continue

        try:
            resolved[f"{field}_src"] = file_to_data_uri(candidate.path)
        except OSError:
            logger.warning("could not read image file %r; using placeholder", candidate.path)
            resolved[f"{field}_src"] = placeholder_data_uri()

    return resolved
