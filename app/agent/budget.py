from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.errors import AppError
from app.config import Settings
from app.storage.postgres.runtime_tables import BudgetAccount


async def reserve(
    session: AsyncSession, owner: str, day: date, dialogue_cost: float, settings: Settings
) -> float:
    if settings.llm_free:
        return 0.0
    if not settings.llm_input_rub_per_million or not settings.llm_output_rub_per_million:
        raise AppError("pricing_missing", "Тариф модели не настроен", 503)
    await session.execute(
        insert(BudgetAccount)
        .values(owner_id=owner, day=day, spent=0, reserved=0)
        .on_conflict_do_nothing(constraint="uq_budget_accounts_owner_id")
    )
    account = await session.scalar(
        select(BudgetAccount)
        .where(BudgetAccount.owner_id == owner, BudgetAccount.day == day)
        .with_for_update()
    )
    assert account is not None
    amount = settings.llm_turn_budget_rub
    if (
        dialogue_cost + amount > settings.llm_dialogue_budget_rub
        or account.spent + account.reserved + amount > settings.llm_daily_budget_rub
    ):
        raise AppError("budget_exhausted", "Лимит расходов исчерпан", 429)
    account.reserved += amount
    return amount


async def settle(
    session: AsyncSession, owner: str, day: date, reserved: float, cost: float
) -> None:
    if not reserved:
        return
    account = await session.scalar(
        select(BudgetAccount)
        .where(BudgetAccount.owner_id == owner, BudgetAccount.day == day)
        .with_for_update()
    )
    assert account is not None
    account.reserved = max(0, account.reserved - reserved)
    account.spent += cost
