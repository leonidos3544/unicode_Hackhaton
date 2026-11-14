import asyncio
import json
from typing import Any

from httpx import AsyncClient

from tests.factories import event_payload


async def upload(
    client: AsyncClient, headers: dict[str, str], rows: list[dict[str, Any]], source: str = "test"
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/admin/imports",
        headers=headers,
        data={"source_name": source},
        files={"file": ("events.json", json.dumps(rows).encode())},
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def publish(client: AsyncClient, headers: dict[str, str], event_id: str) -> dict[str, Any]:
    event = (await client.get(f"/api/v1/admin/events/{event_id}", headers=headers)).json()
    response = await client.post(
        f"/api/v1/admin/events/{event_id}/publish",
        headers=headers,
        json={"version": event["version"], "reason": "Проверено в тесте"},
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


async def test_import_search_cancel_and_etag(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    report = await upload(client, admin_headers, [event_payload()])
    event_id = report["rows"][0]["event_id"]
    assert report["counts"]["added"] == 1
    assert (await client.get(f"/api/v1/events/{event_id}")).status_code == 404
    await publish(client, admin_headers, event_id)
    listing = await client.get("/api/v1/events", params={"query": "выставка", "age": 8})
    assert listing.json()["pagination"]["total"] == 1
    response = await client.get(f"/api/v1/events/{event_id}")
    card = response.json()
    assert card["price_min"] == 300.5
    assert card["verified_at"] is None
    etag = response.headers["etag"]
    assert (
        await client.get(f"/api/v1/events/{event_id}", headers={"If-None-Match": etag})
    ).status_code == 304
    cancelled = await client.post(
        f"/api/v1/admin/events/{event_id}/cancel",
        headers=admin_headers,
        json={"version": card["version"], "reason": "Тестовая отмена"},
    )
    assert cancelled.status_code == 200
    assert (await client.get("/api/v1/events")).json()["items"] == []
    response = await client.get(f"/api/v1/events/{event_id}", headers={"If-None-Match": etag})
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.headers["etag"] != etag


async def test_repeat_and_100_rows(client: AsyncClient, admin_headers: dict[str, str]) -> None:
    rows = [event_payload(external_id=str(i), title=f"Тест {i}") for i in range(100)]
    first = await upload(client, admin_headers, rows)
    assert first["counts"]["added"] == 100
    second = await upload(client, admin_headers, rows)
    assert second["counts"]["unchanged"] == 100
    assert second["counts"]["added"] == 0


async def test_invalid_row_does_not_lose_good_row(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    report = await upload(client, admin_headers, [event_payload(), event_payload(title="")])
    assert report["counts"]["added"] == report["counts"]["rejected"] == 1
    assert report["rows"][1]["errors"][0]["field"] == "title"


async def test_multisource_dedup_and_conflict(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    payload = event_payload()
    report = await upload(client, admin_headers, [payload], "source-a")
    event_id = report["rows"][0]["event_id"]
    second = await upload(
        client, admin_headers, [{**payload, "source_url": "https://example.net/e"}], "source-b"
    )
    assert second["rows"][0]["event_id"] == event_id
    card = await publish(client, admin_headers, event_id)
    assert len(card["sources"]) == 2
    conflict = await upload(client, admin_headers, [{**payload, "price_max": 999}], "source-b")
    assert conflict["counts"]["moderation"] == 1
    public = (await client.get(f"/api/v1/events/{event_id}")).json()
    assert public["price_max"] == 500.5
    queue = (await client.get("/api/v1/admin/moderation", headers=admin_headers)).json()
    assert len(queue) == 1


async def test_changed_source_requires_review_and_preserves_cancellation(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    payload = event_payload()
    report = await upload(client, admin_headers, [payload])
    event_id = report["rows"][0]["event_id"]
    await publish(client, admin_headers, event_id)
    changed = {**payload, "price_max": 700}
    report = await upload(client, admin_headers, [changed])
    assert report["counts"]["updated"] == 1
    assert (await client.get(f"/api/v1/events/{event_id}")).status_code == 404
    card = await publish(client, admin_headers, event_id)
    await client.post(
        f"/api/v1/admin/events/{event_id}/cancel",
        headers=admin_headers,
        json={"version": card["version"], "reason": "Отменено"},
    )
    await upload(client, admin_headers, [{**changed, "price_max": 800}])
    assert (await client.get(f"/api/v1/events/{event_id}")).json()["status"] == "cancelled"


async def test_unknown_values_do_not_pass_strict_filters(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    report = await upload(
        client,
        admin_headers,
        [event_payload(age_limit=None, price_min=None, price_max=None, date_end=None)],
    )
    await publish(client, admin_headers, report["rows"][0]["event_id"])
    assert len((await client.get("/api/v1/events")).json()["items"]) == 1
    for filters in (
        {"price_max": 1000},
        {"age": 8},
        {"is_free": True},
        {"end_before": "2099-01-01T20:00:00+05:00"},
    ):
        assert (await client.get("/api/v1/events", params=filters)).json()["items"] == []


async def test_from_price_does_not_guarantee_budget(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    report = await upload(client, admin_headers, [event_payload(price_min=300, price_max=None)])
    await publish(client, admin_headers, report["rows"][0]["event_id"])
    assert (await client.get("/api/v1/events", params={"price_max": 1000})).json()["items"] == []


async def test_concurrent_import_is_idempotent(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    payload = event_payload()
    results = await asyncio.gather(*[upload(client, admin_headers, [payload]) for _ in range(3)])
    assert sum(report["counts"]["added"] for report in results) == 1
    assert sum(report["counts"]["unchanged"] for report in results) == 2


async def test_auth_validation_openapi_and_health(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    assert (await client.get("/api/v1/admin/events")).status_code == 401
    assert (await client.get("/api/v1/admin/events", headers=admin_headers)).status_code == 200
    error = await client.get("/api/v1/events", params={"age": -1})
    assert error.status_code == 422
    assert error.json()["error"]["code"] == "validation_error"
    assert error.json()["request_id"] == error.headers["x-request-id"]
    assert (await client.get("/health/ready")).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert schema["openapi"].startswith("3.1")
    assert "EventCard" in schema["components"]["schemas"]
    assert (await client.get("/metrics")).status_code == 200
