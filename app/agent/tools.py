import json
import re
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.confirmations import prepare_action
from app.agent.constants import MAX_CARDS
from app.agent.parsing import wants_evening
from app.agent.policy import require_intent
from app.catalog.repository import event_card
from app.catalog.service import get_event
from app.common.errors import AppError
from app.feedback.service import FeedbackInput
from app.ingestion.normalize import normalize_text
from app.search.models import SearchFilters
from app.search.service import filter_conditions, search_events
from app.storage.postgres.tables import City, Event, Venue
from app.subscriptions.service import SubscriptionInput


class EventArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID


class SimilarArgs(EventArgs):
    filters: SearchFilters = Field(default_factory=SearchFilters)


class ScheduleArgs(SearchFilters):
    venue_id: UUID


MODELS: dict[str, type[BaseModel]] = {
    "search_events": SearchFilters,
    "get_event": EventArgs,
    "find_similar": SimilarArgs,
    "get_venue_schedule": ScheduleArgs,
    "subscribe": SubscriptionInput,
    "save_feedback": FeedbackInput,
}
DESCRIPTIONS = {
    "search_events": "Поиск актуальных событий по условиям пользователя. "
    "Возвращает карточки и проверенные варианты ослабления фильтров.",
    "get_event": "Карточка существующего события по ID, включая отмену. "
    "Не соответствующая условиям карточка не является рекомендацией.",
    "find_similar": "Другие события той же категории, с сохранением жёстких "
    "пользовательских условий и исключением исходного события.",
    "get_venue_schedule": "Актуальные сеансы площадки по venue_id из карточки, "
    "за период. page задаёт страницу расписания.",
    "subscribe": "Предложить подписку только по явной просьбе пользователя. "
    "Ничего не исполняет до отдельного подтверждения.",
    "save_feedback": "Предложить обращение модератору только по просьбе пользователя. "
    "Текст берётся из сообщения пользователя, нужна отдельная кнопка подтверждения.",
}


def schemas(names: set[str] | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": DESCRIPTIONS[name],
                "parameters": model.model_json_schema(),
            },
        }
        for name, model in MODELS.items()
        if names is None or name in names
    ]
    for tool in result:
        if tool["function"]["name"] == "find_similar":
            tool["function"]["parameters"] = EventArgs.model_json_schema()
        if tool["function"]["name"] == "search_events":
            # The server has already validated dates, budget and visitors. Asking the
            # model to duplicate those values creates invalid or invented constraints.
            tool["function"]["parameters"] = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "Только явно названное пользователем название "
                        "или тема. Не копируй сюда условия, возраст, дату или весь запрос.",
                    },
                },
                "description": "Условия города, категории, дат, бюджета и возраста "
                "сервер применяет сам. Если название не выбрано, передай {}.",
            }
        if tool["function"]["name"] == "subscribe":
            tool["function"]["parameters"]["properties"]["period_filter"] = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "date_from": {"type": "string", "format": "date-time"},
                    "date_to": {"type": "string", "format": "date-time"},
                },
                "description": "Только границы периода из проверенных фильтров. "
                "Без выбранных дат передай пустой объект {}. Категория задаётся отдельно.",
                "default": {},
            }
    return result


