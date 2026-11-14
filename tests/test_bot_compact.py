from typing import Any

import pytest
from sqlalchemy import select

from app.agent.provider import ModelReply
from app.bot.main import deliver_replies, process_update
from app.bot.transport import TelegramError
from app.config import Settings
from app.storage.postgres.runtime_tables import BotState, BotUpdate
from tests.test_agent import FakeProvider, call
from tests.test_bot_delivery import seed, update


class RecordingTelegram:
    def __init__(self, delete_error: int | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.delete_error = delete_error
        self.identifier = 100

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        self.calls.append((method, payload))
        if method == "deleteMessage":
            if self.delete_error:
                raise TelegramError(self.delete_error, 10)
            return True
        self.identifier += 1
        return {"message_id": self.identifier}


async def test_telegram_replaces_only_its_previous_selection_after_restart(client, admin_headers):
    for index in range(5):
        await seed(
            client,
            admin_headers,
            city="Уфа",
            external_id=f"compact-{index}",
            title=f"Проверка компактной выдачи {index}",
        )
    sessions = client._transport.app.state.sessions
    settings = Settings(_env_file=None, llm_free=True)
    model = FakeProvider([ModelReply(calls=[call("search_events", {})])])
    telegram = RecordingTelegram()
    await process_update(sessions, update(1, "Уфа"), settings, model)
    assert await deliver_replies(sessions, telegram) == 1
    assert telegram.calls[0][1]["text"].count("<b>") == 3
    await process_update(sessions, update(2, "дешевле"), settings, model)
    assert await deliver_replies(sessions, telegram) == 1
    assert [name for name, _ in telegram.calls] == ["sendMessage", "deleteMessage", "sendMessage"]
    assert telegram.calls[1][1] == {"chat_id": 42, "message_id": 101}
    await process_update(sessions, update(2, "дешевле"), settings, model)
    assert await deliver_replies(sessions, telegram) == 0
    assert model.calls == 2
    # A new transport/process still reads the tracked bot message from PostgreSQL.
    restarted = RecordingTelegram()
    await process_update(sessions, update(3, "ещё"), settings, model)
    await deliver_replies(sessions, restarted)
    assert restarted.calls[0] == ("deleteMessage", {"chat_id": 42, "message_id": 102})
    assert restarted.calls[1][1]["text"].count("<b>") == 2
    restarted.calls.clear()
    await process_update(sessions, update(4, "Уфа", user=99), settings, model)
    await deliver_replies(sessions, restarted)
    assert [name for name, _ in restarted.calls] == ["sendMessage"]


@pytest.mark.parametrize("delete_error", [400, 429])
async def test_expired_selection_or_rate_limit_does_not_lose_reply(
    client, admin_headers, delete_error
):
    await seed(client, admin_headers, city="Уфа")
    sessions = client._transport.app.state.sessions
    settings = Settings(_env_file=None, llm_free=True)
    model = FakeProvider([ModelReply(calls=[call("search_events", {})])])
    telegram = RecordingTelegram()
    await process_update(sessions, update(1, "Уфа"), settings, model)
    await deliver_replies(sessions, telegram)
    telegram.calls.clear()
    telegram.delete_error = delete_error
    await process_update(sessions, update(2, "дешевле"), settings, model)
    await deliver_replies(sessions, telegram)
    async with sessions() as session:
        record = await session.get(BotUpdate, 2)
        if delete_error == 400:
            assert record.status == "done"
            assert telegram.calls[-1][0] == "sendMessage"
        else:
            assert record.status == "pending" and record.next_reply == 0
            assert [name for name, _ in telegram.calls] == ["deleteMessage"]
            compact = await session.scalar(select(BotState).where(BotState.key.like("compact:%")))
            assert compact.value["message_id"] == 101
