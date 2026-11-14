import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, or_, select, text

from app.bot.main import Sessions
from app.bot.rendering import card_buttons, message, render_event
from app.bot.transport import TelegramClient, TelegramError
from app.catalog.repository import event_card
from app.common.clock import utcnow
from app.config import Settings, get_settings
from app.search.service import filter_conditions
from app.storage.postgres.engine import build_database
from app.storage.postgres.runtime_tables import Notification, Subscription, TelegramIdentity
from app.storage.postgres.tables import Event
from app.subscriptions.matching import enqueue_matches, subscription_filters

logger = logging.getLogger("bashculture.notifications")


def quiet_until(now: datetime, settings: Settings) -> datetime | None:
    local = now.astimezone(ZoneInfo(settings.event_timezone))
    start, end = settings.notification_quiet_start, settings.notification_quiet_end
    quiet = start != end and (
        start <= local.hour < end if start < end else local.hour >= start or local.hour < end
    )
    if not quiet:
        return None
    target = local.replace(hour=end, minute=0, second=0, microsecond=0)
    return target + timedelta(days=1) if target <= local else target


async def claim_job(
    sessions: Sessions, settings: Settings, *, telegram_ready: bool
) -> dict[str, Any] | None:
    async with sessions() as session, session.begin():
        # Short global lock serializes per-owner daily capacity reservations.
        await session.execute(text("SELECT pg_advisory_xact_lock(71013004)"))
        query = (
            select(Notification)
            .join(Subscription)
            .where(
                Notification.status == "pending",
                Notification.available_at <= utcnow(),
                or_(Notification.lease_until.is_(None), Notification.lease_until < utcnow()),
            )
        )
        if not telegram_ready:
            query = query.where(Subscription.channel != "telegram")
        jobs = (
            await session.scalars(
                query.order_by(Notification.created_at)
                .with_for_update(skip_locked=True, of=Notification)
                .limit(30)
            )
        ).all()
        for job in jobs:
            item = await session.get(Subscription, job.subscription_id)
            if item is None:
                continue
            event = await session.scalar(
                select(Event).where(
                    Event.id == job.event_id, *filter_conditions(subscription_filters(item))
                )
            )
            if event is None:
                job.status = "suppressed"
                continue
            if item.channel == "inbox":
                job.status, job.sent_at = "sent", utcnow()
                continue
            now = utcnow()
            quiet = quiet_until(now, settings)
            if quiet:
                job.available_at = quiet
                continue
            identity = await session.scalar(
                select(TelegramIdentity)
                .where(
                    TelegramIdentity.owner_id == item.owner_id, TelegramIdentity.enabled.is_(True)
                )
                .limit(1)
            )
            if identity is None:
                job.available_at = now + timedelta(minutes=15)
                continue
            midnight = now.astimezone(ZoneInfo(settings.event_timezone)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            used = await session.scalar(
                select(func.count())
                .select_from(Notification)
                .join(Subscription)
                .where(
                    Subscription.owner_id == item.owner_id,
                    Subscription.channel == "telegram",
                    or_(
                        Notification.sent_at >= midnight,
                        (Notification.lease_until > now) & (Notification.id != job.id),
                    ),
                )
            )
            if (used or 0) >= settings.notification_daily_limit:
                job.available_at = midnight + timedelta(
                    days=1, hours=settings.notification_quiet_end
                )
                continue
            job.lease_id, job.lease_until = str(uuid4()), now + timedelta(seconds=60)
            job.attempts += 1
            card = await event_card(session, event)
            return {
                "id": job.id,
                "lease": job.lease_id,
                "chat_id": identity.chat_id,
                "payload": message(
                    identity.chat_id,
                    "Новое в вашей афише:\n" + render_event(card, settings.event_timezone),
                    card_buttons(card),
                )["payload"],
            }
    return None


async def deliver_job(sessions: Sessions, job: dict[str, Any], telegram: TelegramClient) -> None:
    # Recheck immediately before send. A request already accepted by Telegram cannot be recalled.
    async with sessions() as session:
        current = await session.get(Notification, job["id"])
        item = await session.get(Subscription, current.subscription_id) if current else None
        event = (
            await session.scalar(
                select(Event.id).where(
                    Event.id == current.event_id, *filter_conditions(subscription_filters(item))
                )
            )
            if current and item
            else None
        )
        valid = bool(current and current.lease_id == job["lease"] and event)
    error: TelegramError | None = None
    if valid:
        try:
            await telegram.call("sendMessage", job["payload"])
        except TelegramError as exc:
            error = exc
    async with sessions() as session, session.begin():
        current = await session.get(Notification, job["id"], with_for_update=True)
        if not current or current.lease_id != job["lease"]:
            return
        current.lease_id = current.lease_until = None
        if not valid:
            current.status = "suppressed"
        elif error is None:
            current.status, current.sent_at = "sent", utcnow()
        elif error.code in {400, 403} or current.attempts >= 5:
            current.status = "failed"
            if error.code == 403 and item:
                identities = (
                    await session.scalars(
                        select(TelegramIdentity).where(TelegramIdentity.owner_id == item.owner_id)
                    )
                ).all()
                for identity in identities:
                    identity.enabled = False
        else:
            current.available_at = utcnow() + timedelta(
                seconds=max(error.retry_after, 30 * 2**current.attempts)
            )


async def tick(sessions: Sessions, settings: Settings, telegram: TelegramClient | None) -> int:
    async with sessions() as session, session.begin():
        added = await enqueue_matches(session)
    for _ in range(30):
        job = await claim_job(sessions, settings, telegram_ready=telegram is not None)
        if job is None:
            break
        assert telegram is not None
        await deliver_job(sessions, job, telegram)
    return added


async def run_worker() -> None:
    settings = get_settings()
    engine, sessions = build_database(settings.database_url)
    try:
        async with httpx.AsyncClient() as client:
            token = settings.telegram_bot_token.get_secret_value()
            telegram = TelegramClient(token, client) if token else None
            while True:
                try:
                    added = await tick(sessions, settings, telegram)
                    logger.info("notifications_tick added=%s telegram=%s", added, bool(telegram))
                except Exception as exc:
                    logger.error("notifications_failed type=%s", type(exc).__name__)
                await asyncio.sleep(settings.notification_interval_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_worker())
