"""Isolated browser-test application. Never use with the demonstration database."""

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text

from app.agent.provider import ModelReply, ToolCall
from app.api.app import create_app
from app.config import Settings
from app.storage.postgres.tables import Base

database_url = os.environ.get("TEST_DATABASE_URL", "")
if not database_url.rstrip("/").endswith("/bashculture_test"):
    raise RuntimeError("Browser tests require TEST_DATABASE_URL ending with /bashculture_test")


class BrowserTestProvider:
    """Deterministic protocol fixture, not a real model or a quality measurement."""

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        if any(message["role"] == "tool" for message in messages):
            return ModelReply(input_tokens=0, output_tokens=0)
        request = json.loads(messages[1]["content"])
        filters = request["filters"]
        if "подпиш" in request["message"].lower():
            name = "subscribe"
            args = {"city_id": filters["city_id"], "category": filters.get("category")}
        else:
            name, args = "search_events", filters
        return ModelReply(
            calls=[ToolCall(id="test-call", name=name, arguments=json.dumps(args))],
            input_tokens=0,
            output_tokens=0,
        )


app = create_app(
    Settings(
        _env_file=None,
        database_url=database_url,
        admin_api_token=SecretStr("browser-test-only"),
        public_base_url="http://localhost:8080",
        llm_free=True,
    )
)
app.state.agent_provider = BrowserTestProvider()
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def clean_test_database(application: FastAPI) -> AsyncIterator[None]:
    async with original_lifespan(application), application.state.engine.connect() as guard:
        # Only the explicitly guarded bashculture_test database is ever cleared.
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        await guard.execute(text("SELECT pg_advisory_lock(71013999)"))
        try:
            async with application.state.sessions() as session, session.begin():
                await session.execute(text(f"TRUNCATE {tables} CASCADE"))
            yield
        finally:
            await guard.execute(text("SELECT pg_advisory_unlock(71013999)"))


app.router.lifespan_context = clean_test_database
