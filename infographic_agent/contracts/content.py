from pydantic import BaseModel


class SourceRef(BaseModel):
    url: str
    title: str | None = None


class ContentFact(BaseModel):
    label: str
    value: str
    detail: str | None = None
    source: SourceRef | None = None


class ContentPayload(BaseModel):
    """Input contract from the search agent."""

    topic: str
    audience: str
    learning_preference: str  # e.g. "text_heavy", "image_heavy", "balanced"
    headline: str | None = None
    summary: str
    facts: list[ContentFact] = []
    key_points: list[str] = []
    steps: list[str] = []
    comparisons: list[dict] = []  # [{"label": str, "a": str, "b": str}]
    quotes: list[dict] = []  # [{"text": str, "attribution": str}]
    timeline_events: list[dict] = []  # [{"date": str, "label": str}]
    tags: list[str] = []
    sources: list[SourceRef] = []
