"""Bounded HTTP tool-calling protocol; provider URLs and credentials are server settings."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings


class ToolCall(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    arguments: str = Field(max_length=8000)


class ModelReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calls: list[ToolCall] = Field(default_factory=list, max_length=20)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class Provider(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


def failure_reason(error: Exception) -> str:
    """Only stable codes leave the adapter; exception text may contain credentials."""
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "provider_timeout"
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code in {401, 403}:
            return "provider_auth_error"
        if error.response.status_code == 429:
            return "provider_rate_limit"
    if isinstance(error, httpx.RequestError):
        return "provider_unreachable"
    return "provider_error"


class HttpProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[httpx.AsyncClient]:
        if self.client is not None:
            yield self.client
        else:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                yield client

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        config = self.settings
        headers = {"Content-Type": "application/json"}
        if config.llm_api_key.get_secret_value():
            headers["Authorization"] = f"Bearer {config.llm_api_key.get_secret_value()}"
        tool_results = [message for message in messages if message.get("role") == "tool"]
        # A rejected call has not answered the question. Give the model an opportunity to
        # correct it within the service's existing tool, request and cost limits.
        completed = any("error" not in json.loads(item["content"]) for item in tool_results)
        async with (
            asyncio.timeout(config.llm_timeout_seconds),
            self.connection() as client,
        ):
            async with client.stream(
                "POST",
                config.llm_api_url,
                headers=headers,
                json={
                    "model": config.llm_model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto" if completed else "required",
                    "max_tokens": config.llm_max_output_tokens,
                    "temperature": 0,
                },
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 100_000:
                        raise ValueError("provider_response_too_large")
        data = json.loads(body)
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return ModelReply(
            calls=[
                ToolCall(
                    id=call["id"],
                    name=call["function"]["name"],
                    arguments=call["function"]["arguments"],
                )
                for call in message.get("tool_calls", [])
            ],
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
