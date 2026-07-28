from typing import Literal

from pydantic import BaseModel, Field

TemplateId = Literal[
    "stat_grid",
    "timeline",
    "comparison",
    "process_flow",
    "quote_spotlight",
    "ranked_list",
]


class TemplateSelectionOutput(BaseModel):
    """Structured output of LLM call #1."""

    template_id: TemplateId
    confidence: float = Field(ge=0, le=1)
    rationale: str
