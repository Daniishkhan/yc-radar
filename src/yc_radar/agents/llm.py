from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from yc_radar.core.config import Settings, get_settings


class LLMClient(Protocol):
    async def complete(self, system: str, user: str) -> str:
        """Return a single text completion."""


@dataclass
class NullLLMClient:
    reason: str = "OPENAI_API_KEY is not set."

    async def complete(self, system: str, user: str) -> str:
        raise RuntimeError(self.reason)


class OpenAIResponsesClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")

    async def complete(self, system: str, user: str) -> str:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        response = await client.responses.create(
            model=self.settings.openai_model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.output_text


def get_llm_client() -> LLMClient:
    settings = get_settings()
    if settings.openai_api_key:
        return OpenAIResponsesClient(settings)
    return NullLLMClient()

