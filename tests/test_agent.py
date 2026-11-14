import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.agent.parsing import parse_request
from app.agent.provider import HttpProvider, ModelReply, ToolCall
from app.agent.service import respond
from app.common.errors import AppError
from app.config import Settings
from app.storage.postgres.runtime_tables import (
    AgentTrace,
    BudgetAccount,
    Conversation,
    PendingAction,
    Subscription,
)
from tests.factories import event_payload
from tests.test_catalog_integration import publish, upload


def test_offline_parser_shared_categories_timezone_and_ambiguity() -> None:
    cities = [{"id": str(uuid4()), "name": "Уфа"}, {"id": str(uuid4()), "name": "Уфалей"}]
    now = datetime(2026, 11, 14, 22, tzinfo=UTC)  # Already Nov 15 in Ufa.
    filters, question = parse_request("Музей в Уфе завтра", {}, cities, now, "Asia/Yekaterinburg")
    assert question is None
    assert filters.category == "exhibition"
    assert filters.date_from and filters.date_from.day == 16
    assert str(filters.city_id) == cities[0]["id"]
    _, question = parse_request("Уфа или Уфалей", {}, cities, now, "Asia/Yekaterinburg")
    assert question == "city"


class FakeProvider:
    def __init__(self, replies: list[ModelReply], *, delay: float = 0):
        self.replies = replies
        self.calls = 0
        self.delay = delay

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


def call(name: str, arguments: dict[str, Any] | str) -> ToolCall:
    return ToolCall(
        id=str(uuid4()),
        name=name,
        arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
    )


