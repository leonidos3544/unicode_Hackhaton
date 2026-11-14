"""Attach explicit synthetic assumptions to existing events, never overwrite source facts."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.catalog.assumptions import NOTICE, make_assumptions
from app.catalog.repository import event_card, lock_catalog
from app.common.clock import utcnow
from app.config import get_settings
from app.storage.postgres.engine import build_database
from app.storage.postgres.runtime_tables import EventAssumption
from app.storage.postgres.tables import AuditLog, Event


async def enrich() -> dict[str, Any]:
    engine, sessions = build_database(get_settings().database_url)
    try:
        async with sessions() as session, session.begin():
            await lock_catalog(session)
            events = (
                await session.scalars(
                    select(Event)
                    .where(
                        Event.status == "published",
                        Event.merged_into_id.is_(None),
                        Event.date_start > utcnow(),
                    )
                    .order_by(Event.date_start)
                )
            ).all()
            records = []
            changed = 0
            for event in events:
                previous_changed = changed
                card = await event_card(session, event)
                values = make_assumptions(card)
                existing = await session.get(EventAssumption, event.id)
                if not values:
                    continue
                if existing is None:
                    session.add(
                        EventAssumption(
                            event_id=event.id, event_version=event.version, values=values
                        )
                    )
                    changed += 1
                elif existing.event_version != event.version or existing.values != values:
                    existing.event_version, existing.values, existing.created_at = (
                        event.version,
                        values,
                        utcnow(),
                    )
                    changed += 1
                records.append(
                    {
                        "event_id": str(event.id),
                        "title": event.title,
                        "event_version": event.version,
                        "kind": "synthetic_demo",
                        "values": values,
                    }
                )
                if changed != previous_changed:
                    session.add(
                        AuditLog(
                            actor="demo-cli",
                            action="demo.assumptions_attached",
                            event_id=event.id,
                            changes={
                                "values": values,
                                "notice": NOTICE,
                                "canonical_facts_changed": False,
                            },
                        )
                    )
            return {
                "created_at": utcnow().isoformat(),
                "notice": NOTICE,
                "events_changed": changed,
                "events_with_assumptions": len(records),
                "canonical_facts_changed": False,
                "rules": {
                    "unknown_price_min_rub": 500,
                    "unknown_price_max_rub": "max(1000, known minimum * 2)",
                    "unknown_age_limit": 18,
                    "unknown_duration_min": {"theatre": 150, "concert": 120, "other": 90},
                },
                "records": records,
            }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Помеченные демодопущения к существующим мероприятиям"
    )
    parser.add_argument("--output", type=Path, default=Path("data/demo/assumptions.json"))
    args = parser.parse_args()
    report = asyncio.run(enrich())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("events_changed", "events_with_assumptions", "canonical_facts_changed")
            }
        )
    )


if __name__ == "__main__":
    main()
