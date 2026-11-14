"""Pure database-backed command handler; network sends happen after commit."""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.confirmations import resolve_action
from app.auth.service import consume_link_token, telegram_owner
from app.bot.agent import compact_message, decision, queue_request, render_selection
from app.bot.rendering import callback, card_buttons, message, render_event
from app.catalog.schemas import CATEGORIES
from app.common.clock import utcnow
from app.common.errors import AppError
from app.common.quotas import enforce_rate
from app.config import Settings
from app.ingestion.normalize import content_hash
from app.planner.service import PlanInput, build_day_plan
from app.search.models import SearchFilters
from app.search.service import search_events
from app.storage.postgres.runtime_tables import BotState
from app.storage.postgres.tables import City
from app.subscriptions.service import (
    SubscriptionInput,
    create_subscription,
    list_subscriptions,
    unsubscribe,
)

HELP = (
    "<b>Афиша Башкортостана</b>\n"
    "Напишите обычным сообщением: «Уфа, что-нибудь на вечер», «дешевле» или «ещё».\n"
    "/chat — новый диалог с ИИ\n"
    "/search — поиск по кнопкам\n/city — выбрать город\n/category — категория\n"
    "/date — период\n/price — верхняя цена билета\n/age — возраст посетителя\n"
    "/title — название\n/results — найти\n/more — следующая страница\n"
    "/subscribe — подписка на город, категорию и выбранный период\n"
    "/subscriptions — мои подписки\n/unsubscribe — отписка\n"
    "/plan — план дня: дата, бюджет, состав группы и переезды\n"
    "/demo on — план с помеченными демодопущениями; /demo off — выключить\n"
    "/cancel — сбросить поиск\n/help — помощь\n"
    "Цены и возраст проверяются только по известным данным. Афиша не гарантирует наличие билетов."
)


async def state_for(session: AsyncSession, owner: str) -> BotState:
    key = "user:" + owner
    record = await session.get(BotState, key, with_for_update=True)
    if record is None:
        record = BotState(key=key, value={})
        session.add(record)
        await session.flush()
    return record


async def results(
    session: AsyncSession, chat: int, state: dict[str, Any], settings: Settings, owner: str
) -> list[dict[str, Any]]:
    filters = SearchFilters.model_validate(state.get("filters", {}))
    if filters.city_id is None:
        state["waiting"] = "city"
        return await city_menu(session, chat)
    result = await search_events(session, filters)
    state["waiting"] = None
    state["has_more"] = result["pagination"]["has_more"]
    state["search_signature"] = content_hash(filters.model_dump(mode="json"))[:12]
    header = f"Найдено: {result['pagination']['total']}. Страница {filters.page}."
    if not result["items"]:
        header = (
            "По выбранным условиям ничего не найдено. "
            "Неизвестная цена или возраст не считаются подходящими."
        )
    controls = [[callback("Изменить фильтры", "menu"), callback("Подписаться", "subask")]]
    if state["has_more"]:
        controls.insert(0, [callback("Ещё", "more:" + state["search_signature"])])
    return [
        compact_message(owner, chat, render_selection(header, result["items"], settings), controls)
    ]


async def city_menu(session: AsyncSession, chat: int) -> list[dict[str, Any]]:
    cities = (await session.scalars(select(City).order_by(City.name))).all()
    buttons = [[callback(city.name, f"city:{city.id}")] for city in cities[:50]]
    return [
        message(
            chat,
            "Выберите город из каталога:"
            if cities
            else "Каталог пока пуст. Нужен импорт или сбор афиш.",
            buttons,
        )
    ]


def menu(chat: int) -> list[dict[str, Any]]:
    return [
        message(
            chat,
            "Настройте поиск или сразу покажите результаты:",
            [
                [callback("Город", "city"), callback("Категория", "category")],
                [callback("Дата", "date"), callback("Цена", "price")],
                [callback("Возраст", "age"), callback("Название", "title")],
                [callback("Найти", "results"), callback("План дня", "plan")],
                [callback("Мои подписки", "subscriptions"), callback("Сброс", "cancel")],
            ],
        )
    ]


