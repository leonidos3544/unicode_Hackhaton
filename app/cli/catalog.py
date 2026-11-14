"""Local administrator CLI for catalogue review; no web UI or model required."""

import argparse
import asyncio
import json
from typing import Any
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from app.catalog.moderation import accept_candidate
from app.catalog.schemas import StatusChange
from app.catalog.service import change_status
from app.common.errors import AppError
from app.config import get_settings
from app.storage.postgres.engine import build_database
from app.storage.postgres.tables import AuditLog, Event, ModerationItem, RawRecord


async def run(args: argparse.Namespace) -> None:
    engine, sessions = build_database(get_settings().database_url)
    try:
        async with sessions() as session, session.begin():
            result: object
            if args.command == "list":
                events = (
                    await session.scalars(
                        select(Event)
                        .where(Event.status == args.status)
                        .order_by(Event.date_start)
                        .limit(args.limit)
                    )
                ).all()
                result = [
                    {
                        "id": event.id,
                        "title": event.title,
                        "date_start": event.date_start,
                        "version": event.version,
                        "status": event.status,
                    }
                    for event in events
                ]
            elif args.command == "moderation":
                pending_items = (
                    await session.scalars(
                        select(ModerationItem).where(ModerationItem.status == "pending").limit(100)
                    )
                ).all()
                rows: list[dict[str, Any]] = []
                for pending in pending_items:
                    event = await session.get(Event, pending.event_id)
                    raw = await session.get(RawRecord, pending.raw_record_id)
                    rows.append(
                        {
                            "id": pending.id,
                            "event_id": pending.event_id,
                            "version": event.version if event else None,
                            "reason": pending.reason,
                            "candidate": raw.payload if raw else None,
                        }
                    )
                result = rows
            elif args.command == "accept":
                result = await accept_candidate(session, args.id, args.version, args.reason)
            elif args.command == "reject":
                item = await session.get(ModerationItem, args.id, with_for_update=True)
                if item is None or item.status != "pending":
                    raise AppError("not_pending", "Нет ожидающей записи с этим ID", 409)
                item.status = "rejected"
                session.add(
                    AuditLog(
                        actor="cli",
                        action="moderation.rejected",
                        event_id=item.event_id,
                        changes={"item_id": str(item.id), "reason": args.reason},
                    )
                )
                result = {"status": "rejected"}
            else:
                result = await change_status(
                    session,
                    args.id,
                    "published" if args.command == "publish" else "cancelled",
                    StatusChange(version=args.version, reason=args.reason),
                    actor="cli",
                )
        print(json.dumps(jsonable_encoder(result), ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальная модерация, публикация и отмена")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=["draft", "published", "cancelled"], default="draft")
    listing.add_argument("--limit", type=int, choices=range(1, 101), default=20)
    commands.add_parser("moderation")
    for name in ("accept", "reject", "publish", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("id", type=UUID)
        command.add_argument("--reason", required=True)
        if name != "reject":
            command.add_argument("--version", type=int, required=True)
    args = parser.parse_args()
    if hasattr(args, "reason") and len(args.reason.strip()) < 3:
        parser.error("Нужна причина минимум из трёх символов")
    try:
        asyncio.run(run(args))
    except AppError as exc:
        parser.exit(1, exc.message + "\n")


if __name__ == "__main__":
    main()
