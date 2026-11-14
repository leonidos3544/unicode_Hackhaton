from datetime import datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.catalog.schemas import Category


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str | None = Field(default=None, max_length=200)
    city_id: UUID | None = None
    venue_id: UUID | None = None
    exclude_ids: list[UUID] = Field(default_factory=list, max_length=200)
    category: Category | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    end_before: datetime | None = None
    price_max: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    is_free: bool | None = None
    age: int | None = Field(default=None, ge=0, le=120)
    children_ages: list[int] = Field(default_factory=list, max_length=10)
    adults: int = Field(default=1, ge=0, le=10)
    budget_scope: Literal["per_person", "party"] = "per_person"
    duration_max_min: int | None = Field(default=None, gt=0, le=1440)
    language: str | None = Field(default=None, max_length=100)
    sort: Literal["date_asc", "price_asc"] = "date_asc"
    page: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=12, ge=1, le=50)

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("Возраст ребёнка должен быть от 0 до 17 лет")
        if not self.adults and not self.children_ages:
            raise ValueError("Укажите хотя бы одного посетителя")
        for moment in (self.date_from, self.date_to, self.end_before):
            if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
                raise ValueError("Укажите часовой пояс даты")
        if self.date_from and self.date_to and self.date_to <= self.date_from:
            raise ValueError("Конец периода должен быть позже начала")
        return self
