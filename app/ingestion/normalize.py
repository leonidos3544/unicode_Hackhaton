import hashlib
import json
import re
import unicodedata
from typing import Any

from app.catalog.schemas import CATEGORIES


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.findall(r"\w+", text))


def content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in row.items() if value != ""}
    for field in ("age_limit", "duration_min"):
        value = result.get(field)
        if isinstance(value, str) and value.strip().isdigit():
            result[field] = int(value.strip())
    category = result.get("category")
    if isinstance(category, str):
        aliases = {normalize_text(name): code for code, name in CATEGORIES.items()}
        result["category"] = aliases.get(normalize_text(category), category)
    tags = result.get("tags")
    if isinstance(tags, str):
        result["tags"] = [tag.strip() for tag in tags.split(";") if tag.strip()]
    return result
