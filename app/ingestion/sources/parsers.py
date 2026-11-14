import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from app.catalog.schemas import EventInput
from app.ingestion.normalize import normalize_text
from app.ingestion.sources.config import SourceConfig


@dataclass
class Candidate:
    payload: dict[str, Any]
    listing_url: str
    detail_url: str
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False


def parse_age(text: str) -> int | None:
    match = re.search(r"(?<!\d)(0|6|12|16|18)\s*\+", text)
    return int(match[1]) if match else None


def parse_duration(text: str) -> int | None:
    """Read an explicitly labelled duration, never infer it from genre or start times."""
    matches = re.findall(
        r"(?:продолжительность|длительность)(?:\s+(?:спектакля|концерта|мероприятия))?"
        r"(?:\s+(?:с\s+(?:двумя\s+)?антракт(?:ом|ами)|без\s+антракта))?\s*[:—–-]?\s*"
        r"(?:(\d{1,2})\s*(?:час\w*|ч\b)\.?\s*)?(?:(\d{1,3})\s*(?:минут\w*|мин\b)\.?)?",
        text.casefold(),
    )
    values = {int(hours or 0) * 60 + int(minutes or 0) for hours, minutes in matches}
    values.discard(0)
    return next(iter(values)) if len(values) == 1 and next(iter(values)) <= 1440 else None


def parse_price(text: str) -> tuple[Decimal | None, Decimal | None, bool | None]:
    text = re.sub(r"(?<=\d)[\s\u00a0\u202f](?=\d{3}(?:\D|$))", "", text.casefold())
    if "вход свободный" in text or "бесплатный вход" in text:
        return Decimal(0), Decimal(0), True
    values = re.findall(r"\d+(?:[.,]\d{1,2})?", text)
    if not values:
        return None, None, None
    numbers = [Decimal(value.replace(",", ".")) for value in values]
    if len(numbers) == 2 and numbers[1] >= numbers[0]:
        return numbers[0], numbers[1], numbers[1] == 0
    if len(numbers) != 1:
        return None, None, None
    lower = numbers[0]
    # A dangling dash in this plugin means 'from', not a fixed ticket price.
    if "от" in text or re.search(r"\d\s*[-–—]\s*(?:₽|руб|$)", text):
        return lower, None, False if lower > 0 else None
    return lower, lower, lower == 0


def category_for(title: str, default: str) -> str:
    title = title.casefold()
    if "спектакль" in title or "сказка" in title or "комедия" in title:
        return "theatre"
    if "концерт" in title or "музыкальная программа" in title:
        return "concert"
    return default


def base_payload(config: SourceConfig, title: str, start: datetime) -> dict[str, Any]:
    return {
        "title": title,
        "category": category_for(title, config.default_category),
        "date_start": start.isoformat(),
        "city": config.city,
        "venue": config.venue,
        "address": config.address,
        "description": f"{title}. {config.venue}, {config.city}. "
        f"Сеанс {start.strftime('%d.%m.%Y в %H:%M')}. "
        "Подробности и условия посещения — на сайте источника.",
    }


def wordpress_candidates(html: str, url: str, config: SourceConfig) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for article in soup.select(config.article_selector):
        time = article.select_one("abbr.dtstart[title]")
        title_node = article.select_one(config.title_selector)
        if time is None or title_node is None:
            continue
        start = datetime.strptime(str(time["title"]), "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo(config.timezone)
        )
        detail_url = urljoin(url, str(title_node.get("href", "")))
        title = title_node.get_text(" ", strip=True)
        payload = base_payload(config, title, start)
        # WordPress post/permalink identifies a scheduled entry independently of its start time.
        payload["external_id"] = str(article.get("id") or urlsplit(detail_url).path)
        payload["source_url"] = detail_url
        end_node = article.select_one("abbr.dtend[title]")
        warnings = []
        if end_node:
            end = datetime.strptime(str(end_node["title"]), "%Y-%m-%d %H:%M").replace(
                tzinfo=ZoneInfo(config.timezone)
            )
            if end > start:
                payload["date_end"] = end.isoformat()
            else:
                warnings.append(
                    "Окончание совпадает с началом или раньше него; сохранено неизвестным"
                )
        ticket = article.select_one(".event-ticket")
        text = ticket.get_text(" ", strip=True) if ticket else ""
        minimum, maximum, free = parse_price(text)
        payload.update(
            price_min=str(minimum) if minimum is not None else None,
            price_max=str(maximum) if maximum is not None else None,
            is_free=free,
        )
        payload["age_limit"] = parse_age(article.get_text(" ", strip=True))
        buy = next(
            (
                a
                for a in article.select("a[href]")
                if "купить билет" in a.get_text(" ", strip=True).casefold()
            ),
            None,
        )
        if buy:
            payload["org_link"] = urljoin(url, str(buy["href"]))
        candidates.append(Candidate(payload, url, detail_url, warnings))
    return candidates


