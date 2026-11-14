from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.clock import reference_now, utcnow
from app.common.errors import AppError
from app.ingestion.normalize import content_hash
from app.storage.postgres.runtime_tables import Conversation, DialogueTurn


async def owned(
    session: AsyncSession, conversation_id: UUID, owner: str, *, lock: bool = False
) -> Conversation:
    query = select(Conversation).where(
        Conversation.id == conversation_id, Conversation.owner_id == owner
    )
    if lock:
        query = query.with_for_update()
    conversation = await session.scalar(query)
    if conversation is None:
        raise AppError("conversation_not_found", "Диалог не найден", 404)
    return conversation


async def claim(
    session: AsyncSession, conversation_id: UUID, owner: str, client_key: str, message: str
) -> tuple[Conversation, DialogueTurn, str | None]:
    conversation = await owned(session, conversation_id, owner, lock=True)
    turn = await session.scalar(
        select(DialogueTurn).where(
            DialogueTurn.conversation_id == conversation_id, DialogueTurn.client_key == client_key
        )
    )
    digest = content_hash({"message": message})
    if turn:
        if turn.input_hash != digest:
            raise AppError("request_conflict", "Этот ID уже использован для другого сообщения", 409)
        if turn.answer is not None:
            return conversation, turn, None
        # A crashed paid request must never be silently retried with the same request ID.
        raise AppError("turn_pending", "Ход ещё выполняется или был прерван. Обновите историю", 409)
    if conversation.lease_until and conversation.lease_until > utcnow():
        raise AppError("conversation_busy", "Дождитесь ответа на предыдущее сообщение", 409)
    lease = str(uuid4())
    conversation.lease_id = lease
    conversation.lease_until = utcnow() + timedelta(minutes=5)
    turn = DialogueTurn(
        conversation_id=conversation_id,
        client_key=client_key,
        input_hash=digest,
        user_text=message,
        reference_time=reference_now(),
    )
    session.add(turn)
    await session.flush()
    return conversation, turn, lease


async def recent(session: AsyncSession, conversation_id: UUID) -> list[dict[str, Any]]:
    turns = (
        await session.scalars(
            select(DialogueTurn)
            .where(DialogueTurn.conversation_id == conversation_id)
            .order_by(DialogueTurn.created_at.desc(), DialogueTurn.id)
            .limit(10)
        )
    ).all()
    return [
        {
            "id": str(turn.id),
            "message": turn.user_text,
            "answer": turn.answer,
            "reference_time": turn.reference_time.isoformat(),
        }
        for turn in reversed(turns)
    ]
