from pydantic import BaseModel, Field

from infographic_agent.templates.base_slots import ImageSlot


class ProcessFlowSlots(BaseModel):
    title: str = Field(max_length=80)
    steps: list[str] = Field(min_length=3, max_length=6)
    hero_image: ImageSlot | None = None
