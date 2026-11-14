import hashlib
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.dependencies import Session, require_admin
from app.api.schemas import EventCard
from app.catalog.moderation import accept_candidate, edit_event, merge_events
from app.catalog.repository import event_card, lock_catalog
from app.catalog.schemas import EventEdit, EventInput, StatusChange
from app.catalog.service import change_status, get_event
from app.common.errors import AppError
from app.ingestion.files import parse_file
from app.ingestion.service import get_import_report, import_records
from app.storage.postgres.runtime_tables import AgentTrace, Feedback
from app.storage.postgres.tables import (
    AuditLog,
    Event,
    ImportRun,
    ModerationItem,
    SourceRecord,
    SourceRun,
    SourceSnapshot,
)

router = APIRouter(
    prefix="/api/v1/admin", tags=["Администрирование"], dependencies=[Depends(require_admin)]
)


@router.post("/imports", status_code=201)
async def upload_import(
    request: Request,
    session: Session,
    file: Annotated[UploadFile, File()],
    source_name: Annotated[str, Form(min_length=1, max_length=200)],
) -> dict[str, Any]:
    settings = request.app.state.settings
    data = await file.read(settings.max_import_bytes + 1)
    if len(data) > settings.max_import_bytes:
        raise AppError("file_too_large", "Файл превышает допустимый размер", 413)
    if not source_name.strip():
        raise AppError("invalid_source", "Укажите название источника", 422)
    rows = parse_file(data, file.filename or "", settings.max_import_rows)
    return await import_records(
        session, source_name.strip(), rows, hashlib.sha256(data).hexdigest()
    )


@router.get("/imports")
async def list_imports(session: Session) -> list[dict[str, Any]]:
    runs = (
        await session.scalars(select(ImportRun).order_by(ImportRun.created_at.desc()).limit(50))
    ).all()
    return [
        {
            "id": run.id,
            "source_name": run.source_name,
            "counts": run.counts,
            "created_at": run.created_at,
        }
        for run in runs
    ]


@router.get("/imports/{run_id}")
async def read_import(run_id: UUID, session: Session) -> dict[str, Any]:
    return await get_import_report(session, run_id)


@router.post("/events", response_model=EventCard, status_code=201)
async def create_event(data: EventInput, session: Session) -> dict[str, Any]:
    report = await import_records(session, "manual", [data.model_dump(mode="json")], "manual")
    event = await get_event(session, report["rows"][0]["event_id"], admin=True)
    return await event_card(session, event)


@router.get("/events")
async def admin_events(
    session: Session,
    page: Annotated[int, Query(ge=1)] = 1,
    status: Literal["draft", "published", "cancelled"] | None = None,
    query: Annotated[str | None, Query(max_length=200)] = None,
) -> list[dict[str, Any]]:
    statement = select(Event).order_by(Event.updated_at.desc(), Event.id).limit(50)
    if status:
        statement = statement.where(Event.status == status)
    if query:
        statement = statement.where(Event.title.icontains(query, autoescape=True))
    events = (await session.scalars(statement.offset((page - 1) * 50))).all()
    return [await event_card(session, event) for event in events]


@router.get("/events/{event_id}", response_model=EventCard)
async def admin_event(event_id: UUID, session: Session) -> dict[str, Any]:
    return await event_card(session, await get_event(session, event_id, admin=True))


@router.patch("/events/{event_id}", response_model=EventCard)
async def update_event(event_id: UUID, payload: EventEdit, session: Session) -> dict[str, Any]:
    return await edit_event(session, event_id, payload)


class MergeInput(BaseModel):
    canonical_id: UUID
    duplicate_id: UUID
    canonical_version: int = Field(ge=1)
    duplicate_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=1000)


@router.post("/events/merge", response_model=EventCard)
async def merge(payload: MergeInput, session: Session) -> dict[str, Any]:
    return await merge_events(session, **payload.model_dump())


@router.post("/moderation/{item_id}/accept", response_model=EventCard)
async def approve(item_id: UUID, payload: StatusChange, session: Session) -> dict[str, Any]:
    return await accept_candidate(session, item_id, payload.version, payload.reason)


@router.get("/moderation/{item_id}")
async def moderation_detail(item_id: UUID, session: Session) -> dict[str, Any]:
    from app.storage.postgres.tables import RawRecord

    item = await session.get(ModerationItem, item_id)
    if item is None:
        raise AppError("moderation_not_found", "Запись не найдена", 404)
    raw = await session.get(RawRecord, item.raw_record_id)
    return {
        "id": str(item.id),
        "reason": item.reason,
        "status": item.status,
        "candidate": raw.payload if raw else None,
        "canonical": await event_card(session, await get_event(session, item.event_id, admin=True)),
    }


@router.get("/feedback")
async def admin_feedback(session: Session) -> list[dict[str, Any]]:
    items = (
        await session.scalars(select(Feedback).order_by(Feedback.created_at.desc()).limit(100))
    ).all()
    return [
        {
            "id": str(item.id),
            "event_id": str(item.event_id) if item.event_id else None,
            "kind": item.kind,
            "text": item.text,
            "status": item.status,
            "created_at": item.created_at.isoformat(),
        }
        for item in items
    ]


class FeedbackStatusInput(BaseModel):
    status: Literal["new", "reviewed", "resolved"]


