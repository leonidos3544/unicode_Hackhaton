"""Portable binary PostgreSQL dump and isolated restore drill through Docker Compose."""

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def compose(production: bool) -> list[str]:
    if production:
        return [
            "docker",
            "compose",
            "--env-file",
            ".env.production",
            "-f",
            "compose.production.yaml",
        ]
    return ["docker", "compose"]


def execute(prefix: list[str], args: list[str], *, stdin=None, stdout=None) -> bytes:
    result = subprocess.run(
        [*prefix, "exec", "-T", "postgres", *args],
        cwd=ROOT,
        stdin=stdin,
        stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        # Never dump process environment or connection credentials into reports.
        raise RuntimeError(f"Database command failed ({args[0]}, exit {result.returncode})")
    return result.stdout or b""


def backup(prefix: list[str], path: Path) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        execute(prefix, ["pg_dump", "-U", "bashculture", "-d", "bashculture", "-Fc"], stdout=stream)
    data = path.read_bytes()
    if not data.startswith(b"PGDMP"):
        raise RuntimeError("Dump is not a PostgreSQL custom archive")
    with path.open("rb") as stream:
        contents = execute(prefix, ["pg_restore", "--list"], stdin=stream)
    if b"TABLE public events " not in contents:
        raise RuntimeError("Dump does not contain the event catalogue")
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "file": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "archive_list_checked": True,
    }


def verify_restore(prefix: list[str], path: Path) -> dict[str, object]:
    database = "bashculture_restore_test_" + uuid4().hex[:12]
    if not re.fullmatch(r"bashculture_restore_test_[a-f0-9]{12}", database):
        raise ValueError("Invalid isolated restore database name")
    execute(prefix, ["createdb", "-U", "bashculture", database])
    try:
        with path.open("rb") as stream:
            execute(
                prefix,
                [
                    "pg_restore",
                    "-U",
                    "bashculture",
                    "-d",
                    database,
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                ],
                stdin=stream,
            )
        sql = (
            "SELECT json_build_object('events',(SELECT count(*) FROM events),"
            "'venues',(SELECT count(*) FROM venues),"
            "'subscriptions',(SELECT count(*) FROM subscriptions),"
            "'assumptions',(SELECT count(*) FROM event_assumptions),"
            "'migration',(SELECT version_num FROM alembic_version))"
        )
        data = execute(
            prefix,
            ["psql", "-U", "bashculture", "-d", database, "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        )
        return {
            "restore_verified": True,
            "restored_counts": json.loads(data),
            "method": "Restored into a new temporary database; production database was untouched",
        }
    finally:
        # Only the exact random temporary database created above may be removed.
        execute(prefix, ["dropdb", "-U", "bashculture", "--if-exists", database])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backup PostgreSQL and optionally test a real restore"
    )
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "backups")
    args = parser.parse_args()
    name = datetime.now(UTC).strftime("bashculture-%Y%m%dT%H%M%SZ-") + uuid4().hex[:6]
    path = args.output.resolve() / (name + ".dump")
    prefix = compose(args.production)
    report = backup(prefix, path)
    if args.verify:
        report.update(verify_restore(prefix, path))
    path.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
