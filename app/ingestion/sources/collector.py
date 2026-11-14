from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.common.clock import utcnow
from app.ingestion.normalize import normalize_text
from app.ingestion.sources.config import SourceConfig
from app.ingestion.sources.http import USER_AGENT, FetchError, PageSnapshot, SourceClient
from app.ingestion.sources.parsers import (
    Candidate,
    detail_confirms,
    jsonld_candidates,
    wordpress_candidates,
)


@dataclass
class Collection:
    config: SourceConfig
    candidates: list[Candidate] = field(default_factory=list)
    snapshots: dict[str, PageSnapshot] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    found: int = 0
    venue_verified: bool = False


async def collect_source(
    config: SourceConfig,
    *,
    reference: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Collection:
    result = Collection(config)
    reference = reference or utcnow()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20),
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    ) as client:
        source = SourceClient(client, config)
        try:
            venue_page = await source.fetch(config.venue_source_url)
            text = normalize_text(
                BeautifulSoup(venue_page.body, "html.parser").get_text(" ", strip=True)
            )
            if not all(normalize_text(part) in text for part in config.address_evidence):
                raise FetchError("Адрес площадки не подтверждён страницей источника")
            result.venue_verified = True
            unique: dict[str, Candidate] = {}
            for url in config.listing_urls(reference):
                try:
                    page = await source.fetch(url)
                    parser = (
                        wordpress_candidates
                        if config.adapter == "wordpress_events"
                        else jsonld_candidates
                    )
                    extracted = parser(page.body, page.url, config)
                    if not extracted:
                        result.warnings.append(
                            {
                                "url": url,
                                "message": "Сеансы не обнаружены; проверьте разметку и период",
                            }
                        )
                    for candidate in extracted:
                        result.found += 1
                        start = datetime.fromisoformat(candidate.payload["date_start"])
                        if start <= reference:
                            continue
                        identity = candidate.payload["external_id"]
                        previous = unique.get(identity)
                        if previous and previous.payload != candidate.payload:
                            raise FetchError("Один внешний ID соответствует разным сеансам")
                        unique[identity] = candidate
                except (FetchError, ValueError, KeyError) as exc:
                    result.errors.append({"url": url, "message": str(exc)[:300]})
            for candidate in unique.values():
                try:
                    page = await source.fetch(candidate.detail_url)
                    if not detail_confirms(candidate, page.body, config):
                        raise FetchError("Заголовок карточки не подтверждает событие из афиши")
                    result.candidates.append(candidate)
                except (FetchError, ValueError) as exc:
                    result.errors.append({"url": candidate.detail_url, "message": str(exc)[:300]})
            result.snapshots = source.pages
        except FetchError as exc:
            result.errors.append({"url": config.venue_source_url, "message": str(exc)[:300]})
            result.snapshots = source.pages
    return result


def collection_summary(collection: Collection) -> dict[str, Any]:
    return {
        "source": collection.config.id,
        "found": collection.found,
        "verified_candidates": len(collection.candidates),
        "venue_verified": collection.venue_verified,
        "pages": len(collection.snapshots),
        "errors": collection.errors,
        "warnings": collection.warnings,
    }
