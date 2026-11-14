"""Read-only live filter acceptance checks against an independently evaluated catalogue snapshot."""

import argparse
import asyncio
import hashlib
import json
import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


def matches(card: dict[str, Any], filters: dict[str, Any], reference: datetime) -> bool:
    if card["status"] != "published" or datetime.fromisoformat(card["date_start"]) < reference:
        return False
    for key in ("city_id", "category", "is_free", "language"):
        if key in filters and card[key] != filters[key]:
            return False
    ages = [*filters.get("children_ages", []), *([filters["age"]] if "age" in filters else [])]
    if ages and (card["age_limit"] is None or card["age_limit"] > min(ages)):
        return False
    if "price_max" in filters:
        party = (
            filters.get("adults", 1) + len(filters.get("children_ages", []))
            if filters.get("budget_scope") == "party"
            else 1
        )
        if card["price_max"] is None or card["price_max"] * party > filters["price_max"]:
            return False
    if "date_to" in filters and datetime.fromisoformat(
        card["date_start"]
    ) >= datetime.fromisoformat(filters["date_to"]):
        return False
    if "duration_max_min" in filters and (
        card["duration_min"] is None or card["duration_min"] > filters["duration_max_min"]
    ):
        return False
    return True


async def pages(client: httpx.AsyncClient, filters: dict[str, Any]) -> list[dict[str, Any]]:
    cards = []
    for page in range(1, 201):
        response = await client.post(
            "/api/v1/search", json={**filters, "page_size": 50, "page": page}
        )
        response.raise_for_status()
        result = response.json()
        cards.extend(result["items"])
        if not result["pagination"]["has_more"]:
            return cards
    raise ValueError("Catalogue exceeds acceptance runner limit")


async def run(base_url: str, case_path: Path, output: Path) -> dict[str, Any]:
    case_bytes = await asyncio.to_thread(case_path.read_bytes)
    definition = json.loads(case_bytes.decode("utf-8"))
    reference = datetime.now(UTC)
    async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
        cities_response = await client.get("/api/v1/cities")
        cities_response.raise_for_status()
        cities = {item["name"]: item["id"] for item in cities_response.json()}
        snapshot = await pages(client, {"date_from": reference.isoformat()})
        results = []
        for case in definition["cases"]:
            filters = dict(case["filters"])
            city = filters.pop("city", None)
            days = filters.pop("days", None)
            if city and city not in cities:
                results.append(
                    {"id": case["id"], "status": "not_measured", "reason": "city_not_in_catalogue"}
                )
                continue
            if city:
                filters["city_id"] = cities[city]
            if days:
                filters["date_to"] = (reference + timedelta(days=days)).isoformat()
            filters["date_from"] = reference.isoformat()
            expected = {card["id"] for card in snapshot if matches(card, filters, reference)}
            started = time.monotonic()
            try:
                actual = await pages(client, filters)
                actual_ids = [card["id"] for card in actual]
                got = set(actual_ids)
                results.append(
                    {
                        "id": case["id"],
                        "status": "passed"
                        if got == expected and len(got) == len(actual_ids)
                        else "failed",
                        "filters": filters,
                        "expected_ids": sorted(expected),
                        "actual_ids": actual_ids,
                        "missing_ids": sorted(expected - got),
                        "unexpected_ids": sorted(got - expected),
                        "latency_ms": round((time.monotonic() - started) * 1000, 2),
                    }
                )
            except httpx.HTTPError as exc:
                results.append(
                    {"id": case["id"], "status": "failed", "error_type": type(exc).__name__}
                )
    latencies = sorted(result["latency_ms"] for result in results if "latency_ms" in result)
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "reference_time": reference.isoformat(),
        "suite_version": definition["version"],
        "case_file_sha256": hashlib.sha256(case_bytes).hexdigest(),
        "catalogue_sha256": hashlib.sha256(
            json.dumps(snapshot, sort_keys=True).encode()
        ).hexdigest(),
        "catalogue_size": len(snapshot),
        "scope": definition["scope"],
        "model_metrics": "not_applicable: levels 2 and 3 excluded from this work",
        "source_accuracy": "not_measured: this checks filters, not source truth",
        "passed": sum(r["status"] == "passed" for r in results),
        "case_count": len(results),
        "not_measured": sum(r["status"] == "not_measured" for r in results),
        "p95_case_ms": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)]
        if latencies
        else None,
        "latency_note": "End-to-end local HTTP, all pages per case; "
        "catalogue must remain stable during run",
        "results": results,
    }
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        (output / "catalog.json").write_text,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = (
        f"# Проверка каталога\n\nДата: {report['created_at']}\n\n"
        f"Проверено: {len(results)}; успешно: {report['passed']}; "
        f"не измерено: {report['not_measured']}.\n\n"
        f"Карточек в снимке: {len(snapshot)}. "
        f"p95 полного HTTP-сценария: {report['p95_case_ms']} мс.\n\n"
        "Проверяется совпадение структурированных фильтров с независимой фильтрацией снимка. "
        "Это не оценка свободного диалога, фактической точности источников или качества модели. "
        "Полный набор уровня 4 с baseline и внешними диалогами "
        "остаётся отдельной приёмкой агента.\n"
    )
    await asyncio.to_thread((output / "catalog.md").write_text, summary, encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="60 read-only acceptance checks of real catalogue filters"
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--cases", type=Path, default=Path("evals/catalog_cases.json"))
    parser.add_argument("--output", type=Path, default=Path("reports/catalog"))
    args = parser.parse_args()
    report = asyncio.run(run(args.base_url, args.cases, args.output))
    print(
        json.dumps(
            {key: report[key] for key in ("case_count", "passed", "not_measured", "p95_case_ms")}
        )
    )
    if report["passed"] != report["case_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
