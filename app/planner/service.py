import asyncio
import math
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.assumptions import NOTICE, apply_assumptions
from app.config import Settings
from app.search.models import SearchFilters
from app.search.service import search_events


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city_id: UUID
    demo_mode: bool = False
    date_from: datetime
    date_to: datetime
    budget: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    adults: int = Field(default=1, ge=0, le=10)
    children_ages: list[int] = Field(default_factory=list, max_length=10)
    max_travel_min: int = Field(default=30, ge=0, le=180)
    travel_mode: Literal["walk", "car", "transit"] = "walk"
    min_events: int = Field(default=2, ge=2, le=4)
    max_events: int = Field(default=4, ge=2, le=4)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.date_from.tzinfo is None or self.date_to.tzinfo is None:
            raise ValueError("Укажите часовой пояс окна времени")
        if not 0 < (self.date_to - self.date_from).total_seconds() <= 86400:
            raise ValueError("Выберите окно не больше суток")
        if self.max_events < self.min_events:
            raise ValueError("Проверьте количество событий")
        if not self.adults and not self.children_ages:
            raise ValueError("Укажите состав группы")
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("Проверьте возраст детей")
        return self


def card_cost(card: dict[str, Any], party_size: int) -> int | None:
    price = card.get("price_max")
    return int(Decimal(str(price)) * 100) * party_size if price is not None else None


