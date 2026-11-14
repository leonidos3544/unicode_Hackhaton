from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.schemas import Category
from app.common.errors import AppError
from app.ingestion.normalize import content_hash
from app.search.models import SearchFilters
from app.storage.postgres.runtime_tables import Notification, Subscription, TelegramIdentity
from app.storage.postgres.tables import City


class SubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city_id: UUID
    category: Category | None = None
    channel: Literal["inbox", "telegram"] = "inbox"
    period_filter: dict[str, str] = Field(default_factory=dict, max_length=2)

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if self.period_filter.keys() - {"date_from", "date_to"}:
            raise ValueError("Допустимы только date_from и date_to")
        filters = SearchFilters.model_validate(self.period_filter)
        self.period_filter = {
            key: value
            for key, value in filters.model_dump(
                mode="json", include={"date_from", "date_to"}, exclude_none=True
            ).items()
        }
        return self


async def create_subscription(
    session: AsyncSession,
    owner: str,
    payload: SubscriptionInput,
) -> dict[str, Any]:
    if await session.get(City, payload.city_id) is None:
        raise AppError("city_not_found", "Выберите город из справочника", 422)
    fingerprint = content_hash(payload.model_dump(mode="json", exclude={"channel"}))
    await session.execute(
        insert(Subscription)
        .values(
            owner_id=owner,
            city_id=payload.city_id,
            category=payload.category,
            period_filter=payload.period_filter,
            filter_hash=fingerprint,
            channel=payload.channel,
        )
        .on_conflict_do_nothing(constraint="uq_subscriptions_owner_id")
    )
    item = await session.scalar(
        select(Subscription).where(
            Subscription.owner_id == owner,
            Subscription.filter_hash == fingerprint,
            Subscription.channel == payload.channel,
        )
    )
    assert item is not None
    return await subscription_dto(session, item)


async def subscription_dto(session: AsyncSession, item: Subscription) -> dict[str, Any]:
    city = await session.get(City, item.city_id)
    linked = await session.scalar(
        select(TelegramIdentity.telegram_user_id)
        .where(
            TelegramIdentity.owner_id == item.owner_id,
            TelegramIdentity.enabled.is_(True),
        )
        .limit(1)
    )
    return {
        "id": str(item.id),
        "city_id": str(item.city_id),
        "city": city.name if city else "",
        "category": item.category,
        "channel": item.channel,
        "period_filter": item.period_filter,
        "delivery_ready": item.channel == "inbox" or linked is not None,
        "created_at": item.created_at.isoformat(),
    }


async def update_owned_subscription(
    session: AsyncSession, owner: str, item_id: UUID, payload: SubscriptionInput
) -> dict[str, Any]:
    item = await session.scalar(
        select(Subscription)
        .where(Subscription.id == item_id, Subscription.owner_id == owner)
        .with_for_update()
    )
    if item is None:
        raise AppError("subscription_not_found", "Подписка не найдена", 404)
    if await session.get(City, payload.city_id) is None:
        raise AppError("city_not_found", "Выберите город из справочника", 422)
    fingerprint = content_hash(payload.model_dump(mode="json", exclude={"channel"}))
    duplicate = await session.scalar(
        select(Subscription.id).where(
            Subscription.owner_id == owner,
            Subscription.filter_hash == fingerprint,
            Subscription.channel == payload.channel,
            Subscription.id != item_id,
        )
    )
    if duplicate:
        raise AppError("subscription_exists", "Такая подписка уже существует", 409)
    if item.filter_hash != fingerprint or item.channel != payload.channel:
        await session.execute(delete(Notification).where(Notification.subscription_id == item_id))
    item.city_id, item.category = payload.city_id, payload.category
    item.channel, item.period_filter, item.filter_hash = (
        payload.channel,
        payload.period_filter,
        fingerprint,
    )
    await session.flush()
    return await subscription_dto(session, item)


async def list_subscriptions(session: AsyncSession, owner: str) -> list[dict[str, Any]]:
    items = (
        await session.scalars(
            select(Subscription)
            .where(
                Subscription.owner_id == owner,
            )
            .order_by(Subscription.created_at)
        )
    ).all()
    return [await subscription_dto(session, item) for item in items]


async def unsubscribe(session: AsyncSession, owner: str, subscription_id: UUID | None) -> None:
    statement = delete(Subscription).where(Subscription.owner_id == owner)
    if subscription_id:
        statement = statement.where(Subscription.id == subscription_id)
    await session.execute(statement)  # Pending deliveries are removed by ON DELETE CASCADE.


async def list_inbox(session: AsyncSession, owner: str) -> list[dict[str, Any]]:
    from app.catalog.repository import event_card
    from app.search.service import filter_conditions
    from app.storage.postgres.tables import Event

    records = (
        await session.scalars(
            select(Notification)
            .join(Subscription)
            .where(
                Subscription.owner_id == owner,
            )
            .order_by(Notification.created_at.desc())
            .limit(50)
        )
    ).all()
    result = []
    for record in records:
        event = await session.get(Event, record.event_id)
        valid = await session.scalar(
            select(Event.id).where(Event.id == record.event_id, *filter_conditions(SearchFilters()))
        )
        if event and valid:
            result.append({"id": str(record.id), "event": await event_card(session, event)})
    return result
