import secrets
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, SecretStr

from app.api.dependencies import Principal, Session
from app.auth.service import COOKIE, check_origin, create_web_session, make_link_token
from app.common.errors import AppError
from app.common.quotas import enforce_rate
from app.feedback.service import FeedbackInput, submit_feedback
from app.subscriptions.service import (
    SubscriptionInput,
    create_subscription,
    list_inbox,
    list_subscriptions,
    unsubscribe,
    update_owned_subscription,
)

router = APIRouter(prefix="/api/v1", tags=["Личный раздел"])


@router.post("/sessions")
async def new_session(request: Request, response: Response, session: Session) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    await enforce_rate(session, f"session:{ip}", 30)
    return await create_web_session(request, response, session)


@router.delete("/sessions/current", status_code=204)
async def logout(response: Response, session: Session, principal: Principal) -> None:
    await session.delete(principal)
    response.delete_cookie(COOKIE, path="/")


class AdminLogin(BaseModel):
    api_key: SecretStr = Field(max_length=500)


@router.post("/admin/auth/login")
async def login_admin(
    payload: AdminLogin,
    request: Request,
    response: Response,
    session: Session,
) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    check_origin(request)
    async with request.app.state.sessions() as quota_session, quota_session.begin():
        await enforce_rate(quota_session, f"login:{ip}", 10, 300)
    expected = request.app.state.settings.admin_api_token.get_secret_value()
    if not expected or not secrets.compare_digest(
        payload.api_key.get_secret_value().encode(),
        expected.encode(),
    ):
        raise AppError("unauthorized", "Неверный ключ административного доступа", 401)
    return await create_web_session(request, response, session, role="admin")


@router.post("/sessions/telegram-link")
async def link_telegram(request: Request, session: Session, principal: Principal) -> dict[str, Any]:
    token = await make_link_token(session, principal.owner_id)
    username = request.app.state.settings.telegram_bot_username
    return {
        "token": token,
        "url": f"https://t.me/{username}?start=link_{token}" if username else None,
        "expires_in_seconds": 600,
    }


@router.get("/subscriptions")
async def my_subscriptions(session: Session, principal: Principal) -> list[dict[str, Any]]:
    return await list_subscriptions(session, principal.owner_id)


@router.post("/subscriptions", status_code=201)
async def subscribe(
    payload: SubscriptionInput, session: Session, principal: Principal
) -> dict[str, Any]:
    await enforce_rate(session, f"subscriptions:{principal.owner_id}", 20)
    return await create_subscription(session, principal.owner_id, payload)


@router.patch("/subscriptions/{item_id}")
async def update_subscription(
    item_id: UUID,
    payload: SubscriptionInput,
    session: Session,
    principal: Principal,
) -> dict[str, Any]:
    return await update_owned_subscription(session, principal.owner_id, item_id, payload)


@router.delete("/subscriptions/{item_id}", status_code=204)
async def remove_subscription(item_id: UUID, session: Session, principal: Principal) -> None:
    await unsubscribe(session, principal.owner_id, item_id)


@router.get("/notifications")
async def inbox(session: Session, principal: Principal) -> list[dict[str, Any]]:
    return await list_inbox(session, principal.owner_id)


@router.post("/feedback", status_code=201)
async def send_feedback(
    payload: FeedbackInput, session: Session, principal: Principal
) -> dict[str, Any]:
    return await submit_feedback(session, principal.owner_id, payload)
