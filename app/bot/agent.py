"""Durable Telegram jobs for the shared agent. Paid work runs outside database transactions."""

from copy import deepcopy
from datetime import timedelta
from html import escape
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.confirmations import resolve_action
from app.agent.provider import Provider
from app.agent.service import respond
from app.bot.rendering import callback, message, moment, render_event
from app.catalog.schemas import CATEGORIES
from app.common.clock import utcnow
from app.common.errors import AppError
from app.config import Settings
from app.storage.postgres.runtime_tables import BotState, BotUpdate, Conversation
from app.storage.postgres.tables import City

REQUEST_METHOD = "_agent"


async def queue_request(
    session: AsyncSession, owner: str, chat: int, text: str, state: dict[str, Any]
) -> list[dict[str, Any]]:
    conversation = (
        await session.get(Conversation, UUID(state["agent_conversation_id"]))
        if state.get("agent_conversation_id")
        else None
    )
    if conversation is None or conversation.owner_id != owner or conversation.channel != "telegram":
        conversation = Conversation(
            owner_id=owner, channel="telegram", state={"filters": state.get("filters", {})}
        )
        session.add(conversation)
        await session.flush()
        state["agent_conversation_id"] = str(conversation.id)
    return [
        {
            "method": REQUEST_METHOD,
            "payload": {
                "owner": owner,
                "chat_id": chat,
                "conversation_id": str(conversation.id),
                "message": text,
            },
        }
    ]


def compact_message(
    owner: str, chat: int, text: str, buttons: list[list[dict[str, str]]] | None = None
) -> dict[str, Any]:
    reply = message(chat, text, buttons)
    reply["compact_owner"] = owner
    return reply


def render_selection(heading: str, cards: list[dict[str, Any]], settings: Settings) -> str:
    parts = [escape(heading[:400])]
    shown = 0
    for card in cards[:3]:
        brief = {
            **card,
            **{
                key: str(card.get(key, ""))[:limit]
                for key, limit in (("title", 160), ("city", 60), ("venue", 120), ("address", 120))
            },
            "data_warnings": [str(w)[:140] for w in card.get("data_warnings", [])[:2]],
        }
        block = render_event(brief, settings.event_timezone)
        # Escape every source value and keep complete HTML tags within Telegram's limit.
        links = []
        for label, key in (("Источник", "source_url"), ("Билеты", "org_link")):
            url = str(card.get(key) or "")
            if urlsplit(url).scheme in {"http", "https"} and len(url) <= 700:
                links.append(f'<a href="{escape(url, quote=True)}">{label}</a>')
        if links:
            block += "\n" + " · ".join(links)
        if len("\n\n".join([*parts, block])) > 3800:
            break
        parts.append(block)
        shown += 1
    if len(cards) > shown:
        parts.append(f"Показано событий: {shown}. Уточните запрос или напишите «ещё».")
    return "\n\n".join(parts)


async def agent_replies(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    update_id: int,
    job: dict[str, Any],
    provider: Provider | None = None,
) -> list[dict[str, Any]]:
    owner, chat, cid = str(job["owner"]), int(job["chat_id"]), UUID(job["conversation_id"])
    try:
        answer = await respond(
            sessions, settings, owner, cid, f"telegram:{update_id}", str(job["message"]), provider
        )
    except AppError as error:
        # A crash after a paid request is not permission to bill a second attempt.
        return [message(chat, escape(error.message) + " Напишите новый запрос или /chat.")]
    async with sessions() as session, session.begin():
        state = await session.get(BotState, "user:" + owner, with_for_update=True)
        if state and state.value.get("agent_conversation_id") == str(cid):
            value = deepcopy(state.value)
            value["filters"] = answer["filters"]
            value["waiting"] = None
            state.value = value
        city_names = {}
        for action in answer["pending_actions"]:
            city_id = action["payload"].get("city_id")
            city = await session.get(City, UUID(city_id)) if city_id else None
            if city:
                city_names[city_id] = city.name
    replies = []
    cards = answer["items"]
    heading = answer["message"]
    if not cards and answer.get("details"):
        cards = answer["details"]
        heading += " Эти карточки не подходят под текущие условия."
    # Confirmation proposals are separate messages, never removed as search results.
    if not answer["pending_actions"] or cards:
        replies.append(compact_message(owner, chat, render_selection(heading, cards, settings)))
    for action in answer["pending_actions"]:
        payload = action["payload"]
        if action["action_type"] == "subscribe":
            period = payload.get("period_filter", {})
            lines = [
                "<b>Подтвердите подписку</b>",
                "Город: " + escape(city_names.get(payload["city_id"], payload["city_id"])),
                "Категория: " + escape(CATEGORIES.get(payload.get("category"), "Все категории")),
                "Уведомления: Telegram"
                if payload["channel"] == "telegram"
                else "Уведомления: на сайте",
            ]
            for key, label in (("date_from", "С"), ("date_to", "До")):
                if period.get(key):
                    lines.append(label + ": " + moment(period[key], settings.event_timezone))
            if not period:
                lines.append("Период: все будущие события")
            lines.append("Цена и возраст в подписке не учитываются.")
        else:
            lines = ["<b>Отправить обращение модератору?</b>", escape(payload["text"][:2000])]
            if payload.get("event_id"):
                lines.append("Событие: " + escape(str(payload["event_id"])))
        replies.append(
            message(
                chat,
                "\n".join(lines),
                [
                    [
                        callback("Подтвердить", "agent:yes:" + action["id"]),
                        callback("Отказаться", "agent:no:" + action["id"]),
                    ]
                ],
            )
        )
    return replies


async def complete_pending(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    provider: Provider | None = None,
    *,
    update_id: int | None = None,
) -> bool:
    lease = str(uuid4())
    async with sessions() as session, session.begin():
        query = (
            select(BotUpdate)
            .where(
                BotUpdate.status.in_(["agent_pending", "agent_processing"]),
                or_(BotUpdate.lease_until.is_(None), BotUpdate.lease_until < utcnow()),
            )
            .order_by(BotUpdate.update_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if update_id is not None:
            query = query.where(BotUpdate.update_id == update_id)
        record = await session.scalar(query)
        if record is None:
            return False
        identifier = record.update_id
        job = record.replies[0]["payload"]
        record.status = "agent_processing"
        record.lease_id, record.lease_until = lease, utcnow() + timedelta(minutes=5)
    replies = await agent_replies(sessions, settings, identifier, job, provider)
    async with sessions() as session, session.begin():
        record = await session.get(BotUpdate, identifier, with_for_update=True)
        if record is None or record.lease_id != lease:
            return False
        record.replies, record.next_reply = replies, 0
        record.status = "pending" if replies else "done"
        record.lease_id = record.lease_until = None
    return True


async def decision(
    session: AsyncSession, owner: str, chat: int, command: str
) -> list[dict[str, Any]]:
    kind, action_id = command.split(":", 1)
    if kind not in {"agent_yes", "agent_no"}:
        raise ValueError("invalid_decision")
    result = await resolve_action(session, owner, UUID(action_id), confirm=kind == "agent_yes")
    return [message(chat, "Отменено." if result.get("status") == "rejected" else "Подтверждено.")]
