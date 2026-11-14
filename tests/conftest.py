import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text

from app.api.app import create_app
from app.config import Settings


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # Tests must never guess a database or wipe an existing environment.
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated migrated test PostgreSQL")
    if not url.rstrip("/").endswith("/bashculture_test"):
        pytest.fail("Integration tests require a database named bashculture_test")
    app = create_app(
        Settings(
            _env_file=None,
            database_url=url,
            admin_api_token=SecretStr("test-admin-key"),
            llm_api_url="",
            llm_model="",
            llm_api_key=SecretStr(""),
        )
    )
    async with app.router.lifespan_context(app), app.state.engine.connect() as guard:
        # Concurrent developer runs and browser fixtures share this dedicated database.
        # Keep their TRUNCATE operations outside each other's active scenarios.
        await guard.execute(text("SELECT pg_advisory_lock(71013999)"))
        try:
            async with app.state.sessions() as session, session.begin():
                await session.execute(
                    text(
                        "TRUNCATE bot_state, bot_updates, web_sessions, telegram_identities, "
                        "link_tokens, conversations, pending_actions, budget_accounts, "
                        "rate_buckets, "
                        "feedback, subscriptions, audit_log, moderation_items, "
                        "import_rows, import_runs, raw_records, "
                        "source_records, events, venues, cities, source_snapshots, "
                        "source_runs CASCADE"
                    )
                )
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as http:
                yield http
        finally:
            await guard.execute(text("SELECT pg_advisory_unlock(71013999)"))


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin-key"}
