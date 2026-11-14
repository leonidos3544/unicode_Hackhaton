import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.bot.main import deliver_replies, process_update
from app.bot.transport import TelegramClient, TelegramError
from app.config import Settings
from app.storage.postgres.runtime_tables import (
    BotState,
    BotUpdate,
    Notification,
    Subscription,
    TelegramIdentity,
)
from app.subscriptions.matching import enqueue_matches
from app.subscriptions.service import SubscriptionInput, create_subscription, unsubscribe
from app.subscriptions.worker import claim_job, deliver_job, quiet_until, tick
from tests.factories import event_payload


@pytest.fixture
def sessions(client: httpx.AsyncClient) -> Any:
    return client._transport.app.state.sessions


@pytest.fixture
def config() -> Settings:
    return Settings(_env_file=None, notification_quiet_start=0, notification_quiet_end=0)


def update(index: int, text: str, *, callback: bool = False, user: int = 42) -> dict[str, Any]:
    msg = {"chat": {"id": user, "type": "private"}, "from": {"id": user}, "text": text}
    if callback:
        return {
            "update_id": index,
            "callback_query": {
                "id": str(index),
                "from": {"id": user},
                "message": msg,
                "data": text,
            },
        }
    return {"update_id": index, "message": msg}


async def seed(client: httpx.AsyncClient, headers: dict[str, str], **extra: Any) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/admin/events", headers=headers, json=event_payload(**extra)
    )
    assert response.status_code == 201, response.text
    card = response.json()
    response = await client.post(
        f"/api/v1/admin/events/{card['id']}/publish",
        headers=headers,
        json={"version": card["version"], "reason": "Test publication"},
    )
    assert response.status_code == 200, response.text
    return response.json()


