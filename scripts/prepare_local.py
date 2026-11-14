"""Create local settings once. Used inside Docker; existing credentials are preserved."""

import argparse
import os
import re
import secrets
from pathlib import Path


def prepare(root: Path) -> bool:
    target = root / ".env"
    if target.exists():
        return False
    content = (root / ".env.example").read_text(encoding="utf-8")
    password = secrets.token_hex(24)
    port_match = re.search(r"^POSTGRES_PORT=(\d+)$", content, flags=re.M)
    port = port_match[1] if port_match else "55432"
    values = {
        "POSTGRES_PASSWORD": password,
        "ADMIN_API_TOKEN": secrets.token_urlsafe(32),
        "DATABASE_URL": f"postgresql+asyncpg://bashculture:{password}@localhost:{port}/bashculture",
    }
    for key, value in values.items():
        content = re.sub(rf"^{key}=.*$", f"{key}={value}", content, flags=re.M)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    args = parser.parse_args()
    created = prepare(args.root)
    print("Created .env with private local credentials." if created else "Existing .env preserved.")
    print("Admin login key: ADMIN_API_TOKEN in .env. Secret values were not printed.")


if __name__ == "__main__":
    main()
