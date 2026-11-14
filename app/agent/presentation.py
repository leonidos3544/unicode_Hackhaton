from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.catalog.schemas import CATEGORIES
from app.search.models import SearchFilters


def explain(card: dict[str, Any], filters: SearchFilters, timezone: str) -> str:
    """Explain only constraints actually proved by the returned catalog fields."""
    parts = [str(card["city"])]
    if filters.category:
        parts.append(CATEGORIES[card["category"]].lower())
    ages = filters.children_ages or ([filters.age] if filters.age is not None else [])
    if ages and card.get("age_limit") is not None and card["age_limit"] <= min(ages):
        parts.append(
            "возраст "
            + str(card["age_limit"])
            + "+ подходит посетителям "
            + ", ".join(map(str, ages))
            + " лет"
        )
    if filters.is_free is True and card.get("is_free") is True:
        parts.append("вход бесплатный")
    if filters.price_max is not None and card.get("price_max") is not None:
        size = filters.adults + len(filters.children_ages) if filters.budget_scope == "party" else 1
        price = float(card["price_max"]) * size
        if price <= float(filters.price_max):
            parts.append(f"не дороже {price:g} ₽ " + ("на всю группу" if size > 1 else "за билет"))
    if filters.date_from or filters.date_to:
        start = datetime.fromisoformat(str(card["date_start"])).astimezone(ZoneInfo(timezone))
        parts.append("начало " + start.strftime("%d.%m в %H:%M"))
    if filters.duration_max_min and card.get("duration_min") is not None:
        parts.append(f"длительность {card['duration_min']} мин")
    if filters.end_before and card.get("date_end"):
        end = datetime.fromisoformat(str(card["date_end"])).astimezone(ZoneInfo(timezone))
        parts.append("окончание " + end.strftime("%H:%M"))
    if filters.language and card.get("language"):
        parts.append("язык в источнике: " + str(card["language"]))
    return "По данным афиши: " + "; ".join(parts) + "."


def present(
    items: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    question: str | None,
    mode: str,
    reason: str | None,
    cities: list[str] | None = None,
) -> dict[str, Any]:
    from app.agent.parsing import QUESTIONS

    if question:
        text = QUESTIONS.get(question, question)
        if question in {"city", "city_unavailable"}:
            text += (
                " Сейчас доступны: " + ", ".join(cities) + ". Выберите город из списка."
                if cities
                else " Каталог городов пока пуст. Данные ещё загружаются."
            )
    elif actions:
        text = "Проверьте предложение. Действие выполнится только после подтверждения."
    elif items:
        text = "Найдены события по указанным условиям. Данные и ограничения приведены в карточках."
    else:
        text = "В каталоге нет подтверждённых событий по этим условиям. Измените фильтры или дату."
    if mode == "fallback" and not question:
        explanations = {
            "model_not_configured": "ИИ ещё не настроен.",
            "provider_timeout": "ИИ не успел ответить вовремя.",
            "provider_auth_error": "Не удалось подключиться к ИИ: ошибка доступа.",
            "provider_rate_limit": "Сервис ИИ временно ограничил число запросов.",
            "provider_unreachable": "Не удалось связаться с сервисом ИИ.",
            "provider_error": "При обращении к ИИ произошла ошибка.",
            "invalid_tool_arguments": "ИИ не смог корректно применить условия поиска.",
            "no_grounded_result": "ИИ не вернул результат, подтверждённый каталогом.",
            "budget_exhausted": "Достигнут лимит расходов на ИИ.",
            "pricing_missing": "Для ИИ ещё не настроен тариф.",
            "turn_budget": "Достигнут лимит расходов на этот запрос.",
            "tool_limit": "Достигнут лимит действий ИИ для этого запроса.",
            "context_limit": "Диалог превысил допустимый объём контекста ИИ.",
        }
        text = explanations.get(reason or "", "ИИ не смог завершить обработку запроса.") + (
            " Выполнен обычный поиск по сохранённым условиям. " + text
        )
    return {
        "message": text,
        "items": items,
        "pending_actions": actions,
        "question": question,
        "mode": mode,
        "fallback_reason": reason,
    }
