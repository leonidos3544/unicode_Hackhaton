import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from fastapi.encoders import jsonable_encoder

from app.common.errors import AppError
from app.config import get_settings
from app.ingestion.files import parse_file
from app.ingestion.service import import_records
from app.storage.postgres.engine import build_database


async def import_file(path: Path, source: str) -> None:
    settings = get_settings()
    with path.open("rb") as stream:
        data = stream.read(settings.max_import_bytes + 1)
    if len(data) > settings.max_import_bytes:
        raise AppError("file_too_large", "Файл превышает допустимый размер", 413)
    rows = parse_file(data, path.name, settings.max_import_rows)
    engine, sessions = build_database(settings.database_url)
    try:
        async with sessions() as session, session.begin():
            result = await import_records(session, source, rows, hashlib.sha256(data).hexdigest())
        print(json.dumps(jsonable_encoder(result), ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Импорт реальных мероприятий в черновики")
    parser.add_argument("path", type=Path)
    parser.add_argument("--source", required=True, help="Стабильное название источника")
    args = parser.parse_args()
    if not args.source.strip() or len(args.source) > 200:
        parser.error("Название источника должно содержать от 1 до 200 символов")
    try:
        asyncio.run(import_file(args.path, args.source.strip()))
    except AppError as exc:
        parser.exit(1, f"{exc.message}\n")


if __name__ == "__main__":
    main()