def solve_chains(
    cards: list[dict[str, Any]],
    request: PlanInput,
    travel: dict[tuple[str, str], int | None],
    *,
    max_states: int = 20000,
    stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    party = request.adults + len(request.children_ages)
    budget = int(request.budget * 100)
    eligible = [
        card
        for card in cards
        if card.get("status", "published") == "published"
        and str(card.get("city_id", request.city_id)) == str(request.city_id)
        and (
            not request.children_ages
            or (
                card.get("age_limit") is not None
                and card["age_limit"] <= min(request.children_ages)
            )
        )
    ]
    ordered = sorted(eligible, key=lambda card: (str(card["date_start"]), str(card["id"])))
    solutions: list[tuple[list[dict[str, Any]], int, list[int]]] = []
    stack: list[tuple[list[dict[str, Any]], int, list[int], int]] = [([], 0, [], 0)]
    visited = 0
    while stack and visited < max_states:
        chain, spent, legs, cursor = stack.pop()
        visited += 1
        if len(chain) >= request.min_events:
            solutions.append((chain, spent, legs))
            solutions.sort(key=lambda item: (-len(item[0]), item[1], sum(item[2])))
            del solutions[3:]
        if len(chain) >= request.max_events:
            continue
        for index in range(cursor, len(ordered)):
            card = ordered[index]
            cost = card_cost(card, party)
            if cost is None or spent + cost > budget or not card.get("date_end"):
                continue
            start = datetime.fromisoformat(str(card["date_start"]))
            end = datetime.fromisoformat(str(card["date_end"]))
            if start < request.date_from or end > request.date_to or end <= start:
                continue
            next_legs = legs
            if chain:
                previous = chain[-1]
                same = str(previous["venue_id"]) == str(card["venue_id"])
                duration = (
                    0 if same else travel.get((str(previous["venue_id"]), str(card["venue_id"])))
                )
                if duration is None or duration > request.max_travel_min:
                    continue
                previous_end = datetime.fromisoformat(str(previous["date_end"]))
                if (start - previous_end).total_seconds() < duration * 60:
                    continue
                next_legs = [*legs, duration]
            stack.append(([*chain, card], spent + cost, next_legs, index + 1))
    solutions.sort(key=lambda item: (-len(item[0]), item[1], sum(item[2])))
    if stats is not None:
        stats.update(states_examined=visited, solver_limited=bool(stack))
    return [
        {
            "events": chain,
            "total_cost": spent / 100,
            "travel_minutes": legs,
            "travel_is_estimate": True,
            "pricing_note": "Верхняя оценка по известным ценам на каждого посетителя; "
            "наличие билетов проверяется у продавца. Льготы не предполагаются.",
            "evidence": [
                {
                    "event_id": str(card["id"]),
                    "version": card.get("version"),
                    "source_url": card.get("source_url"),
                    "demo_fields": card.get("demo_fields", []),
                }
                for card in chain
            ],
        }
        for chain, spent, legs in solutions[:3]
    ]


async def route_minutes(
    client: httpx.AsyncClient,
    settings: Settings,
    first: dict[str, Any],
    second: dict[str, Any],
    mode: str,
) -> int | None:
    if str(first["venue_id"]) == str(second["venue_id"]):
        return 0
    if not settings.route_api_url or mode != "car":
        return None
    if any(card.get(field) is None for card in (first, second) for field in ("lat", "lon")):
        return None
    coordinates = f"{first['lon']},{first['lat']};{second['lon']},{second['lat']}"
    try:
        response = await client.get(
            settings.route_api_url.rstrip("/") + f"/route/v1/driving/{coordinates}",
            params={"overview": "false"},
            timeout=1.5,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "Ok":
            return None
        seconds = float(payload["routes"][0]["duration"])
        return math.ceil(seconds / 60) if seconds >= 0 and math.isfinite(seconds) else None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return None


async def build_day_plan(
    session: AsyncSession,
    request: PlanInput,
    settings: Settings,
) -> dict[str, Any]:
    started = time.monotonic()
    filters = SearchFilters(
        city_id=request.city_id,
        date_from=request.date_from,
        date_to=request.date_to,
        adults=request.adults,
        children_ages=[] if request.demo_mode else request.children_ages,
        page_size=50,
    )
    result = await search_events(session, filters, diagnose=False)
    cards = (
        [apply_assumptions(card) for card in result["items"]]
        if request.demo_mode
        else result["items"]
    )
    if request.demo_mode and request.children_ages:
        cards = [
            card
            for card in cards
            if card.get("age_limit") is not None and card["age_limit"] <= min(request.children_ages)
        ]
    unique = {str(card["venue_id"]): card for card in cards}
    travel: dict[tuple[str, str], int | None] = {}
    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient() as client:

        async def fetch_pair(a: str, b: str) -> None:
            async with semaphore:
                travel[(a, b)] = await route_minutes(
                    client, settings, unique[a], unique[b], request.travel_mode
                )

        pairs = [(a, b) for a in list(unique)[:8] for b in list(unique)[:8] if a != b]
        try:
            async with asyncio.timeout(2):
                await asyncio.gather(*(fetch_pair(a, b) for a, b in pairs))
        except TimeoutError:
            pass
    stats: dict[str, Any] = {}
    plans = solve_chains(cards, request, travel, stats=stats)
    missing = []
    if not cards:
        missing.append("Нет будущих сеансов с подтверждённым возрастным допуском в этом окне")
    if any(not card.get("date_end") for card in cards):
        missing.append("Не для всех событий известно время окончания")
    if cards and any(
        card_cost(card, request.adults + len(request.children_ages)) is None for card in cards
    ):
        missing.append("Не для всех событий известна верхняя граница цены")
    if len(unique) > 1 and (
        len(travel) < len(unique) * (len(unique) - 1)
        or any(value is None for value in travel.values())
    ):
        missing.append("Не все перемещения подтверждены: нужны координаты и сервис маршрутов")
    suggestions = []
    if not plans:
        # The cheapest chain of the minimum required length gives an exact sufficient
        # budget among examined candidates. Other constraints remain in effect.
        upper = sum(
            card_cost(card, request.adults + len(request.children_ages)) or 0 for card in cards
        )
        changed = request.model_copy(
            update={"budget": Decimal(upper) / 100, "max_events": request.min_events}
        )
        alternatives = solve_chains(cards, changed, travel)
        if alternatives and alternatives[0]["total_cost"] > float(request.budget):
            suggestions.append(
                {
                    "field": "budget",
                    "value": alternatives[0]["total_cost"],
                    "message": "Проверенный достаточный бюджет для найденной цепочки",
                    "example": alternatives[0],
                }
            )
        if request.min_events > 2:
            changed = request.model_copy(update={"min_events": 2})
            alternatives = solve_chains(cards, changed, travel)
            if alternatives:
                suggestions.append(
                    {
                        "field": "min_events",
                        "value": 2,
                        "message": "Можно уменьшить число событий до двух",
                        "example": alternatives[0],
                    }
                )
        durations = sorted(
            {
                value
                for value in travel.values()
                if value is not None and request.max_travel_min < value <= 180
            }
        )
        for duration in durations:
            changed = request.model_copy(update={"max_travel_min": duration})
            alternatives = solve_chains(cards, changed, travel)
            if alternatives:
                suggestions.append(
                    {
                        "field": "max_travel_min",
                        "value": duration,
                        "message": "Проверенный план с таким пределом переезда между событиями",
                        "example": alternatives[0],
                    }
                )
                break
    limited = bool(
        result["pagination"]["has_more"] or stats.get("solver_limited") or len(unique) > 8
    )
    return {
        "demo_mode": request.demo_mode,
        "assumptions_notice": NOTICE if request.demo_mode else None,
        "kind": "plans" if plans else "no_plan",
        "plans": plans,
        "message": (
            "Демонстрационный план с явно указанными допущениями"
            if request.demo_mode
            else "План построен по подтверждённым данным"
        )
        if plans
        else (
            "План не найден в проверенной части каталога. Полнота поиска ограничена."
            if limited
            else "Подтверждённый план не найден. "
            "Измените окно, бюджет или дождитесь уточнения данных"
        ),
        "missing_data": missing,
        "suggestions": suggestions,
        "candidate_count": len(cards),
        "candidates_limited": result["pagination"]["has_more"],
        "search_complete": not limited,
        "solver": stats,
        "travel_scope": "между событиями; путь от дома и обратно не включён",
        "same_venue_transfer": "0 минут — допущение внутри одной площадки; "
        "уточняйте переход между залами",
        "travel_source": settings.route_api_url or None,
        "calculated_at": datetime.now(request.date_from.tzinfo).isoformat(),
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