async def setup_dialogue(client: AsyncClient, headers: dict[str, str]) -> tuple[Any, str, Any, str]:
    report = await upload(client, headers, [event_payload(city="Уфа")])
    event_id = report["rows"][0]["event_id"]
    event = await publish(client, headers, event_id)
    response = await client.post("/api/v1/sessions")
    csrf = response.json()["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    dialogue = await client.post("/api/v1/conversations")
    assert dialogue.status_code == 201, dialogue.text
    app = client._transport.app
    async with app.state.sessions() as session:
        conversation = await session.get(Conversation, dialogue.json()["id"])
        owner = conversation.owner_id
    return app, owner, conversation.id, event["city_id"]


async def test_fallback_memory_replay_ownership_and_csrf(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    app, owner, cid, _ = await setup_dialogue(client, admin_headers)
    first = await client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"message": "Выставки в Уфе", "client_request_id": "first"},
    )
    assert first.status_code == 200, first.text
    answer = first.json()
    assert answer["mode"] == "fallback" and len(answer["items"]) == 1
    replay = await client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"message": "Выставки в Уфе", "client_request_id": "first"},
    )
    assert replay.json() == answer
    for index, text in enumerate(["дешевле", "до 1000", "ещё", "бесплатно", "платные тоже"]):
        result = await respond(app.state.sessions, app.state.settings, owner, cid, str(index), text)
        assert result["filters"]["city_id"] == answer["filters"]["city_id"]
        assert result["filters"]["category"] == "exhibition"
    history = (await client.get(f"/api/v1/conversations/{cid}")).json()
    assert len(history["turns"]) == 6
    assert len((await client.get("/api/v1/conversations")).json()) == 1
    async with app.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentTrace)) == 6
    response = await client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"message": "другой", "client_request_id": "first"},
    )
    assert response.status_code == 409
    response = await client.post(
        f"/api/v1/conversations/{cid}/messages",
        headers={"X-CSRF-Token": "wrong"},
        json={"message": "Уфа", "client_request_id": "csrf"},
    )
    assert response.status_code == 403
    client.cookies.clear()
    csrf = (await client.post("/api/v1/sessions")).json()["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    assert (await client.get(f"/api/v1/conversations/{cid}")).status_code == 404


async def test_batch_limit_bad_args_and_grounding(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    app, owner, cid, _ = await setup_dialogue(client, admin_headers)
    provider = FakeProvider(
        [
            ModelReply(
                calls=[
                    call("unknown", {}),
                    call("search_events", "{"),
                    *[call("search_events", {}) for _ in range(4)],
                ]
            )
        ]
    )
    result = await respond(
        app.state.sessions, Settings(llm_free=True), owner, cid, "batch", "Выставки в Уфе", provider
    )
    assert len(result["items"]) == 1
    assert provider.calls == 1
    async with app.state.sessions() as session:
        trace = await session.scalar(select(AgentTrace))
        assert len(trace.calls) == 5
        assert trace.calls[0]["status"] == "unknown_tool"
        assert trace.calls[1]["status"] == "invalid_arguments"
    replay = await respond(
        app.state.sessions, Settings(llm_free=True), owner, cid, "batch", "Выставки в Уфе", provider
    )
    assert replay == result and provider.calls == 1


async def test_actions_require_intent_confirmation_owner_and_unchanged_payload(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    app, owner, cid, city = await setup_dialogue(client, admin_headers)
    payload = {"city_id": city}
    provider = FakeProvider([ModelReply(calls=[call("subscribe", payload)]), ModelReply()])
    result = await respond(
        app.state.sessions,
        Settings(llm_free=True),
        owner,
        cid,
        "no-intent",
        "Выставки в Уфе",
        provider,
    )
    assert result["pending_actions"] == []
    provider = FakeProvider([ModelReply(calls=[call("subscribe", payload)]), ModelReply()])
    result = await respond(
        app.state.sessions,
        Settings(llm_free=True),
        owner,
        cid,
        "intent",
        "Подпиши на выставки в Уфе",
        provider,
    )
    action = result["pending_actions"][0]
    async with app.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 0
    response = await client.post(
        "/api/v1/actions/" + action["id"] + "/decision", json={"confirm": True}
    )
    assert response.status_code == 200, response.text
    repeat = await client.post(
        "/api/v1/actions/" + action["id"] + "/decision", json={"confirm": True}
    )
    assert repeat.json() == response.json()
    async with app.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 1
    # Payload tampering is detected even if the changed payload remains valid.
    provider = FakeProvider([ModelReply(calls=[call("subscribe", payload)]), ModelReply()])
    result = await respond(
        app.state.sessions, Settings(llm_free=True), owner, cid, "tamper", "Подпиши в Уфе", provider
    )
    action_id = result["pending_actions"][0]["id"]
    async with app.state.sessions() as session, session.begin():
        action_row = await session.get(PendingAction, action_id)
        action_row.payload = {**action_row.payload, "channel": "telegram"}
    assert (
        await client.post("/api/v1/actions/" + action_id + "/decision", json={"confirm": True})
    ).status_code == 409


async def test_parallel_budget_reservation_and_unknown_usage(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    app, owner, cid, _ = await setup_dialogue(client, admin_headers)
    async with app.state.sessions() as session, session.begin():
        other = Conversation(owner_id=owner)
        session.add(other)
        await session.flush()
        other_id = other.id
    settings = Settings(
        llm_turn_budget_rub=1,
        llm_daily_budget_rub=1,
        llm_input_rub_per_million=1,
        llm_output_rub_per_million=1,
    )
    provider = FakeProvider([ModelReply(calls=[call("search_events", {})])], delay=0.1)
    results = await asyncio.gather(
        *[
            respond(app.state.sessions, settings, owner, c, "parallel", "Уфа", provider)
            for c in [cid, other_id]
        ]
    )
    assert provider.calls == 1
    assert sum(item["cost"]["rub"] for item in results) == 1
    assert any(item["fallback_reason"] == "budget_exhausted" for item in results)
    async with app.state.sessions() as session:
        account = await session.scalar(select(BudgetAccount))
        assert account.spent == 1 and account.reserved == 0


async def test_same_conversation_parallel_lease(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    app, owner, cid, _ = await setup_dialogue(client, admin_headers)
    provider = FakeProvider(
        [ModelReply(calls=[call("search_events", {})]), ModelReply()], delay=0.1
    )
    results = await asyncio.gather(
        *[
            respond(
                app.state.sessions, Settings(llm_free=True), owner, cid, str(i), "Уфа", provider
            )
            for i in range(2)
        ],
        return_exceptions=True,
    )
    assert (
        len(
            [
                result
                for result in results
                if isinstance(result, AppError) and result.code == "conversation_busy"
            ]
        )
        == 1
    )


async def test_provider_failure_preserves_catalog_and_marks_estimate(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    app, owner, cid, _ = await setup_dialogue(client, admin_headers)

    class FailedProvider:
        async def complete(self, messages: Any, tools: Any) -> ModelReply:
            raise TimeoutError

    result = await respond(
        app.state.sessions,
        Settings(llm_input_rub_per_million=1, llm_output_rub_per_million=1),
        owner,
        cid,
        "timeout",
        "Уфа",
        FailedProvider(),
    )
    assert result["mode"] == "fallback" and result["items"]
    assert result["fallback_reason"] == "provider_timeout"
    assert "не успел ответить" in result["message"]
    assert result["cost"] == {"rub": 1, "estimated": True, "usage_known": False}


@pytest.mark.parametrize(
    ("tool_result", "expected_choice"),
    [(None, "required"), ({"error": "invalid_arguments"}, "required"), ({"items": []}, "auto")],
)
async def test_http_adapter_parses_protocol(
    monkeypatch: pytest.MonkeyPatch, tool_result: dict[str, Any] | None, expected_choice: str
) -> None:
    original = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        assert data["model"] == "test-model" and data["max_tokens"] == 700
        assert data["tool_choice"] == expected_choice
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {"name": "search_events", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 20},
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    messages = (
        [{"role": "tool", "tool_call_id": "previous", "content": json.dumps(tool_result)}]
        if tool_result is not None
        else []
    )
    reply = await HttpProvider(
        Settings(llm_api_url="https://provider.example/chat", llm_model="test-model")
    ).complete(messages, [])
    assert (
        reply.input_tokens == 30
        and reply.output_tokens == 20
        and reply.calls[0].name == "search_events"
    )
