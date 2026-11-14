from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.catalog.assumptions import NOTICE, apply_assumptions, make_assumptions
from app.catalog.repository import event_card
from app.storage.postgres.runtime_tables import EventAssumption
from app.storage.postgres.tables import Event
from tests.test_bot_delivery import seed


def test_assumptions_fill_only_missing_fields_and_label_every_value():
    original = {
        "id": "test",
        "version": 2,
        "category": "theatre",
        "price_min": 700,
        "price_max": None,
        "age_limit": 12,
        "date_start": "2030-01-01T12:00:00+05:00",
        "date_end": None,
        "duration_min": None,
        "data_warnings": [],
    }
    values = make_assumptions(original)
    assert values == {"price_max": "1400.00", "duration_min": 150}
    card = {**original, "demo_assumptions": {"event_version": 2, "values": values}}
    result = apply_assumptions(card)
    assert result["price_min"] == 700 and result["age_limit"] == 12
    assert result["price_max"] == 1400 and result["date_end"] == "2030-01-01T14:30:00+05:00"
    assert "price_max" in result["demo_fields"] and NOTICE in result["data_warnings"]
    assert original["price_max"] is None and original["date_end"] is None
    stale = apply_assumptions({**card, "version": 3})
    assert stale["price_max"] is None and "demo_fields" not in stale


def test_newly_confirmed_values_always_override_demo_assumptions():
    card = {
        "version": 2,
        "price_min": 100,
        "price_max": 200,
        "age_limit": 6,
        "date_start": "2030-01-01T10:00:00+05:00",
        "date_end": "2030-01-01T11:00:00+05:00",
        "demo_assumptions": {
            "event_version": 2,
            "values": {"price_max": "1000", "age_limit": 18, "duration_min": 150},
        },
    }
    result = apply_assumptions(card)
    assert result["price_max"] == 200 and result["age_limit"] == 6
    assert result["date_end"] == card["date_end"] and "demo_fields" not in result


async def test_demo_plan_is_opt_in_and_public_facts_stay_unknown(client, admin_headers):
    start = (datetime.now(UTC) + timedelta(days=5)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    cards = []
    for n in range(2):
        cards.append(
            await seed(
                client,
                admin_headers,
                external_id=f"demo-{n}",
                title=f"Демо сеанс {n}",
                date_start=(start + timedelta(hours=3 * n)).isoformat(),
                date_end=None,
                price_min=None,
                price_max=None,
                age_limit=None,
            )
        )
    sessions = client._transport.app.state.sessions
    async with sessions() as session, session.begin():
        for card in cards:
            event = await session.get(Event, card["id"])
            dto = await event_card(session, event)
            session.add(
                EventAssumption(
                    event_id=event.id, event_version=event.version, values=make_assumptions(dto)
                )
            )
    public = (await client.get(f"/api/v1/events/{cards[0]['id']}")).json()
    assert (
        public["price_max"] is None and public["age_limit"] is None and public["date_end"] is None
    )
    assert public["demo_assumptions"]["kind"] == "synthetic_demo"
    filtered = (await client.post("/api/v1/search", json={"price_max": 5000})).json()
    assert not filtered["items"]
    payload = {
        "city_id": cards[0]["city_id"],
        "date_from": start.isoformat(),
        "date_to": (start + timedelta(hours=10)).isoformat(),
        "budget": 4000,
        "adults": 2,
    }
    strict = (await client.post("/api/v1/plans/day", json=payload)).json()
    assert strict["kind"] == "no_plan" and strict["demo_mode"] is False
    demo = (await client.post("/api/v1/plans/day", json={**payload, "demo_mode": True})).json()
    assert demo["kind"] == "plans" and demo["demo_mode"] is True
    assert demo["assumptions_notice"] == NOTICE
    assert demo["plans"][0]["total_cost"] == 4000
    assert all(item["demo_fields"] for item in demo["plans"][0]["events"])
    async with sessions() as session:
        events = (await session.scalars(select(Event))).all()
        assert all(event.price_max_minor is None and event.age_limit is None for event in events)
    family = (
        await client.post(
            "/api/v1/plans/day", json={**payload, "demo_mode": True, "children_ages": [8]}
        )
    ).json()
    assert family["kind"] == "no_plan"
