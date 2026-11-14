"""Paid, bounded live acceptance via the public API; never confirms actions."""

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean
from time import perf_counter, time
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from dotenv import dotenv_values


@asynccontextmanager
async def application_client(args):
    if not args.baseline and not args.frozen_snapshot:
        async with httpx.AsyncClient(base_url=args.base, timeout=90) as client:
            yield client, None
        return
    # A separate in-process app reads the same catalog. The running site's settings
    # are never changed and the baseline cannot issue a paid completion.
    from pydantic import SecretStr

    from app.api.app import create_app
    from app.common.clock import at_reference_time
    from app.config import get_settings

    settings = get_settings()
    if args.baseline:
        settings = settings.model_copy(
            update={
                "llm_api_url": "",
                "llm_model": "",
                "llm_api_key": SecretStr(""),
            }
        )
    if args.frozen_snapshot:
        from sqlalchemy import text
        from sqlalchemy.engine import make_url

        from scripts.agent_snapshot import EVALUATION_DATABASE, restore_snapshot

        if make_url(settings.database_url).database != EVALUATION_DATABASE:
            raise ValueError("Frozen runs require the dedicated bashculture_agent_eval database")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        if args.frozen_snapshot:
            cards = json.loads(args.frozen_snapshot.read_text(encoding="utf-8"))
            with at_reference_time(datetime.fromisoformat(args.reference_time)):
                async with app.state.engine.connect() as guard:
                    await guard.execute(text("SELECT pg_advisory_lock(71014000)"))
                    await guard.commit()
                    try:
                        await restore_snapshot(app.state.sessions, settings.database_url, cards)
                        async with httpx.AsyncClient(
                            base_url=args.base, timeout=90, transport=httpx.ASGITransport(app=app)
                        ) as client:
                            yield client, app.state.sessions
                    finally:
                        await guard.execute(text("SELECT pg_advisory_unlock(71014000)"))
                        await guard.commit()
            return
        async with httpx.AsyncClient(
            base_url=args.base, timeout=90, transport=httpx.ASGITransport(app=app)
        ) as client:
            yield client, app.state.sessions


async def post(client, path, **kwargs):
    # Respect the application's minute quotas, retaining the same idempotency key.
    for _ in range(3):
        response = await client.post(path, **kwargs)
        if response.status_code != 429:
            return response
        seconds = 60 - time() % 60 + 0.1
        print(json.dumps({"rate_limit_wait_seconds": round(seconds, 1)}), flush=True)
        await asyncio.sleep(seconds)
    return response


def expand(value, replacements):
    if isinstance(value, str):
        for key, replacement in replacements.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, dict):
        return {key: expand(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, replacements) for item in value]
    return value


