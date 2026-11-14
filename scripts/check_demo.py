"""Check demo readiness without confirming actions or sending Telegram messages."""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings


async def check(base: str, with_ai: bool) -> dict:
    settings = get_settings()
    report = {
        "checked_at_moscow": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(),
        "base": base,
        "actions_confirmed": False,
        "telegram_messages_sent": False,
        "pages": [],
    }
    async with httpx.AsyncClient(base_url=base, timeout=45) as client:
        for path in ("/", "/chat", "/planner", "/personal", "/admin", "/widget", "/health/ready"):
            response = await client.get(path)
            report["pages"].append({"path": path, "status": response.status_code})
        cities = (await client.get("/api/v1/cities")).json()
        report["cities"] = cities
        ufa = next(city["id"] for city in cities if city["name"] == "Уфа")
        sterlitamak = next(city["id"] for city in cities if city["name"] == "Стерлитамак")
        response = await client.get("/api/v1/events", params={"page_size": 100})
        if response.status_code == 422:
            response = await client.get("/api/v1/events", params={"page_size": 50})
        response.raise_for_status()
        report["public_sessions"] = response.json()["pagination"]["total"]
        params = {
            "city_id": sterlitamak,
            "category": "theatre",
            "age": 12,
            "price_max": 700,
            "date_from": "2026-11-15T18:00:00+05:00",
            "date_to": "2026-11-16T00:00:00+05:00",
        }
        response = await client.get("/api/v1/events", params=params)
        response.raise_for_status()
        report["demo_filters"] = params
        report["demo_cards"] = response.json()["items"]
        for name, path in (
            ("moderation", "/moderation"),
            ("imports", "/imports"),
            ("source_runs", "/source-runs"),
        ):
            response = await client.get(
                "/api/v1/admin" + path,
                headers={
                    "Authorization": "Bearer " + settings.admin_api_token.get_secret_value(),
                },
            )
            data = response.json()
            report["admin_" + name] = {
                "status": response.status_code,
                "rows": len(data) if isinstance(data, list) else None,
            }
        plan = {
            "city_id": ufa,
            "date_from": "2026-11-15T10:00:00+05:00",
            "date_to": "2026-11-15T23:00:00+05:00",
            "budget": 3000,
            "adults": 1,
            "children_ages": [],
            "travel_mode": "walk",
            "max_travel_min": 30,
        }
        report["planner"] = []
        for demo in (False, True):
            response = await client.post("/api/v1/plans/day", json={**plan, "demo_mode": demo})
            response.raise_for_status()
            answer = response.json()
            report["planner"].append(
                {
                    "demo_mode": demo,
                    "message": answer["message"],
                    "missing_data": answer["missing_data"],
                    "plans": [
                        {
                            "total_cost": item["total_cost"],
                            "events": [
                                {
                                    key: event.get(key)
                                    for key in ("title", "date_start", "demo_fields")
                                }
                                for event in item["events"]
                            ],
                        }
                        for item in answer["plans"]
                    ],
                }
            )
        if report["demo_cards"]:
            source = report["demo_cards"][0]["source_url"]
            try:
                response = await client.get(source, follow_redirects=True, timeout=15)
                report["primary_source"] = {"url": source, "status": response.status_code}
            except httpx.HTTPError as error:
                report["primary_source"] = {"url": source, "error_type": type(error).__name__}
        if with_ai:
            response = await client.post("/api/v1/sessions")
            response.raise_for_status()
            client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            response = await client.post("/api/v1/conversations")
            response.raise_for_status()
            conversation = response.json()["id"]
            report["chat"] = []
            for message in (
                "Спектакли в Стерлитамаке",
                "15 ноября 2026 вечером",
                "Пойдем с ребёнком",
                "12 лет",
                "До 700 рублей на человека",
            ):
                started = perf_counter()
                response = await client.post(
                    f"/api/v1/conversations/{conversation}/messages",
                    json={
                        "message": message,
                        "client_request_id": "demo-check-" + str(uuid4()),
                    },
                )
                response.raise_for_status()
                answer = response.json()
                result = {
                    key: answer.get(key)
                    for key in ("message", "question", "mode", "fallback_reason", "filters", "cost")
                }
                result.update(
                    input=message,
                    latency_ms=round((perf_counter() - started) * 1000),
                    items=[
                        {
                            key: card.get(key)
                            for key in (
                                "title",
                                "date_start",
                                "price_max",
                                "age_limit",
                                "source_url",
                            )
                        }
                        for card in answer["items"]
                    ],
                )
                report["chat"].append(result)
                print(
                    json.dumps(
                        {
                            "input": message,
                            "mode": answer["mode"],
                            "question": answer["question"],
                            "items": len(answer["items"]),
                            "latency_ms": result["latency_ms"],
                        },
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
            turns = report["chat"]
            report["chat_checks"] = {
                "initial_selection": bool(turns[0]["items"]),
                "evening_selection": bool(turns[1]["items"]),
                "asks_child_age": turns[2]["question"] == "child_age",
                "accepts_child_age": turns[3]["filters"].get("children_ages") == [12],
                "price_filter": float(turns[4]["filters"].get("price_max", -1)) == 700,
                "final_selection": bool(turns[4]["items"]),
                "real_agent_on_search_turns": all(
                    turns[index]["mode"] == "agent" for index in (0, 1, 3, 4)
                ),
            }
            report["chat_cost_rub"] = round(sum(turn["cost"]["rub"] for turn in turns), 6)
        token = settings.telegram_bot_token.get_secret_value()
        if token:
            try:
                response = await client.post("https://api.telegram.org/bot" + token + "/getMe")
                me = response.json()
                response = await client.post(
                    "https://api.telegram.org/bot" + token + "/getMyCommands"
                )
                commands = response.json()
                report["telegram"] = {
                    "authenticated": me.get("ok", False),
                    "username": (me.get("result") or {}).get("username"),
                    "chat_command_registered": any(
                        item["command"] == "chat" for item in commands.get("result", [])
                    ),
                    "receipt_checked": False,
                }
            except (httpx.HTTPError, ValueError) as error:
                report["telegram"] = {"error_type": type(error).__name__}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8081")
    parser.add_argument("--with-ai", action="store_true", help="Make five bounded model chat turns")
    parser.add_argument("--output", type=Path, default=Path("reports/demo/preflight.json"))
    args = parser.parse_args()
    report = asyncio.run(check(args.base, args.with_ai))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(args.output),
                "public_sessions": report["public_sessions"],
                "chat_checks": report.get("chat_checks"),
                "telegram": report.get("telegram"),
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
