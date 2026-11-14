from datetime import timedelta
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import func, select

from app.agent.confirmations import prepare_action, resolve_action
from app.auth.service import token_hash
from app.bot.handlers import handle_update
from app.common.clock import utcnow
from app.common.errors import AppError
from app.storage.postgres.runtime_tables import (
    LinkToken,
    PendingAction,
    Subscription,
    TelegramIdentity,
)
from tests.test_agent import setup_dialogue


async def test_telegram_link_requires_real_user_confirmation(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    app, owner, _, _ = await setup_dialogue(client, admin_headers)
    link = (await client.post("/api/v1/sessions/telegram-link")).json()["token"]
    update = {
        "message": {
            "from": {"id": 98765},
            "chat": {"id": 98765, "type": "private"},
            "text": "/start link_" + link,
        }
    }
    async with app.state.sessions() as session, session.begin():
        replies = await handle_update(session, update, app.state.settings)
        assert await session.get(LinkToken, token_hash(link)) is not None
        identity = await session.get(TelegramIdentity, "98765")
        assert identity.owner_id != owner
        callback_data = replies[0]["payload"]["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]
    callback_update = {
        "callback_query": {
            "id": "link-confirm",
            "from": {"id": 98765},
            "message": {"chat": {"id": 98765, "type": "private"}},
            "data": callback_data,
        }
    }
    async with app.state.sessions() as session, session.begin():
        await handle_update(session, callback_update, app.state.settings)
        identity = await session.get(TelegramIdentity, "98765")
        assert identity.owner_id == owner
        assert await session.get(LinkToken, token_hash(link)) is None


async def test_subscription_patch_ownership_preserves_id(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    app, owner, _, city = await setup_dialogue(client, admin_headers)
    original = (await client.post("/api/v1/subscriptions", json={"city_id": city})).json()
    changed = await client.patch(
        "/api/v1/subscriptions/" + original["id"], json={"city_id": city, "category": "concert"}
    )
    assert changed.status_code == 200
    assert changed.json()["id"] == original["id"]
    assert changed.json()["category"] == "concert"
    missing = await client.patch("/api/v1/subscriptions/" + str(uuid4()), json={"city_id": city})
    assert missing.status_code == 404
    client.cookies.clear()
    token = (await client.post("/api/v1/sessions")).json()["csrf_token"]
    client.headers["X-CSRF-Token"] = token
    foreign = await client.patch("/api/v1/subscriptions/" + original["id"], json={"city_id": city})
    assert foreign.status_code == 404
    async with app.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 1


async def test_login_rate_survives_rejected_transactions(client: AsyncClient) -> None:
    for _ in range(10):
        response = await client.post("/api/v1/admin/auth/login", json={"api_key": "invalid-key"})
        assert response.status_code == 401
    assert (
        await client.post("/api/v1/admin/auth/login", json={"api_key": "test-admin-key"})
    ).status_code == 429


async def test_origin_and_csrf_boundaries(client: AsyncClient) -> None:
    assert (
        await client.post("/api/v1/sessions", headers={"Origin": "https://untrusted.example"})
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/admin/auth/login",
            headers={"Origin": "https://untrusted.example"},
            json={"api_key": "test-admin-key"},
        )
    ).status_code == 403
    csrf = (await client.post("/api/v1/sessions")).json()["csrf_token"]
    assert (await client.delete("/api/v1/sessions/current")).status_code == 403
    assert (
        await client.delete(
            "/api/v1/sessions/current",
            headers={"X-CSRF-Token": csrf, "Origin": "https://untrusted.example"},
        )
    ).status_code == 403
    assert (
        await client.delete("/api/v1/sessions/current", headers={"X-CSRF-Token": csrf})
    ).status_code == 204
    assert (await client.get("/api/v1/subscriptions")).status_code == 401


async def test_feedback_honeypot_period_validation_and_trace_access(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    _, _, _, city = await setup_dialogue(client, admin_headers)
    for period in [
        {"unknown": "x"},
        {"date_from": "2026-11-14T00:00:00"},
        {"date_from": "2026-11-16T00:00:00+05:00", "date_to": "2026-11-15T00:00:00+05:00"},
    ]:
        response = await client.post(
            "/api/v1/subscriptions", json={"city_id": city, "period_filter": period}
        )
        assert response.status_code == 422
    response = await client.post(
        "/api/v1/feedback", json={"text": "Тестовое обращение", "website": "spam.example"}
    )
    assert response.status_code == 422
    assert (await client.get("/api/v1/admin/traces")).status_code == 401


async def test_pending_expiry_owner_rejection_and_replay(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    app, owner, _, city = await setup_dialogue(client, admin_headers)
    async with app.state.sessions() as session, session.begin():
        proposal = await prepare_action(session, owner, "subscribe", {"city_id": city})
        action_id = UUID(proposal["id"])
    try:
        async with app.state.sessions() as session, session.begin():
            await resolve_action(session, "another-owner", action_id, confirm=True)
    except AppError as exc:
        assert exc.status == 404
    else:
        raise AssertionError("Foreign proposal must not execute")
    for _ in range(2):
        async with app.state.sessions() as session, session.begin():
            assert await resolve_action(session, owner, action_id, confirm=False) == {
                "status": "rejected"
            }
    async with app.state.sessions() as session, session.begin():
        expired = await prepare_action(session, owner, "subscribe", {"city_id": city})
        record = await session.get(PendingAction, expired["id"])
        record.expires_at = utcnow() - timedelta(seconds=1)
    response = await client.post(
        "/api/v1/actions/" + expired["id"] + "/decision", json={"confirm": True}
    )
    assert response.status_code == 409
