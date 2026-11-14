import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.repository import get_city, get_venue, lock_catalog
from app.catalog.schemas import EventInput
from app.common.clock import utcnow
from app.common.errors import AppError
from app.ingestion.normalize import content_hash, normalize_row, normalize_text
from app.storage.postgres.tables import (
    AuditLog,
    Event,
    ImportRow,
    ImportRun,
    ModerationItem,
    RawRecord,
    SourceRecord,
)


def event_values(data: EventInput, city_id: UUID, venue_id: UUID) -> dict[str, Any]:
    key = f"{normalize_text(data.title)}|{venue_id}|{data.date_start.isoformat()}"
    return {
        "title": data.title,
        "normalized_title": normalize_text(data.title),
        "category": data.category,
        "tags": data.tags,
        "date_start": data.date_start,
        "date_end": data.date_end,
        "city_id": city_id,
        "venue_id": venue_id,
        "address": data.address,
        "description": data.description,
        "price_min_minor": int(data.price_min * 100) if data.price_min is not None else None,
        "price_max_minor": int(data.price_max * 100) if data.price_max is not None else None,
        "is_free": data.is_free,
        "age_limit": data.age_limit,
        "language": data.language,
        "org_link": str(data.org_link) if data.org_link else None,
        "dedup_key": hashlib.sha256(key.encode()).hexdigest(),
    }


async def process_record(
    session: AsyncSession, source_name: str, payload: dict[str, Any]
) -> tuple[str, RawRecord, UUID | None, list[dict[str, Any]]]:
    fingerprint = content_hash(payload)
    try:
        data = EventInput.model_validate(normalize_row(payload))
    except ValidationError as exc:
        invalid_raw = RawRecord(payload=payload, content_hash=fingerprint)
        session.add(invalid_raw)
        await session.flush()
        errors = [
            {"field": ".".join(map(str, e["loc"])), "code": e["type"], "message": e["msg"]}
            for e in exc.errors(include_input=False, include_context=False, include_url=False)
        ]
        return "rejected", invalid_raw, None, errors

    source = await session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_name == source_name, SourceRecord.external_id == data.external_id
        )
    )
    if source is None:
        source = SourceRecord(
            source_name=source_name,
            external_id=data.external_id,
            source_url=str(data.source_url),
            source_type="manual" if source_name == "manual" else "import",
        )
        session.add(source)
        await session.flush()
    raw = await session.scalar(
        select(RawRecord).where(
            RawRecord.source_record_id == source.id, RawRecord.content_hash == fingerprint
        )
    )
    if raw is None:
        raw = RawRecord(source_record_id=source.id, payload=payload, content_hash=fingerprint)
        session.add(raw)
        await session.flush()
    if source.latest_hash == fingerprint:
        return "unchanged", raw, source.event_id, []

    city = await get_city(session, data.city, normalize_text(data.city))
    venue = await get_venue(session, city.id, data.venue, normalize_text(data.venue), data.address)
    if data.lat is not None and data.lon is not None:
        venue.lat, venue.lon = data.lat, data.lon
    values = event_values(data, city.id, venue.id)
    event = await session.get(Event, source.event_id) if source.event_id else None
    linked_before = event is not None
    if event is None:
        event = await session.scalar(
            select(Event)
            .where(Event.dedup_key == values["dedup_key"])
            .order_by(Event.created_at)
            .limit(1)
        )
    result = "unchanged"
    field_changes: dict[str, Any] = {}
    if event is None:
        event = Event(**values, status="draft")
        session.add(event)
        await session.flush()
        result = "added"
        field_changes = {key: {"from": None, "to": value} for key, value in values.items()}
    else:
        changed = {key: value for key, value in values.items() if getattr(event, key) != value}
        source_count = await session.scalar(
            select(func.count())
            .select_from(SourceRecord)
            .where(SourceRecord.event_id == event.id, SourceRecord.applied_hash.is_not(None))
        )
        if changed and (not linked_before or (source_count or 0) > 1):
            existing = await session.scalar(
                select(ModerationItem).where(ModerationItem.raw_record_id == raw.id)
            )
            if existing is None:
                session.add(
                    ModerationItem(
                        raw_record_id=raw.id,
                        event_id=event.id,
                        reason="Источники расходятся: " + ", ".join(sorted(changed)),
                    )
                )
            # Keep the canonical facts and the applied source URL/hash unchanged.
            source.latest_hash = fingerprint
            source.event_id = event.id
            await session.flush()
            return "moderation", raw, event.id, []
        if changed:
            field_changes = {
                key: {"from": getattr(event, key), "to": value} for key, value in changed.items()
            }
            field_changes["status"] = {
                "from": event.status,
                "to": "cancelled" if event.status == "cancelled" else "draft",
            }
            for key, value in changed.items():
                setattr(event, key, value)
            # Changes from import require a fresh moderator decision. Never revive cancellation.
            if event.status != "cancelled":
                event.status = "draft"
            event.version += 1
            event.updated_at = utcnow()
            result = "updated"

    provenance_changed = source.applied_hash != fingerprint
    source.event_id = event.id
    source.latest_hash = source.applied_hash = fingerprint
    source.source_url = str(data.source_url)
    source.verified_at = None  # Importing a URL is not a network verification.
    if result == "unchanged" and provenance_changed:
        event.version += 1
        event.updated_at = utcnow()
        result = "updated"
    if result != "unchanged":
        session.add(
            AuditLog(
                actor="admin",
                action=f"import.{result}",
                event_id=event.id,
                changes={
                    "source_name": source_name,
                    "raw_record_id": str(raw.id),
                    "event_version": event.version,
                    "fields": json.loads(json.dumps(field_changes, default=str)),
                },
            )
        )
    await session.flush()
    return result, raw, event.id, []


async def import_records(
    session: AsyncSession, source_name: str, rows: list[dict[str, Any]], file_hash: str
) -> dict[str, Any]:
    await lock_catalog(session)
    run = ImportRun(source_name=source_name, file_hash=file_hash, actor="admin")
    session.add(run)
    await session.flush()
    counts = dict.fromkeys(("added", "updated", "unchanged", "rejected", "moderation"), 0)
    for number, payload in enumerate(rows, start=1):
        result, raw, event_id, errors = await process_record(session, source_name, payload)
        counts[result] += 1
        session.add(
            ImportRow(
                import_run_id=run.id,
                row_number=number,
                raw_record_id=raw.id,
                event_id=event_id,
                result=result,
                errors=errors,
            )
        )
    run.counts = counts
    await session.flush()
    return await get_import_report(session, run.id)


async def get_import_report(session: AsyncSession, run_id: UUID) -> dict[str, Any]:
    run = await session.get(ImportRun, run_id)
    if run is None:
        raise AppError("import_not_found", "Отчёт импорта не найден", 404)
    rows = (
        await session.scalars(
            select(ImportRow)
            .where(ImportRow.import_run_id == run.id)
            .order_by(ImportRow.row_number)
        )
    ).all()
    return {
        "id": run.id,
        "source_name": run.source_name,
        "counts": run.counts,
        "created_at": run.created_at,
        "rows": [
            {
                "row_number": row.row_number,
                "result": row.result,
                "event_id": row.event_id,
                "errors": row.errors,
            }
            for row in rows
        ],
    }
