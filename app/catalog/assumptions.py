"""Synthetic data is opt-in, version-bound and never written to canonical fact columns."""

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NOTICE = (
    "ДЕМО: дополнительные значения придуманы для проверки сценария и не подтверждены организатором."
)


class AssumedValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_min: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    price_max: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    duration_min: int | None = Field(default=None, ge=1, le=1440)
    age_limit: Literal[0, 6, 12, 16, 18] | None = None


def make_assumptions(card: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if card.get("price_min") is None:
        values["price_min"] = "500.00"
    if card.get("price_max") is None:
        minimum = Decimal(str(card.get("price_min") or 500))
        values["price_max"] = str(max(Decimal(1000), minimum * 2).quantize(Decimal(".01")))
    if card.get("age_limit") is None:
        values["age_limit"] = (
            18  # Conservative demo choice; never pretend to verify child admission.
        )
    if card.get("date_end") is None:
        values["duration_min"] = {"theatre": 150, "concert": 120}.get(card["category"], 90)
    return AssumedValues.model_validate(values).model_dump(mode="json", exclude_none=True)


def apply_assumptions(card: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(card)
    meta = card.get("demo_assumptions") or {}
    if meta.get("event_version") != card["version"]:
        return result
    supplied = AssumedValues.model_validate(meta.get("values", {}))
    applied: list[str] = []
    confirmed: dict[str, Any] = {}
    for key, value in supplied.model_dump(mode="json", exclude_none=True).items():
        if key == "duration_min":
            if result.get("date_end") is None:
                start = datetime.fromisoformat(str(card["date_start"]))
                confirmed["date_end"] = None
                result["date_end"] = (start + timedelta(minutes=int(value))).isoformat()
                result["duration_min"] = int(value)
                applied.extend(["duration_min", "date_end"])
        elif result.get(key) is None:
            confirmed[key] = None
            result[key] = float(value) if key.startswith("price_") else value
            applied.append(key)
    if applied:
        result["demo_fields"] = applied
        result["confirmed_values"] = confirmed
        result["data_warnings"] = [NOTICE, *card.get("data_warnings", [])]
    return result
