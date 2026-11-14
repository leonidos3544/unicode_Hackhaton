from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.agent.parsing import parse_request
from app.agent.presentation import explain
from app.search.models import SearchFilters
from app.search.service import search_events
from app.storage.postgres.tables import City
from tests.test_bot_delivery import seed

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
ZONE = ZoneInfo("Asia/Yekaterinburg")
CITIES = [{"id": str(uuid4()), "name": "Уфа"}, {"id": str(uuid4()), "name": "Стерлитамак"}]


def test_pdf_friday_time_price_and_typo() -> None:
    filters, question = parse_request(
        "Концерт в Стерлитамаке в пятницу после 19:00 до 1500 рублей",
        {},
        CITIES,
        NOW,
        "Asia/Yekaterinburg",
    )
    assert question is None and filters.category == "concert" and filters.price_max == 1500
    assert filters.date_from == datetime(2026, 11, 20, 19, tzinfo=ZONE)
    assert filters.date_to == datetime(2026, 11, 21, tzinfo=ZONE)
    typo, question = parse_request(
        "Концерт в стерлитакмае в субботу", {}, CITIES, NOW, "Asia/Yekaterinburg"
    )
    assert question is None and str(typo.city_id) == CITIES[1]["id"]
    assert typo.date_from == datetime(2026, 11, 14, tzinfo=ZONE)


def test_calendar_month_end_next_week_and_group_budget() -> None:
    filters, _ = parse_request(
        "Бесплатные выставки в Уфе до конца месяца", {}, CITIES, NOW, "Asia/Yekaterinburg"
    )
    assert filters.date_to == datetime(2026, 12, 1, tzinfo=ZONE)
    assert filters.is_free is True
    week, _ = parse_request("Уфа на следующей неделе", {}, CITIES, NOW, "Asia/Yekaterinburg")
    assert week.date_from == datetime(2026, 11, 16, tzinfo=ZONE)
    assert week.date_to == datetime(2026, 11, 23, tzinfo=ZONE)
    party, question = parse_request(
        "Уфа с ребёнком 8 лет, до 1000 рублей на двоих, не дольше двух часов",
        {},
        CITIES,
        NOW,
        "Asia/Yekaterinburg",
    )
    assert question is None and party.adults == 1 and party.children_ages == [8]
    assert party.budget_scope == "party" and party.duration_max_min == 120


def test_after_time_ambiguity_is_retained_until_day_is_supplied() -> None:
    filters, question = parse_request(
        "Уфа на следующей неделе после 19:00", {}, CITIES, NOW, "Asia/Yekaterinburg"
    )
    assert question == "time_date"
    state = {
        "filters": filters.model_dump(mode="json"),
        "question": question,
        "start_time": [19, 0],
    }
    changed, question = parse_request("завтра", state, CITIES, NOW, "Asia/Yekaterinburg")
    assert question is None and changed.date_from == datetime(2026, 11, 15, 19, tzinfo=ZONE)


async def test_language_requires_metadata_and_topic_can_match_tags(client, admin_headers):
    card = await seed(client, admin_headers, city="Уфа", language="ba", tags=["джаз"])
    sessions = client._transport.app.state.sessions
    async with sessions() as session:
        result = await search_events(
            session, SearchFilters(city_id=card["city_id"], language="башкирский", query="джаз")
        )
        assert [str(item["id"]) for item in result["items"]] == [card["id"]]
        wrong = await search_events(
            session, SearchFilters(city_id=card["city_id"], language="русский")
        )
        assert wrong["items"] == []
        city = await session.scalar(select(City).where(City.id == card["city_id"]))
        assert city.name == "Уфа"


def test_reason_uses_known_fields_and_never_calls_unknown_price_free() -> None:
    card = {
        "city": "Уфа",
        "category": "theatre",
        "age_limit": 6,
        "price_max": 500,
        "date_start": "2026-11-20T14:00:00Z",
        "duration_min": 90,
    }
    filters = SearchFilters(
        category="theatre", price_max=1000, budget_scope="party", children_ages=[8]
    )
    reason = explain(card, filters, "Asia/Yekaterinburg")
    assert "1000 ₽ на всю группу" in reason and "8 лет" in reason
    unknown = explain({"city": "Уфа", "price_max": None}, SearchFilters(), "Asia/Yekaterinburg")
    assert "бесплат" not in unknown and "₽" not in unknown
