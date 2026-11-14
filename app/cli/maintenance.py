import argparse
import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete

from app.common.clock import utcnow
from app.config import get_settings
from app.storage.postgres.engine import build_database
from app.storage.postgres.runtime_tables import (
    AgentTrace,
    BotUpdate,
    Conversation,
    DialogueTurn,
    LinkToken,
    PendingAction,
    RateBucket,
    WebSession,
)

logger = logging.getLogger("bashculture.maintenance")


async def run(watch: bool) -> None:
    settings = get_settings()
    engine, sessions = build_database(settings.database_url)
    try:
        while True:
            now = utcnow()
            cutoff = now - timedelta(days=settings.retention_days)
            async with sessions() as session, session.begin():
                await session.execute(delete(WebSession).where(WebSession.expires_at < now))
                await session.execute(delete(LinkToken).where(LinkToken.expires_at < now))
                await session.execute(delete(RateBucket).where(RateBucket.expires_at < now))
                await session.execute(delete(AgentTrace).where(AgentTrace.created_at < cutoff))
                await session.execute(delete(DialogueTurn).where(DialogueTurn.created_at < cutoff))
                await session.execute(
                    delete(Conversation).where(
                        Conversation.updated_at < cutoff, Conversation.lease_until.is_(None)
                    )
                )
                await session.execute(
                    delete(PendingAction).where(PendingAction.expires_at < cutoff)
                )
                await session.execute(
                    delete(BotUpdate).where(
                        BotUpdate.created_at < cutoff, BotUpdate.status.in_(["done", "failed"])
                    )
                )
                # Notification IDs are durable deduplication tombstones until unsubscribe.
                # Removing them by age would re-enqueue a distant future event.
            logger.info("expired_runtime_data_cleaned")
            if not watch:
                break
            await asyncio.sleep(3600)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Очистка истёкших сессий, лимитов и завершённых доставок"
    )
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args.watch))


if __name__ == "__main__":
    main()
