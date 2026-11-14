"""Conservative, offline extraction. Questions are explicit; facts come only from tools."""

import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any
from zoneinfo import ZoneInfo

from app.agent.constants import MAX_CARDS
from app.agent.dates import calendar_window, requested_start_time
from app.search.models import SearchFilters

QUESTIONS = {
    "city": "В каком городе ищем мероприятия?",
    "city_unavailable": "Не удалось найти этот город в нашем каталоге.",
    "child_age": "Сколько лет ребёнку? Это нужно для проверки возрастного ограничения.",
    "origin": "Для поиска рядом укажите площадку. Без исходной точки расстояние проверить нельзя.",
    "origin_unavailable": "Для поиска по расстоянию нужны координаты и проверенный маршрут. "
    "Пока напишите «без расстояния», чтобы искать по остальным условиям.",
    "budget_scope": "Указанный бюджет — на одного человека или на всю группу?",
    "budget": "Какой максимальный бюджет в рублях вы считаете недорогим?",
    "event_status": "Какое именно событие проверяем? Пришлите ссылку или ID карточки.",
    "date": "На какую дату ищем? Укажите дату в формате ГГГГ-ММ-ДД или напишите «завтра».",
    "evening_date": "На какой день подобрать вечерние мероприятия? Напишите дату или «завтра».",
    "time_date": "На какой день ищем мероприятия после выбранного времени? "
    "Напишите дату или «завтра».",
}
INJECTION = re.compile(
    r"игнорируй.{0,30}(инструкц|правил)|забудь.{0,20}(правил|инструкц)|"
    r"системн.{0,10}промпт|ignore.{0,30}(instruction|previous)|system\s*prompt",
    re.I,
)


def redact(value: str) -> str:
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[email]", value)
    return re.sub(r"(?<!\d)\+?\d[\d ()-]{9,}\d", "[phone]", value)


def wants_evening(message: str) -> bool:
    # A quoted production title, e.g. «Зимний вечер», is a title search.
    unquoted = re.sub(r'[«"][^»"]+[»"]', "", message.lower())
    return bool(re.search(r"\b(?:на\s+вечер|вечером|вечерние)\b", unquoted)) or (
        unquoted.strip() == "вечер"
    )


