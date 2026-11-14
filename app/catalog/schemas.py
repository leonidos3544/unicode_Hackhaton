from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

CATEGORIES = {
    "concert": "Концерт",
    "theatre": "Спектакль",
    "exhibition": "Выставка",
    "festival": "Фестиваль",
    "cinema": "Кино",
    "excursion": "Экскурсия",
    "other": "Другое",
}
Category = Literal["concert", "theatre", "exhibition", "festival", "cinema", "excursion", "other"]
Status = Literal["draft", "published", "cancelled"]


def clean_text(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


NonEmpty = Annotated[str, BeforeValidator(clean_text), Field(min_length=1, max_length=500)]
Money = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2)]


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    external_id: NonEmpty
    title: NonEmpty
    category: Category
    date_start: datetime
    date_end: datetime | None = None
    duration_min: int | None = Field(default=None, gt=0, le=525600)
    city: NonEmpty
    venue: NonEmpty
    address: NonEmpty
    lat: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    lon: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    description: str = Field(min_length=1, max_length=10000)
    price_min: Money | None = None
    price_max: Money | None = None
    is_free: bool | None = None
    age_limit: Literal[0, 6, 12, 16, 18] | None = None
    language: str | None = Field(default=None, max_length=100)
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=30
    )
    source_url: HttpUrl
    org_link: HttpUrl | None = None

    @field_validator("date_start", "date_end")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Укажите часовой пояс времени, например +05:00")
            return value.astimezone(UTC)
        return value

    @field_validator("source_url", "org_link")
    @classmethod
    def no_url_credentials(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (value.username or value.password):
            raise ValueError("Ссылка не должна содержать логин или пароль")
        return value

    @model_validator(mode="after")
    def validate_facts(self) -> Self:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("Укажите обе координаты или оставьте обе неизвестными")
        if self.duration_min is not None:
            computed = self.date_start + timedelta(minutes=self.duration_min)
            if self.date_end is None:
                self.date_end = computed
            elif self.date_end != computed:
                raise ValueError("Длительность противоречит времени окончания")
        if self.date_end is not None and self.date_end <= self.date_start:
            raise ValueError("Окончание должно быть позже начала")
        if self.price_max is not None and (
            self.price_min is None or self.price_max < self.price_min
        ):
            raise ValueError("Проверьте диапазон цены")
        if self.is_free is True:
            if any(p is not None and p != 0 for p in (self.price_min, self.price_max)):
                raise ValueError("Бесплатный вход противоречит цене")
            self.price_min = self.price_max = Decimal(0)
        elif self.price_min == 0 and self.price_max == 0:
            if self.is_free is False:
                raise ValueError("Нулевая цена противоречит платному входу")
            self.is_free = True
        return self


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=1000)


class EventEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    event: EventInput
    reason: str = Field(min_length=3, max_length=1000)
