import httpx
import pytest
from httpx import AsyncClient

from app.agent.presentation import present
from app.agent.provider import failure_reason
from app.agent.service import respond
from app.config import Settings
from tests.test_agent import FakeProvider, ModelReply, call, setup_dialogue


async def test_greeting_unknown_city_then_supported_city(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    app, owner, cid, _ = await setup_dialogue(client, admin_headers)
    provider = FakeProvider([ModelReply(calls=[call("search_events", {})])])
    settings = Settings(llm_free=True)
    first = await respond(app.state.sessions, settings, owner, cid, "hello", "привет", provider)
    assert first["question"] == "city" and "Уфа" in first["message"]
    for index in range(2):
        unsupported = await respond(
            app.state.sessions, settings, owner, cid, f"unknown-{index}", "ижевск", provider
        )
        assert unsupported["question"] == "city_unavailable"
        assert "Сейчас доступны: Уфа" in unsupported["message"]
        assert "В каком городе" not in unsupported["message"]
        assert not unsupported["items"]
    assert provider.calls == 0
    supported = await respond(
        app.state.sessions, settings, owner, cid, "supported", "уфа", provider
    )
    assert supported["question"] is None
    assert supported["mode"] == "agent" and supported["items"]
    assert provider.calls == 1


@pytest.mark.parametrize(
    ("reason", "explanation"),
    [
        ("provider_timeout", "не успел ответить"),
        ("invalid_tool_arguments", "не смог корректно применить"),
        ("budget_exhausted", "лимит расходов"),
        ("provider_auth_error", "ошибка доступа"),
        ("provider_rate_limit", "ограничил число запросов"),
        ("model_not_configured", "ещё не настроен"),
    ],
)
def test_fallback_explains_actual_reason(reason: str, explanation: str) -> None:
    answer = present([], [], None, "fallback", reason)
    assert explanation in answer["message"]
    assert "Выполнен обычный поиск" in answer["message"]
    assert "модель недоступна" not in answer["message"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "provider_auth_error"),
        (403, "provider_auth_error"),
        (429, "provider_rate_limit"),
        (503, "provider_error"),
    ],
)
def test_provider_http_errors_expose_codes_only(status: int, expected: str) -> None:
    request = httpx.Request("POST", "https://model.example?key=private-test-value")
    response = httpx.Response(status, request=request)
    error = httpx.HTTPStatusError("private-test-value", request=request, response=response)
    assert failure_reason(error) == expected


def test_provider_network_and_timeout_codes() -> None:
    assert failure_reason(TimeoutError()) == "provider_timeout"
    assert failure_reason(httpx.ReadTimeout("private-test-value")) == "provider_timeout"
    assert failure_reason(httpx.ConnectError("private-test-value")) == "provider_unreachable"
