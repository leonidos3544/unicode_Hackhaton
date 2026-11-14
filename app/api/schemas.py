from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.catalog.schemas import Category, Status


class SourceLink(BaseModel):
    source_url: str
    source_name: str
    source_type: str
    source_hash: str | None
    verified_at: datetime | None
    verification_status: str | None = None
    listing_url: str | None = None
    last_checked_at: datetime | None = None


class EventCard(BaseModel):
    demo_assumptions: dict[str, Any] | None = None
    id: UUID
    title: str
    normalized_title: str
    category: Category
    tags: list[str]
    date_start: datetime
    date_end: datetime | None
    duration_min: int | None
    city_id: UUID
    city: str
    venue_id: UUID
    venue: str
    address: str
    lat: float | None
    lon: float | None
    description: str
    price_min: float | None
    price_max: float | None
    is_free: bool | None
    age_limit: int | None
    language: str | None
    org_link: str | None
    status: Status
    version: int
    dedup_key: str
    source_url: str | None
    source_type: str | None
    source_hash: str | None
    verified_at: datetime | None
    confidence: float | None
    sources: list[SourceLink]
    data_warnings: list[str]
    created_at: datetime
    updated_at: datetime
    constraint_evidence: list[dict[str, Any]] = []


class Pagination(BaseModel):
    page: int
    page_size: int
    total: int
    has_more: bool


class SearchResult(BaseModel):
    items: list[EventCard]
    pagination: Pagination
    applied_filters: dict[str, Any]
    relaxations: list[dict[str, Any]]
    coverage_warnings: list[str]


class ErrorDetails(BaseModel):
    code: str
    message: str
    details: Any


class ErrorResponse(BaseModel):
    error: ErrorDetails
    request_id: str
