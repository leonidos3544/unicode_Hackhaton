import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.ingestion.sources.collector import collect_source
from app.ingestion.sources.config import SourceConfig
from app.ingestion.sources.http import FetchError, SourceClient, validate_url
from app.ingestion.sources.parsers import (
    detail_confirms,
    jsonld_candidates,
    parse_price,
    wordpress_candidates,
)


def source_config(**overrides: object) -> SourceConfig:
    return SourceConfig.model_validate(
        {
            "id": "test-source",
            "name": "Тестовый источник",
            "adapter": "wordpress_events",
            "listing_url": "https://example.org/afisha/",
            "allowed_hosts": ["example.org"],
            "city": "Тестовый город",
            "venue": "Тестовая площадка",
            "address": "Тестовая улица, 1",
            "venue_source_url": "https://example.org/contacts/",
            "address_evidence": ["Тестовая улица", "1"],
            "default_category": "concert",
            **overrides,
        }
    )


def article_html(start: datetime, *, end: datetime | None = None, post_id: int = 1) -> str:
    return f'''<article class="hcalendar" id="post-{post_id}">
        <abbr class="dtstart" title="{start:%Y-%m-%d %H:%M}"></abbr>
        <abbr class="dtend" title="{(end or start):%Y-%m-%d %H:%M}"></abbr>
        <h3 class="entry-title"><a href="/events/{post_id}/">Тестовый концерт {post_id}</a></h3>
        <span class="event-ticket">Цена: 600 - 800 ₽</span>
        <a href="https://tickets.example.org/{post_id}">Купить билет</a></article>'''


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Цена: 600 - 800 ₽", (Decimal(600), Decimal(800), False)),
        ("Цена: 1 500 - 2 000 ₽", (Decimal(1500), Decimal(2000), False)),
        ("Цена: 400 - ₽", (Decimal(400), None, False)),
        ("от 300 рублей", (Decimal(300), None, False)),
        ("Цена: 300,50 ₽", (Decimal("300.50"), Decimal("300.50"), False)),
        ("Вход свободный", (Decimal(0), Decimal(0), True)),
        ("Бесплатно", (None, None, None)),  # Plugin default is insufficient proof.
        ("Цена не указана", (None, None, None)),
    ],
)
def test_source_price_semantics(text: str, expected: tuple[object, ...]) -> None:
    assert parse_price(text) == expected


def test_placeholder_end_and_stable_identity() -> None:
    start = datetime(2030, 1, 12, 18, 0, tzinfo=UTC)
    config = source_config()
    original = wordpress_candidates(article_html(start), config.listing_url, config)[0]
    moved = wordpress_candidates(
        article_html(start + timedelta(days=1)), config.listing_url, config
    )[0]
    assert "date_end" not in original.payload
    assert original.warnings
    assert original.payload["external_id"] == moved.payload["external_id"]
    assert original.payload["age_limit"] is None


def test_jsonld_distinguishes_sessions_and_skips_touring_venue() -> None:
    config = source_config(adapter="jsonld")
    event = {
        "@type": "TheaterEvent",
        "name": "Тест",
        "startDate": "2030-01-12T18:00:00+05:00",
        "url": "/event/test",
        "location": {"name": config.venue},
        "typicalAgeRange": "6+",
    }
    other_session = {**event, "startDate": "2030-01-12T20:00:00+05:00"}
    touring = {**event, "location": {"name": "Другой город"}}
    html = (
        '<script type="application/ld+json">'
        + json.dumps([event, other_session, touring])
        + "</script>"
    )
    result = jsonld_candidates(html, config.listing_url, config)
    assert len(result) == 2
    assert result[0].payload["external_id"] != result[1].payload["external_id"]
    assert result[0].payload["age_limit"] == 6


