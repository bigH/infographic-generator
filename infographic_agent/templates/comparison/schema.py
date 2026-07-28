from pydantic import BaseModel, Field

from infographic_agent.templates.base_slots import ImageSlot


class ComparisonRow(BaseModel):
    label: str = Field(max_length=60)
    left: str = Field(max_length=80)
    right: str = Field(max_length=80)


class ComparisonSlots(BaseModel):
    title: str = Field(max_length=80)
    left_label: str = Field(max_length=40)
    right_label: str = Field(max_length=40)
    rows: list[ComparisonRow] = Field(min_length=2, max_length=6)
    left_image: ImageSlot | None = None
    right_image: ImageSlot | None = None
