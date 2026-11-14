from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.ingestion.sources.parsers import parse_duration
from app.planner.service import PlanInput, route_minutes, solve_chains

START = datetime(2030, 1, 1, 9, tzinfo=UTC)
CITY = uuid4()


def request(**extra):
    return PlanInput.model_validate(
        {
            "city_id": CITY,
            "date_from": START,
            "date_to": START + timedelta(hours=12),
            "budget": 1500,
            "adults": 2,
            **extra,
        }
    )


def card(index, *, venue="a", price=200, hour=None, **extra):
    start = START + timedelta(hours=hour if hour is not None else index * 2)
    return {
        "id": str(index),
        "city_id": str(CITY),
        "venue_id": venue,
        "date_start": start.isoformat(),
        "date_end": (start + timedelta(hours=1)).isoformat(),
        "price_max": price,
        "age_limit": 6,
        "version": 1,
        "source_url": "https://example.org/test-only",
        **extra,
    }


def test_group_cost_and_four_event_limit():
    cards = [card(i) for i in range(6)]
    plans = solve_chains(cards, request(budget=1600), {})
    assert len(plans[0]["events"]) == 4
    assert plans[0]["total_cost"] == 1600
    assert len(plans[0]["evidence"]) == 4
    assert all(p["total_cost"] <= 1600 for p in plans)
    assert not solve_chains(cards, request(budget=799), {})


def test_age_unknown_price_overlap_and_status():
    cards = [
        card(0),
        card(1, date_end=None),
        card(2, price=None),
        card(3, age_limit=18),
        card(4, status="cancelled"),
        card(5, hour=0),
    ]
    assert not solve_chains(cards, request(children_ages=[8]), {})
    cards.append(card(6, hour=2))
    plans = solve_chains(cards, request(children_ages=[8]), {})
    assert plans and len(plans[0]["events"]) == 2
    assert plans[0]["total_cost"] == 1200


def test_missing_routes_are_not_zero_and_travel_fits_gap():
    cards = [card(0), card(1, venue="b", hour=1)]
    assert not solve_chains(cards, request(), {})
    assert not solve_chains(cards, request(), {("a", "b"): 1})
    cards[1] = card(1, venue="b", hour=2)
    assert not solve_chains(cards, request(max_travel_min=10), {("a", "b"): 20})
    assert solve_chains(cards, request(), {("a", "b"): 20})[0]["travel_minutes"] == [20]


def test_solver_reports_limits():
    stats = {}
    solve_chains([card(i) for i in range(6)], request(), {}, max_states=1, stats=stats)
    assert stats == {"states_examined": 1, "solver_limited": True}


@pytest.mark.parametrize(
    "extra",
    [
        {"adults": 0},
        {"children_ages": [18]},
        {"date_to": START},
        {"budget": Decimal("-1")},
        {"min_events": 4, "max_events": 2},
    ],
)
def test_invalid_plan_inputs(extra):
    with pytest.raises(ValidationError):
        request(**extra)


@pytest.mark.parametrize(
    "text,minutes",
    [
        ("Продолжительность 2 часа 30 минут", 150),
        ("Длительность: 45 минут", 45),
        ("Продолжительность спектакля — 1 час", 60),
        ("Продолжительность спектакля с двумя антрактами - 2 часа 45 минут", 165),
        ("Продолжительность: 2 ч. 15 мин.", 135),
        ("Начало 18:00, два действия", None),
        ("Продолжительность 1 час. Продолжительность 2 часа", None),
        ("Продолжительность уточняется", None),
    ],
)
def test_duration_requires_explicit_unambiguous_fact(text, minutes):
    assert parse_duration(text) == minutes


async def test_route_adapter_validates_unknown_and_mode():
    a, b = card(0, lat=54.7, lon=55.9), card(1, venue="b", lat=54.8, lon=56.0)
    settings = Settings(route_api_url="https://routing.example.org")
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"code": "Ok", "routes": [{"duration": 121}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        assert await route_minutes(client, settings, a, b, "car") == 3
        assert await route_minutes(client, settings, a, b, "walk") is None
        assert await route_minutes(client, settings, a, {**b, "lat": None}, "car") is None
        assert len(calls) == 1
