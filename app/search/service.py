from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.catalog.policy import SOURCE_FRESHNESS
from app.catalog.repository import event_card
from app.common.clock import reference_now
from app.ingestion.normalize import normalize_text
from app.search.models import SearchFilters
from app.storage.postgres.tables import City, Event, SourceRecord, Venue


def filter_conditions(filters: SearchFilters) -> list[ColumnElement[bool]]:
    conditions = [
        Event.status == "published",
        Event.merged_into_id.is_(None),
        Event.date_start >= max(filters.date_from or reference_now(), reference_now()),
    ]
    conditions.append(
        select(SourceRecord.id)
        .where(
            SourceRecord.event_id == Event.id,
            SourceRecord.applied_hash.is_not(None),
            or_(
                SourceRecord.source_type != "page",
                (SourceRecord.verification_status == "verified")
                & (SourceRecord.verified_at >= reference_now() - SOURCE_FRESHNESS),
            ),
        )
        .exists()
    )
    if filters.exclude_ids:
        conditions.append(Event.id.not_in(filters.exclude_ids))
    for column, value in (
        (Event.city_id, filters.city_id),
        (Event.venue_id, filters.venue_id),
        (Event.category, filters.category),
    ):
        if value is not None:
            conditions.append(column == value)
    if filters.language:
        aliases = {
            "башкирский": ["башкирский", "башкирский язык", "ba", "bashkir", "башҡортса"],
            "русский": ["русский", "русский язык", "ru", "russian"],
            "английский": ["английский", "английский язык", "en", "english"],
        }
        language = normalize_text(filters.language)
        candidates = next((values for values in aliases.values() if language in values), [language])
        conditions.append(func.lower(Event.language).in_(candidates))
    if filters.date_to:
        conditions.append(Event.date_start < filters.date_to)
    if filters.end_before:
        conditions.append(Event.date_end <= filters.end_before)
    if filters.price_max is not None:
        # A lower bound ('from 300') cannot prove that a ticket costs <= 500.
        party_size = (
            filters.adults + len(filters.children_ages) if filters.budget_scope == "party" else 1
        )
        conditions.append(Event.price_max_minor * party_size <= int(filters.price_max * 100))
    if filters.is_free is not None:
        conditions.append(Event.is_free == filters.is_free)
    ages = [*filters.children_ages, *([filters.age] if filters.age is not None else [])]
    if ages:
        conditions.append(Event.age_limit <= min(ages))
    if filters.duration_max_min is not None:
        conditions.append(
            func.extract("epoch", Event.date_end - Event.date_start)
            <= filters.duration_max_min * 60
        )
    if filters.query:
        terms = []
        for term in normalize_text(filters.query).split():
            terms.append(
                or_(
                    Event.normalized_title.contains(term, autoescape=True),
                    func.lower(Event.description).contains(term, autoescape=True),
                    func.lower(func.array_to_string(Event.tags, " ")).contains(
                        term, autoescape=True
                    ),
                    Event.city_id.in_(
                        select(City.id).where(City.normalized_name.contains(term, autoescape=True))
                    ),
                    Event.venue_id.in_(
                        select(Venue.id).where(
                            Venue.normalized_name.contains(term, autoescape=True)
                        )
                    ),
                )
            )
        if terms:
            vector = func.to_tsvector("russian", Event.normalized_title + " " + Event.description)
            conditions.append(
                or_(and_(*terms), vector.op("@@")(func.plainto_tsquery("russian", filters.query)))
            )
    return conditions


async def search_events(
    session: AsyncSession,
    filters: SearchFilters,
    *,
    diagnose: bool = True,
) -> dict[str, Any]:
    conditions = filter_conditions(filters)
    count = await session.scalar(select(func.count()).select_from(Event).where(*conditions))
    order: list[ColumnElement[Any]] = [Event.date_start.asc(), Event.id.asc()]
    if filters.sort == "price_asc":
        order = [Event.price_min_minor.asc().nulls_last(), *order]
    events = (
        await session.scalars(
            select(Event)
            .where(*conditions)
            .order_by(*order)
            .offset((filters.page - 1) * filters.page_size)
            .limit(filters.page_size)
        )
    ).all()
    items = [await event_card(session, event) for event in events]
    relaxations = []
    if not count and diagnose:
        labels = {
            "price_max": "Увеличить бюджет",
            "age": "Снять возрастной фильтр",
            "category": "Показать другие категории",
            "is_free": "Показать платные события",
            "end_before": "Изменить время окончания",
            "query": "Убрать текстовый запрос",
            "duration_max_min": "Снять ограничение длительности",
        }
        for field, label in labels.items():
            if getattr(filters, field) is None:
                continue
            alternative = filters.model_copy(update={field: None, "page": 1, "page_size": 3})
            found = await session.scalar(
                select(func.count()).select_from(Event).where(*filter_conditions(alternative))
            )
            if found:
                relaxations.append(
                    {
                        "field": field,
                        "label": label,
                        "match_count": found,
                        "filters": alternative.model_dump(mode="json", exclude_none=True),
                    }
                )
    for item in items:
        evidence = []
        for key in (
            "city_id",
            "category",
            "age",
            "price_max",
            "date_from",
            "date_to",
            "end_before",
            "duration_max_min",
            "language",
            "is_free",
        ):
            value = getattr(filters, key)
            if value is not None:
                evidence.append(
                    {
                        "constraint": key,
                        "result": "satisfied",
                        "source_ref": item["source_url"],
                        "event_version": item["version"],
                    }
                )
        item["constraint_evidence"] = evidence
    return {
        "items": items,
        "pagination": {
            "page": filters.page,
            "page_size": filters.page_size,
            "total": count,
            "has_more": filters.page * filters.page_size < (count or 0),
        },
        "applied_filters": filters.model_dump(mode="json", exclude_none=True),
        "relaxations": relaxations,
        "coverage_warnings": [
            "В каталоге нет событий по выбранным условиям. Попробуйте изменить дату или фильтры."
        ]
        if not count
        else [],
    }
