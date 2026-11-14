"""Create a private deployment env without displaying or reusing development passwords."""

import argparse
import os
import re
import secrets
from pathlib import Path


def generate(domain: str, path: Path) -> None:
    if not re.fullmatch(
        r"(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}", domain
    ):
        raise ValueError("Use a DNS hostname without protocol, path or port")
    example = Path(__file__).resolve().parents[1] / ".env.production.example"
    content = example.read_text(encoding="utf-8")
    values = {
        "APP_DOMAIN": domain.lower(),
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "ADMIN_API_TOKEN": secrets.token_urlsafe(36),
    }
    for key, value in values.items():
        content = re.sub(rf"^{key}=.*$", f"{key}={value}", content, flags=re.M)
    # Exclusive creation: never replace existing credentials on a repeated command.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate .env.production with random secrets")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--output", type=Path, default=Path(".env.production"))
    args = parser.parse_args()
    try:
        generate(args.domain, args.output)
    except FileExistsError:
        parser.exit(1, "File already exists; existing secrets were preserved.\n")
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    print(
        f"Created {args.output}. Secret values were not printed. Fill Telegram and model settings."
    )


if __name__ == "__main__":
    main()
