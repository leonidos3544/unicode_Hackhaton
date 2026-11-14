"""Extract short primary-source facts for assisted review, without auto-approving rows."""

import argparse
import asyncio
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.config import get_settings
from app.ingestion.normalize import normalize_text
from app.ingestion.sources.config import load_sources
from app.ingestion.sources.parsers import jsonld_objects
from app.storage.postgres.engine import build_database
from app.storage.postgres.tables import SourceSnapshot


async def gather(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    configs = {config.id: config for config in load_sources(Path("config/sources.json"))}
    manifest = await asyncio.to_thread(Path("data/open/manifest.json").read_text, encoding="utf-8")
    reference = datetime.fromisoformat(json.loads(manifest)["created_at"])
    engine, sessions = build_database(get_settings().database_url)
    try:
        async with sessions() as session:
            pages = (
                await session.scalars(
                    select(SourceSnapshot).order_by(SourceSnapshot.fetched_at.desc())
                )
            ).all()
            latest: dict[tuple[str, str], SourceSnapshot] = {}
            for page in pages:
                latest.setdefault((page.source_name, page.url), page)
            result = []
            for index, row in enumerate(rows, 1):
                config = configs[row["source"]]
                proof: dict[str, Any] = {}
                for url in config.listing_urls(reference):
                    listing = latest.get((config.id, url))
                    if listing is None:
                        continue
                    soup = BeautifulSoup(listing.body, "html.parser")
                    if config.adapter == "wordpress_events":
                        anchor = next(
                            (
                                a
                                for a in soup.select(config.title_selector)
                                if urljoin(url, str(a.get("href", ""))) == row["source_url"]
                            ),
                            None,
                        )
                        article = anchor.find_parent("article") if anchor else None
                        if article is None or anchor is None:
                            continue
                        start = article.select_one("abbr.dtstart[title]")
                        end = article.select_one("abbr.dtend[title]")
                        price = article.select_one(".event-ticket")
                        proof = {
                            "title": anchor.get_text(" ", strip=True),
                            "start": str(start["title"]) if start else None,
                            "end": str(end["title"]) if end else None,
                            "ticket_text": price.get_text(" ", strip=True) if price else None,
                            "age_marks": list(
                                dict.fromkeys(
                                    re.findall(
                                        r"(?<!\d)(?:0|6|12|16|18)\s*\+",
                                        article.get_text(" ", strip=True),
                                    )
                                )
                            )[:5],
                        }
                    else:
                        for script in soup.select('script[type="application/ld+json"]'):
                            for node in jsonld_objects(json.loads(script.get_text())):
                                if (
                                    node.get("name") == row["title"]
                                    and node.get("startDate")
                                    and datetime.fromisoformat(node["startDate"])
                                    == datetime.fromisoformat(row["date_start"])
                                ):
                                    proof = {
                                        key: node.get(key)
                                        for key in (
                                            "name",
                                            "startDate",
                                            "endDate",
                                            "typicalAgeRange",
                                            "eventStatus",
                                            "location",
                                            "offers",
                                        )
                                    }
                    if proof:
                        proof["listing_url"], proof["listing_sha256"] = url, listing.content_hash
                        break
                detail = latest.get((config.id, row["source_url"]))
                if detail:
                    soup = BeautifulSoup(detail.body, "html.parser")
                    for element in soup.select("script,style,nav,footer"):
                        element.decompose()
                    content = soup.select_one(config.detail_selector)
                    text = content.get_text(" ", strip=True) if content else ""
                    proof["detail_heading"] = " | ".join(
                        n.get_text(" ", strip=True)
                        for n in soup.select("h1,h2,h3")
                        if normalize_text(row["title"])
                        in normalize_text(n.get_text(" ", strip=True))
                    )
                    proof["duration_text"] = list(
                        dict.fromkeys(
                            re.findall(
                                r"(?:Продолжительность|Длительность)(?![а-я])[^.]{0,90}", text
                            )
                        )
                    )[:2]
                    proof["detail_age_marks"] = list(
                        dict.fromkeys(re.findall(r"(?<!\d)(?:0|6|12|16|18)\s*\+", text))
                    )[:5]
                    proof["detail_sha256"] = detail.content_hash
                venue = latest.get((config.id, config.venue_source_url))
                if venue:
                    text = BeautifulSoup(venue.body, "html.parser").get_text(" ", strip=True)
                    proof["venue_address_fragments"] = [
                        text[
                            max(
                                0, text.casefold().find(part.casefold()) - 15
                            ) : text.casefold().find(part.casefold()) + len(part) + 40
                        ]
                        for part in config.address_evidence
                        if part.casefold() in text.casefold()
                    ]
                    proof["venue_source_url"] = config.venue_source_url
                result.append(
                    {
                        "row": index,
                        "extracted": {
                            key: row.get(key)
                            for key in (
                                "source",
                                "external_id",
                                "title",
                                "date_start",
                                "date_end",
                                "price_min",
                                "price_max",
                                "age_limit",
                            )
                        },
                        "evidence": proof,
                    }
                )
            return result
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare source facts for review without auto-approval"
    )
    parser.add_argument("--from-row", type=int, default=1)
    parser.add_argument("--to-row", type=int, default=50)
    args = parser.parse_args()
    with Path("reports/sources/manual_source_review.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    records = asyncio.run(gather(rows))
    Path("reports/sources/evidence.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for record in records[args.from_row - 1 : args.to_row]:
        evidence = record["evidence"]
        # Full URLs/hashes are retained in the JSON pack; keep interactive review readable.
        compact = {k: v for k, v in evidence.items() if not k.endswith(("url", "sha256"))}
        print(
            json.dumps(
                {"row": record["row"], "extracted": record["extracted"], "primary_facts": compact},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
