from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import update

from app.ingestion.sources.collector import Collection
from app.ingestion.sources.http import PageSnapshot
from app.ingestion.sources.parsers import wordpress_candidates
from app.ingestion.sources.service import apply_collection
from app.storage.postgres.tables import SourceRecord
from tests.test_source_parsers import article_html, source_config


def collected(*ids: int) -> Collection:
    config = source_config()
    start = (datetime.now(UTC) + timedelta(days=10)).replace(second=0, microsecond=0)
    listing = "".join(article_html(start, post_id=item_id) for item_id in ids)
    candidates = wordpress_candidates(listing, config.listing_url, config)
    urls = [
        config.venue_source_url,
        config.listing_url,
        *[candidate.detail_url for candidate in candidates],
    ]
    snapshots = {url: PageSnapshot(url, listing, "a" * 64, datetime.now(UTC), 200) for url in urls}
    return Collection(
        config,
        candidates=candidates,
        snapshots=snapshots,
        found=len(candidates),
        venue_verified=True,
    )


async def persist(client: AsyncClient, collection: Collection) -> dict[str, object]:
    app = client._transport.app  # type: ignore[attr-defined]
    async with app.state.sessions() as session, session.begin():
        return await apply_collection(session, collection, publish_verified=True)


async def test_verified_collection_publishes_and_keeps_provenance(client: AsyncClient) -> None:
    report = await persist(client, collected(1, 2))
    assert report["published"] == 2
    listing = (await client.get("/api/v1/events")).json()
    assert listing["pagination"]["total"] == 2
    card = listing["items"][0]
    assert card["source_type"] == "page"
    assert card["verified_at"] is not None
    assert card["sources"][0]["listing_url"] == "https://example.org/afisha/"
    venues = (await client.get("/api/v1/venues")).json()
    assert venues[0]["source_url"] == "https://example.org/contacts/"


async def test_not_seen_disappears_without_claiming_cancellation(client: AsyncClient) -> None:
    await persist(client, collected(1, 2))
    original = (await client.get("/api/v1/events")).json()["items"]
    report = await persist(client, collected(2))
    assert report["no_longer_listed"] == 1
    assert (await client.get("/api/v1/events")).json()["pagination"]["total"] == 1
    old = await client.get(f"/api/v1/events/{original[0]['id']}")
    assert old.status_code == 200
    assert old.json()["status"] != "cancelled"


async def test_failed_collection_does_not_erase_cached_events(client: AsyncClient) -> None:
    await persist(client, collected(1, 2))
    partial = collected(2)
    partial.errors.append({"url": "https://example.org/events/1/", "message": "Сетевая ошибка"})
    report = await persist(client, partial)
    assert report["no_longer_listed"] == 0
    assert (await client.get("/api/v1/events")).json()["pagination"]["total"] == 2


async def test_expired_verification_is_not_recommended(client: AsyncClient) -> None:
    await persist(client, collected(1))
    app = client._transport.app  # type: ignore[attr-defined]
    async with app.state.sessions() as session, session.begin():
        await session.execute(
            update(SourceRecord).values(verified_at=datetime.now(UTC) - timedelta(hours=40))
        )
    assert (await client.get("/api/v1/events")).json()["items"] == []


async def test_explicit_source_cancellation_is_kept_on_next_refresh(client: AsyncClient) -> None:
    first = collected(1)
    await persist(client, first)
    card = (await client.get("/api/v1/events")).json()["items"][0]
    cancellation = collected(1)
    cancellation.candidates[0].cancelled = True
    await persist(client, cancellation)
    assert (await client.get(f"/api/v1/events/{card['id']}")).json()["status"] == "cancelled"
    await persist(client, collected(1))
    assert (await client.get("/api/v1/events")).json()["items"] == []
