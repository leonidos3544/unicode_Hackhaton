"""Small live application check. Creates an isolated anonymous dialogue, never confirms actions."""

import argparse
import asyncio
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx


async def check(base: str) -> dict[str, object]:
    results = []
    async with httpx.AsyncClient(base_url=base, timeout=60) as client:
        response = await client.post("/api/v1/sessions")
        response.raise_for_status()
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        response = await client.post("/api/v1/conversations")
        response.raise_for_status()
        conversation_id = response.json()["id"]
        for index, message in enumerate(("Концерты в Уфе", "дешевле", "подпиши на концерты в Уфе")):
            payload = {"message": message, "client_request_id": "live-check-" + str(uuid4())}
            started = perf_counter()
            response = await client.post(
                f"/api/v1/conversations/{conversation_id}/messages", json=payload
            )
            response.raise_for_status()
            answer = response.json()
            result = {
                "message": message,
                "mode": answer["mode"],
                "reason": answer["fallback_reason"],
                "question": answer["question"],
                "items": len(answer["items"]),
                "actions": len(answer["pending_actions"]),
                "filters": answer["filters"],
                "cost": answer["cost"],
                "latency_ms": round((perf_counter() - started) * 1000),
                "trace_id": answer["trace_id"],
            }
            if index == 0:
                replay = await client.post(
                    f"/api/v1/conversations/{conversation_id}/messages", json=payload
                )
                result["replay_identical"] = replay.json() == answer
            results.append(result)
            print(json.dumps(result, ensure_ascii=True), flush=True)
        history = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
    checks = {
        "real_agent": all(item["mode"] == "agent" for item in results),
        "replay": results[0]["replay_identical"],
        "cheaper_not_title": "query" not in results[1]["filters"],
        "cheaper_sort": results[1]["filters"].get("sort") == "price_asc",
        "cheaper_same_city": results[1]["filters"].get("city_id")
        == results[0]["filters"].get("city_id"),
        "cheaper_same_venue": results[1]["filters"].get("venue_id")
        == results[0]["filters"].get("venue_id"),
        "cheaper_preserves_results": results[0]["items"] == 0 or results[1]["items"] > 0,
        "subscription_proposal": results[2]["actions"] == 1,
        "history": len(history["turns"]) == 3,
    }
    return {
        "kind": "small_live_smoke_not_full_evaluation",
        "base": base,
        "checks": checks,
        "passed": all(checks.values()),
        "results": results,
        "cost_rub": sum(item["cost"]["rub"] for item in results),
        "actions_confirmed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8081")
    parser.add_argument("--output", type=Path, default=Path("reports/cloudru_smoke.json"))
    args = parser.parse_args()
    report = asyncio.run(check(args.base))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"passed": report["passed"], "checks": report["checks"], "cost_rub": report["cost_rub"]}
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
