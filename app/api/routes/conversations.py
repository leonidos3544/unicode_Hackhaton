from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agent.confirmations import resolve_action
from app.agent.history import owned, recent
from app.agent.service import respond
from app.api.dependencies import Principal, Session
from app.common.quotas import enforce_rate
from app.storage.postgres.runtime_tables import Conversation

router = APIRouter(prefix="/api/v1", tags=["Диалог"])


@router.get("/conversations")
async def list_conversations(session: Session, principal: Principal) -> list[dict[str, str]]:
    items = (
        await session.scalars(
            select(Conversation)
            .where(Conversation.owner_id == principal.owner_id)
            .order_by(Conversation.updated_at.desc())
            .limit(50)
        )
    ).all()
    return [{"id": str(item.id), "created_at": item.created_at.isoformat()} for item in items]


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=2000)
    client_request_id: str = Field(min_length=1, max_length=100)


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool


@router.post("/conversations", status_code=201)
async def create_conversation(session: Session, principal: Principal) -> dict[str, str]:
    await enforce_rate(session, f"conversation:{principal.owner_id}", 20)
    item = Conversation(owner_id=principal.owner_id)
    session.add(item)
    await session.flush()
    return {"id": str(item.id)}


@router.get("/conversations/{conversation_id}")
async def read_conversation(
    conversation_id: UUID, session: Session, principal: Principal
) -> dict[str, Any]:
    item = await owned(session, conversation_id, principal.owner_id)
    return {"id": str(item.id), "state": item.state, "turns": await recent(session, item.id)}


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: UUID,
    payload: MessageInput,
    request: Request,
    session: Session,
    principal: Principal,
) -> dict[str, Any]:
    owner = principal.owner_id
    await owned(session, conversation_id, owner)
    await enforce_rate(session, f"message:{owner}", 30)
    # Release authentication's read transaction before any network/model work.
    await session.commit()
    return await respond(
        request.app.state.sessions,
        request.app.state.settings,
        owner,
        conversation_id,
        payload.client_request_id,
        payload.message,
        getattr(request.app.state, "agent_provider", None),
    )


@router.post("/actions/{action_id}/decision")
async def decide(
    action_id: UUID, payload: DecisionInput, session: Session, principal: Principal
) -> dict[str, Any]:
    return await resolve_action(session, principal.owner_id, action_id, confirm=payload.confirm)
