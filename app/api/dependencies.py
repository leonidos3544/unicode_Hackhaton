import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import resolve_web_session
from app.common.errors import AppError
from app.storage.postgres.runtime_tables import WebSession

bearer = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessions() as session, session.begin():
        yield session


async def require_admin(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> None:
    expected = request.app.state.settings.admin_api_token.get_secret_value()
    if not expected:
        raise AppError("admin_disabled", "Административный доступ не настроен", 503)
    supplied = credentials.credentials if credentials else ""
    if secrets.compare_digest(supplied.encode(), expected.encode()):
        return
    if request.cookies.get("bc_session"):
        principal = await resolve_web_session(request, session)
        if principal.role == "admin":
            return
    raise AppError("unauthorized", "Нужен ключ административного доступа", 401)


Session = Annotated[AsyncSession, Depends(get_session, scope="function")]


async def get_principal(request: Request, session: Session) -> WebSession:
    return await resolve_web_session(request, session)


Principal = Annotated[WebSession, Depends(get_principal)]
