"""Restore a public catalog snapshot only into the dedicated evaluation database."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.normalize import normalize_text
from app.storage.postgres.runtime_tables import EventAssumption
from app.storage.postgres.tables import Base, City, Event, SourceRecord, Venue

EVALUATION_DATABASE = "bashculture_agent_eval"
TRACE_FIELDS = (
    "plan",
    "calls",
    "evidence",
    "model_id",
    "prompt_version",
    "input_tokens",
    "output_tokens",
    "usage_known",
    "latency_ms",
    "error_code",
)


def trace_snapshot(trace: Any) -> dict[str, Any]:
    return {key: getattr(trace, key) for key in TRACE_FIELDS}


def moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def restore_snapshot(
    sessions: async_sessionmaker[AsyncSession], url: str, cards: list[dict[str, Any]]
) -> None:
    if make_url(url).database != EVALUATION_DATABASE:
        raise ValueError("Snapshot restore requires the dedicated bashculture_agent_eval database")
    if not cards:
        raise ValueError("An empty catalog is not a valid acceptance snapshot")
    async with sessions() as session, session.begin():
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        await session.execute(text(f"TRUNCATE {tables} CASCADE"))
        cities, venues = {}, {}
        for card in cards:
            cities[card["city_id"]] = card
            venues[card["venue_id"]] = card
        for city_id, card in cities.items():
            session.add(
                City(
                    id=UUID(city_id),
                    name=card["city"],
                    normalized_name=normalize_text(card["city"]),
                )
            )
        await session.flush()
        for venue_id, card in venues.items():
            session.add(
                Venue(
                    id=UUID(venue_id),
                    city_id=UUID(card["city_id"]),
                    name=card["venue"],
                    normalized_name=normalize_text(card["venue"]),
                    address=card["address"],
                    lat=card.get("lat"),
                    lon=card.get("lon"),
                )
            )
        await session.flush()
        for card in cards:
            fields = {
                key: card.get(key)
                for key in (
                    "title",
                    "normalized_title",
                    "category",
                    "tags",
                    "address",
                    "description",
                    "is_free",
                    "age_limit",
                    "language",
                    "org_link",
                    "status",
                    "version",
                    "dedup_key",
                )
            }
            for key in ("date_start", "date_end", "created_at", "updated_at"):
                fields[key] = moment(card.get(key))
            for key in ("price_min", "price_max"):
                value = card.get(key)
                fields[key + "_minor"] = (
                    int(Decimal(str(value)) * 100) if value is not None else None
                )
            session.add(
                Event(
                    id=UUID(card["id"]),
                    city_id=UUID(card["city_id"]),
                    venue_id=UUID(card["venue_id"]),
                    **fields,
                )
            )
        await session.flush()
        source_index = 1
        for card in cards:
            for source in card["sources"]:
                session.add(
                    SourceRecord(
                        id=UUID(int=source_index),
                        event_id=UUID(card["id"]),
                        source_name=source["source_name"],
                        external_id=f"snapshot-{source_index}",
                        source_url=source["source_url"],
                        source_type=source["source_type"],
                        latest_hash=source["source_hash"],
                        applied_hash=source["source_hash"],
                        verified_at=moment(source.get("verified_at")),
                        verification_status=source.get("verification_status"),
                        listing_url=source.get("listing_url"),
                        last_checked_at=moment(source.get("last_checked_at")),
                    )
                )
                source_index += 1
            assumptions = card.get("demo_assumptions")
            if assumptions:
                session.add(
                    EventAssumption(
                        event_id=UUID(card["id"]),
                        event_version=assumptions["event_version"],
                        values=assumptions["values"],
                    )
                )
