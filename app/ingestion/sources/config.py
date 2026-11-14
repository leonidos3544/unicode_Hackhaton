import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.catalog.schemas import Category


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,39}$")
    name: str = Field(min_length=1, max_length=200)
    adapter: Literal["wordpress_events", "jsonld"]
    listing_url: str
    monthly_url_template: str | None = None
    months_ahead: int = Field(default=2, ge=1, le=3)
    allowed_hosts: list[str] = Field(min_length=1, max_length=5)
    city: str
    venue: str
    address: str
    venue_source_url: str
    address_evidence: list[str] = Field(min_length=1)
    timezone: str = "Asia/Yekaterinburg"
    default_category: Category
    article_selector: str = "article.hcalendar"
    title_selector: str = ".entry-title a"
    detail_selector: str = ".entry-content"
    excluded_title_terms: list[str] = Field(default_factory=list)
    allowed_scene_terms: list[str] = Field(default_factory=list)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_urls(self) -> Self:
        ZoneInfo(self.timezone)
        for url in (self.listing_url, self.venue_source_url):
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or parts.hostname not in self.allowed_hosts
                or parts.username
                or parts.password
                or parts.port not in (None, 443)
            ):
                raise ValueError("Source URL must use HTTPS and an explicitly allowed hostname")
        return self

    def listing_urls(self, reference: datetime) -> list[str]:
        if not self.monthly_url_template:
            return [self.listing_url]
        local = reference.astimezone(ZoneInfo(self.timezone))
        urls = []
        for offset in range(self.months_ahead):
            year, month = divmod(local.year * 12 + local.month - 1 + offset, 12)
            urls.append(self.monthly_url_template.format(year=year, month=month + 1))
        return urls


def load_sources(path: Path) -> list[SourceConfig]:
    sources = [SourceConfig.model_validate(item) for item in json.loads(path.read_text("utf-8"))]
    if len({source.id for source in sources}) != len(sources):
        raise ValueError("Source IDs must be unique")
    return [source for source in sources if source.enabled]
