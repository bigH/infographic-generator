from pydantic import BaseModel, Field

from infographic_agent.templates.base_slots import ImageSlot, StatSlot


class RankedListSlots(BaseModel):
    title: str = Field(max_length=80)
    items: list[StatSlot] = Field(min_length=3, max_length=7)  # rank order = list order
    hero_image: ImageSlot | None = None
