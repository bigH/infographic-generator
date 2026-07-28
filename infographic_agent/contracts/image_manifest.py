from pydantic import BaseModel


class ImageCandidate(BaseModel):
    """One image the image-search agent found, referenced by slots via `id`."""

    id: str
    path: str  # local file path — the image agent is expected to hand off downloaded files
    alt_text: str | None = None
    tags: list[str] = []
    width: int | None = None
    height: int | None = None


class ImageManifest(BaseModel):
    """Input contract from the image-search agent."""

    topic: str
    candidates: list[ImageCandidate] = []