def test_detail_requires_title_and_reads_age_without_running_scripts() -> None:
    config = source_config()
    candidate = wordpress_candidates(
        article_html(datetime(2030, 1, 1, tzinfo=UTC)), config.listing_url, config
    )[0]
    html = '<header><h1>Тестовый концерт 1</h1></header><div class="entry-content">6+</div>'
    assert detail_confirms(candidate, html, config)
    assert candidate.payload["age_limit"] == 6
    assert not detail_confirms(candidate, "<h1>Другое мероприятие</h1>", config)


def test_jsonld_price_requires_explicit_currency_and_preserves_from_price() -> None:
    config = source_config(adapter="jsonld")
    event = {
        "@type": "TheaterEvent",
        "name": "Тест",
        "startDate": "2030-01-12T18:00:00+05:00",
        "url": "/event/test",
        "offers": {"@type": "AggregateOffer", "lowPrice": "300", "priceCurrency": "RUB"},
    }
    html = '<script type="application/ld+json">' + json.dumps(event) + "</script>"
    result = jsonld_candidates(html, config.listing_url, config)[0]
    assert result.payload["price_min"] == "300" and "price_max" not in result.payload
    event["offers"]["priceCurrency"] = "USD"
    html = '<script type="application/ld+json">' + json.dumps(event) + "</script>"
    assert "price_min" not in jsonld_candidates(html, config.listing_url, config)[0].payload


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/a",
        "https://127.0.0.1/a",
        "https://example.org.evil.test/a",
        "https://user:password@example.org/a",
        "https://example.org:8443/a",
    ],
)
def test_fetch_is_restricted_to_reviewed_https_hosts(url: str) -> None:
    with pytest.raises(FetchError):
        validate_url(url, ["example.org"])


async def test_robots_policy_and_external_redirect() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/internal"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        source = SourceClient(http, source_config())
        with pytest.raises(FetchError, match="robots"):
            await source.fetch("https://example.org/private/events")
        with pytest.raises(FetchError, match="HTTPS"):
            await source.fetch("https://example.org/redirect")


async def test_collector_verifies_venue_listing_and_detail() -> None:
    start = (datetime.now(UTC) + timedelta(days=10)).replace(second=0, microsecond=0)

    def handle(request: httpx.Request) -> httpx.Response:
        routes = {
            "/robots.txt": "User-agent: *\nDisallow:\n",
            "/contacts/": "Тестовая улица, 1",
            "/afisha/": article_html(start),
            "/events/1/": '<h1>Тестовый концерт 1</h1><div class="entry-content">12+</div>',
        }
        return httpx.Response(
            200 if request.url.path in routes else 404, text=routes.get(request.url.path, "")
        )

    result = await collect_source(source_config(), transport=httpx.MockTransport(handle))
    assert result.venue_verified
    assert len(result.candidates) == 1
    assert result.candidates[0].payload["age_limit"] == 12
    assert len(result.snapshots) == 3
    assert not result.errors


async def test_robots_query_rule_does_not_block_plain_homepage() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\r\r\nDisallow: /?\r\r\n")
        return httpx.Response(200, text="Public page")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        source = SourceClient(http, source_config())
        assert (await source.fetch("https://example.org/")).status_code == 200
        with pytest.raises(FetchError, match="robots"):
            await source.fetch("https://example.org/?private=1")


def test_home_address_requires_matching_schedule_scene() -> None:
    config = source_config(adapter="jsonld", allowed_scene_terms=["Большая сцена"])
    node = {
        "@type": "TheaterEvent",
        "name": "Тест",
        "startDate": "2030-01-12T18:00:00+05:00",
        "url": "/event/test",
        "location": {"name": config.venue},
    }
    html = '<script type="application/ld+json">' + json.dumps(node) + "</script>"
    show = """<article class="show" data-date="2030-01-12">
        <h3 class="show__title"><a href="/event/test">Тест</a></h3>
        <span class="show__time">18:00</span><p class="show__meta">{scene}</p></article>"""
    assert not jsonld_candidates(
        html + show.format(scene="Гастроли в другом городе"), config.listing_url, config
    )
    assert (
        len(
            jsonld_candidates(html + show.format(scene="Большая сцена"), config.listing_url, config)
        )
        == 1
    )
