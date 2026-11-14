"""Enrich a completed local acceptance report with stored traces, without paid calls."""

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path
from statistics import mean
from types import SimpleNamespace
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.storage.postgres.runtime_tables import AgentTrace
from scripts.agent_snapshot import trace_snapshot


async def report(
    path: Path, base: str, cases_path: Path, snapshot_path: Path | None = None
) -> None:
    data = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    if data.get("partial"):
        raise ValueError("Only completed reports may be summarized")
    cases = {
        case["id"]: case
        for case in json.loads(await asyncio.to_thread(cases_path.read_text, encoding="utf-8"))[
            "cases"
        ]
    }
    engine = create_async_engine(get_settings().database_url)
    tool_checks = []
    prompt_versions, models = set(), set()
    async with async_sessionmaker(engine)() as session:
        for case in data["results"]:
            for turn in case["turns"]:
                if "trace_id" not in turn:
                    continue
                trace = await session.get(AgentTrace, UUID(turn["trace_id"]))
                if trace is None and turn.get("trace"):
                    trace = SimpleNamespace(**turn["trace"])
                if trace is None:
                    raise ValueError("A recorded trace is missing")
                prompt_versions.add(trace.prompt_version)
                models.add(trace.model_id or "disabled")
                turn["trace"] = trace_snapshot(trace)
                turn["tools"] = [
                    {"name": call["tool"], "status": call.get("status")} for call in trace.calls
                ]
                turn["usage"] = {
                    "input_tokens": trace.input_tokens,
                    "output_tokens": trace.output_tokens,
                    "known": trace.usage_known,
                }
                expected = cases[case["id"]]["turns"][turn["index"]]
                if (
                    isinstance(expected.get("question"), str)
                    and turn["question"] != expected["question"]
                    and "wrong_clarification" not in turn["errors"]
                ):
                    turn["errors"].append("wrong_clarification")
                if expected.get("tools_any_of"):
                    success = any(
                        call["tool"] in expected["tools_any_of"] and call.get("status") == "ok"
                        for call in trace.calls
                    )
                    if data.get("baseline"):
                        success = (
                            "search_events" in expected["tools_any_of"] and not turn["question"]
                        )
                    turn["expected_tool_succeeded"] = success
                    tool_checks.append(success)
                    if not success and "expected_tool_not_used" not in turn["errors"]:
                        turn["errors"].append("expected_tool_not_used")
            case["passed"] = case["complete"] and all(not turn["errors"] for turn in case["turns"])
    data["cases_passed"] = sum(case["passed"] for case in data["results"])
    await engine.dispose()
    turns = [turn for case in data["results"] for turn in case["turns"]]
    paid = [turn for turn in turns if turn.get("cost", {}).get("rub", 0) > 0]
    errors = Counter(error for turn in turns for error in turn["errors"])
    data["prompt_versions"] = sorted(prompt_versions)
    data["models"] = sorted(models)
    data["summary"] = {
        "turns": len(turns),
        "paid_turns": len(paid),
        "turns_without_behavior_errors": sum(
            not any(not error.startswith("model_fallback:") for error in turn["errors"])
            for turn in turns
        ),
        "error_counts": dict(errors),
        "model_fallback_turns": sum(turn.get("mode") == "fallback" for turn in turns),
        "expected_tool_success": sum(tool_checks),
        "expected_tool_checks": len(tool_checks),
        "tool_calls_ok": sum(
            call["status"] == "ok" for turn in turns for call in turn.get("tools", [])
        ),
        "tool_calls_total": sum(len(turn.get("tools", [])) for turn in turns),
        "usage_input_tokens": sum(turn.get("usage", {}).get("input_tokens", 0) for turn in turns),
        "usage_output_tokens": sum(turn.get("usage", {}).get("output_tokens", 0) for turn in turns),
        "estimated_cost_turns": sum(turn.get("cost", {}).get("estimated", False) for turn in turns),
        "known_usage_cost_rub": sum(
            turn["cost"]["rub"] for turn in paid if not turn["cost"]["estimated"]
        ),
        "conservative_cost_rub": sum(
            turn["cost"]["rub"] for turn in paid if turn["cost"]["estimated"]
        ),
        "mean_accounted_cost_per_case_rub": mean(
            sum(turn.get("cost", {}).get("rub", 0) for turn in case["turns"])
            for case in data["results"]
        ),
    }
    snapshot = (
        json.loads(await asyncio.to_thread(snapshot_path.read_text, encoding="utf-8"))
        if snapshot_path
        else []
    )
    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        page = 1
        while snapshot_path is None:
            response = await client.get("/api/v1/events", params={"page": page, "page_size": 50})
            response.raise_for_status()
            batch = response.json()
            snapshot.extend(batch["items"])
            if not batch["pagination"]["has_more"]:
                break
            page += 1
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    if digest == data["catalog_sha256"]:
        catalog_path = path.with_suffix(".catalog.json")
        await asyncio.to_thread(
            catalog_path.write_text,
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest_path = Path("evals") / (
            "agent_dataset_manifest.json"
            if cases_path.name == "agent_cases.json" and not data.get("baseline")
            else (path.stem if data.get("baseline") else cases_path.stem) + "_dataset_manifest.json"
        )
        await asyncio.to_thread(
            manifest_path.write_text,
            json.dumps(
                {
                    "kind": "public_catalog_snapshot_application_acceptance",
                    "reference_time": data["reference_time"],
                    "sha256": digest,
                    "events": len(snapshot),
                    "path": catalog_path.as_posix(),
                    "external_ground_truth": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    data["snapshot_still_matches"] = digest == data["catalog_sha256"]
    data["limits"][0] = (
        "Tool selection additionally checked from stored traces on explicit tool cases."
    )
    await asyncio.to_thread(
        path.write_text, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# Обычный поиск без модели" if data.get("baseline") else "# Проверка агента с Cloud.ru",
        "",
        f"Время начала: {data['reference_time']}. Модель: `{', '.join(data['models'])}`. "
        f"Промпт: `{', '.join(data['prompt_versions'])}`.",
        "",
        f"Пройдено полностью: **{data['cases_passed']} из {data['cases_run']} кейсов**; "
        f"{len(turns)} реплик. Происхождение: "
        f"{data.get('case_scope', 'внутренние сценарии разработчика')}.",
        "",
        "| Группа | Пройдено | Всего |",
        "| --- | ---: | ---: |",
    ]
    for group in data["groups"]:
        selected = [case for case in data["results"] if case["group"] == group]
        lines.append(f"| {group} | {sum(case['passed'] for case in selected)} | {len(selected)} |")
    summary = data["summary"]
    lines += [
        "",
        f"Средняя задержка: {data['latency_mean_ms']} мс; "
        f"p95: {data['latency_p95_ms']} мс. Учитывается весь HTTP-ход, включая fallback.",
        "Измерение через ASGI внутри процесса; сетевой задержки клиента нет."
        if data.get("baseline") or data.get("frozen_time_and_catalog")
        else "Измерение выполнено через локальный HTTP-сайт.",
        "Время и каталог зафиксированы, служебные таймеры работают по реальным часам."
        if data.get("frozen_time_and_catalog")
        else "Время не заморожено; относительные даты зависят от момента запуска.",
        "",
        f"Учтённый расход: {data['cost_rub']:.4f} ₽; "
        f"из него по известному usage {summary['known_usage_cost_rub']:.4f} ₽, "
        f"консервативный резерв при неизвестном результате "
        f"{summary['conservative_cost_rub']:.4f} ₽. "
        "Это расчёт приложения, не сверка со списаниями Cloud.ru.",
        "",
        f"Успешные инструменты: {summary['tool_calls_ok']} / {summary['tool_calls_total']} "
        f"вызовов. В специальных сценариях выбора инструмента: "
        f"{summary['expected_tool_success']} / {summary['expected_tool_checks']}.",
        "",
        "Ошибки по репликам:",
        "",
    ]
    lines += [f"- `{name}`: {count}" for name, count in errors.items()] or ["- Не обнаружены."]
    lines += [
        "",
        "Карточки сравниваются со снимком публичного каталога. "
        "Структурированный поиск по ожидаемым фильтрам используется как эталон выдачи; "
        "это не независимый поиск по ключевым словам и не проверка истинности источников.",
        "",
        "Ни одна подписка и ни одно обращение при этом прогоне не подтверждались. "
        + (
            f"{data['groups']['attack']} атак проверяют заранее известные случаи и серверные "
            "полномочия; это не гарантия защиты от произвольных инъекций."
            if data["groups"].get("attack")
            else "Атаки в этот дополнительный прогон не включены."
        ),
        "",
        "Остаются неизвестные заранее внешние кейсы "
        "и отдельная сверка карточек с источниками. JSON содержит все результаты, "
        "трассы, условия, расходы и ограничения.",
    ]
    await asyncio.to_thread(
        path.with_suffix(".md").write_text, "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(json.dumps(data["summary"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--base", default="http://localhost:8081")
    parser.add_argument("--cases", type=Path, default=Path("evals/agent_cases.json"))
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    asyncio.run(report(args.path, args.base, args.cases, args.snapshot))
