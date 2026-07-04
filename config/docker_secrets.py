"""Bootstrap and validate local Docker secret files."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = ROOT / "infra" / "docker" / "secrets"

REQUIRED_SECRETS = (
    "orc_api_key",
    "ollama_api_key",
    "rag_api_key",
    "qdrant_api_key",
    "audio_transcribe_api_key",
    "internal_api_key",
    "clickhouse_password",
    "grafana_password",
    "langfuse_db_password",
    "langfuse_nextauth_secret",
    "langfuse_salt",
)


def _token() -> str:
    return secrets.token_urlsafe(32)


def ensure_secrets(secrets_dir: Path = SECRETS_DIR) -> list[Path]:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name in REQUIRED_SECRETS:
        path = secrets_dir / name
        if not path.exists() or path.stat().st_size == 0:
            path.write_text(f"{_token()}\n", encoding="utf-8")
            created.append(path)
        os.chmod(path, 0o600)
    gitignore = secrets_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n!.gitignore\n", encoding="utf-8")
    return created


def validate_secrets(secrets_dir: Path = SECRETS_DIR) -> list[str]:
    errors: list[str] = []
    for name in REQUIRED_SECRETS:
        path = secrets_dir / name
        if not path.exists():
            errors.append(f"missing credential file: {name}")
            continue
        if path.stat().st_size == 0:
            errors.append(f"empty credential file: {name}")
            continue
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            errors.append(f"credential file {name} has mode {mode:o}, expected 600")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m config.docker_secrets")
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--dir", default=str(SECRETS_DIR))
    args = parser.parse_args(argv)

    secrets_dir = Path(args.dir).expanduser().resolve()
    if args.ensure:
        created = ensure_secrets(secrets_dir)
        if created:
            print(f"Generated {len(created)} Docker credential file(s)")
        else:
            print("Docker credential files ready")

    if args.validate or args.ensure:
        errors = validate_secrets(secrets_dir)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print("OK: Docker credential files are present")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
