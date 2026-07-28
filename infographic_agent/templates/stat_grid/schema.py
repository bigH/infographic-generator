from pydantic import BaseModel, Field

from infographic_agent.templates.base_slots import ImageSlot, StatSlot


class StatGridSlots(BaseModel):
    title: str = Field(max_length=80)
    subtitle: str | None = Field(default=None, max_length=140)
    stats: list[StatSlot] = Field(min_length=3, max_length=6)
    hero_image: ImageSlot | None = None
    footer_source: str | None = Field(default=None, max_length=120)
