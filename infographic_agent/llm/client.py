import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

from infographic_agent.config import settings

load_dotenv()  # picks up ANTHROPIC_API_KEY from a .env file in the working directory, if present

_client = anthropic.Anthropic()


def structured_call(prompt: str, output_format: type[BaseModel], max_tokens: int = 2048) -> BaseModel:
    # No retry logic for v1 — the SDK's default retries (429/5xx/connection errors)
    # apply automatically; add app-level retries here later if calls prove flaky.
    response = _client.messages.parse(
        model=settings.llm_model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        output_format=output_format,
    )
    return response.parsed_output
