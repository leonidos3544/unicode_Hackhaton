import csv
import io
import json
from typing import Any

from app.common.errors import AppError


def reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def parse_file(data: bytes, filename: str, max_rows: int) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AppError("invalid_encoding", "Сохраните файл в кодировке UTF-8") from exc
    try:
        if filename.lower().endswith(".json"):
            rows = json.loads(text, parse_constant=reject_json_constant)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise AppError("invalid_file", "JSON должен содержать массив объектов")
        elif filename.lower().endswith(".csv"):
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise AppError("invalid_file", "Проверьте названия колонок CSV")
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise AppError("invalid_file", "Число значений не совпадает с колонками CSV")
                rows.append(dict(row))
                if len(rows) > max_rows:
                    raise AppError("too_many_rows", f"Разрешено не больше {max_rows} строк", 413)
        else:
            raise AppError("unsupported_format", "Загрузите файл CSV или JSON")
    except (ValueError, csv.Error, RecursionError) as exc:
        raise AppError("invalid_file", "Не удалось прочитать CSV или JSON") from exc
    if not rows:
        raise AppError("empty_file", "Файл не содержит мероприятий")
    if len(rows) > max_rows:
        raise AppError("too_many_rows", f"Разрешено не больше {max_rows} строк", 413)
    return rows
