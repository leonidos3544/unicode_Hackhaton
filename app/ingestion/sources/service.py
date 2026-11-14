from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.repository import get_city, get_venue, lock_catalog
from app.catalog.schemas import StatusChange
from app.catalog.service import change_status
from app.common.clock import utcnow
from app.ingestion.normalize import content_hash, normalize_text
from app.ingestion.service import import_records
from app.ingestion.sources.collector import Collection, collection_summary
from app.storage.postgres.tables import (
    AuditLog,
    Event,
    ModerationItem,
    SourceRecord,
    SourceRun,
    SourceSnapshot,
)


async def apply_collection(
    session: AsyncSession,
    collection: Collection,
    *,
    publish_verified: bool = False,
    reference: datetime | None = None,
) -> dict[str, Any]:
    """Persist evidence and use the shared import pipeline, without network under DB locks."""
    await lock_catalog(session)
    config = collection.config
    now = reference or utcnow()
    snapshot_ids: dict[str, UUID] = {}
    for url, page in collection.snapshots.items():
        snapshot = SourceSnapshot(
            source_name=config.id,
            url=page.url,
            content_hash=page.content_hash,
            body=page.body,
            status_code=page.status_code,
            fetched_at=page.fetched_at,
        )
        session.add(snapshot)
        await session.flush()
        snapshot_ids[url] = snapshot.id
        snapshot_ids[page.url] = snapshot.id
    summary = collection_summary(collection)
    summary.update(published=0, cancelled=0, no_longer_listed=0, import_report=None)
    if collection.venue_verified:
        city = await get_city(session, config.city, normalize_text(config.city))
        venue = await get_venue(
            session, city.id, config.venue, normalize_text(config.venue), config.address
        )
        venue.source_url = config.venue_source_url
        venue.verified_at = collection.snapshots[config.venue_source_url].fetched_at
    if collection.candidates:
        rows = [candidate.payload for candidate in collection.candidates]
        report = await import_records(session, config.id, rows, content_hash({"rows": rows}))
        summary["import_report"] = {"id": str(report["id"]), "counts": report["counts"]}
        for candidate, row in zip(collection.candidates, report["rows"], strict=True):
            if row["event_id"] is None:
                continue
            record = await session.scalar(
                select(SourceRecord).where(
                    SourceRecord.source_name == config.id,
                    SourceRecord.external_id == candidate.payload["external_id"],
                )
            )
            assert record is not None
            record.last_checked_at = now
            if row["result"] in ("moderation", "rejected"):
                record.verification_status = "conflict"
                continue
            if record.applied_hash != content_hash(candidate.payload):
                continue
            record.source_type = "page"
            record.verification_status = "verified"
            record.verified_at = collection.snapshots[candidate.detail_url].fetched_at
            record.listing_url = candidate.listing_url
            record.listing_snapshot_id = snapshot_ids[candidate.listing_url]
            record.detail_snapshot_id = snapshot_ids[candidate.detail_url]
            event = await session.get(Event, record.event_id)
            assert event is not None
            pending = await session.scalar(
                select(ModerationItem.id)
                .where(ModerationItem.event_id == event.id, ModerationItem.status == "pending")
                .limit(1)
            )
            if candidate.cancelled and event.status != "cancelled":
                await change_status(
                    session,
                    event.id,
                    "cancelled",
                    StatusChange(
                        version=event.version, reason="Источник явно пометил сеанс отменённым"
                    ),
                    actor=f"source:{config.id}",
                )
                summary["cancelled"] += 1
            elif (
                publish_verified
                and event.status == "draft"
                and not pending
                and event.date_start > now
            ):
                await change_status(
                    session,
                    event.id,
                    "published",
                    StatusChange(
                        version=event.version,
                        reason="Проверены официальная афиша, карточка события и адрес площадки",
                    ),
                    actor=f"source:{config.id}",
                )
                summary["published"] += 1

    # Absence is not cancellation. Hide unconfirmed source evidence from search only after a
    # complete successful scan of the SAME listing scope; a failed scan keeps cached evidence.
    if collection.candidates and not collection.errors and not collection.warnings:
        seen = {candidate.payload["external_id"] for candidate in collection.candidates}
        listing_urls = {candidate.listing_url for candidate in collection.candidates}
        records = (
            await session.scalars(
                select(SourceRecord)
                .join(Event, Event.id == SourceRecord.event_id)
                .where(
                    SourceRecord.source_name == config.id,
                    SourceRecord.source_type == "page",
                    SourceRecord.listing_url.in_(listing_urls),
                    Event.date_start > now,
                )
            )
        ).all()
        for record in records:
            if record.external_id in seen or record.verification_status == "not_seen":
                continue
            record.verification_status = "not_seen"
            record.last_checked_at = now
            summary["no_longer_listed"] += 1
            if record.event_id:
                event = await session.get(Event, record.event_id)
                if event and any(
                    term.casefold() in event.title.casefold()
                    for term in config.excluded_title_terms
                ):
                    previous_status = event.status
                    event.status = "draft"
                    event.version += 1
                    event.updated_at = now
                    session.add(
                        AuditLog(
                            actor=f"source:{config.id}",
                            action="source.venue_mismatch",
                            event_id=event.id,
                            changes={
                                "status": {"from": previous_status, "to": "draft"},
                                "reason": "Выездной сеанс: домашний адрес площадки неприменим",
                            },
                        )
                    )
                session.add(
                    AuditLog(
                        actor=f"source:{config.id}",
                        action="source.not_seen",
                        event_id=record.event_id,
                        changes={
                            "reason": "Сеанс не подтверждён полной повторной проверкой афиши",
                            "source_record_id": str(record.id),
                        },
                    )
                )
    session.add(
        SourceRun(
            source_name=config.id,
            status="partial" if collection.errors else "ok",
            report=summary,
        )
    )
    await session.flush()
    return summary