async def handle_update(
    session: AsyncSession, update: dict[str, Any], settings: Settings
) -> list[dict[str, Any]]:
    query = update.get("callback_query")
    msg = query.get("message", {}) if query else update.get("message", {})
    sender = query.get("from", {}) if query else msg.get("from", {})
    if msg.get("chat", {}).get("type") != "private" or not sender.get("id"):
        return []  # Never store group conversations or trust user IDs from text.
    chat = int(msg["chat"]["id"])
    owner = await telegram_owner(session, int(sender["id"]), chat)
    record = await state_for(session, owner)
    state = deepcopy(record.value)
    previous = deepcopy(state)
    command = str(query.get("data", "")) if query else str(msg.get("text", ""))[:2000]
    if command.startswith("/"):
        parts = command[1:].split(maxsplit=1)
        command = parts[0].split("@")[0] + (" " + parts[1] if len(parts) > 1 else "")
    prefix = (
        [{"method": "answerCallbackQuery", "payload": {"callback_query_id": str(query["id"])}}]
        if query
        else []
    )
    try:
        await enforce_rate(session, "bot:" + owner, 30)
        if command.startswith("start link_"):
            token = command[len("start link_") :]
            if not 20 <= len(token) <= 100:
                raise ValueError("invalid_link")
            state["link_token"] = token
            signature = content_hash({"token": token})[:16]
            replies = [
                message(
                    chat,
                    "Привязать этот Telegram к профилю, открытому на сайте?",
                    [
                        [
                            callback("Подтвердить привязку", "link:" + signature),
                            callback("Отказаться", "link_reject"),
                        ]
                    ],
                )
            ]
        elif command == "link_reject":
            state.pop("link_token", None)
            replies = [message(chat, "Привязка отменена")]
        elif command.startswith("link:"):
            token = str(state.get("link_token", ""))
            if not token or content_hash({"token": token})[:16] != command[5:]:
                raise AppError("link_expired", "Предложение привязки устарело", 409)
            linked_owner = await consume_link_token(session, token, int(sender["id"]), chat)
            state.pop("link_token", None)
            if linked_owner != owner:
                previous_record = record
                record = await state_for(session, linked_owner)
                if record.value:
                    state = deepcopy(record.value)
                await session.delete(previous_record)
            replies = [
                message(
                    chat,
                    "Telegram привязан к профилю сайта. "
                    "Подписки доступны в разделе «Мои подписки».",
                )
            ]
        elif query and command.startswith(("agent_yes:", "agent_no:")):
            replies = await decision(session, owner, chat, command)
        elif (
            not query
            and bool(msg.get("text"))
            and not str(msg["text"]).startswith("/")
            and not state.get("waiting")
        ):
            replies = await queue_request(session, owner, chat, command, state)
        else:
            replies = await dispatch(session, owner, chat, command, state, settings)
        if state.get("filters") != previous.get("filters") and not any(
            reply["method"] == "_agent" for reply in replies
        ):
            # Menu changes start a new agent context, seeded from the new manual filters.
            state.pop("agent_conversation_id", None)
    except (ValidationError, ValueError, InvalidOperation):
        state = previous
        replies = [
            message(
                chat,
                "Проверьте ввод: дату в формате ГГГГ-ММ-ДД, цену числом, "
                "возраст от 0 до 120. /cancel — сброс.",
            )
        ]
    except AppError as exc:
        replies = [message(chat, escape(exc.message))]
    record.value = state
    return prefix + replies


