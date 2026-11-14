from typing import Any

from fastapi import APIRouter, Request

from app.api.dependencies import Session
from app.planner.service import PlanInput, build_day_plan

router = APIRouter(prefix="/api/v1/plans", tags=["Планировщик"])


@router.post("/day")
async def plan_day(payload: PlanInput, request: Request, session: Session) -> dict[str, Any]:
    return await build_day_plan(session, payload, request.app.state.settings)
