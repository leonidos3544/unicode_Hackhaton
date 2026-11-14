from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.assumptions import NOTICE
from app.catalog.policy import SOURCE_FRESHNESS
from app.common.clock import reference_now
from app.storage.postgres.runtime_tables import EventAssumption
from app.storage.postgres.tables import City, Event, SourceRecord, Venue


async def lock_catalog(session: AsyncSession) -> None:
    # First release: serialize catalog writes, including imports and moderator actions.
    # PostgreSQL releases this lock on commit/rollback. Readers never wait on it.
    await session.execute(text("SELECT pg_advisory_xact_lock(71013001)"))


async def get_city(session: AsyncSession, name: str, normalized: str) -> City:
    city = await session.scalar(select(City).where(City.normalized_name == normalized))
    if city is None:
        city = City(name=name, normalized_name=normalized)
        session.add(city)
        await session.flush()
    return city


async def get_venue(
    session: AsyncSession, city_id: UUID, name: str, normalized: str, address: str
) -> Venue:
    venue = await session.scalar(
        select(Venue).where(
            Venue.city_id == city_id, Venue.normalized_name == normalized, Venue.address == address
        )
    )
    if venue is None:
        venue = Venue(city_id=city_id, name=name, normalized_name=normalized, address=address)
        session.add(venue)
        await session.flush()
    return venue


async def event_card(session: AsyncSession, event: Event) -> dict[str, Any]:
    city = await session.get(City, event.city_id)
    venue = await session.get(Venue, event.venue_id)
    assert city is not None and venue is not None
    assumption = await session.get(EventAssumption, event.id)
    sources = (
        await session.scalars(
            select(SourceRecord)
            .where(SourceRecord.event_id == event.id, SourceRecord.applied_hash.is_not(None))
            .order_by(SourceRecord.id)
        )
    ).all()
    sources = sorted(
        sources,
        key=lambda source: (
            source.verification_status == "conflict",
            source.source_type != "manual",
            str(source.id),
        ),
    )
    warnings = []
    if event.price_min_minor is None:
        warnings.append("Цена не указана")
    if event.age_limit is None:
        warnings.append("Возрастное ограничение не указано")
    if event.date_end is None:
        warnings.append("Время окончания неизвестно")
    if any(
        source.source_type == "page" and source.verification_status != "verified"
        for source in sources
    ):
        warnings.append("Часть источников больше не подтверждает сеанс; проверьте актуальную афишу")
    if any(
        source.source_type == "page"
        and source.verified_at
        and source.verified_at < reference_now() - SOURCE_FRESHNESS
        for source in sources
    ):
        warnings.append("Проверка источника устарела; требуется обновление афиши")
    return {
        "demo_assumptions": {
            "kind": "synthetic_demo",
            "notice": NOTICE,
            "event_version": assumption.event_version,
            "values": assumption.values,
            "active": assumption.event_version == event.version,
        }
        if assumption
        else None,
        "id": event.id,
        "title": event.title,
        "normalized_title": event.normalized_title,
        "category": event.category,
        "tags": event.tags,
        "date_start": event.date_start,
        "date_end": event.date_end,
        "duration_min": int((event.date_end - event.date_start).total_seconds() // 60)
        if event.date_end
        else None,
        "city_id": city.id,
        "city": city.name,
        "venue_id": venue.id,
        "venue": venue.name,
        "address": event.address,
        "lat": venue.lat,
        "lon": venue.lon,
        "description": event.description,
        "price_min": event.price_min_minor / 100 if event.price_min_minor is not None else None,
        "price_max": event.price_max_minor / 100 if event.price_max_minor is not None else None,
        "is_free": event.is_free,
        "age_limit": event.age_limit,
        "language": event.language,
        "org_link": event.org_link,
        "status": event.status,
        "version": event.version,
        "dedup_key": event.dedup_key,
        "source_url": sources[0].source_url if sources else None,
        "source_type": sources[0].source_type if sources else None,
        "source_hash": sources[0].applied_hash if sources else None,
        "verified_at": sources[0].verified_at if sources else None,
        "confidence": None,
        "sources": [
            {
                "source_url": source.source_url,
                "source_name": source.source_name,
                "source_type": source.source_type,
                "source_hash": source.applied_hash,
                "verified_at": source.verified_at,
                "verification_status": source.verification_status,
                "listing_url": source.listing_url,
                "last_checked_at": source.last_checked_at,
            }
            for source in sources
        ],
        "data_warnings": warnings,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
    }
