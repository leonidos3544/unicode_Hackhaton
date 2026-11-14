import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from app.cli.bootstrap import bootstrap
from app.storage.postgres.tables import Event, ImportRun, SourceRecord
from scripts.prepare_local import prepare
from tests.test_bot_delivery import seed

SNAPSHOT_TIME = datetime.fromisoformat(
    json.loads(Path("data/open/manifest.json").read_text(encoding="utf-8"))["created_at"]
)


async def test_saved_snapshot_bootstrap_is_idempotent_and_not_live_verified(client, monkeypatch):
    monkeypatch.setattr("app.cli.bootstrap.utcnow", lambda: SNAPSHOT_TIME)
    monkeypatch.setattr("app.catalog.service.utcnow", lambda: SNAPSHOT_TIME)
    sessions = client._transport.app.state.sessions
    result = await bootstrap(sessions)
    assert result["status"] == "loaded" and result["published"] > 50
    assert result["rejected"] == 0 and result["live_verification"] is False
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(Event))
        runs = await session.scalar(select(func.count()).select_from(ImportRun))
        source = await session.scalar(select(SourceRecord))
        assert source.source_type == "import" and source.verified_at is None
    assert (await bootstrap(sessions))["status"] == "skipped"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == count
        assert await session.scalar(select(func.count()).select_from(ImportRun)) == runs


async def test_bootstrap_with_expired_snapshot_does_not_publish_past_events(client, monkeypatch):
    reference = datetime(2070, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("app.cli.bootstrap.utcnow", lambda: reference)
    monkeypatch.setattr("app.catalog.service.utcnow", lambda: reference)
    sessions = client._transport.app.state.sessions
    result = await bootstrap(sessions)
    assert result["status"] == "loaded" and result["published"] == 0
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) > 0
        assert await session.scalar(select(Event.id).where(Event.status == "published")) is None


async def test_bootstrap_preserves_existing_catalog(client, admin_headers):
    await seed(client, admin_headers, title="Уже существующее событие")
    sessions = client._transport.app.state.sessions
    assert (await bootstrap(sessions))["status"] == "skipped"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        assert (await session.scalar(select(Event))).title == "Уже существующее событие"


def test_local_settings_are_created_once_with_matching_database_password(tmp_path: Path):
    (tmp_path / ".env.example").write_text(
        "POSTGRES_PASSWORD=\nADMIN_API_TOKEN=\nDATABASE_URL=\n", encoding="utf-8"
    )
    assert prepare(tmp_path)
    original = (tmp_path / ".env").read_bytes()
    lines = dict(line.split("=", 1) for line in original.decode().splitlines())
    assert len(lines["ADMIN_API_TOKEN"]) >= 32
    assert lines["POSTGRES_PASSWORD"] in lines["DATABASE_URL"]
    assert not prepare(tmp_path)
    assert (tmp_path / ".env").read_bytes() == original


def test_local_settings_never_replace_existing_tokens(tmp_path: Path):
    original = b"TELEGRAM_BOT_TOKEN=test-private-value\nADMIN_API_TOKEN=existing\n"
    (tmp_path / ".env").write_bytes(original)
    assert not prepare(tmp_path)
    assert (tmp_path / ".env").read_bytes() == original
