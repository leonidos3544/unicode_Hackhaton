import csv
import json
from datetime import UTC, datetime

from app.cli.source_report import build_report
from tests.factories import event_payload


def test_report_computes_end_and_preserves_completed_reviews(tmp_path):
    data, output = tmp_path / "data", tmp_path / "report"
    data.mkdir()
    rows = [
        event_payload(external_id=str(i), title=f"Тест {i}", date_end=None, duration_min=90)
        for i in range(60)
    ]
    (data / "test.json").write_text(json.dumps(rows), encoding="utf-8")
    (data / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(UTC).isoformat(),
                "parser_version": "test",
                "sources": [{"source_id": "test"}],
            }
        ),
        encoding="utf-8",
    )
    report = build_report(data, output)
    assert report["validated_records"] == 60 and report["unknown_end_time"] == 0
    assert report["manual_accuracy_evaluation"] is None
    path = output / "manual_source_review.csv"
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reviews = list(csv.DictReader(stream))
    assert len(reviews) == 50 and reviews[0]["date_end"]
    reviews[0]["review_status"], reviews[0]["notes"] = "reviewed", "Human note"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=reviews[0].keys())
        writer.writeheader()
        writer.writerows(reviews)
    build_report(data, output)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        saved = list(csv.DictReader(stream))
    assert saved[0]["notes"] == "Human note" and saved[0]["review_status"] == "reviewed"
