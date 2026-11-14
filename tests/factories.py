from datetime import UTC, datetime, timedelta
from typing import Any


def event_payload(**overrides: Any) -> dict[str, Any]:
    # Deliberately fictitious records, used only by isolated tests.
    start = (datetime.now(UTC) + timedelta(days=10)).replace(microsecond=0)
    payload: dict[str, Any] = {
        "external_id": "test-session-1",
        "title": "Тестовая выставка",
        "category": "exhibition",
        "date_start": start.isoformat(),
        "date_end": (start + timedelta(hours=2)).isoformat(),
        "city": "Тестовый город",
        "venue": "Тестовая площадка",
        "address": "Тестовый адрес",
        "description": "Искусственная запись исключительно для тестов.",
        "price_min": "300.50",
        "price_max": "500.50",
        "age_limit": 6,
        "source_url": "https://example.org/test-only/event-1",
    }
    payload.update(overrides)
    return payload
