"""Load the bundled public snapshot into an empty catalog, without inventing fresh verification."""

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog.repository import lock_catalog
from app.catalog.schemas import StatusChange
from app.catalog.service import change_status
from app.common.clock import utcnow
from app.config import get_settings
from app.ingestion.service import import_records
from app.storage.postgres.engine import build_database
from app.storage.postgres.tables import Event, Venue


async def bootstrap(
    sessions: async_sessionmaker[AsyncSession], directory: Path = Path("data/open")
) -> dict[str, Any]:
    async with sessions() as session, session.begin():
        await lock_catalog(session)
        existing = await session.scalar(select(func.count()).select_from(Event))
        if existing:
            return {"status": "skipped", "reason": "catalog_not_empty", "events": existing}
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        published = 0
        rejected = 0
        for source in manifest["sources"]:
            source_id = source["source_id"]
            if Path(source_id).name != source_id:
                raise ValueError("Invalid snapshot source ID")
            content = (directory / (source_id + ".json")).read_bytes()
            rows = json.loads(content)
            result = await import_records(
                session, source_id, rows, hashlib.sha256(content).hexdigest()
            )
            rejected += result["counts"]["rejected"]
            for row in result["rows"]:
                if row["event_id"] is None:
                    continue
                event = await session.get(Event, row["event_id"])
                if (
                    event
                    and event.status == "draft"
                    and row["result"] == "added"
                    and event.date_start > utcnow()
                ):
                    await change_status(
                        session,
                        event.id,
                        "published",
                        StatusChange(
                            version=event.version,
                            reason="Начальный сохранённый снимок открытой афиши; "
                            + manifest["created_at"],
                        ),
                        actor="bootstrap:snapshot",
                    )
                    published += 1
                if event:
                    venue = await session.get(Venue, event.venue_id)
                    evidence = source["venue"]
                    if venue and evidence.get("verified_at"):
                        venue.source_url = evidence["source_url"]
                        venue.verified_at = datetime.fromisoformat(evidence["verified_at"])
        return {
            "status": "loaded",
            "published": published,
            "rejected": rejected,
            "snapshot_date": manifest["created_at"],
            "live_verification": False,
            "note": "Saved public snapshot. Background sources refresh it from official websites.",
        }


async def run() -> None:
    engine, sessions = build_database(get_settings().database_url)
    try:
        print(json.dumps(await bootstrap(sessions), ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
