"""Reproducible data-quality report and a review pack. No invented review results."""

import argparse
import csv
import hashlib
import json
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from app.catalog.schemas import EventInput
from app.ingestion.normalize import normalize_text


def build_report(directory: Path, output: Path) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    groups: dict[str, list[dict[str, Any]]] = {}
    hashes = {}
    for source in manifest["sources"]:
        path = directory / f"{source['source_id']}.json"
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        groups[source["source_id"]] = json.loads(path.read_text(encoding="utf-8"))
    rows = [(source, row) for source, records in groups.items() for row in records]
    validated = [EventInput.model_validate(row) for _, row in rows]
    keys = [
        (normalize_text(r.title), normalize_text(r.city), normalize_text(r.venue), r.date_start)
        for r in validated
    ]
    source_ids = [(source, r["external_id"]) for source, r in rows]
    dates = [r.date_start for r in validated]
    report = {
        "report_created_at": datetime.now(UTC).isoformat(),
        "dataset_created_at": manifest["created_at"],
        "parser_version": manifest["parser_version"],
        "file_sha256": hashes,
        "validated_records": len(rows),
        "source_count": len(groups),
        "duplicate_source_ids": len(source_ids) - len(set(source_ids)),
        "duplicate_session_keys": len(keys) - len(set(keys)),
        "unknown_age": sum(r.age_limit is None for r in validated),
        "unknown_price": sum(r.price_min is None for r in validated),
        "unknown_price_upper_bound": sum(r.price_max is None for r in validated),
        "unknown_end_time": sum(r.date_end is None for r in validated),
        "planner_complete_records": sum(
            r.date_end is not None and r.price_max is not None and r.age_limit is not None
            for r in validated
        ),
        "date_from": min(dates).isoformat() if dates else None,
        "date_to": max(dates).isoformat() if dates else None,
        "manual_accuracy_evaluation": None,
        "note": "Автоматическая проверка схемы и дублей ключа. "
        "Точность фактов требует ручной сверки; неизвестное не заполняется догадкой.",
    }
    (directory / "quality.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output.mkdir(parents=True, exist_ok=True)
    # Round-robin includes every source rather than taking 50 cards from one theatre.
    selected: list[tuple[str, dict[str, Any]]] = []
    for index in range(max((len(records) for records in groups.values()), default=0)):
        for source, records in groups.items():
            if index < len(records) and len(selected) < 50:
                selected.append(
                    (source, EventInput.model_validate(records[index]).model_dump(mode="json"))
                )
    fields = [
        "source",
        "external_id",
        "title",
        "city",
        "venue",
        "date_start",
        "date_end",
        "price_min",
        "price_max",
        "age_limit",
        "source_url",
        "review_status",
        "title_correct",
        "time_correct",
        "place_correct",
        "price_correct",
        "age_correct",
        "is_duplicate",
        "reviewer",
        "reviewed_at",
        "notes",
    ]
    csv_path = output / "manual_source_review.csv"
    previous: dict[tuple[str, str], dict[str, str]] = {}
    if csv_path.exists():
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            previous = {(row["source"], row["external_id"]): row for row in csv.DictReader(stream)}
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for source, row in selected:
            old = previous.get((source, str(row["external_id"])))
            # Completed reviews retain their exact original facts. Pending templates refresh.
            if old and old.get("review_status") != "pending":
                writer.writerow(old)
            else:
                writer.writerow(
                    {
                        **{key: row.get(key) for key in fields if key in row},
                        "source": source,
                        "review_status": "pending",
                    }
                )
    blocks = []
    for index, (source, row) in enumerate(selected, 1):
        values = "".join(
            f"<dt>{escape(key)}</dt><dd>{escape(str(row.get(key, 'неизвестно')))}</dd>"
            for key in (
                "title",
                "city",
                "venue",
                "address",
                "date_start",
                "date_end",
                "duration_min",
                "price_min",
                "price_max",
                "age_limit",
            )
        )
        blocks.append(
            f"<article><h2>{index}. {escape(source)}</h2><dl>{values}</dl>"
            f"<a href='{escape(row['source_url'], quote=True)}' rel='noreferrer'>"
            "Открыть источник для сверки</a></article>"
        )
    html = (
        "<!doctype html><html lang='ru'><meta charset='utf-8'><title>Сверка 50 карточек</title>"
        "<style>body{font:16px system-ui;max-width:900px;margin:2rem auto;padding:1rem}"
        "article{border:1px solid #bbb;padding:1rem;margin:1rem 0}"
        "dt{font-weight:bold}dd{margin-bottom:.5rem}</style>"
        "<h1>Ручная сверка источников</h1><p>Результаты ещё не заполнены. "
        "Проверяйте факты по ссылке, отмечайте неизвестное отдельно от ошибки. "
        "Заполните CSV; не подменяйте ручную проверку автоматической.</p>"
        + "".join(blocks)
        + "</html>"
    )
    (output / "review.html").write_text(html, encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Качество открытого снимка и набор для ручной сверки"
    )
    parser.add_argument("--data", type=Path, default=Path("data/open"))
    parser.add_argument("--output", type=Path, default=Path("reports/sources"))
    args = parser.parse_args()
    print(json.dumps(build_report(args.data, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