async def evaluate(args):
    dataset = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    if args.only:
        cases = [case for case in cases if case["id"] in args.only.split(",")]
    now = (
        datetime.fromisoformat(args.reference_time)
        if args.frozen_snapshot
        else datetime.now(ZoneInfo("Asia/Yekaterinburg"))
    )
    now = now.astimezone(ZoneInfo("Asia/Yekaterinburg"))
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    results, snapshot = [], []
    spent = 0.0
    async with application_client(args) as (client, trace_sessions):
        for _ in range(30):
            ready = await client.get("/health/ready")
            if ready.status_code == 200:
                break
            await asyncio.sleep(1)
        ready.raise_for_status()
        page = 1
        while True:
            response = await client.get("/api/v1/events", params={"page": page, "page_size": 50})
            response.raise_for_status()
            content = response.json()
            snapshot.extend(content["items"])
            if not content["pagination"]["has_more"]:
                break
            page += 1
        if not snapshot:
            raise RuntimeError("The catalog must contain current events")
        cities = {card["city"]: card["city_id"] for card in snapshot}
        reference = next(card for card in snapshot if card["city"] == "Уфа")
        replacements = {
            "event_id": reference["id"],
            "venue_id": reference["venue_id"],
            "tomorrow": tomorrow.isoformat(),
            "after_tomorrow": (tomorrow + timedelta(days=1)).isoformat(),
        }
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for label, weekday in (("friday", 4), ("saturday", 5)):
            day = midnight + timedelta(days=(weekday - now.weekday()) % 7)
            replacements[label] = day.isoformat()
            replacements["after_" + label] = (day + timedelta(days=1)).isoformat()
            replacements[label + "_19"] = day.replace(hour=19).isoformat()
        replacements["month_end"] = (
            midnight.replace(year=now.year + 1, month=1, day=1)
            if now.month == 12
            else midnight.replace(month=now.month + 1, day=1)
        ).isoformat()
        replacements["after_weekend"] = (
            datetime.fromisoformat(replacements["saturday"]) + timedelta(days=2)
        ).isoformat()
        by_id = {card["id"]: card for card in snapshot}
        args.output.with_suffix(".catalog.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        response = await client.post("/api/v1/sessions")
        response.raise_for_status()
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        for original in cases:
            case = expand(original, replacements)
            response = await post(client, "/api/v1/conversations")
            response.raise_for_status()
            conversation_id = response.json()["id"]
            turns, seen = [], set()
            for index, expected in enumerate(case["turns"]):
                # Each paid turn reserves up to one ruble in the configured application.
                if spent + args.turn_reserve_rub > args.max_rub:
                    break
                started = perf_counter()
                response = await post(
                    client,
                    f"/api/v1/conversations/{conversation_id}/messages",
                    json={"message": expected["message"], "client_request_id": str(uuid4())},
                )
                latency = round((perf_counter() - started) * 1000)
                if response.status_code != 200:
                    turns.append({"index": index, "errors": [f"http_{response.status_code}"]})
                    continue
                answer = response.json()
                recorded_trace = None
                if trace_sessions is not None:
                    from uuid import UUID

                    from app.storage.postgres.runtime_tables import AgentTrace
                    from scripts.agent_snapshot import trace_snapshot

                    async with trace_sessions() as trace_session:
                        stored_trace = await trace_session.get(AgentTrace, UUID(answer["trace_id"]))
                        if stored_trace is None:
                            raise RuntimeError("Completed answer has no stored trace")
                        recorded_trace = trace_snapshot(stored_trace)
                spent += answer["cost"]["rub"]
                actual = answer["filters"]
                errors = []
                question = expected.get("question")
                if question:
                    if not answer["question"]:
                        errors.append("missing_question")
                    elif isinstance(question, str) and answer["question"] != question:
                        errors.append("wrong_clarification")
                    if answer["pending_actions"] or answer["items"]:
                        errors.append("results_before_clarification")
                elif answer["question"]:
                    errors.append("unexpected_question")
                gold = dict(expected.get("filters", {}))
                city = gold.pop("city", None)
                if city:
                    gold["city_id"] = cities[city]
                for key, value in gold.items():
                    candidate = actual.get(key)
                    if key in {"date_from", "date_to"} and candidate:
                        same = datetime.fromisoformat(candidate) == datetime.fromisoformat(value)
                    elif key == "price_max" and candidate is not None:
                        same = Decimal(str(candidate)) == Decimal(str(value))
                    else:
                        same = candidate == value
                    if not same:
                        errors.append(f"filter:{key}")
                if expected.get("no_query") and actual.get("query"):
                    errors.append("unexpected_title_query")
                ids = [card["id"] for card in answer["items"]]
                if expected.get("required_id") and expected["required_id"] not in ids:
                    errors.append("missing_requested_event")
                if expected.get("excluded_id") in ids:
                    errors.append("reference_event_recommended")
                if "allowed_ids" in expected and not set(ids) <= set(expected["allowed_ids"]):
                    errors.append("outside_allowed_ids")
                if expected.get("no_repeat") and seen.intersection(ids):
                    errors.append("repeated_items")
                if expected.get("empty") and ids:
                    errors.append("expected_empty")
                actions = [action["action_type"] for action in answer["pending_actions"]]
                if expected.get("action") and expected["action"] not in actions:
                    errors.append("missing_action_proposal")
                if expected.get("no_actions") and actions:
                    errors.append("unexpected_action")
                for card in answer["items"]:
                    saved = by_id.get(card["id"])
                    if not saved:
                        errors.append("unknown_event")
                        continue
                    fields = (
                        "title",
                        "city_id",
                        "venue_id",
                        "category",
                        "price_min",
                        "price_max",
                        "date_start",
                        "date_end",
                        "status",
                        "source_url",
                    )

                    def equal_field(key, card=card, saved=saved):
                        left, right = card.get(key), saved.get(key)
                        if key in {"date_start", "date_end"} and left and right:
                            return datetime.fromisoformat(left) == datetime.fromisoformat(right)
                        return left == right

                    if any(not equal_field(key) for key in fields):
                        errors.append("card_changed_since_snapshot")
                baseline_count = None
                # The ordinary search is the baseline, with independently specified gold filters.
                if gold and not question and not expected.get("action"):
                    baseline = await client.post("/api/v1/search", json={**gold, "page_size": 50})
                    baseline.raise_for_status()
                    baseline_count = baseline.json()["pagination"]["total"]
                    allowed = {card["id"] for card in baseline.json()["items"]}
                    baseline_page = 1
                    while baseline.json()["pagination"]["has_more"]:
                        baseline_page += 1
                        baseline = await client.post(
                            "/api/v1/search",
                            json={
                                **gold,
                                "page_size": 50,
                                "page": baseline_page,
                            },
                        )
                        baseline.raise_for_status()
                        allowed.update(card["id"] for card in baseline.json()["items"])
                    if not set(ids) <= allowed:
                        errors.append("violates_gold_constraints")
                    if baseline_count and not ids and not expected.get("no_repeat"):
                        errors.append("false_empty")
                if not args.baseline and not question and answer["mode"] != "agent":
                    errors.append("model_fallback:" + str(answer["fallback_reason"]))
                turns.append(
                    {
                        "index": index,
                        "message": expected["message"],
                        "errors": sorted(set(errors)),
                        "mode": answer["mode"],
                        "question": answer["question"],
                        "filters": actual,
                        "ids": ids,
                        "actions": actions,
                        "cost": answer["cost"],
                        "latency_ms": latency,
                        "trace_id": answer["trace_id"],
                        "trace": recorded_trace,
                        "baseline_count": baseline_count,
                        "answer": {
                            key: answer.get(key)
                            for key in ("message", "items", "details", "relaxations")
                        },
                    }
                )
                seen.update(ids)
            result = {
                "id": case["id"],
                "group": case["group"],
                "turns": turns,
                "complete": len(turns) == len(case["turns"]),
            }
            result["passed"] = result["complete"] and all(not t["errors"] for t in turns)
            results.append(result)
            print(
                json.dumps(
                    {
                        "id": case["id"],
                        "passed": result["passed"],
                        "errors": [t["errors"] for t in turns],
                        "spent": spent,
                    }
                ),
                flush=True,
            )
            args.output.write_text(
                json.dumps({"partial": True, "results": results}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if spent + args.turn_reserve_rub > args.max_rub:
                break
    latencies = sorted(
        t["latency_ms"] for case in results for t in case["turns"] if "latency_ms" in t
    )
    return {
        "kind": "keyword_filter_fallback_baseline"
        if args.baseline
        else "application_acceptance_not_independent_external_evaluation",
        "baseline": args.baseline,
        "frozen_time_and_catalog": bool(args.frozen_snapshot),
        "transport": "in_process" if args.baseline or args.frozen_snapshot else "http",
        "reference_time": now.isoformat(),
        "model_settings": {
            key: value
            for key, value in dotenv_values().items()
            if key
            in {
                "LLM_MODEL",
                "LLM_INPUT_RUB_PER_MILLION",
                "LLM_OUTPUT_RUB_PER_MILLION",
                "LLM_TIMEOUT_SECONDS",
            }
        },
        "cases_version": dataset["version"],
        "case_scope": dataset.get("scope", "Developer-authored application scenarios"),
        "case_provenance": dataset.get("provenance"),
        "catalog_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        "catalog_events": len(snapshot),
        "cases_requested": len(cases),
        "cases_run": len(results),
        "cases_passed": sum(case["passed"] for case in results),
        "groups": dict(Counter(case["group"] for case in results)),
        "cost_rub": spent,
        "max_rub": args.max_rub,
        "turn_reserve_rub": args.turn_reserve_rub,
        "actions_confirmed": False,
        "latency_mean_ms": round(mean(latencies)) if latencies else None,
        "latency_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        if latencies
        else None,
        "limits": [
            "Tool selection is not graded; public behavior is graded.",
            "Baseline uses the same search engine as the application.",
            "Catalog truth and source extraction require a separate external review.",
            "No subscription or feedback proposal was confirmed.",
        ],
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8081")
    parser.add_argument("--cases", type=Path, default=Path("evals/agent_cases.json"))
    parser.add_argument("--output", type=Path, default=Path("reports/agent_live.json"))
    parser.add_argument("--max-rub", type=float, default=10)
    parser.add_argument(
        "--turn-reserve-rub",
        type=float,
        default=float(dotenv_values().get("LLM_TURN_BUDGET_RUB") or "1"),
    )
    parser.add_argument("--only", default="")
    parser.add_argument("--frozen-snapshot", type=Path)
    parser.add_argument("--reference-time", default="2026-11-14T12:00:00+00:00")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Run offline keyword/filter parsing and ordinary search, no model calls",
    )
    args = parser.parse_args()
    if args.max_rub <= 0 or args.turn_reserve_rub <= 0:
        parser.error("Budgets must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(evaluate(args))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}))
    if report["cases_passed"] != report["cases_requested"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
