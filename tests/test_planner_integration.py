from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.bot.main import process_update
from app.config import Settings
from app.storage.postgres.runtime_tables import BotUpdate
from tests.test_bot_delivery import seed, update


async def setup_plan(client, admin_headers):
    start = (datetime.now(UTC) + timedelta(days=10)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    first = await seed(
        client,
        admin_headers,
        date_start=start.isoformat(),
        date_end=(start + timedelta(hours=1)).isoformat(),
        price_min="200",
        price_max="200",
    )
    await seed(
        client,
        admin_headers,
        external_id="second",
        title="Второе событие",
        date_start=(start + timedelta(hours=2)).isoformat(),
        date_end=(start + timedelta(hours=3)).isoformat(),
        price_min="300",
        price_max="300",
    )
    return first, {
        "city_id": first["city_id"],
        "date_from": start.isoformat(),
        "date_to": (start + timedelta(hours=10)).isoformat(),
        "budget": 1500,
        "adults": 2,
        "children_ages": [8],
    }


async def test_api_plan_cost_age_and_checked_budget_relaxation(client, admin_headers):
    card, payload = await setup_plan(client, admin_headers)
    response = await client.post("/api/v1/plans/day", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["kind"] == "plans"
    assert data["plans"][0]["total_cost"] == 1500
    assert data["plans"][0]["travel_minutes"] == [0]
    response = await client.post("/api/v1/plans/day", json={**payload, "budget": 1499})
    data = response.json()
    assert data["kind"] == "no_plan"
    assert data["suggestions"][0]["field"] == "budget"
    assert data["suggestions"][0]["value"] == 1500
    response = await client.post("/api/v1/plans/day", json={**payload, "children_ages": [0]})
    assert response.json()["kind"] == "no_plan"
    assert not response.json()["suggestions"]


async def test_bot_plan_without_model_or_web_session(client, admin_headers):
    card, payload = await setup_plan(client, admin_headers)
    sessions = client._transport.app.state.sessions
    settings = Settings()
    await process_update(sessions, update(1, "city:" + card["city_id"], callback=True), settings)
    await process_update(sessions, update(2, "/plan"), settings)
    start = datetime.fromisoformat(payload["date_from"]).astimezone(ZoneInfo("Asia/Yekaterinburg"))
    text = f"{start:%Y-%m-%d}; 13:00; 23:00; 1500; 2; 8; walk; 30"
    await process_update(sessions, update(3, text), settings)
    async with sessions() as session:
        saved = await session.get(BotUpdate, 3)
        responses = [reply["payload"].get("text", "") for reply in saved.replies]
        assert "План построен" in responses[0]
        assert any("1500" in value for value in responses)


async def test_plan_diagnoses_missing_end_and_price(client, admin_headers):
    start = (datetime.now(UTC) + timedelta(days=10)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    card = await seed(
        client,
        admin_headers,
        date_start=start.isoformat(),
        date_end=None,
        price_min=None,
        price_max=None,
    )
    response = await client.post(
        "/api/v1/plans/day",
        json={
            "city_id": card["city_id"],
            "date_from": start.isoformat(),
            "date_to": (start + timedelta(hours=10)).isoformat(),
            "budget": 500,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["kind"] == "no_plan" and data["missing_data"]
    assert any("окончания" in note for note in data["missing_data"])
    assert any("цены" in note for note in data["missing_data"])
