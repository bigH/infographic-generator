from pydantic import BaseModel, Field


class ImageSlot(BaseModel):
    image_id: str | None = None  # references ImageCandidate.id from the manifest, or None
    alt_text: str | None = None


class StatSlot(BaseModel):
    label: str = Field(max_length=60)
    value: str = Field(max_length=30)
    detail: str | None = Field(default=None, max_length=120)


class TimelineEventSlot(BaseModel):
    date: str = Field(max_length=30)
    label: str = Field(max_length=120)
