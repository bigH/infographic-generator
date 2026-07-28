from pydantic import BaseModel, Field

from infographic_agent.templates.base_slots import ImageSlot, StatSlot


class QuoteSpotlightSlots(BaseModel):
    quote_text: str = Field(max_length=280)
    attribution: str = Field(max_length=80)
    supporting_stat: StatSlot | None = None
    background_image: ImageSlot | None = None
