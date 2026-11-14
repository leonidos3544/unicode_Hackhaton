import hashlib
from datetime import datetime, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.clock import utcnow
from app.common.errors import AppError
from app.storage.postgres.runtime_tables import RateBucket


async def enforce_rate(
    session: AsyncSession,
    scope: str,
    limit: int,
    seconds: int = 60,
    *,
    now: datetime | None = None,
) -> None:
    moment = now or utcnow()
    window = int(moment.timestamp()) // seconds
    key = hashlib.sha256(f"{scope}|{window}|{seconds}".encode()).hexdigest()
    statement = insert(RateBucket).values(
        key=key, count=1, expires_at=moment + timedelta(seconds=seconds * 2)
    )
    count = await session.scalar(
        statement.on_conflict_do_update(
            index_elements=[RateBucket.key],
            set_={"count": RateBucket.count + 1},
        ).returning(RateBucket.count)
    )
    if (count or 0) > limit:
        raise AppError("rate_limit", "Слишком много запросов. Попробуйте немного позже", 429)
