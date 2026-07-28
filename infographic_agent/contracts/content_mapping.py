from pydantic import BaseModel, ConfigDict

from infographic_agent.contracts.template_selection import TemplateId


class ContentMappingResult(BaseModel):
    """Envelope around LLM call #2's output. `slots` is an instance of whichever
    per-template slot model TEMPLATE_REGISTRY[template_id].slot_model points to.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    template_id: TemplateId
    slots: BaseModel
