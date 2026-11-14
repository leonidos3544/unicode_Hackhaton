import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import Request, Response
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.clock import utcnow
from app.common.errors import AppError
from app.storage.postgres.runtime_tables import LinkToken, TelegramIdentity, WebSession

COOKIE = "bc_session"


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    expected = request.app.state.settings.public_base_url.rstrip("/")
    if origin and origin.rstrip("/") != expected:
        raise AppError("origin_failed", "Запрос с другого сайта запрещён", 403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise AppError("origin_failed", "Запрос с другого сайта запрещён", 403)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_for(token: str) -> str:
    return hmac.new(token.encode(), b"bashculture-csrf-v1", hashlib.sha256).hexdigest()


async def resolve_web_session(request: Request, session: AsyncSession) -> WebSession:
    token = request.cookies.get(COOKIE, "")
    record: WebSession | None = (
        await session.scalar(
            select(WebSession).where(
                WebSession.token_hash == token_hash(token),
                WebSession.expires_at > utcnow(),
            )
        )
        if token
        else None
    )
    if record is None:
        raise AppError("session_required", "Обновите страницу и создайте сессию", 401)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        check_origin(request)
        supplied = request.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(supplied, csrf_for(token)):
            raise AppError("csrf_failed", "Обновите страницу перед отправкой формы", 403)
    return record


async def create_web_session(
    request: Request,
    response: Response,
    session: AsyncSession,
    *,
    role: str = "resident",
) -> dict[str, Any]:
    check_origin(request)
    previous = request.cookies.get(COOKIE)
    current = (
        await session.scalar(
            select(WebSession).where(
                WebSession.token_hash == token_hash(previous or ""),
                WebSession.expires_at > utcnow(),
            )
        )
        if previous
        else None
    )
    if current and role == "resident":
        return {"csrf_token": csrf_for(previous or ""), "role": current.role}
    token = secrets.token_urlsafe(32)
    record = WebSession(
        owner_id=current.owner_id if current else str(uuid4()),
        token_hash=token_hash(token),
        role=role,
        expires_at=utcnow() + timedelta(days=30),
    )
    if current:
        await session.delete(current)
    session.add(record)
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.session_secure_cookie,
        max_age=30 * 86400,
        path="/",
    )
    return {"csrf_token": csrf_for(token), "role": role}


async def telegram_owner(session: AsyncSession, user_id: int, chat_id: int) -> str:
    await session.execute(
        insert(TelegramIdentity)
        .values(
            telegram_user_id=str(user_id),
            chat_id=str(chat_id),
            owner_id=str(uuid4()),
            enabled=True,
        )
        .on_conflict_do_nothing(index_elements=[TelegramIdentity.telegram_user_id])
    )
    record = await session.get(TelegramIdentity, str(user_id))
    assert record is not None
    record.chat_id, record.enabled = str(chat_id), True
    return record.owner_id


async def make_link_token(session: AsyncSession, owner_id: str) -> str:
    token = secrets.token_urlsafe(24)
    session.add(
        LinkToken(
            token_hash=token_hash(token),
            owner_id=owner_id,
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
    return token


async def consume_link_token(
    session: AsyncSession,
    token: str,
    user_id: int,
    chat_id: int,
) -> str:
    link = await session.scalar(
        select(LinkToken)
        .where(
            LinkToken.token_hash == token_hash(token),
            LinkToken.expires_at > utcnow(),
        )
        .with_for_update()
    )
    if link is None:
        raise AppError("link_expired", "Ссылка привязки устарела. Создайте новую на сайте", 409)
    await telegram_owner(session, user_id, chat_id)
    identity = await session.get(TelegramIdentity, str(user_id))
    assert identity is not None
    # Preserve the existing Telegram account until a supported data transfer is available.
    from app.storage.postgres.runtime_tables import Conversation, Feedback, Subscription

    if identity.owner_id != link.owner_id:
        for model in (Conversation, Feedback, Subscription, WebSession):
            existing = await session.scalar(
                select(model.owner_id).where(model.owner_id == identity.owner_id).limit(1)
            )
            if existing:
                raise AppError(
                    "link_conflict",
                    "У Telegram уже есть личные данные. Для привязки требуется их перенос",
                    409,
                )
    identity.owner_id = link.owner_id
    await session.execute(delete(LinkToken).where(LinkToken.token_hash == link.token_hash))
    return identity.owner_id
