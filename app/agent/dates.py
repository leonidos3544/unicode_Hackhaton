"""Calendar expressions use the event timezone and a caller-supplied reference time."""

import re
from datetime import datetime, timedelta
from typing import Any

WEEKDAYS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень")
MONTHS = (
    "январ",
    "феврал",
    "март",
    "апрел",
    "ма",
    "июн",
    "июл",
    "август",
    "сентябр",
    "октябр",
    "ноябр",
    "декабр",
)


def calendar_window(text: str, local: datetime) -> tuple[datetime, int] | None:
    text = re.sub(r'[«"][^»"]+[»"]', "", text)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r"следующ\w*\s+недел", text):
        return start + timedelta(days=7 - start.weekday()), 7
    if "до конца месяца" in text:
        end = (
            start.replace(year=start.year + 1, month=1, day=1)
            if start.month == 12
            else start.replace(month=start.month + 1, day=1)
        )
        return local, (end.date() - local.date()).days
    named = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\w*(?:\s+(20\d{2}))?\b", text)
    if named:
        month = MONTHS.index(named[2]) + 1
        return start.replace(year=int(named[3] or start.year), month=month, day=int(named[1])), 1
    for day, stem in enumerate(WEEKDAYS):
        if re.search(r"\b" + stem + r"[а-я]*\b", text):
            delta = (day - start.weekday()) % 7
            if not delta and "следующ" in text:
                delta = 7
            return start + timedelta(days=delta), 1
    return None


def requested_start_time(message: str, state: dict[str, Any]) -> list[int] | None:
    text = message.lower()
    if text.strip() in {"сброс", "новый поиск", "/cancel"} or "без времени начала" in text:
        return None
    match = re.search(r"\bпосле\s+(\d{1,2})(?::(\d{2}))?(?!\d)", text)
    if match:
        hour, minute = int(match[1]), int(match[2] or 0)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Некорректное время начала")
        return [hour, minute]
    value = state.get("start_time")
    return value if isinstance(value, list) and len(value) == 2 else None
