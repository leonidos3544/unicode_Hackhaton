from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.service import get_event
from app.common.errors import AppError
from app.common.quotas import enforce_rate
from app.storage.postgres.runtime_tables import Feedback


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    event_id: UUID | None = None
    kind: Literal["correction", "organizer", "question"] = "question"
    text: str = Field(min_length=5, max_length=2000)
    website: str = Field(default="", max_length=200)  # Honeypot, hidden from residents.


async def submit_feedback(
    session: AsyncSession,
    owner: str,
    payload: FeedbackInput,
) -> dict[str, Any]:
    if payload.website:
        raise AppError("spam_detected", "Не удалось отправить форму", 422)
    await enforce_rate(session, f"feedback:{owner}", 5, 3600)
    if payload.event_id:
        await get_event(session, payload.event_id)
    item = Feedback(
        owner_id=owner,
        event_id=payload.event_id,
        kind=payload.kind,
        text=payload.text,
        status="new",
    )
    session.add(item)
    await session.flush()
    return {
        "ticket_id": str(item.id),
        "status": "new",
        "message": "Обращение сохранено и доступно модератору",
    }