async def dispatch(
    session: AsyncSession,
    owner: str,
    chat: int,
    command: str,
    state: dict[str, Any],
    settings: Settings,
) -> list[dict[str, Any]]:
    filters = dict(state.get("filters", {"page_size": 3}))
    state["filters"] = filters
    if command in {"start", "help"}:
        return [message(chat, HELP)] + menu(chat)
    if command == "chat":
        state.clear()
        return [message(chat, "Напишите город и пожелания, например: «Уфа, что-нибудь на вечер».")]
    if command.startswith("agent:"):
        _, decision, action_id = command.split(":", 2)
        if decision not in {"yes", "no"}:
            raise ValueError("invalid_decision")
        result = await resolve_action(session, owner, UUID(action_id), confirm=decision == "yes")
        return [
            message(chat, "Отменено." if result.get("status") == "rejected" else "Подтверждено.")
        ]
    if command in {"demo", "demo on", "demo off"}:
        state["demo_mode"] = command != "demo off"
        return [
            message(
                chat,
                "Деморежим планировщика включён. "
                "Придуманные значения помечаются и не подтверждены организатором. "
                "/plan — построить план."
                if state["demo_mode"]
                else "Деморежим выключен. /plan использует подтверждённые данные.",
            )
        ]
    if command == "cancel":
        state.clear()
        return [
            message(chat, "Поиск сброшен. Подписки сохранены; /unsubscribe — управление отпиской.")
        ] + menu(chat)
    if command in {"search", "menu"}:
        state["waiting"] = None
        return menu(chat)
    if command == "city":
        return await city_menu(session, chat)
    if command.startswith("city:"):
        city = await session.get(City, UUID(command[5:]))
        if city is None:
            raise AppError("city_not_found", "Город не найден", 404)
        filters.update(city_id=str(city.id), page=1)
        return [message(chat, f"Город: {escape(city.name)}")] + menu(chat)
    if command == "category":
        return [
            message(
                chat,
                "Выберите категорию:",
                [[callback(label, "cat:" + code)] for code, label in CATEGORIES.items()]
                + [[callback("Все", "cat:all")]],
            )
        ]
    if command.startswith("cat:"):
        value = command[4:]
        if value != "all" and value not in CATEGORIES:
            raise ValueError("category")
        filters.update(category=None if value == "all" else value, page=1)
        return menu(chat)
    if command == "date":
        state["waiting"] = "date"
        return [
            message(
                chat,
                "Выберите период или пришлите ГГГГ-ММ-ДД. Часовой пояс — Башкортостан (+05:00).",
                [
                    [callback("Сегодня", "day:0"), callback("Завтра", "day:1")],
                    [callback("Ближайшие 7 дней", "day:7"), callback("Все будущие", "day:all")],
                ],
            )
        ]
    if command.startswith("day:"):
        value = command[4:]
        local = utcnow().astimezone(ZoneInfo(settings.event_timezone))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if value == "all":
            filters.pop("date_from", None)
            filters.pop("date_to", None)
        else:
            offset = int(value)
            if offset not in {0, 1, 7}:
                raise ValueError("day")
            if offset == 1:
                start += timedelta(days=1)
            filters["date_from"] = max(local, start).isoformat()
            filters["date_to"] = (start + timedelta(days=7 if offset == 7 else 1)).isoformat()
        filters["page"] = 1
        state["waiting"] = None
        return menu(chat)
    if command in {"price", "age", "title"}:
        state["waiting"] = command
        prompts = {
            "price": "Введите максимальную цену одного билета в рублях, «бесплатно» или «любая».",
            "age": "Введите возраст посетителя числом или «любой». "
            "Для семьи укажите возраст самого младшего.",
            "title": "Введите часть названия (до 200 символов) или «любое».",
        }
        return [message(chat, prompts[command])]
    if command == "results":
        filters["page"] = 1
        return await results(session, chat, state, settings, owner)
    if command == "more" or command.startswith("more:"):
        if command.startswith("more:") and command[5:] != state.get("search_signature"):
            return [message(chat, "Эта кнопка относится к старому поиску. Используйте /results.")]
        if not state.get("has_more"):
            return [message(chat, "Других страниц пока нет. /search — изменить условия.")]
        filters["page"] = int(filters.get("page", 1)) + 1
        return await results(session, chat, state, settings, owner)
    if command in {"subscribe", "subask"}:
        if not filters.get("city_id"):
            return await city_menu(session, chat)
        payload = SubscriptionInput(
            city_id=filters["city_id"],
            category=filters.get("category"),
            channel="telegram",
            period_filter={
                key: filters[key] for key in ("date_from", "date_to") if filters.get(key)
            },
        )
        state["subscription_proposal"] = {
            "payload": payload.model_dump(mode="json"),
            "expires": (utcnow() + timedelta(minutes=10)).isoformat(),
        }
        signature = content_hash(state["subscription_proposal"])[:16]
        return [
            message(
                chat,
                "Подписка на новые или обновлённые события выбранного города, категории и периода. "
                "Фильтры цены/возраста в подписку не входят. "
                f"До {settings.notification_daily_limit} уведомлений в день, "
                "без ночных отправок. Подтвердить?",
                [[callback("Подтвердить", "subyes:" + signature), callback("Отмена", "subno")]],
            )
        ]
    if command == "subno":
        state.pop("subscription_proposal", None)
        return [message(chat, "Подписка не создана.")]
    if command.startswith("subyes:"):
        proposal = state.get("subscription_proposal")
        if (
            not proposal
            or content_hash(proposal)[:16] != command[7:]
            or datetime.fromisoformat(proposal["expires"]) <= utcnow()
        ):
            raise AppError("expired", "Подтверждение устарело. /subscribe — создать заново", 409)
        await create_subscription(
            session, owner, SubscriptionInput.model_validate(proposal["payload"])
        )
        state.pop("subscription_proposal", None)
        return [
            message(chat, "Подписка сохранена. /subscriptions — список, /unsubscribe — отписка.")
        ]
    if command in {"subscriptions", "unsubscribe"}:
        items = await list_subscriptions(session, owner)
        replies = []
        for item in items:
            label = (
                f"{item['city']} · {CATEGORIES.get(item['category'], 'Все категории')} · "
                f"{item['period_filter'] or 'Все будущие'}"
            )
            replies.append(
                message(chat, escape(label), [[callback("Отписаться", "unsub:" + item["id"])]])
            )
        return replies or [message(chat, "Подписок пока нет. /subscribe — создать.")]
    if command.startswith("unsub:"):
        await unsubscribe(session, owner, UUID(command[6:]))
        return [message(chat, "Подписка удалена. Новых отправок по ней не будет.")]
    if command == "plan":
        state["waiting"] = "plan"
        return [
            message(
                chat,
                "Выберите город через /city, затем пришлите одной строкой:\n"
                "ГГГГ-ММ-ДД; 10:00; 21:00; бюджет; взрослых; "
                "возрасты детей через запятую или -; walk/car/transit; максимум минут переезда\n"
                "Например: 2026-12-01; 10:00; 21:00; 2000; 2; 8,12; walk; 30\n"
                "План включает 2–4 события. Без цены, окончания "
                "или проверенного переезда событие не включается.",
            )
        ]
    waiting = state.get("waiting")
    if waiting == "plan":
        return await plan_reply(
            session, chat, command, filters, settings, demo_mode=bool(state.get("demo_mode"))
        )
    if waiting == "date":
        start = datetime.strptime(command.strip(), "%Y-%m-%d").replace(
            tzinfo=ZoneInfo(settings.event_timezone)
        )
        filters.update(
            date_from=max(start, utcnow()).isoformat(),
            date_to=(start + timedelta(days=1)).isoformat(),
            page=1,
        )
    elif waiting == "price":
        filters.pop("is_free", None)
        filters.pop("price_max", None)
        if command.lower() == "бесплатно":
            filters["is_free"] = True
        elif command.lower() != "любая":
            filters["price_max"] = str(Decimal(command.replace(",", ".")))
    elif waiting == "age":
        filters["age"] = None if command.lower() == "любой" else int(command)
    elif waiting == "title":
        filters["query"] = None if command.lower() == "любое" else command[:200]
    else:
        return await queue_request(session, owner, chat, command, state)
    SearchFilters.model_validate(filters)
    filters["page"] = 1
    state["waiting"] = None
    return await results(session, chat, state, settings, owner)


