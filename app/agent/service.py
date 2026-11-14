import json
import re
import time
from typing import Any
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import budget, history, metrics
from app.agent.constants import MAX_CARDS
from app.agent.dates import requested_start_time
from app.agent.parsing import INJECTION, parse_request
from app.agent.policy import PROMPT_VERSION, SYSTEM_PROMPT, available_tools, mask
from app.agent.presentation import explain, present
from app.agent.provider import HttpProvider, Provider, failure_reason
from app.agent.tools import MODELS, execute_tool, schemas
from app.catalog.service import get_event
from app.common.clock import utcnow
from app.common.errors import AppError
from app.config import Settings
from app.ingestion.normalize import content_hash
from app.search.models import SearchFilters
from app.search.service import search_events
from app.storage.postgres.runtime_tables import AgentTrace, DialogueTurn
from app.storage.postgres.tables import City


async def respond(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    owner: str,
    conversation_id: UUID,
    client_key: str,
    message: str,
    provider: Provider | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    async with sessions() as session, session.begin():
        conversation, turn, lease = await history.claim(
            session, conversation_id, owner, client_key, message
        )
        if lease is None:
            assert turn.answer is not None
            return turn.answer
        turn_id, reference_time = turn.id, turn.reference_time
        state = dict(conversation.state)
        if message.lower().strip() in {"сброс", "новый поиск", "/cancel"}:
            state = {}
        previous = await history.recent(session, conversation_id)
        cities = [
            {"id": str(city.id), "name": city.name}
            for city in (await session.scalars(select(City))).all()
        ]
        dialogue_cost = conversation.total_cost
        channel = conversation.channel
    calls: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    relaxations: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    proposed: dict[str, dict[str, Any]] = {}
    filters = SearchFilters(page_size=MAX_CARDS)
    mode, reason, question = "agent", None, None
    reserved, cost = 0.0, 0.0
    input_tokens, output_tokens = 0, 0
    cost_estimated = False
    usage_known = True
    grounded = False
    start_time = None
    try:
        try:
            filters, question = parse_request(
                message, state, cities, reference_time, settings.event_timezone
            )
            start_time = requested_start_time(message, state)
        except (ValueError, ValidationError):
            question = "Проверьте дату, возраст и бюджет. Укажите условия ещё раз."
        reference_id = re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", message, re.I)
        if re.search(r"похож", message, re.I) and reference_id:
            try:
                async with sessions() as session:
                    reference = await get_event(session, UUID(reference_id[0]))
                    filters = filters.model_copy(
                        update={
                            "city_id": filters.city_id or reference.city_id,
                            "category": filters.category or reference.category,
                            "exclude_ids": list(
                                dict.fromkeys(
                                    [
                                        UUID(reference_id[0]),
                                        *filters.exclude_ids,
                                    ]
                                )
                            ),
                        }
                    )
                    if question == "city":
                        question = None
            except AppError:
                question = "Не удалось найти исходное событие. Уточните карточку для сравнения."
        if INJECTION.search(message):
            question = "Могу помочь с поиском культурных событий. Укажите город и условия."
        if channel == "telegram":
            filters = filters.model_copy(update={"page_size": 3})
        configured = bool(settings.llm_api_url and settings.llm_model) or provider is not None
        if (
            settings.llm_require_api_key
            and not settings.llm_api_key.get_secret_value()
            and provider is None
        ):
            configured = False
        if not configured:
            mode, reason = "fallback", "model_not_configured"
        if not question and configured:
            try:
                async with sessions() as session, session.begin():
                    reserved = await budget.reserve(
                        session, owner, reference_time.date(), dialogue_cost, settings
                    )
            except AppError as exc:
                mode, reason = "fallback", exc.code
            if mode == "agent":
                model = provider or HttpProvider(settings)
                allowed_tools = available_tools(message)
                tool_schemas = schemas(allowed_tools)
                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "reference_time": reference_time.isoformat(),
                                "timezone": settings.event_timezone,
                                "channel": channel,
                                "cities": cities,
                                "filters": filters.model_dump(mode="json", exclude_none=True),
                                "history": [
                                    {
                                        "message": item["message"],
                                        "answer": (item["answer"] or {}).get("message"),
                                        "items": [
                                            {
                                                key: card.get(key)
                                                for key in (
                                                    "id",
                                                    "title",
                                                    "venue_id",
                                                    "city_id",
                                                    "category",
                                                )
                                            }
                                            for card in (item["answer"] or {}).get("items", [])[:6]
                                        ],
                                    }
                                    for item in previous[-6:]
                                ],
                                "message": message,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
                for _ in range(6):
                    encoded = json.dumps(
                        {"messages": messages, "tools": tool_schemas}, ensure_ascii=False
                    )
                    if len(encoded.encode()) > 36_000:
                        reason = "context_limit"
                        break
                    maximum = (
                        len(encoded.encode()) * settings.llm_input_rub_per_million
                        + settings.llm_max_output_tokens * settings.llm_output_rub_per_million
                    ) / 1_000_000
                    if not settings.llm_free and cost + maximum > reserved:
                        reason = "turn_budget"
                        break
                    try:
                        reply = await model.complete(messages, tool_schemas)
                    except Exception as exc:
                        # A timed-out call may have been billed; retain the entire reservation.
                        cost, cost_estimated = reserved, True
                        usage_known = False
                        reason = failure_reason(exc)
                        raise
                    usage_known = (
                        usage_known
                        and reply.input_tokens is not None
                        and reply.output_tokens is not None
                    )
                    if not settings.llm_free:
                        if reply.input_tokens is None or reply.output_tokens is None:
                            cost, cost_estimated = reserved, True
                        else:
                            cost += (
                                reply.input_tokens * settings.llm_input_rub_per_million
                                + reply.output_tokens * settings.llm_output_rub_per_million
                            ) / 1_000_000
                    input_tokens += reply.input_tokens or 0
                    output_tokens += reply.output_tokens or 0
                    if not reply.calls:
                        break
                    messages.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": call.id,
                                    "type": "function",
                                    "function": {"name": call.name, "arguments": call.arguments},
                                }
                                for call in reply.calls
                            ],
                        }
                    )
                    for call in reply.calls:
                        if len(calls) >= 5:
                            reason = "tool_limit"
                            break
                        try:
                            argument_model = MODELS.get(call.name)
                            traced_arguments = (
                                mask(
                                    argument_model.model_validate_json(call.arguments).model_dump(
                                        mode="json"
                                    )
                                )
                                if argument_model
                                else {}
                            )
                        except ValidationError as exc:
                            traced_arguments = {
                                "error": "invalid_arguments",
                                "fields": [
                                    {
                                        "field": ".".join(map(str, error["loc"])),
                                        "type": error["type"],
                                    }
                                    for error in exc.errors(include_input=False, include_url=False)
                                ],
                            }
                        except ValueError:
                            traced_arguments = {"error": "invalid_arguments"}
                        record: dict[str, Any] = {"tool": call.name, "arguments": traced_arguments}
                        calls.append(record)  # Invalid calls count towards the same limit.
                        try:
                            if call.name in MODELS and call.name not in allowed_tools:
                                raise AppError(
                                    "tool_not_available", "Инструмент не нужен для запроса", 422
                                )
                            action_key = content_hash(
                                {"tool": call.name, "payload": json.loads(call.arguments)}
                            )
                            if (
                                call.name in {"subscribe", "save_feedback"}
                                and action_key in proposed
                            ):
                                result = proposed[action_key]
                            else:
                                async with sessions() as session, session.begin():
                                    result = await execute_tool(
                                        session,
                                        owner,
                                        call.name,
                                        call.arguments,
                                        message,
                                        filters,
                                        channel=channel,
                                    )
                            result = dict(jsonable_encoder(result))
                            grounded = grounded or "items" in result or "detail" in result
                            if result.get("requires_confirmation"):
                                proposed[action_key] = result
                                if not any(action["id"] == result["id"] for action in actions):
                                    actions.append(result)
                            if result.get("detail") and not result.get("matches_constraints"):
                                details.append(result["detail"])
                            if result.get("applied_filters"):
                                filters = SearchFilters.model_validate(result["applied_filters"])
                            relaxations = result.get("relaxations", [])
                            if call.name in {"find_similar", "get_venue_schedule"}:
                                # Lookup cards are context, not extra recommendations in the
                                # final selection (especially the reference for "similar").
                                items = []
                            for item in result.get("items", []):
                                if all(
                                    str(existing["id"]) != str(item["id"]) for existing in items
                                ):
                                    items.append(item)
                            record["result_ids"] = [
                                str(item["id"]) for item in result.get("items", [])
                            ]
                            record["status"] = "ok"
                        except (AppError, ValueError, ValidationError) as exc:
                            result = {
                                "error": exc.code
                                if isinstance(exc, AppError)
                                else "invalid_arguments"
                            }
                            if isinstance(exc, ValidationError):
                                result["fields"] = [
                                    {
                                        "field": ".".join(map(str, error["loc"])),
                                        "type": error["type"],
                                    }
                                    for error in exc.errors(include_input=False, include_url=False)
                                ]
                                if call.name == "subscribe":
                                    result["hint"] = (
                                        "period_filter: только date_from/date_to с часовым поясом "
                                        "или {} без дат; channel: inbox либо telegram; "
                                        "category: значение из проверенных filters.category"
                                    )
                            record["status"] = result["error"]
                        model_result = dict(result)
                        if "items" in model_result:
                            model_result["items"] = [
                                {
                                    key: item.get(key)
                                    for key in (
                                        "id",
                                        "title",
                                        "version",
                                        "category",
                                        "date_start",
                                        "date_end",
                                        "city",
                                        "city_id",
                                        "venue",
                                        "venue_id",
                                        "price_min",
                                        "price_max",
                                        "age_limit",
                                        "source_url",
                                        "status",
                                        "data_warnings",
                                    )
                                }
                                for item in result["items"]
                            ]
                        model_result.pop("detail", None)
                        model_result.pop("relaxations", None)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": json.dumps(
                                    model_result, ensure_ascii=False, default=str
                                ),
                            }
                        )
                    if reason == "tool_limit" or len(calls) >= 5 or cost_estimated:
                        break
                    # Card text is rendered by the server. A successful final tool needs no
                    # second paid completion merely to paraphrase the same result.
                    chain_requested = re.search(r"похож|расписан", message, re.I)
                    chain_finished = any(
                        call["tool"] in {"find_similar", "get_venue_schedule"}
                        and call.get("status") == "ok"
                        for call in calls
                    )
                    if (grounded or actions or details) and (not chain_requested or chain_finished):
                        break
        if not question and not grounded and not actions and not details:
            # Provider failure, missing tools or exhausted quotas all retain real catalog search.
            if mode == "agent":
                mode = "fallback"
                reason = reason or (
                    "invalid_tool_arguments"
                    if any(call.get("status") == "invalid_arguments" for call in calls)
                    else "no_grounded_result"
                )
            async with sessions() as session, session.begin():
                result = await search_events(session, filters)
                items = list(jsonable_encoder(result["items"]))
                relaxations = list(jsonable_encoder(result["relaxations"]))
    except Exception:
        mode, reason = "fallback", reason or "provider_error"
        if not items and not question:
            async with sessions() as session, session.begin():
                result = await search_events(session, filters)
                items = list(jsonable_encoder(result["items"]))
                relaxations = list(jsonable_encoder(result["relaxations"]))
    items = items[:MAX_CARDS]
    for item in items:
        item["recommendation_reason"] = explain(item, filters, settings.event_timezone)
    answer = present(items, actions, question, mode, reason, [city["name"] for city in cities])
    if details and not items and not question:
        answer["message"] = (
            "Карточка найдена, но она не подходит под текущие условия. "
            "Проверьте статус и сведения ниже."
        )
    answer.update(
        filters=filters.model_dump(mode="json", exclude_none=True),
        client_request_id=client_key,
        cost={"rub": cost, "estimated": cost_estimated, "usage_known": usage_known},
        relaxations=relaxations,
        details=details,
    )
    async with sessions() as session, session.begin():
        conversation = await history.owned(session, conversation_id, owner, lock=True)
        if conversation.lease_id != lease:
            raise AppError("lease_lost", "Ход прерван. Обновите историю", 409)
        stored_turn = await session.get(DialogueTurn, turn_id)
        assert stored_turn is not None
        conversation.state = {
            "filters": filters.model_dump(mode="json", exclude_none=True),
            "question": question,
            "budget_scope_explicit": state.get("budget_scope_explicit", False)
            or any(
                word in message.lower()
                for word in ("на всех", "на двоих", "общий бюджет", "на человека")
            ),
            "needs_child_age": bool(
                state.get("needs_child_age")
                or re.search(r"реб[её]н|с детьми|для детей", message, re.I)
            )
            and not filters.children_ages
            and "без детей" not in message.lower(),
            "start_time": start_time,
            "needs_event_status": bool(
                state.get("needs_event_status") or re.search(r"отмен|перенесли", message, re.I)
            )
            and reference_id is None,
            "needs_budget": bool(state.get("needs_budget") or "недорого" in message.lower())
            and filters.price_max is None
            and not filters.is_free
            and not re.search(r"любая цена|без бюджета", message.lower()),
            "seen_ids": list(
                dict.fromkeys([*state.get("seen_ids", []), *[str(item["id"]) for item in items]])
            )[-200:],
        }
        conversation.lease_id, conversation.lease_until = None, None
        conversation.updated_at = utcnow()
        conversation.total_cost += cost
        await budget.settle(session, owner, reference_time.date(), reserved, cost)
        trace = AgentTrace(
            conversation_id=conversation_id,
            turn_id=turn_id,
            query_redacted=json.dumps(
                mask(
                    {
                        "filters": filters.model_dump(mode="json", exclude_none=True),
                        "question": question,
                        "has_action_proposal": bool(actions),
                    }
                ),
                ensure_ascii=False,
            ),
            plan=[
                "Проверить условия",
                "Получить подтверждённые данные",
                "Сформировать ответ из карточек",
            ],
            calls=mask(calls),
            evidence=[{"id": str(item["id"]), "version": item["version"]} for item in items],
            mode=mode,
            model_id=settings.llm_model or None,
            prompt_version=PROMPT_VERSION,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_rub=cost,
            cost_estimated=cost_estimated,
            usage_known=usage_known,
            latency_ms=int((time.monotonic() - started) * 1000),
            error_code=reason,
        )
        session.add(trace)
        await session.flush()
        answer["trace_id"] = str(trace.id)
        stored_turn.answer = dict(answer)
    metrics.record(mode, reason, calls, time.monotonic() - started, cost, cost_estimated)
    return answer