class FakeTelegram:
    def __init__(self, error: TelegramError | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error = error

    async def call(self, method: str, payload: dict[str, Any]) -> None:
        self.calls.append((method, payload))
        if self.error:
            raise self.error


async def test_bot_search_subscription_restart_and_duplicate_updates(
    client, admin_headers, sessions, config
):
    card = await seed(client, admin_headers)
    for item in [
        update(1, "/start"),
        update(2, "city:" + card["city_id"], callback=True),
        update(3, "/results"),
        update(4, "/subscribe"),
    ]:
        await process_update(sessions, item, config)
    async with sessions() as session:
        proposal = await session.get(BotUpdate, 4)
        button = proposal.replies[-1]["payload"]["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]
    await process_update(sessions, update(5, button, callback=True), config)
    await process_update(sessions, update(5, button, callback=True), config)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 1
        identity = await session.get(TelegramIdentity, "42")
        state = await session.get(BotState, "user:" + identity.owner_id)
        assert state.value["filters"]["city_id"] == card["city_id"]
        replies = await session.get(BotUpdate, 3)
        assert any(card["title"] in r["payload"].get("text", "") for r in replies.replies)
    fake = FakeTelegram()
    assert await deliver_replies(sessions, fake) > 0
    assert await deliver_replies(sessions, fake) == 0
    async with sessions() as session, session.begin():
        # Retention can delete finished replies; persisted offset still prevents re-execution.
        await session.delete(await session.get(BotUpdate, 5))
    await process_update(sessions, update(5, button, callback=True), config)
    async with sessions() as session:
        assert await session.get(BotUpdate, 5) is None
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 1


async def test_invalid_bot_filter_does_not_poison_state(client, admin_headers, sessions, config):
    card = await seed(client, admin_headers)
    for item in [
        update(1, "city:" + card["city_id"], callback=True),
        update(2, "/age"),
        update(3, "-10"),
        update(4, "8"),
    ]:
        await process_update(sessions, item, config)
    async with sessions() as session:
        identity = await session.get(TelegramIdentity, "42")
        state = await session.get(BotState, "user:" + identity.owner_id)
        assert state.value["filters"]["age"] == 8
        assert state.value["waiting"] is None


async def test_matching_idempotency_and_no_token_inbox(client, admin_headers, sessions, config):
    card = await seed(client, admin_headers)
    async with sessions() as session, session.begin():
        for channel in ("inbox", "telegram"):
            await create_subscription(
                session, "test-owner", SubscriptionInput(city_id=card["city_id"], channel=channel)
            )
    for n in range(3):
        await seed(client, admin_headers, external_id=f"new-{n}", title=f"Новое событие {n}")
    assert await tick(sessions, config, None) == 6
    assert await tick(sessions, config, None) == 0
    async with sessions() as session:
        jobs = (await session.scalars(select(Notification))).all()
        assert len(jobs) == 6
        assert sum(job.status == "sent" for job in jobs) == 3
        assert sum(job.status == "pending" for job in jobs) == 3


async def setup_delivery(client, headers, sessions):
    card = await seed(client, headers)
    async with sessions() as session, session.begin():
        sub = await create_subscription(
            session, "owner", SubscriptionInput(city_id=card["city_id"], channel="telegram")
        )
        session.add(TelegramIdentity(telegram_user_id="42", chat_id="42", owner_id="owner"))
    added = await seed(client, headers, external_id="new", title="Новый концерт")
    async with sessions() as session, session.begin():
        assert await enqueue_matches(session) == 1
    return sub, added


async def test_delivery_claim_cancel_and_unsubscribe(client, admin_headers, sessions, config):
    sub, card = await setup_delivery(client, admin_headers, sessions)
    first, second = await asyncio.gather(
        claim_job(sessions, config, telegram_ready=True),
        claim_job(sessions, config, telegram_ready=True),
    )
    job = first or second
    assert job and not (first and second)
    response = await client.post(
        f"/api/v1/admin/events/{card['id']}/cancel",
        headers=admin_headers,
        json={"version": card["version"], "reason": "Cancelled test"},
    )
    assert response.status_code == 200
    fake = FakeTelegram()
    await deliver_job(sessions, job, fake)
    assert not fake.calls
    async with sessions() as session, session.begin():
        await unsubscribe(session, "other-owner", sub["id"])
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 1
        await unsubscribe(session, "owner", sub["id"])
        assert await session.scalar(select(func.count()).select_from(Notification)) == 0


async def test_delivery_retry_expired_lease_and_daily_limit(
    client, admin_headers, sessions, config
):
    await setup_delivery(client, admin_headers, sessions)
    job = await claim_job(sessions, config, telegram_ready=True)
    await deliver_job(sessions, job, FakeTelegram(TelegramError(429, 60)))
    async with sessions() as session, session.begin():
        record = await session.get(Notification, job["id"])
        assert record.status == "pending" and record.attempts == 1
        assert record.available_at > datetime.now(UTC) + timedelta(seconds=50)
        record.available_at = datetime.now(UTC) - timedelta(seconds=1)
    reclaimed = await claim_job(sessions, config, telegram_ready=True)
    async with sessions() as session, session.begin():
        record = await session.get(Notification, job["id"])
        record.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    recovered = await claim_job(sessions, config, telegram_ready=True)
    assert recovered["lease"] != reclaimed["lease"]
    fake = FakeTelegram()
    await deliver_job(sessions, reclaimed, fake)
    assert not fake.calls
    await deliver_job(sessions, recovered, fake)
    assert len(fake.calls) == 1
    await seed(client, admin_headers, external_id="another", title="Другой концерт")
    async with sessions() as session, session.begin():
        await enqueue_matches(session)
    config.notification_daily_limit = 1
    assert await claim_job(sessions, config, telegram_ready=True) is None


def test_quiet_hours_wrap_midnight():
    settings = Settings(notification_quiet_start=22, notification_quiet_end=9)
    now = datetime(2030, 1, 1, 20, tzinfo=UTC)  # 01:00 next day in Bashkortostan
    assert quiet_until(now, settings).isoformat() == "2030-01-02T09:00:00+05:00"
    assert quiet_until(datetime(2030, 1, 1, 7, tzinfo=UTC), settings) is None


async def test_telegram_transport_sanitizes_network_errors():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 12}}
            )
        )
    ) as client:
        telegram = TelegramClient("TOP-SECRET", client)
        with pytest.raises(TelegramError) as captured:
            await telegram.call("sendMessage", {"chat_id": 42, "text": "hello"})
        assert captured.value.retry_after == 12
        assert "TOP-SECRET" not in str(captured.value)