def jsonld_objects(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [node for item in value for node in jsonld_objects(item)]
    if isinstance(value, dict):
        return [value, *jsonld_objects(value.get("@graph", []))]
    return []


def jsonld_candidates(html: str, url: str, config: SourceConfig) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for script in soup.select('script[type="application/ld+json"]'):
        for node in jsonld_objects(json.loads(script.get_text())):
            kinds = node.get("@type", [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if not any(kind in ("Event", "TheaterEvent", "MusicEvent") for kind in kinds):
                continue
            if any(
                term.casefold() in str(node.get("name", "")).casefold()
                for term in config.excluded_title_terms
            ):
                continue
            location = node.get("location", {})
            location_name = location.get("name", "") if isinstance(location, dict) else ""
            if location_name and normalize_text(location_name) != normalize_text(config.venue):
                continue  # Touring performances must not receive the home venue's address.
            start = datetime.fromisoformat(node["startDate"])
            if start.tzinfo is None:
                raise ValueError("JSON-LD event time is missing its timezone")
            detail = urljoin(url, node["url"])
            if config.allowed_scene_terms:
                local = start.astimezone(ZoneInfo(config.timezone))
                matching_scenes = []
                for show in soup.select("article.show[data-date]"):
                    show_link = show.select_one(".show__title a[href]")
                    show_time = show.select_one(".show__time")
                    if (
                        show_link
                        and show_time
                        and urljoin(url, str(show_link["href"])) == detail
                        and show.get("data-date") == local.strftime("%Y-%m-%d")
                        and show_time.get_text(strip=True) == local.strftime("%H:%M")
                    ):
                        scene = show.select_one(".show__meta")
                        matching_scenes.append(scene.get_text(" ", strip=True) if scene else "")
                if not matching_scenes or not all(
                    any(term.casefold() in scene.casefold() for term in config.allowed_scene_terms)
                    for scene in matching_scenes
                ):
                    continue
            payload = base_payload(config, node["name"], start)
            offers = node.get("offers", {})
            offer = offers[0] if isinstance(offers, list) and offers else offers
            ticket = offer.get("url") if isinstance(offer, dict) else None
            identifier = ticket or f"{detail}|{start.isoformat()}"
            payload["external_id"] = hashlib.sha256(identifier.encode()).hexdigest()
            payload["source_url"] = detail
            payload["age_limit"] = parse_age(str(node.get("typicalAgeRange", "")))
            if isinstance(offer, dict) and str(offer.get("priceCurrency", "")).upper() == "RUB":
                low = offer.get("lowPrice", offer.get("price"))
                high = offer.get("highPrice", offer.get("price"))
                if low is not None:
                    payload["price_min"] = str(low)
                if high is not None:
                    payload["price_max"] = str(high)
                if low is not None and high is not None:
                    payload["is_free"] = Decimal(str(low)) == Decimal(str(high)) == 0
            if ticket:
                payload["org_link"] = ticket
            end = node.get("endDate")
            if end:
                payload["date_end"] = end
            candidates.append(
                Candidate(
                    payload,
                    url,
                    detail,
                    [] if ticket else ["Нет устойчивого ID сеанса: идентификатор содержит время"],
                    str(node.get("eventStatus", "")).endswith("EventCancelled"),
                )
            )
    return candidates


def detail_confirms(candidate: Candidate, html: str, config: SourceConfig) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for unwanted in soup.select("script, style, nav, footer"):
        unwanted.decompose()
    title = normalize_text(candidate.payload["title"])
    headings = " ".join(node.get_text(" ", strip=True) for node in soup.select("h1, h2, h3"))
    if title not in normalize_text(headings):
        return False
    content = soup.select_one(config.detail_selector)
    if isinstance(content, Tag):
        text = content.get_text(" ", strip=True)
        duration = parse_duration(text)
        if duration is not None:
            candidate.payload["duration_min"] = duration
        if candidate.payload.get("price_min") is None:
            labelled_price = re.search(
                r"(?:стоимость\s+билетов?|цена\s+билетов?)\s*[:—–-]?\s*"
                r"((?:от\s*)?\d[\d\s.,]*(?:(?:до|[-–—])\s*\d[\d\s.,]*)?\s*(?:₽|руб\w*))",
                text,
                re.I,
            )
            if labelled_price:
                low, high, free = parse_price(labelled_price[1])
                candidate.payload.update(
                    price_min=str(low) if low is not None else None,
                    price_max=str(high) if high is not None else None,
                    is_free=free,
                )
    if isinstance(content, Tag) and candidate.payload.get("age_limit") is None:
        candidate.payload["age_limit"] = parse_age(content.get_text(" ", strip=True))
    if isinstance(content, Tag) and re.search(
        r"(?:сеанс|спектакль|концерт|мероприятие)\s+отмен[её]н[ао]?\b",
        content.get_text(" ", strip=True),
        re.I,
    ):
        candidate.cancelled = True
    # Validate the complete extraction after optional detail-page enrichment.
    EventInput.model_validate(candidate.payload)
    return True