async def plan_reply(
    session: AsyncSession,
    chat: int,
    text: str,
    filters: dict[str, Any],
    settings: Settings,
    *,
    demo_mode: bool = False,
) -> list[dict[str, Any]]:

    parts = [p.strip() for p in text.split(";")]
    if len(parts) != 8 or not filters.get("city_id"):
        return [
            message(chat, "Сначала /city, затем /plan и восемь значений через точку с запятой.")
        ]
    start = datetime.fromisoformat(parts[0] + "T" + parts[1]).replace(
        tzinfo=ZoneInfo(settings.event_timezone)
    )
    end = datetime.fromisoformat(parts[0] + "T" + parts[2]).replace(
        tzinfo=ZoneInfo(settings.event_timezone)
    )
    request = PlanInput.model_validate(
        {
            "city_id": filters["city_id"],
            "demo_mode": demo_mode,
            "date_from": start,
            "date_to": end,
            "budget": Decimal(parts[3]),
            "adults": int(parts[4]),
            "children_ages": [] if parts[5] == "-" else [int(a) for a in parts[5].split(",")],
            "travel_mode": parts[6],
            "max_travel_min": int(parts[7]),
        }
    )
    result = await build_day_plan(session, request, settings)
    replies = [message(chat, escape(result["message"]))]
    if result.get("assumptions_notice"):
        replies.append(message(chat, escape(result["assumptions_notice"])))
    if result["plans"]:
        plan = result["plans"][0]
        replies.append(
            message(
                chat,
                f"Верхняя оценка билетов для всей группы: {plan['total_cost']} ₽. "
                f"Переезды между событиями: {plan['travel_minutes']} мин. "
                "Наличие билетов уточняйте в источнике.",
            )
        )
        for card in plan["events"]:
            replies.append(
                message(chat, render_event(card, settings.event_timezone), card_buttons(card))
            )
    else:
        for note in result["missing_data"]:
            replies.append(message(chat, escape(note)))
        for suggestion in result["suggestions"]:
            replies.append(
                message(chat, escape(suggestion["message"]) + f": {suggestion['value']}")
            )
    return replies
