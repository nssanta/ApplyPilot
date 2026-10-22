"""Local-only secret storage helpers.

Secrets never leave this machine: they are written with mode 0600, replaced
atomically, and are never included in status payloads or logs.  Callers must
pass only labels/metadata to the UI; raw values stay inside these files.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SECRET_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def ensure_private_dir(path: Path) -> Path:
    """Creates a private directory and tightens its mode when possible."""
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    try:
        path.chmod(PRIVATE_DIR_MODE)
    except OSError:
        pass
    return path


def write_private_json(path: Path, payload: Any) -> Path:
    """Atomically writes JSON with 0600 permissions.

    A partially written credential file must never be readable by another
    process, so the temporary file is created with mkstemp and chmod'ed before
    the atomic replace.
    """
    ensure_private_dir(path.parent)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, SECRET_FILE_MODE)
        os.replace(temporary, path)
        os.chmod(path, SECRET_FILE_MODE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def read_private_json(path: Path) -> dict[str, Any] | None:
    """Reads a local JSON object; malformed or missing files return None."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def secret_state(value: str | None) -> str:
    """Returns a safe status marker instead of the secret itself."""
    return "set" if str(value or "").strip() else "missing"


def redact(value: str | None) -> str:
    """Never expose a secret; used by status/debug payloads."""
    return "***" if str(value or "").strip() else ""
