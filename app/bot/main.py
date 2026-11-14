import asyncio
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.provider import HttpProvider, Provider
from app.bot.agent import REQUEST_METHOD, complete_pending
from app.bot.handlers import handle_update
from app.bot.transport import TelegramClient, TelegramError
from app.common.clock import utcnow
from app.config import Settings, get_settings
from app.storage.postgres.engine import build_database
from app.storage.postgres.runtime_tables import BotState, BotUpdate

logger = logging.getLogger("bashculture.bot")
Sessions = async_sessionmaker[AsyncSession]


async def process_update(
    sessions: Sessions, update: dict[str, Any], settings: Settings, provider: Provider | None = None
) -> None:
    update_id = int(update["update_id"])
    async with sessions() as session, session.begin():
        # Serializes state transitions across accidental duplicate bot processes.
        await session.execute(text("SELECT pg_advisory_xact_lock(71013003)"))
        offset = await session.get(BotState, "poll_offset")
        if offset and update_id < int(offset.value.get("offset", 0)):
            return
        if await session.get(BotUpdate, update_id) is None:
            replies = await handle_update(session, update, settings)
            session.add(
                BotUpdate(
                    update_id=update_id,
                    replies=replies,
                    status="agent_pending"
                    if replies and replies[0]["method"] == REQUEST_METHOD
                    else "pending"
                    if replies
                    else "done",
                )
            )
        if offset is None:
            session.add(BotState(key="poll_offset", value={"offset": update_id + 1}))
        else:
            offset.value = {"offset": max(int(offset.value.get("offset", 0)), update_id + 1)}
    await complete_pending(sessions, settings, provider, update_id=update_id)


async def deliver_replies(sessions: Sessions, telegram: TelegramClient) -> int:
    delivered = 0
    for _ in range(30):
        lease = str(uuid4())
        async with sessions() as session, session.begin():
            record = await session.scalar(
                select(BotUpdate)
                .where(
                    BotUpdate.status == "pending",
                    BotUpdate.available_at <= utcnow(),
                    or_(BotUpdate.lease_until.is_(None), BotUpdate.lease_until < utcnow()),
                )
                .order_by(BotUpdate.update_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if record is None:
                break
            if record.next_reply >= len(record.replies):
                record.status = "done"
                continue
            record.lease_id, record.lease_until = lease, utcnow() + timedelta(seconds=60)
            record.attempts += 1
            reply, index, update_id = (
                record.replies[record.next_reply],
                record.next_reply,
                record.update_id,
            )
        error: TelegramError | None = None
        response: Any = None
        compact_key = "compact:" + reply["compact_owner"] if reply.get("compact_owner") else None
        try:
            if compact_key:
                async with sessions() as session:
                    previous = await session.get(BotState, compact_key)
                    previous_value = dict(previous.value) if previous else {}
                if previous_value.get("message_id") and str(previous_value.get("chat_id")) == str(
                    reply["payload"]["chat_id"]
                ):
                    try:
                        await telegram.call(
                            "deleteMessage",
                            {
                                "chat_id": reply["payload"]["chat_id"],
                                "message_id": previous_value["message_id"],
                            },
                        )
                    except TelegramError as deletion_error:
                        # Old/deleted messages must not prevent delivery of a new result.
                        if deletion_error.code not in {400, 403}:
                            raise
            response = await telegram.call(reply["method"], reply["payload"])
        except TelegramError as exc:
            error = exc
        async with sessions() as session, session.begin():
            record = await session.get(BotUpdate, update_id, with_for_update=True)
            if record is None or record.lease_id != lease:
                continue
            record.lease_until = record.lease_id = None
            expired_callback = (
                error and error.code == 400 and reply["method"] == "answerCallbackQuery"
            )
            if not error or expired_callback:
                if compact_key and isinstance(response, dict) and response.get("message_id"):
                    compact = await session.get(BotState, compact_key, with_for_update=True)
                    if compact is None:
                        compact = BotState(key=compact_key)
                        session.add(compact)
                    compact.value = {
                        "chat_id": str(reply["payload"]["chat_id"]),
                        "message_id": int(response["message_id"]),
                    }
                record.next_reply = index + 1
                record.attempts = 0
                record.status = "done" if record.next_reply == len(record.replies) else "pending"
                delivered += 1
            elif error.code in {400, 401, 403} or record.attempts >= 5:
                record.status = "failed"
                logger.warning("reply_failed code=%s", error.code)
            else:
                record.available_at = utcnow() + timedelta(
                    seconds=max(error.retry_after, 2**record.attempts * 10)
                )
    return delivered


async def run_bot() -> None:
    settings = get_settings()
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        logger.info("telegram_disabled: token is not configured")
        await asyncio.Event().wait()
    engine, sessions = build_database(settings.database_url)
    try:
        async with httpx.AsyncClient() as client:
            telegram = TelegramClient(token, client)
            provider = (
                HttpProvider(settings, client)
                if (
                    settings.llm_api_url
                    and settings.llm_model
                    and (
                        not settings.llm_require_api_key or settings.llm_api_key.get_secret_value()
                    )
                )
                else None
            )
            commands = {
                "commands": [
                    {"command": name, "description": description}
                    for name, description in (
                        ("chat", "Диалог с ИИ"),
                        ("search", "Найти событие"),
                        ("plan", "План дня"),
                        ("subscriptions", "Мои подписки"),
                        ("unsubscribe", "Отписаться"),
                        ("cancel", "Сбросить поиск"),
                        ("help", "Помощь"),
                    )
                ]
            }
            commands_ready = False
            while True:
                try:
                    if not commands_ready:
                        await telegram.call("setMyCommands", commands)
                        commands_ready = True
                    await complete_pending(sessions, settings, provider)
                    await deliver_replies(sessions, telegram)
                    async with sessions() as session:
                        state = await session.get(BotState, "poll_offset")
                        offset = int(state.value.get("offset", 0)) if state else 0
                    updates = await telegram.call(
                        "getUpdates",
                        {
                            "offset": offset,
                            "timeout": 20,
                            "limit": 30,
                            "allowed_updates": ["message", "callback_query"],
                        },
                    )
                    for update in updates or []:
                        await process_update(sessions, update, settings, provider)
                        await deliver_replies(sessions, telegram)
                except TelegramError as exc:
                    logger.warning("telegram_unavailable code=%s", exc.code)
                    await asyncio.sleep(max(5, min(exc.retry_after or 10, 30)))
                except Exception as exc:
                    logger.error("bot_iteration_failed type=%s", type(exc).__name__)
                    await asyncio.sleep(5)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_bot())