@router.patch("/feedback/{item_id}")
async def handle_feedback(
    item_id: UUID, payload: FeedbackStatusInput, session: Session
) -> dict[str, str]:
    item = await session.get(Feedback, item_id)
    if item is None:
        raise AppError("feedback_not_found", "Обращение не найдено", 404)
    item.status = payload.status
    return {"status": item.status}


@router.get("/traces")
async def admin_traces(session: Session) -> list[dict[str, Any]]:
    items = (
        await session.scalars(select(AgentTrace).order_by(AgentTrace.created_at.desc()).limit(100))
    ).all()
    return [
        {
            "id": str(item.id),
            "query": item.query_redacted,
            "mode": item.mode,
            "latency_ms": item.latency_ms,
            "cost_rub": item.cost_rub,
            "cost_estimated": item.cost_estimated,
            "usage_known": item.usage_known,
            "error_code": item.error_code,
            "created_at": item.created_at.isoformat(),
        }
        for item in items
    ]


@router.get("/traces/{trace_id}")
async def admin_trace(trace_id: UUID, session: Session) -> dict[str, Any]:
    item = await session.get(AgentTrace, trace_id)
    if item is None:
        raise AppError("trace_not_found", "Трасса не найдена", 404)
    return {
        "id": str(item.id),
        "query": item.query_redacted,
        "mode": item.mode,
        "plan": item.plan,
        "calls": item.calls,
        "evidence": item.evidence,
        "model_id": item.model_id,
        "prompt_version": item.prompt_version,
        "input_tokens": item.input_tokens,
        "output_tokens": item.output_tokens,
        "cost_rub": item.cost_rub,
        "cost_estimated": item.cost_estimated,
        "usage_known": item.usage_known,
        "latency_ms": item.latency_ms,
        "error_code": item.error_code,
    }


@router.post("/events/{event_id}/publish", response_model=EventCard)
async def publish_event(event_id: UUID, change: StatusChange, session: Session) -> dict[str, Any]:
    return await change_status(session, event_id, "published", change)


@router.post("/events/{event_id}/cancel", response_model=EventCard)
async def cancel_event(event_id: UUID, change: StatusChange, session: Session) -> dict[str, Any]:
    return await change_status(session, event_id, "cancelled", change)


@router.get("/moderation")
async def list_moderation(session: Session) -> list[dict[str, Any]]:
    items = (
        await session.scalars(
            select(ModerationItem)
            .where(ModerationItem.status == "pending")
            .order_by(ModerationItem.created_at)
            .limit(100)
        )
    ).all()
    return [
        {
            "id": item.id,
            "event_id": item.event_id,
            "raw_record_id": item.raw_record_id,
            "reason": item.reason,
            "status": item.status,
        }
        for item in items
    ]


@router.post("/moderation/{item_id}/reject")
async def reject_candidate(item_id: UUID, change: StatusChange, session: Session) -> dict[str, str]:
    await lock_catalog(session)
    item = await session.get(ModerationItem, item_id)
    if item is None:
        raise AppError("moderation_not_found", "Запись модерации не найдена", 404)
    if item.status == "rejected":
        return {"status": "rejected"}
    event = await get_event(session, item.event_id, admin=True)
    if event.version != change.version:
        raise AppError("version_conflict", "Обновите карточку перед решением", 409)
    item.status = "rejected"
    session.add(
        AuditLog(
            actor="admin",
            action="moderation.rejected",
            event_id=event.id,
            changes={"item_id": str(item.id), "reason": change.reason},
        )
    )
    return {"status": "rejected"}


@router.get("/audit")
async def read_audit(session: Session, event_id: UUID | None = None) -> list[dict[str, Any]]:
    statement = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100)
    if event_id:
        statement = statement.where(AuditLog.event_id == event_id)
    rows = (await session.scalars(statement)).all()
    return [
        {
            "id": row.id,
            "actor": row.actor,
            "action": row.action,
            "event_id": row.event_id,
            "changes": row.changes,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/source-runs")
async def read_source_runs(session: Session) -> list[dict[str, Any]]:
    runs = (
        await session.scalars(select(SourceRun).order_by(SourceRun.created_at.desc()).limit(50))
    ).all()
    return [
        {
            "id": run.id,
            "source_name": run.source_name,
            "status": run.status,
            "created_at": run.created_at,
            "report": run.report,
        }
        for run in runs
    ]


@router.get("/source-snapshots/{snapshot_id}")
async def read_snapshot(snapshot_id: UUID, session: Session) -> dict[str, Any]:
    snapshot = await session.get(SourceSnapshot, snapshot_id)
    if snapshot is None:
        raise AppError("snapshot_not_found", "Снимок источника не найден", 404)
    return {
        "id": snapshot.id,
        "source_name": snapshot.source_name,
        "url": snapshot.url,
        "content_hash": snapshot.content_hash,
        "fetched_at": snapshot.fetched_at,
        "parser_version": snapshot.parser_version,
        "body": snapshot.body,
    }


@router.get("/events/{event_id}/evidence")
async def read_evidence(event_id: UUID, session: Session) -> list[dict[str, Any]]:
    await get_event(session, event_id, admin=True)
    sources = (
        await session.scalars(select(SourceRecord).where(SourceRecord.event_id == event_id))
    ).all()
    return [
        {
            "source_name": source.source_name,
            "source_url": source.source_url,
            "listing_url": source.listing_url,
            "verified_at": source.verified_at,
            "verification_status": source.verification_status,
            "listing_snapshot_id": source.listing_snapshot_id,
            "detail_snapshot_id": source.detail_snapshot_id,
        }
        for source in sources
    ]
