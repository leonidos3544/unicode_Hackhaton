from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.repository import event_card, lock_catalog
from app.catalog.schemas import StatusChange
from app.common.clock import utcnow
from app.common.errors import AppError
from app.storage.postgres.tables import AuditLog, Event, ModerationItem, SourceRecord


async def get_event(session: AsyncSession, event_id: UUID, *, admin: bool = False) -> Event:
    event = await session.get(Event, event_id)
    visited = {event_id}
    while event is not None and event.merged_into_id and not admin:
        if event.merged_into_id in visited:
            raise AppError("event_alias_conflict", "Карточка временно недоступна", 409)
        visited.add(event.merged_into_id)
        event = await session.get(Event, event.merged_into_id)
    if event is None or (not admin and event.status == "draft"):
        raise AppError("event_not_found", "Мероприятие не найдено", 404)
    return event


async def change_status(
    session: AsyncSession,
    event_id: UUID,
    target: str,
    change: StatusChange,
    *,
    actor: str = "admin",
) -> dict[str, Any]:
    await lock_catalog(session)
    event = await get_event(session, event_id, admin=True)
    if event.version != change.version:
        raise AppError(
            "version_conflict", "Карточка изменена. Обновите её и повторите действие", 409
        )
    if target == "published":
        pending = await session.scalar(
            select(ModerationItem.id)
            .where(ModerationItem.event_id == event.id, ModerationItem.status == "pending")
            .limit(1)
        )
        if pending:
            raise AppError("moderation_pending", "Сначала разрешите спорные записи источников", 409)
        source = await session.scalar(
            select(SourceRecord.id).where(SourceRecord.event_id == event.id)
        )
        if source is None:
            raise AppError("missing_source", "Для публикации нужен источник", 422)
        if event.date_start <= utcnow():
            raise AppError("event_in_past", "Нельзя публиковать прошедший сеанс", 422)
    old_status = event.status
    if old_status != target:
        event.status = target
        event.version += 1
        event.updated_at = utcnow()
        session.add(
            AuditLog(
                actor=actor,
                action=f"event.{target}",
                event_id=event.id,
                changes={"status": {"from": old_status, "to": target}, "reason": change.reason},
            )
        )
    await session.flush()
    return await event_card(session, event)
