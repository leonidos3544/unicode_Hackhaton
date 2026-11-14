import hashlib
import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from app.api.dependencies import Session
from app.api.schemas import EventCard, SearchResult
from app.catalog.repository import event_card
from app.catalog.schemas import CATEGORIES
from app.catalog.service import get_event
from app.search.models import SearchFilters
from app.search.service import search_events
from app.storage.postgres.tables import City, Venue

router = APIRouter(prefix="/api/v1", tags=["Каталог"])


@router.get("/events", response_model=SearchResult)
async def list_events(
    session: Session, filters: Annotated[SearchFilters, Query()], response: Response
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await search_events(session, filters)


@router.post("/search", response_model=SearchResult)
async def post_search(
    filters: SearchFilters, session: Session, response: Response
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await search_events(session, filters)


@router.get("/events/{event_id}", response_model=EventCard)
async def read_event(event_id: UUID, session: Session, request: Request) -> Response:
    card = jsonable_encoder(await event_card(session, await get_event(session, event_id)))
    # Hash the full response: adding a source must also invalidate an existing ETag.
    body = json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    etag = '"' + hashlib.sha256(body.encode()).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    tags = [
        tag.strip().removeprefix("W/")
        for tag in request.headers.get("If-None-Match", "").split(",")
    ]
    if etag in tags or "*" in tags:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/cities")
async def list_cities(session: Session, response: Response) -> list[dict[str, Any]]:
    response.headers["Cache-Control"] = "public, max-age=60"
    cities = (await session.scalars(select(City).order_by(City.name))).all()
    return [{"id": city.id, "name": city.name} for city in cities]


@router.get("/venues")
async def list_venues(
    session: Session, response: Response, city_id: UUID | None = None
) -> list[dict[str, Any]]:
    response.headers["Cache-Control"] = "public, max-age=60"
    statement = select(Venue).order_by(Venue.name)
    if city_id:
        statement = statement.where(Venue.city_id == city_id)
    venues = (await session.scalars(statement)).all()
    return [
        {
            "id": venue.id,
            "name": venue.name,
            "city_id": venue.city_id,
            "address": venue.address,
            "source_url": venue.source_url,
            "verified_at": venue.verified_at,
        }
        for venue in venues
    ]


@router.get("/categories")
async def list_categories(response: Response) -> list[dict[str, str]]:
    response.headers["Cache-Control"] = "public, max-age=3600"
    return [{"id": code, "name": name} for code, name in CATEGORIES.items()]
