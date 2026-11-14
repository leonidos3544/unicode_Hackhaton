import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.repository import event_card, get_city, get_venue, lock_catalog
from app.catalog.schemas import EventEdit, EventInput
from app.catalog.service import get_event
from app.common.clock import utcnow
from app.common.errors import AppError
from app.ingestion.normalize import content_hash, normalize_row, normalize_text
from app.ingestion.service import event_values
from app.storage.postgres.tables import AuditLog, ModerationItem, RawRecord, SourceRecord


async def edit_event(
    session: AsyncSession,
    event_id: UUID,
    edit: EventEdit,
) -> dict[str, Any]:
    await lock_catalog(session)
    event = await get_event(session, event_id, admin=True)
    if event.version != edit.version:
        raise AppError("version_conflict", "Карточка изменена. Обновите страницу", 409)
    data = edit.event
    city = await get_city(session, data.city, normalize_text(data.city))
    venue = await get_venue(session, city.id, data.venue, normalize_text(data.venue), data.address)
    if data.lat is not None and data.lon is not None:
        venue.lat, venue.lon = data.lat, data.lon
    values = event_values(data, city.id, venue.id)
    changes = {
        key: {"from": getattr(event, key), "to": value}
        for key, value in values.items()
        if getattr(event, key) != value
    }
    for key, value in values.items():
        setattr(event, key, value)
    event.version += 1
    event.updated_at = utcnow()
    event.status = "cancelled" if event.status == "cancelled" else "draft"
    payload = data.model_dump(mode="json")
    fingerprint = content_hash(payload)
    source = await session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_name == "manual",
            SourceRecord.external_id == f"event:{event.id}",
        )
    )
    if source is None:
        source = SourceRecord(
            source_name="manual",
            external_id=f"event:{event.id}",
            source_url=str(data.source_url),
            event_id=event.id,
            source_type="manual",
        )
        session.add(source)
        await session.flush()
    source.latest_hash = source.applied_hash = fingerprint
    source.source_url = str(data.source_url)
    raw = await session.scalar(
        select(RawRecord).where(
            RawRecord.source_record_id == source.id,
            RawRecord.content_hash == fingerprint,
        )
    )
    if raw is None:
        session.add(
            RawRecord(source_record_id=source.id, content_hash=fingerprint, payload=payload)
        )
    session.add(
        AuditLog(
            actor="admin",
            action="event.edited",
            event_id=event.id,
            changes={"fields": json.loads(json.dumps(changes, default=str)), "reason": edit.reason},
        )
    )
    await session.flush()
    return await event_card(session, event)


async def accept_candidate(
    session: AsyncSession,
    item_id: UUID,
    version: int,
    reason: str,
) -> dict[str, Any]:
    await lock_catalog(session)
    item = await session.get(ModerationItem, item_id)
    if item is None:
        raise AppError("moderation_not_found", "Запись не найдена", 404)
    if item.status != "pending":
        raise AppError("already_reviewed", "Решение уже принято", 409)
    raw = await session.get(RawRecord, item.raw_record_id)
    assert raw is not None
    data = EventInput.model_validate(normalize_row(raw.payload))
    result = await edit_event(
        session, item.event_id, EventEdit(version=version, event=data, reason=reason)
    )
    item.status = "accepted"
    session.add(
        AuditLog(
            actor="admin",
            action="moderation.accepted",
            event_id=item.event_id,
            changes={"item_id": str(item.id), "reason": reason},
        )
    )
    return result


async def merge_events(
    session: AsyncSession,
    canonical_id: UUID,
    duplicate_id: UUID,
    canonical_version: int,
    duplicate_version: int,
    reason: str,
) -> dict[str, Any]:
    await lock_catalog(session)
    canonical = await get_event(session, canonical_id, admin=True)
    duplicate = await get_event(session, duplicate_id, admin=True)
    if canonical.id == duplicate.id or canonical.merged_into_id or duplicate.merged_into_id:
        raise AppError("invalid_merge", "Выберите две разные исходные карточки", 422)
    if canonical.version != canonical_version or duplicate.version != duplicate_version:
        raise AppError("version_conflict", "Обновите обе карточки перед объединением", 409)
    sources = (
        await session.scalars(
            select(SourceRecord).where(
                SourceRecord.event_id == duplicate.id,
            )
        )
    ).all()
    for source in sources:
        source.event_id = canonical.id
        source.verification_status = "conflict"  # Choosing canonical does not validate other facts.
    duplicate.merged_into_id, duplicate.status = canonical.id, "draft"
    duplicate.version += 1
    canonical.version += 1
    canonical.updated_at = duplicate.updated_at = utcnow()
    for event in (canonical, duplicate):
        session.add(
            AuditLog(
                actor="admin",
                action="event.merged",
                event_id=event.id,
                changes={
                    "canonical_id": str(canonical.id),
                    "duplicate_id": str(duplicate.id),
                    "reason": reason,
                },
            )
        )
    await session.flush()
    return await event_card(session, canonical)