def parse_request(
    message: str,
    state: dict[str, Any],
    cities: list[dict[str, str]],
    now: datetime,
    timezone: str,
) -> tuple[SearchFilters, str | None]:
    text = message.lower().replace("ё", "е")
    evening = wants_evening(message) or state.get("question") == "evening_date"
    data = dict(state.get("filters", {}))
    if (data.get("query") or "").lower() in {"дешевле", "ещё", "еще", "бесплатно", "платные тоже"}:
        data.pop("query", None)
    data.update(page=1, page_size=MAX_CARDS, exclude_ids=[])
    if text.strip() in ("сброс", "новый поиск", "/cancel"):
        return SearchFilters(page_size=MAX_CARDS), "city"
    matches = []
    for city in cities:
        name = city["name"].lower().replace("ё", "е")
        stem = name[:-1] if name.endswith(("а", "ь")) else name
        words = re.findall(r"[а-яa-z-]+", text)
        if any(w in {name, stem, stem + "е", stem + "у", stem + "ы"} for w in words) or any(
            len(w) >= 4 and SequenceMatcher(None, name, w).ratio() >= 0.82 for w in words
        ):
            matches.append(city)
    if len(matches) == 1:
        data["city_id"] = matches[0]["id"]
    elif len(matches) > 1:
        data.pop("city_id", None)
    named_city = re.search(r"\b[вВ]\s+([А-ЯЁ][а-яё-]{2,})", message)
    unavailable_city = bool(named_city and not matches)
    if unavailable_city:
        data.pop("city_id", None)
    for pattern, category in (
        (r"спектакл|театр", "theatre"),
        (r"концерт|музык", "concert"),
        (r"выставк", "exhibition"),
        (r"музе", "exhibition"),
        (r"мастер.?класс", "other"),
        (r"экскурс", "excursion"),
        (r"кино|фильм", "cinema"),
        (r"фестивал", "festival"),
    ):
        if re.search(pattern, text):
            # Categories are checked against the shared API enum below.
            data["category"] = category
    if "любая категори" in text or "все категори" in text:
        data.pop("category", None)
    if "бесплат" in text:
        data["is_free"] = True
        data.pop("price_max", None)
    if "платные тоже" in text:
        data.pop("is_free", None)
    if "без бюджета" in text or "любая цена" in text:
        data.pop("price_max", None)
        data.pop("is_free", None)
    if "на человека" in text:
        data["budget_scope"] = "per_person"
    if "без детей" in text:
        data["children_ages"] = []
    for pattern, language in (
        (r"на башкирском|башкирск\w* язык", "башкирский"),
        (r"на русском|русск\w* язык", "русский"),
        (r"на английском|английск\w* язык", "английский"),
    ):
        if re.search(pattern, text):
            data["language"] = language
    for topic in ("джаз", "балет"):
        if topic in text:
            data["query"] = topic
    budget = re.search(r"(?:до|бюджет(?:ом)?|не дороже)\s*(\d+(?:[.,]\d{1,2})?)(?![\d:])", text)
    if budget:
        data["price_max"] = budget[1].replace(",", ".")
    elif state.get("question") == "budget" and re.fullmatch(r"\d+(?:[.,]\d{1,2})?", text.strip()):
        data["price_max"] = text.strip().replace(",", ".")
    if "дешев" in text:
        data["sort"] = "price_asc"
    if "на всех" in text or "общий бюджет" in text:
        data["budget_scope"] = "party"
    adults = re.search(r"(\d+)\s*взросл", text)
    if adults:
        data["adults"] = int(adults[1])
    age = re.search(r"(?:ребен\w*|дет\w*)\D{0,10}(\d{1,2})\s*(?:лет|год)", text)
    if not age and state.get("question") == "child_age":
        age = re.search(r"\b(\d{1,2})\b", text)
    if age and int(age[1]) <= 17:
        data["children_ages"] = [int(age[1])]
    ages = re.search(
        r"(?:дети|детям|детей|возраст)\s*(\d{1,2}(?:(?:\s*,\s*|\s+и\s+)\d{1,2})*)\s*(?:лет|год)",
        text,
    )
    if ages:
        data["children_ages"] = [int(value) for value in re.findall(r"\d+", ages[1])]
    if "на двоих" in text:
        data["budget_scope"] = "party"
        if not adults:
            data["adults"] = max(0, 2 - len(data.get("children_ages", [])))
    local = now.astimezone(ZoneInfo(timezone))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    days: int | None = None
    calendar = calendar_window(text, local)
    if "послезавтра" in text:
        start += timedelta(days=2)
        days = 1
    elif "завтра" in text:
        start += timedelta(days=1)
        days = 1
    elif "сегодня" in text:
        start = local
        days = 1
    elif calendar:
        start, days = calendar
    elif "выходн" in text:
        start += timedelta(days=(5 - local.weekday()) % 7)
        days = 2
    elif "недел" in text:
        start = local
        days = 7
    date = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if date:
        try:
            start = datetime.fromisoformat(date[1]).replace(tzinfo=ZoneInfo(timezone))
            days = 1
        except ValueError:
            raise ValueError("Некорректная дата") from None
    if days:
        data["date_from"] = start.isoformat()
        end = (start + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        data["date_to"] = end.isoformat()
        data.pop("end_before", None)
    evening_needs_date = False
    if evening:
        data.pop("query", None)
        selected_start = (
            datetime.fromisoformat(data["date_from"]).astimezone(ZoneInfo(timezone))
            if data.get("date_from")
            else local
        )
        selected_end = (
            datetime.fromisoformat(data["date_to"]).astimezone(ZoneInfo(timezone))
            if data.get("date_to")
            else None
        )
        evening_needs_date = bool(
            selected_end and selected_end - selected_start > timedelta(days=1)
        )
        if not evening_needs_date:
            # Without a selected date, "на вечер" means today, not the next event
            # whose title happens to contain "вечер". Published times use event_timezone.
            evening_start = selected_start.replace(hour=18, minute=0, second=0, microsecond=0)
            evening_end = (evening_start + timedelta(days=1)).replace(hour=0)
            data["date_from"] = evening_start.isoformat()
            data["date_to"] = evening_end.isoformat()
    after = requested_start_time(message, state)
    time_needs_date = False
    if after:
        selected = datetime.fromisoformat(data["date_from"]) if data.get("date_from") else local
        selected = selected.astimezone(ZoneInfo(timezone)).replace(
            hour=after[0], minute=after[1], second=0, microsecond=0
        )
        end = (
            datetime.fromisoformat(data["date_to"])
            if data.get("date_to")
            else (selected + timedelta(days=1)).replace(hour=0, minute=0)
        )
        time_needs_date = end.date() - selected.date() > timedelta(days=1)
        data.update(date_from=selected.isoformat(), date_to=end.isoformat())
    finish = re.search(r"до\s*(\d{1,2}):(\d{2})", text)
    if finish:
        day = datetime.fromisoformat(data["date_from"]) if data.get("date_from") else local
        data["end_before"] = day.replace(
            hour=int(finish[1]), minute=int(finish[2]), second=0, microsecond=0
        ).isoformat()
    duration = re.search(
        r"(?:не дольше|длительностью до)\s*(\d+|одного|двух|трех|четырех|пяти)\s*(мин|час)", text
    )
    if duration:
        duration_words = {"одного": 1, "двух": 2, "трех": 3, "четырех": 4, "пяти": 5}
        value = int(duration[1]) if duration[1].isdigit() else duration_words[duration[1]]
        data["duration_max_min"] = value * (60 if duration[2] == "час" else 1)
    if "еще" in text or "другие варианты" in text:
        data["exclude_ids"] = state.get("seen_ids", [])[-200:]
    quoted = re.search(r'[«"]([^»"]+)[»"]', message)
    if quoted:
        data["query"] = quoted[1][:200]
    if "без названия" in text:
        data.pop("query", None)
    question = None
    if not data.get("city_id"):
        question = (
            "city_unavailable"
            if unavailable_city
            or (not matches and state.get("question") in {"city", "city_unavailable"})
            else "city"
        )
    elif evening_needs_date:
        question = "evening_date"
    elif time_needs_date:
        question = "time_date"
    elif (re.search(r"отмен|перенесли", text) or state.get("needs_event_status")) and not re.search(
        r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", text
    ):
        question = "event_status"
    elif (
        (re.search(r"ребен|с детьми|для детей", text) or state.get("needs_child_age"))
        and not data.get("children_ages")
        and "без детей" not in text
    ):
        question = "child_age"
    elif re.search(r"рядом|поближе|ближе", text):
        question = "origin"
    elif state.get("question") in {"origin", "origin_unavailable"} and not re.search(
        r"без расстояния|неважно где|любой район", text
    ):
        question = "origin_unavailable"
    elif (
        state.get("question") == "child_age"
        and not data.get("children_ages")
        and "без детей" not in text
    ):
        question = "child_age"
    elif "на другую дату" in text and days is None:
        question = "date"
    elif state.get("question") == "date" and days is None:
        question = "date"
    elif (
        ("недорого" in text or state.get("needs_budget"))
        and data.get("price_max") is None
        and not data.get("is_free")
        and not re.search(r"любая цена|без бюджета", text)
    ):
        question = "budget"
    elif (
        data.get("price_max") is not None
        and data.get("adults", 1) + len(data.get("children_ages", [])) > 1
        and not (
            "на всех" in text
            or "на двоих" in text
            or "общий бюджет" in text
            or "на человека" in text
            or state.get("budget_scope_explicit")
        )
    ):
        question = "budget_scope"
    return SearchFilters.model_validate(data), question
