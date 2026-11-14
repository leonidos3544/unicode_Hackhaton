from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.clock import utcnow
from app.common.errors import AppError
from app.feedback.service import FeedbackInput, submit_feedback
from app.ingestion.normalize import content_hash
from app.storage.postgres.runtime_tables import PendingAction
from app.subscriptions.service import SubscriptionInput, create_subscription


async def prepare_action(
    session: AsyncSession,
    owner: str,
    action_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if action_type not in ("subscribe", "save_feedback"):
        raise AppError("unknown_action", "Действие не поддерживается", 422)
    item = PendingAction(
        owner_id=owner,
        action_type=action_type,
        payload=payload,
        payload_hash=content_hash(payload),
        expires_at=utcnow() + timedelta(minutes=10),
    )
    session.add(item)
    await session.flush()
    return {
        "id": str(item.id),
        "action_type": action_type,
        "payload": payload,
        "expires_at": item.expires_at.isoformat(),
        "requires_confirmation": True,
    }


async def resolve_action(
    session: AsyncSession,
    owner: str,
    action_id: UUID,
    *,
    confirm: bool,
) -> dict[str, Any]:
    item = await session.scalar(
        select(PendingAction)
        .where(
            PendingAction.id == action_id,
            PendingAction.owner_id == owner,
        )
        .with_for_update()
    )
    if item is None:
        raise AppError("action_not_found", "Предложение не найдено", 404)
    if item.status == "done":
        return item.result or {}
    if item.status == "rejected" and not confirm:
        return {"status": "rejected"}
    if item.status != "pending" or item.expires_at <= utcnow():
        raise AppError("action_expired", "Предложение устарело. Повторите запрос", 409)
    if item.payload_hash != content_hash(item.payload):
        raise AppError("action_conflict", "Содержание действия изменилось", 409)
    if not confirm:
        item.status = "rejected"
        return {"status": "rejected"}
    if item.action_type not in {"subscribe", "save_feedback"}:
        raise AppError("unknown_action", "Действие не поддерживается", 422)
    if item.action_type == "subscribe":
        result = await create_subscription(
            session, owner, SubscriptionInput.model_validate(item.payload)
        )
    else:
        result = await submit_feedback(session, owner, FeedbackInput.model_validate(item.payload))
    item.status, item.result = "done", result
    return result
