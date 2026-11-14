import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.catalog.schemas import EventInput
from app.common.errors import AppError
from app.ingestion.files import parse_file
from app.ingestion.normalize import normalize_row, normalize_text
from app.search.models import SearchFilters
from tests.factories import event_payload


@pytest.mark.parametrize(
    "overrides",
    [
        {"price_min": -1},
        {"price_min": "1.001"},
        {"price_min": 300, "price_max": 200},
        {"is_free": True},
        {"is_free": False, "price_min": 0, "price_max": 0},
        {"age_limit": 7},
        {"title": "  "},
        {"description": "  "},
        {"date_start": "2030-01-01T12:00:00"},
        {"date_end": "2020-01-01T12:00:00+05:00"},
        {"source_url": "javascript:alert(1)"},
        {"source_url": "https://secret:password@example.org/event"},
        {"duration_min": 30},
        {"status": "published"},
    ],
)
def test_rejects_untrustworthy_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EventInput.model_validate(event_payload(**overrides))


def test_unknown_price_is_not_free() -> None:
    event = EventInput.model_validate(event_payload(price_min=None, price_max=None, age_limit=None))
    assert event.is_free is None
    assert event.age_limit is None


def test_free_and_duration_are_derived_only_from_explicit_facts() -> None:
    event = EventInput.model_validate(
        event_payload(is_free=True, price_min=None, price_max=None, date_end=None, duration_min=90)
    )
    assert event.price_min == event.price_max == Decimal(0)
    assert event.date_end is not None
    assert (event.date_end - event.date_start).total_seconds() == 5400


def test_csv_and_json_share_the_same_normalizer() -> None:
    csv = ("external_id,title,category,tags\n1,Название,Выставка,дети;живопись\n").encode()
    row = parse_file(csv, "events.csv", 100)[0]
    assert normalize_row(row)["category"] == "exhibition"
    assert normalize_row(row)["tags"] == ["дети", "живопись"]
    assert parse_file(json.dumps([row]).encode(), "events.json", 100) == [row]


@pytest.mark.parametrize(
    "data,filename",
    [
        (b"{}", "events.json"),
        (b"[]", "events.json"),
        (b"[1]", "events.json"),
        (b"[NaN]", "events.json"),
        (b'[{"price_min":NaN}]', "events.json"),
        (b"a,a\n1,2", "events.csv"),
        (b"a,b\n1,2,3", "events.csv"),
        (b"\xff", "events.csv"),
        (b"{}", "events.txt"),
    ],
)
def test_bad_files_are_user_errors(data: bytes, filename: str) -> None:
    with pytest.raises(AppError):
        parse_file(data, filename, 10)


def test_file_row_limit() -> None:
    with pytest.raises(AppError) as error:
        parse_file(b"[{},{},{}]", "events.json", 2)
    assert error.value.status == 413


def test_timezone_and_filter_bounds() -> None:
    with pytest.raises(ValidationError):
        SearchFilters(date_from="2030-01-01T12:00:00")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SearchFilters.model_validate({"page_size": 1000})
    assert normalize_text("  ЁЛКА — детям! ") == "елка детям"
