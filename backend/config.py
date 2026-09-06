"""Environment configuration, with a local .env for developer convenience.

Connection strings carry credentials, so none of them are defaulted in code. A
default like `postgres:postgres@localhost` is a real password on someone's
machine, and committing it teaches everyone who clones the repo to use it.

Precedence, highest first:
  1. a variable already set in the environment
  2. a line in .env at the repo root (gitignored)
  3. nothing — the feature is off, and the app says so on /api/health

Absence is a supported state everywhere: no Postgres means datasets are
generated in memory, no Redis means the in-process store. The app runs either
way; it just reports what it is running without.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

_loaded = False


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Read KEY=value lines into the environment without overwriting real ones."""
    global _loaded
    found: dict[str, str] = {}
    if not path.exists():
        _loaded = True
        return found

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        found[key] = value
        # an explicitly exported variable always wins over the file
        os.environ.setdefault(key, value)

    _loaded = True
    return found


def get(name: str, default: str | None = None) -> str | None:
    if not _loaded:
        load_env()
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def flag(name: str, default: bool = False) -> bool:
    raw = get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "on", "true", "yes")


load_env()
