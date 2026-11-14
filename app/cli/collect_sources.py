import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.ingestion.sources.collector import collect_source
from app.ingestion.sources.config import load_sources
from app.ingestion.sources.service import apply_collection
from app.storage.postgres.engine import build_database

logger = logging.getLogger("bashculture.sources")


async def collect_all(args: argparse.Namespace) -> None:
    settings = get_settings()
    sources = load_sources(args.config)
    if args.source:
        sources = [source for source in sources if source.id in args.source]
        if not sources:
            raise ValueError("Нет выбранных включённых источников")
    engine, sessions = build_database(settings.database_url)
    try:
        while True:
            summaries = []
            manifest: list[dict[str, Any]] = []
            for config in sources:
                logger.info("collection_started source=%s", config.id)
                collection = await collect_source(config)
                async with sessions() as session, session.begin():
                    summary = await apply_collection(
                        session,
                        collection,
                        publish_verified=args.publish_verified,
                    )
                summaries.append(summary)
                logger.info(
                    "collection_finished source=%s verified=%s errors=%s",
                    config.id,
                    len(collection.candidates),
                    len(collection.errors),
                )
                if args.export:
                    args.export.mkdir(parents=True, exist_ok=True)
                    (args.export / f"{config.id}.json").write_text(
                        json.dumps(
                            [candidate.payload for candidate in collection.candidates],
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    manifest.append(
                        {
                            "source_id": config.id,
                            "name": config.name,
                            "venue": {
                                "name": config.venue,
                                "city": config.city,
                                "address": config.address,
                                "source_url": config.venue_source_url,
                                "verified": collection.venue_verified,
                                "verified_at": collection.snapshots[
                                    config.venue_source_url
                                ].fetched_at.isoformat()
                                if collection.venue_verified
                                else None,
                            },
                            "event_count": len(collection.candidates),
                            "pages": [
                                {
                                    "url": page.url,
                                    "sha256": page.content_hash,
                                    "fetched_at": page.fetched_at.isoformat(),
                                }
                                for page in collection.snapshots.values()
                            ],
                            "errors": collection.errors,
                        }
                    )
            if args.export:
                (args.export / "places.json").write_text(
                    json.dumps(
                        [entry["venue"] for entry in manifest if entry["venue"]["verified"]],
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (args.export / "manifest.json").write_text(
                    json.dumps(
                        {
                            "created_at": datetime.now(UTC).isoformat(),
                            "parser_version": "official-html-v2",
                            "sources": manifest,
                            "note": "Открытые афиши: снимок на дату проверки. "
                            "Будущая актуальность не гарантируется.",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            print(json.dumps({"sources": summaries}, ensure_ascii=False, indent=2), flush=True)
            if not args.watch:
                break
            await asyncio.sleep(args.interval)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сбор событий и площадок из открытых официальных афиш"
    )
    parser.add_argument("--config", type=Path, default=Path("config/sources.json"))
    parser.add_argument("--source", action="append", help="ID источника, можно повторить")
    parser.add_argument("--publish-verified", action="store_true")
    parser.add_argument(
        "--export", type=Path, help="Каталог снимка нормализованных событий и manifest"
    )
    parser.add_argument("--watch", action="store_true", help="Периодически повторять сбор")
    parser.add_argument("--interval", type=int, default=21600)
    args = parser.parse_args()
    if args.interval < 3600:
        parser.error("Интервал полного сбора должен быть не меньше часа")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(collect_all(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
