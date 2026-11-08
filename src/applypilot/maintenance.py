"""Safe housekeeping for the local data directory.

Only three kinds of data are ever touched:

* scan snapshots older than ``days`` inside ``snapshots/``;
* copies of vacancy payloads in ``vacancy_observations`` that the code never
  reads back (the newest observation of each vacancy keeps its payload);
* extra database copies in ``backups/`` (one fresh copy is kept).

Browser profiles, sessions, cookies, provider logins, configuration and
screening answers are never candidates: any path that looks like one is
refused even if it sits inside a cleaned directory.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import AppConfig

PROTECTED_MARKERS = (
    "profile", "session", "cookie", "storage_state", "providers", "chrome", "browser",
    "login", "token", "secret", "answers", "config",
)
MARKER_FILE = ".last_maintenance"
LATEST_BACKUP = "applypilot-latest.sqlite3"


def is_protected(path: Path) -> bool:
    """True for anything that may hold a login, a session or owner settings."""
    return any(marker in part.casefold() for part in path.parts for marker in PROTECTED_MARKERS)


def _old_snapshots(snapshots: Path, cutoff: float) -> list[Path]:
    if not snapshots.is_dir() or is_protected(snapshots):
        return []
    return sorted(
        path for path in snapshots.rglob("*")
        if path.is_file() and not path.is_symlink() and not is_protected(path.relative_to(snapshots))
        and path.stat().st_mtime < cutoff
    )


def _compact_database(db_path: Path, backups: Path, dry_run: bool) -> dict[str, Any]:
    if not db_path.exists():
        return {"skipped": "no database"}
    older = """payload_json!='{}' AND id NOT IN
               (SELECT max(id) FROM vacancy_observations GROUP BY account,source,vacancy_id,track)"""
    stale = f"SELECT count(*) FROM vacancy_observations WHERE {older}"
    clear = f"UPDATE vacancy_observations SET payload_json='{{}}' WHERE {older}"
    size_before = db_path.stat().st_size
    with sqlite3.connect(db_path, timeout=60) as conn:
        payloads = int(conn.execute(stale).fetchone()[0])
    result: dict[str, Any] = {"stale_payloads": payloads, "size_before": size_before}
    if dry_run:
        return result
    backups.mkdir(parents=True, exist_ok=True)
    backup_path = backups / LATEST_BACKUP
    source = sqlite3.connect(db_path, timeout=60)
    try:
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
        with source:
            source.execute(clear)
        source.execute("VACUUM")
        integrity = str(source.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        source.close()
    result.update({"backup": str(backup_path), "integrity": integrity, "size_after": db_path.stat().st_size})
    return result


def run_maintenance(config: AppConfig, *, days: int = 7, dry_run: bool = False,
                    now: float | None = None) -> dict[str, Any]:
    """Clean old snapshots, compact the database and keep one backup."""
    if days < 1:
        raise ValueError("days must be at least 1")
    now = time.time() if now is None else now
    data_dir = config.data_dir
    backups = data_dir / "backups"
    snapshots = _old_snapshots(config.snapshots_dir, now - days * 86400)
    extra_backups = sorted(
        path for path in backups.glob("*.sqlite3")
        if path.name != LATEST_BACKUP and not is_protected(path.relative_to(data_dir))
    ) if backups.is_dir() else []
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "snapshots_removed": len(snapshots),
        "snapshot_bytes": sum(path.stat().st_size for path in snapshots),
        "extra_backups_removed": [path.name for path in extra_backups],
    }
    report["database"] = _compact_database(config.db_path, backups, dry_run)
    if not dry_run:
        integrity = report["database"].get("integrity")
        if integrity not in (None, "ok"):
            report["error"] = "integrity check failed; nothing else removed"
            return report
        for path in [*snapshots, *extra_backups]:
            path.unlink(missing_ok=True)
        (data_dir / MARKER_FILE).write_text(str(int(now)), encoding="utf-8")
    return report


def maintenance_due(config: AppConfig, *, every_days: int = 7, now: float | None = None) -> bool:
    marker = config.data_dir / MARKER_FILE
    now = time.time() if now is None else now
    try:
        last = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return now - last >= every_days * 86400
