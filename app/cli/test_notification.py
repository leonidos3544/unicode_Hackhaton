"""Explicit, labelled live delivery check for the sole consenting Telegram subscription."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import delete, func, select

from app.bot.transport import TelegramClient
from app.common.clock import utcnow
from app.config import get_settings
from app.search.service import filter_conditions
from app.storage.postgres.engine import build_database
from app.storage.postgres.runtime_tables import Notification, Subscription, TelegramIdentity
from app.storage.postgres.tables import AuditLog, Event
from app.subscriptions.matching import subscription_filters
from app.subscriptions.worker import claim_job, deliver_job


async def run() -> dict[str, Any]:
    settings = get_settings()
    if not settings.telegram_bot_token.get_secret_value():
        raise ValueError("Telegram token is not configured")
    engine, sessions = build_database(settings.database_url)
    notification_id = None
    try:
        async with sessions() as session, session.begin():
            items = (
                await session.scalars(
                    select(Subscription).where(Subscription.channel == "telegram")
                )
            ).all()
            if len(items) != 1:
                raise ValueError(
                    "Live check requires exactly one Telegram subscription; "
                    "target selection is ambiguous"
                )
            item = items[0]
            linked = await session.scalar(
                select(TelegramIdentity.telegram_user_id).where(
                    TelegramIdentity.owner_id == item.owner_id, TelegramIdentity.enabled.is_(True)
                )
            )
            if linked is None:
                raise ValueError("Subscription has no active Telegram identity")
            pending = await session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.status == "pending")
            )
            if pending:
                raise ValueError(
                    "Let existing pending notifications finish before the isolated delivery check"
                )
            event = await session.scalar(
                select(Event)
                .where(
                    *filter_conditions(subscription_filters(item)),
                    ~select(Notification.id)
                    .where(
                        Notification.subscription_id == item.id, Notification.event_id == Event.id
                    )
                    .exists(),
                )
                .order_by(Event.date_start)
                .limit(1)
            )
            if event is None:
                raise ValueError("No current event fits this subscription")
            job = Notification(subscription_id=item.id, event_id=event.id)
            session.add(job)
            await session.flush()
            notification_id, event_id = job.id, event.id
        claimed = await claim_job(sessions, settings, telegram_ready=True)
        if claimed is None or claimed["id"] != notification_id:
            raise ValueError(
                "Delivery not claimed: quiet hours, daily limit or concurrent worker; "
                "no test was sent"
            )
        claimed["payload"]["text"] = (
            "<b>ПРОВЕРКА УВЕДОМЛЕНИЙ</b>\nЭто тест доставки по вашей подписке. "
            "Событие ниже уже было в каталоге и не объявляется новым.\n\n"
            + claimed["payload"]["text"].removeprefix("Новое в вашей афише:\n")
        )
        async with httpx.AsyncClient() as client:
            await deliver_job(
                sessions,
                claimed,
                TelegramClient(settings.telegram_bot_token.get_secret_value(), client),
            )
        async with sessions() as session, session.begin():
            current = await session.get(Notification, notification_id)
            delivered = current is not None and current.status == "sent"
            session.add(
                AuditLog(
                    actor="live-delivery-check",
                    action="notification.test_delivery",
                    event_id=event_id,
                    changes={"telegram_accepted": delivered, "labelled_as_test": True},
                )
            )
        return {
            "checked_at": utcnow().isoformat(),
            "telegram_accepted": delivered,
            "labelled_as_test": True,
            "real_event_id": str(event_id),
            "normal_subscription_preserved": True,
            "note": "Worker and real Telegram transport used. "
            "User receipt is a separate confirmation.",
        }
    finally:
        if notification_id is not None:
            async with sessions() as session, session.begin():
                await session.execute(
                    delete(Notification).where(Notification.id == notification_id)
                )
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send one labelled test through the actual notification worker"
    )
    parser.add_argument("--send", action="store_true", required=True)
    args = parser.parse_args()
    if not args.send:
        return
    result = asyncio.run(run())
    output = Path("reports/operations/notification.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
