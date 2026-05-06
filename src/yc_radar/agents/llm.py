from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from yc_radar.core.config import Settings, get_settings


class LLMClient(Protocol):
    async def complete(self, system: str, user: str) -> str:
        """Return a single text completion."""

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], name: str) -> Any:
        """Return a JSON completion matching the provided schema."""


@dataclass
class NullLLMClient:
    reason: str = "OPENAI_API_KEY is not set."

    async def complete(self, system: str, user: str) -> str:
        raise RuntimeError(self.reason)

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], name: str) -> Any:
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

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], name: str) -> Any:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        response = await client.responses.create(
            model=self.settings.openai_model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": name,
                    "schema": schema,
                    "strict": True,
                }
            },
            temperature=0,
        )
        return json.loads(response.output_text)


def get_llm_client() -> LLMClient:
    settings = get_settings()
    if settings.openai_api_key:
        return OpenAIResponsesClient(settings)
    return NullLLMClient()
