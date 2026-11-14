import asyncio
import json

import httpx
import pytest

from app.agent.provider import HttpProvider
from app.api.app import create_app
from app.config import Settings


async def test_shared_connection_survives_calls_and_retains_deadline() -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if json.loads(request.content)["model"] == "slow":
            await asyncio.sleep(1)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        settings = Settings(
            _env_file=None, llm_api_url="https://model.example/chat", llm_model="fast"
        )
        provider = HttpProvider(settings, client)
        for _ in range(2):
            result = await provider.complete([], [])
            assert result.input_tokens == 3
            assert not client.is_closed
        slow = HttpProvider(
            settings.model_copy(
                update={
                    "llm_model": "slow",
                    "llm_timeout_seconds": 0.01,
                }
            ),
            client,
        )
        with pytest.raises(TimeoutError):
            await slow.complete([], [])
        assert calls == 3  # Timeout never silently retries a possibly billed request.
        assert not client.is_closed
    assert client.is_closed


async def test_application_closes_owned_client_and_can_restart_lifespan() -> None:
    app = create_app(
        Settings(_env_file=None, llm_api_url="https://model.example/chat", llm_model="test")
    )
    for _ in range(2):
        async with app.router.lifespan_context(app):
            provider = app.state.agent_provider
            assert isinstance(provider, HttpProvider)
            assert provider.client is not None and not provider.client.is_closed
        assert provider.client.is_closed
        assert not hasattr(app.state, "agent_provider")
