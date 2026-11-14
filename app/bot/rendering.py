from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo


def moment(value: Any, timezone: str) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M")


def render_event(card: dict[str, Any], timezone: str) -> str:
    labels = {
        "price_min": "минимальная цена",
        "price_max": "максимальная цена",
        "age_limit": "возрастной допуск",
        "duration_min": "длительность",
        "date_end": "время окончания",
    }
    demo = (
        "ДЕМО — непроверенные сведения: "
        + ", ".join(labels.get(field, field) for field in card["demo_fields"])
        + "\n"
        if card.get("demo_fields")
        else ""
    )
    price = "Цена неизвестна"
    if card.get("is_free") is True:
        price = "Бесплатно"
    elif card.get("price_min") is not None:
        price = (
            f"от {card['price_min']:g} ₽"
            if isinstance(card["price_min"], (int, float))
            else f"от {card['price_min']} ₽"
        )
        if card.get("price_max") == card["price_min"]:
            price = f"{card['price_min']} ₽"
        elif card.get("price_max") is not None:
            price = f"{card['price_min']}–{card['price_max']} ₽"
    age = f"{card['age_limit']}+" if card.get("age_limit") is not None else "Возраст не указан"
    end = (
        f" — {moment(card['date_end'], timezone)}"
        if card.get("date_end")
        else " · окончание неизвестно"
    )
    title = escape(str(card["title"])[:500])
    status = "ОТМЕНЕНО\n" if card.get("status") == "cancelled" else ""
    return (
        demo
        + (
            f"{status}<b>{title}</b>\n{moment(card['date_start'], timezone)}{end}\n"
            f"{escape(str(card['city']))} · {escape(str(card['venue']))}\n"
            f"{escape(str(card['address']))}\n{escape(price)} · {age}\n"
            + (
                escape(card["recommendation_reason"]) + "\n"
                if card.get("recommendation_reason")
                else ""
            )
            + "\n".join(escape(str(w)) for w in card.get("data_warnings", []))
        )[:3800]
    )


def message(
    chat_id: int | str, text: str, buttons: list[list[dict[str, str]]] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return {"method": "sendMessage", "payload": payload}


def callback(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def card_buttons(card: dict[str, Any]) -> list[list[dict[str, str]]]:
    buttons = []
    if card.get("source_url"):
        buttons.append({"text": "Источник", "url": str(card["source_url"])})
    if card.get("org_link"):
        buttons.append({"text": "Условия и билеты", "url": str(card["org_link"])})
    return [buttons] if buttons else []
