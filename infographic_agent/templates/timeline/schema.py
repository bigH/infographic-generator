from pydantic import BaseModel, Field

from infographic_agent.templates.base_slots import ImageSlot, TimelineEventSlot


class TimelineSlots(BaseModel):
    title: str = Field(max_length=80)
    events: list[TimelineEventSlot] = Field(min_length=3, max_length=8)
    hero_image: ImageSlot | None = None