async def execute_tool(
    session: AsyncSession,
    owner: str,
    name: str,
    raw: str,
    message: str,
    hard_filters: SearchFilters,
    *,
    channel: str = "web",
) -> dict[str, Any]:
    if name not in MODELS:
        raise AppError("unknown_tool", "Неизвестный инструмент", 422)
    if len(raw) > 8000:
        raise AppError("arguments_too_large", "Слишком большой запрос", 422)
    arguments = json.loads(raw)
    if not isinstance(arguments, dict):
        raise ValueError("arguments_must_be_object")
    require_intent(name, message, arguments)
    payload = MODELS[name].model_validate(arguments)
    if name in {"subscribe", "save_feedback"}:
        if isinstance(payload, SubscriptionInput):
            if channel == "telegram":
                payload.channel = "telegram"
            if await session.get(City, payload.city_id) is None:
                raise AppError("city_not_found", "Выберите существующий город", 422)
            if hard_filters.city_id and payload.city_id != hard_filters.city_id:
                raise AppError("constraint_conflict", "Город подписки не совпадает с запросом", 422)
            if hard_filters.category:
                payload.category = hard_filters.category
            period = dict(payload.period_filter)
            for key in ("date_from", "date_to"):
                value = getattr(hard_filters, key)
                if value:
                    period[key] = value.isoformat()
            payload = SubscriptionInput.model_validate(
                {**payload.model_dump(mode="json"), "period_filter": period}
            )
        if isinstance(payload, FeedbackInput) and payload.event_id:
            await get_event(session, payload.event_id)
        return await prepare_action(session, owner, name, payload.model_dump(mode="json"))
    if isinstance(payload, EventArgs) and name == "get_event":
        event = await get_event(session, payload.event_id)
        matching = await session.scalar(
            select(Event.id).where(Event.id == event.id, *filter_conditions(hard_filters))
        )
        card = await event_card(session, event)
        return {
            "items": [card] if matching else [],
            "detail": card,
            "matches_constraints": bool(matching),
        }
    if isinstance(payload, SimilarArgs):
        reference = await get_event(session, payload.event_id)
        filters = payload.filters.model_dump(mode="json", exclude_none=True)
        filters["category"] = reference.category
        filters["exclude_ids"] = list(
            dict.fromkeys([str(payload.event_id), *filters.get("exclude_ids", [])])
        )
    else:
        filters = payload.model_dump(mode="json", exclude_none=True)
    # Validate model IDs even if they will not become a trusted search constraint.
    if filters.get("venue_id"):
        venue = await session.get(Venue, UUID(str(filters["venue_id"])))
        if venue is None:
            raise AppError("venue_not_found", "Используйте ID площадки из карточки события", 422)
    model_query, model_page = filters.get("query"), filters.get("page", 1)
    similar_category = filters.get("category") if isinstance(payload, SimilarArgs) else None
    # Null is also authoritative: a model must not invent a date, age, price or
    # paid-only filter which the resident never requested.
    trusted = hard_filters.model_dump(mode="json", exclude_none=True)
    trusted.pop("page", None)
    trusted.pop("page_size", None)
    filters = hard_filters.model_dump(mode="json", exclude_none=True)
    filters["page"] = model_page if isinstance(payload, ScheduleArgs) else hard_filters.page
    if not hard_filters.category and similar_category:
        filters["category"] = similar_category
    if hard_filters.query is None and model_query and not wants_evening(message):
        filters["query"] = model_query
    if hard_filters.query is None and filters.get("query"):
        query_terms = set(normalize_text(str(filters["query"])).split())
        message_terms = set(normalize_text(message).split())
        controls = {
            "дешевле",
            "еще",
            "бесплатно",
            "бесплатные",
            "платные",
            "тоже",
            "только",
            "подпиши",
            "подписка",
            "найди",
            "покажи",
            "события",
            "мероприятия",
            "завтра",
            "сегодня",
            "послезавтра",
            "выходные",
            "без",
            "расстояния",
            "неважно",
            "где",
            "любой",
            "район",
            "на",
            "всех",
            "все",
            "категории",
            "с",
            "в",
            "до",
            "лет",
            "года",
            "рублей",
            "ребенком",
            "детей",
            "дети",
            "человека",
            "человек",
            "вечер",
            "вечером",
            "вечерние",
            "давай",
            "что",
            "нибудь",
        }
        meaningful = {
            term
            for term in query_terms
            if term not in controls
            and not re.search(
                r"\d|^уф[аыуе]$|^стерлитамак|^концерт|^спектакл|^театр|^выставк|^экскурс|^кино$",
                term,
            )
        }
        meaningful = {
            term
            for term in meaningful
            if not re.search(
                r"^понедель|^вторник|^сред[ау]|^четверг|^пятниц|^суббот|^воскрес|"
                r"^башкир|^русск|^английск|^час|^минут|^после$|^двух$|^трех$|^недел|^месяц",
                term,
            )
        }
        city = await session.get(City, hard_filters.city_id) if hard_filters.city_id else None
        if city:
            meaningful = {
                term
                for term in meaningful
                if SequenceMatcher(None, normalize_text(city.name), term).ratio() < 0.82
            }
        if not meaningful or meaningful != query_terms or not query_terms <= message_terms:
            filters.pop("query", None)
    if isinstance(payload, ScheduleArgs):
        if hard_filters.venue_id and payload.venue_id != hard_filters.venue_id:
            raise AppError("constraint_conflict", "Площадка не совпадает с запросом", 422)
        filters["venue_id"] = str(payload.venue_id)
    if isinstance(payload, SimilarArgs):
        filters["exclude_ids"] = list(
            dict.fromkeys([str(payload.event_id), *trusted.get("exclude_ids", [])])
        )
    filters["page_size"] = min(hard_filters.page_size, MAX_CARDS)
    checked = SearchFilters.model_validate(filters)
    if checked.city_id and await session.get(City, checked.city_id) is None:
        raise AppError("city_not_found", "Используйте ID города из доступного списка", 422)
    if checked.venue_id:
        venue = await session.get(Venue, checked.venue_id)
        if venue is None:
            raise AppError("venue_not_found", "Используйте ID площадки из карточки события", 422)
        if checked.city_id and venue.city_id != checked.city_id:
            raise AppError("venue_city_conflict", "Площадка находится в другом городе", 422)
    result = await search_events(session, checked)
    return dict(jsonable_encoder(result))
