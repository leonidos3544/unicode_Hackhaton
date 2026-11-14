from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.search.models import SearchFilters
from app.search.service import filter_conditions
from app.storage.postgres.runtime_tables import Notification, Subscription
from app.storage.postgres.tables import Event


def subscription_filters(item: Subscription) -> SearchFilters:
    return SearchFilters.model_validate(
        {"city_id": item.city_id, "category": item.category, **item.period_filter}
    )


async def enqueue_matches(session: AsyncSession) -> int:
    """Notify only about events created/changed after subscription. No historical flood."""
    items = (await session.scalars(select(Subscription))).all()
    added = 0
    for item in items:
        filters = subscription_filters(item)
        # INSERT ... SELECT stays bounded in Python even for a large catalogue.
        from sqlalchemy import func, literal
        from sqlalchemy.dialects.postgresql import UUID as PGUUID

        from app.common.clock import utcnow

        values = select(
            func.gen_random_uuid(),
            literal(item.id, type_=PGUUID(as_uuid=True)),
            Event.id,
            literal("pending"),
            literal(0),
            literal(utcnow()),
            literal(utcnow()),
        ).where(*filter_conditions(filters), Event.updated_at >= item.created_at)
        statement: Any = insert(Notification).from_select(
            [
                "id",
                "subscription_id",
                "event_id",
                "status",
                "attempts",
                "available_at",
                "created_at",
            ],
            values,
            include_defaults=False,
        )
        result = await session.execute(
            statement.on_conflict_do_nothing(
                constraint="uq_notifications_subscription_id"
            ).returning(Notification.id)
        )
        added += len(result.all())
    return added
