import json
from uuid import UUID

from sqlalchemy import func, select, text

from app.agent.provider import ModelReply
from app.agent.service import respond
from app.bot.agent import complete_pending
from app.bot.handlers import handle_update
from app.bot.main import process_update
from app.config import Settings
from app.storage.postgres.runtime_tables import AgentTrace, BotState, BotUpdate, Subscription
from tests.test_agent import call
from tests.test_bot_delivery import seed, update


async def test_telegram_agent_memory_owner_confirmation_and_replay(client, admin_headers):
    card = await seed(client, admin_headers, city="Уфа")
    sessions = client._transport.app.state.sessions
    settings = Settings(_env_file=None, llm_free=True)

    class Model:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            # The bot's state transaction must be released before model/network work.
            async with sessions() as session, session.begin():
                assert await session.scalar(text("SELECT pg_try_advisory_xact_lock(71013003)"))
            request = json.loads(messages[1]["content"])
            if "подпиш" in request["message"]:
                return ModelReply(calls=[call("subscribe", {"city_id": card["city_id"]})])
            return ModelReply(calls=[call("search_events", {})])

    model = Model()
    for item in [update(1, "Выставки в Уфе"), update(2, "дешевле"), update(3, "подпиши")]:
        await process_update(sessions, item, settings, model)
    await process_update(sessions, update(3, "подпиши"), settings, model)
    assert model.calls == 3
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 0
        proposal = await session.get(BotUpdate, 3)
        button = proposal.replies[-1]["payload"]["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]
        trace = await session.scalar(select(AgentTrace).where(AgentTrace.mode == "agent"))
        assert trace is not None
        second = await session.get(BotUpdate, 2)
        assert any(card["title"] in item["payload"]["text"] for item in second.replies)
    await process_update(sessions, update(4, button, callback=True, user=99), settings, model)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 0
    await process_update(sessions, update(5, button, callback=True), settings, model)
    await process_update(sessions, update(6, button, callback=True), settings, model)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 1
        subscription = await session.scalar(select(Subscription))
        assert subscription.channel == "telegram"
        state = await session.scalar(select(BotState).where(BotState.key.like("user:%")))
        assert state.value["agent_conversation_id"]
        assert state.value["filters"]["sort"] == "price_asc"


async def test_pending_telegram_job_replays_completed_turn_without_billing(client, admin_headers):
    await seed(client, admin_headers, city="Уфа")
    sessions = client._transport.app.state.sessions
    settings = Settings(_env_file=None)
    async with sessions() as session, session.begin():
        replies = await handle_update(session, update(100, "Выставки в Уфе"), settings)
        session.add(BotUpdate(update_id=100, replies=replies, status="agent_pending"))
        request = replies[0]["payload"]
    await respond(
        sessions,
        settings,
        request["owner"],
        UUID(request["conversation_id"]),
        "telegram:100",
        request["message"],
    )
    assert await complete_pending(sessions, settings, update_id=100)
    assert not await complete_pending(sessions, settings, update_id=100)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentTrace)) == 1
        record = await session.get(BotUpdate, 100)
        assert record.status == "pending"
        assert record.replies[0]["method"] == "sendMessage"
