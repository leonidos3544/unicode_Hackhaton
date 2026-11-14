import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from app.agent.parsing import parse_request
from app.agent.tools import execute_tool
from app.search.models import SearchFilters
from tests.factories import event_payload
from tests.test_agent import setup_dialogue
from tests.test_catalog_integration import publish, upload

CITY = "9a8d17b6-3df7-4bfd-9388-4a40dc12e14d"
CITIES = [{"id": CITY, "name": "Уфа"}]
NOW = datetime(2026, 11, 14, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("message", "day"),
    [
        ("давай что нибудь на вечер", 14),
        ("сегодня вечером", 14),
        ("завтра вечером", 15),
        ("2026-11-20 вечером", 20),
    ],
)
def test_evening_is_a_date_window_not_a_title(message: str, day: int) -> None:
    filters, question = parse_request(
        message,
        {"filters": {"city_id": CITY, "query": "вечер", "price_max": "1000"}},
        CITIES,
        NOW,
        "Asia/Yekaterinburg",
    )
    assert question is None and filters.query is None
    assert filters.date_from == datetime(2026, 11, day, 18, tzinfo=ZoneInfo("Asia/Yekaterinburg"))
    assert filters.date_to == datetime(2026, 11, day + 1, tzinfo=ZoneInfo("Asia/Yekaterinburg"))
    assert str(filters.city_id) == CITY and filters.price_max == 1000


def test_evening_preserves_selected_date_and_quoted_title() -> None:
    filters, _ = parse_request(
        "на вечер",
        {
            "filters": {
                "city_id": CITY,
                "date_from": "2026-11-18T00:00:00+05:00",
                "date_to": "2026-11-19T00:00:00+05:00",
            }
        },
        CITIES,
        NOW,
        "Asia/Yekaterinburg",
    )
    assert filters.date_from and filters.date_from.day == 18 and filters.date_from.hour == 18
    title, _ = parse_request(
        "Уфа найди «Зимний вечер с классикой»", {}, CITIES, NOW, "Asia/Yekaterinburg"
    )
    assert title.query == "Зимний вечер с классикой" and title.date_from is None


def test_evening_over_several_days_asks_for_a_day() -> None:
    filters, question = parse_request(
        "Уфа на выходных вечером", {}, CITIES, NOW, "Asia/Yekaterinburg"
    )
    assert question == "evening_date"
    answer, question = parse_request(
        "завтра",
        {"filters": filters.model_dump(mode="json"), "question": question},
        CITIES,
        NOW,
        "Asia/Yekaterinburg",
    )
    assert question is None
    assert answer.date_from and answer.date_from.day == 15 and answer.date_from.hour == 18


async def test_evening_tool_ignores_model_title_and_excludes_other_days(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    app, owner, _, city = await setup_dialogue(client, admin_headers)
    tomorrow = (datetime.now(ZoneInfo("Asia/Yekaterinburg")) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    events = []
    for index, (title, day, hour) in enumerate(
        [
            ("Утренняя программа", 0, 10),
            ("Музыкальная программа", 0, 19),
            ("Зимний вечер с классикой", 20, 19),
        ]
    ):
        start = tomorrow + timedelta(days=day, hours=hour)
        events.append(
            event_payload(
                external_id=f"evening-{index}",
                title=title,
                city="Уфа",
                date_start=start.isoformat(),
                date_end=(start + timedelta(hours=1)).isoformat(),
                source_url=f"https://example.org/test-only/evening-{index}",
            )
        )
    report = await upload(client, admin_headers, events)
    for row in report["rows"]:
        await publish(client, admin_headers, row["event_id"])
    filters, _ = parse_request(
        "давай что нибудь завтра на вечер",
        {"filters": {"city_id": city}},
        [{"id": city, "name": "Уфа"}],
        datetime.now(UTC),
        "Asia/Yekaterinburg",
    )
    async with app.state.sessions() as session, session.begin():
        result = await execute_tool(
            session,
            owner,
            "search_events",
            json.dumps({"query": "вечер"}),
            "давай что нибудь завтра на вечер",
            SearchFilters.model_validate(filters),
        )
    assert [item["title"] for item in result["items"]] == ["Музыкальная программа"]
    assert "query" not in result["applied_filters"]
