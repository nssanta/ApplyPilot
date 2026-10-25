from __future__ import annotations

import contextlib
import csv
import fcntl
import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, account TEXT NOT NULL, mode TEXT NOT NULL,
  input_path TEXT NOT NULL DEFAULT '', requested_limit INTEGER NOT NULL DEFAULT 0,
  selected_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
  stop_reason TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS run_items (
  run_id TEXT NOT NULL, vacancy_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
  score INTEGER NOT NULL DEFAULT 0, resume TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, vacancy_id), FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS attempts (
  account TEXT NOT NULL DEFAULT 'default', vacancy_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
  score INTEGER NOT NULL DEFAULT 0, resume TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY (account, vacancy_id)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, vacancy_id TEXT NOT NULL,
  status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_legacy_event
  ON events(account, vacancy_id, status, note, run_id, created_at);
CREATE TABLE IF NOT EXISTS reservations (
  account TEXT NOT NULL, vacancy_id TEXT NOT NULL, run_id TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY (account, vacancy_id)
);
CREATE TABLE IF NOT EXISTS negotiation_statuses (
  account TEXT NOT NULL, vacancy_id TEXT NOT NULL, status TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', resume TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (account, vacancy_id)
);
CREATE TABLE IF NOT EXISTS sync_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL DEFAULT 'default',
  source TEXT NOT NULL, status TEXT NOT NULL, item_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_imports (
  account TEXT NOT NULL, source_sha256 TEXT NOT NULL, source_path TEXT NOT NULL,
  physical_data_lines INTEGER NOT NULL, logical_rows INTEGER NOT NULL,
  imported_at TEXT NOT NULL, PRIMARY KEY (account, source_sha256)
);
CREATE TABLE IF NOT EXISTS campaign_runs (
  campaign_id TEXT PRIMARY KEY, account TEXT NOT NULL, source TEXT NOT NULL,
  track TEXT NOT NULL, status TEXT NOT NULL, config_json TEXT NOT NULL,
  snapshot_path TEXT NOT NULL DEFAULT '', previous_campaign_id TEXT NOT NULL DEFAULT '',
  item_count INTEGER NOT NULL DEFAULT 0, first_seen_count INTEGER NOT NULL DEFAULT 0,
  new_count INTEGER NOT NULL DEFAULT 0, updated_count INTEGER NOT NULL DEFAULT 0,
  known_count INTEGER NOT NULL DEFAULT 0, matched_count INTEGER NOT NULL DEFAULT 0,
  maybe_count INTEGER NOT NULL DEFAULT 0, rejected_count INTEGER NOT NULL DEFAULT 0,
  applied_count INTEGER NOT NULL DEFAULT 0, error_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_campaign_history
  ON campaign_runs(account, source, track, started_at DESC);
CREATE TABLE IF NOT EXISTS vacancies (
  account TEXT NOT NULL, source TEXT NOT NULL, vacancy_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
  review_state TEXT NOT NULL DEFAULT 'UNREVIEWED', review_reason TEXT NOT NULL DEFAULT '',
  review_notes TEXT NOT NULL DEFAULT '', application_state TEXT NOT NULL DEFAULT 'NOT_STARTED',
  application_note TEXT NOT NULL DEFAULT '',
  conversation_state TEXT NOT NULL DEFAULT 'NOT_CHECKED', next_action TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, last_campaign_id TEXT NOT NULL DEFAULT '',
  last_checked_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
  PRIMARY KEY(account, source, vacancy_id)
);
CREATE TABLE IF NOT EXISTS vacancy_track_scores (
  account TEXT NOT NULL, source TEXT NOT NULL, vacancy_id TEXT NOT NULL, track TEXT NOT NULL,
  score INTEGER NOT NULL DEFAULT 0, score_decision TEXT NOT NULL DEFAULT 'unknown',
  filter_status TEXT NOT NULL DEFAULT 'unknown', reasons_json TEXT NOT NULL DEFAULT '[]',
  problems_json TEXT NOT NULL DEFAULT '[]', filter_reasons_json TEXT NOT NULL DEFAULT '[]',
  review_state TEXT NOT NULL DEFAULT 'UNREVIEWED', review_reason TEXT NOT NULL DEFAULT '',
  review_notes TEXT NOT NULL DEFAULT '',
  resume TEXT NOT NULL DEFAULT '', campaign_id TEXT NOT NULL DEFAULT '',
  latest_change TEXT NOT NULL DEFAULT 'first_seen', last_seen_at TEXT NOT NULL,
  PRIMARY KEY(account, source, vacancy_id, track),
  FOREIGN KEY(account, source, vacancy_id) REFERENCES vacancies(account, source, vacancy_id)
    ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vacancy_track_queue
  ON vacancy_track_scores(account, track, score DESC, last_seen_at DESC);
CREATE TABLE IF NOT EXISTS vacancy_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
  account TEXT NOT NULL, source TEXT NOT NULL, vacancy_id TEXT NOT NULL, track TEXT NOT NULL,
  change_kind TEXT NOT NULL, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  UNIQUE(campaign_id, account, source, vacancy_id)
);
CREATE INDEX IF NOT EXISTS idx_vacancy_observation_history
  ON vacancy_observations(account, source, vacancy_id, observed_at DESC);
CREATE TABLE IF NOT EXISTS workflow_tasks (
  task_id TEXT PRIMARY KEY, account TEXT NOT NULL, source TEXT NOT NULL,
  vacancy_id TEXT NOT NULL, track TEXT NOT NULL, task_type TEXT NOT NULL,
  status TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', draft_text TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_task_queue
  ON workflow_tasks(account, task_type, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS inbox_messages (
  account TEXT NOT NULL, source TEXT NOT NULL, thread_id TEXT NOT NULL, message_id TEXT NOT NULL,
  vacancy_id TEXT NOT NULL DEFAULT '', sent_at TEXT NOT NULL DEFAULT '', sender_type TEXT NOT NULL DEFAULT 'unknown',
  sender_name TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, raw_json TEXT NOT NULL DEFAULT '{}',
  first_seen_at TEXT NOT NULL, observed_at TEXT NOT NULL,
  PRIMARY KEY(account, source, thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_inbox_messages_thread
  ON inbox_messages(account, source, thread_id, sent_at);
CREATE TABLE IF NOT EXISTS vacancy_links (
  account TEXT NOT NULL, source TEXT NOT NULL, vacancy_id TEXT NOT NULL,
  group_key TEXT NOT NULL DEFAULT '', channel_key TEXT NOT NULL DEFAULT '',
  channel_type TEXT NOT NULL DEFAULT '', application_url TEXT NOT NULL DEFAULT '',
  mirror_source TEXT NOT NULL DEFAULT '', mirror_native_id TEXT NOT NULL DEFAULT '',
  duplicate_of_source TEXT NOT NULL DEFAULT '', duplicate_of_vacancy_id TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account, source, vacancy_id)
);
CREATE INDEX IF NOT EXISTS idx_vacancy_links_group
  ON vacancy_links(account, group_key, source);
CREATE INDEX IF NOT EXISTS idx_vacancy_links_channel
  ON vacancy_links(account, channel_key);
CREATE TABLE IF NOT EXISTS source_application_attempts (
  attempt_id TEXT PRIMARY KEY, account TEXT NOT NULL, source TEXT NOT NULL,
  vacancy_id TEXT NOT NULL, track TEXT NOT NULL, channel_key TEXT NOT NULL,
  channel_type TEXT NOT NULL DEFAULT 'native_form', status TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'run', note TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(account, source, vacancy_id, track, channel_key)
);
CREATE INDEX IF NOT EXISTS idx_source_attempts_queue
  ON source_application_attempts(account, source, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_attempts_channel
  ON source_application_attempts(account, channel_key, status);
CREATE TABLE IF NOT EXISTS source_reservations (
  account TEXT NOT NULL, source TEXT NOT NULL, vacancy_id TEXT NOT NULL,
  track TEXT NOT NULL, channel_key TEXT NOT NULL, run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(account, source, vacancy_id, track)
);
CREATE TABLE IF NOT EXISTS channel_reservations (
  account TEXT NOT NULL, channel_key TEXT NOT NULL, source TEXT NOT NULL,
  vacancy_id TEXT NOT NULL, track TEXT NOT NULL, run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(account, channel_key)
);
CREATE INDEX IF NOT EXISTS idx_channel_reservations_run
  ON channel_reservations(account, run_id);
CREATE TABLE IF NOT EXISTS external_accounts (
  provider TEXT NOT NULL,
  account TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  checked_at TEXT NOT NULL,
  PRIMARY KEY(provider, account)
);
CREATE TABLE IF NOT EXISTS telegram_messages (
  account TEXT NOT NULL,
  chat TEXT NOT NULL,
  message_id INTEGER NOT NULL,
  sent_at TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  buttons_json TEXT NOT NULL DEFAULT '[]',
  vacancy_refs_json TEXT NOT NULL DEFAULT '[]',
  application_likely INTEGER NOT NULL DEFAULT 0,
  has_callback INTEGER NOT NULL DEFAULT 0,
  outgoing INTEGER NOT NULL DEFAULT -1,
  first_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account, chat, message_id)
);
CREATE INDEX IF NOT EXISTS idx_telegram_messages_account
  ON telegram_messages(account, sent_at DESC);
"""

AUTO_APPROVAL_REASON = "FIT by private search criteria; full description and conditions verified"


def needs_manual_reapproval(item: dict[str, Any]) -> bool:
    """Invalidate stale automatic approvals when current title policy is stricter."""
    return (
        item.get("review_state") == "APPROVED"
        and item.get("review_reason") == AUTO_APPROVAL_REASON
        and item.get("auto_apply_eligible") is not True
    )

def _vacancy_group_key(company: str, title: str) -> str:
    normalized = f"{company or ''}|{title or ''}".casefold()
    normalized = re.sub(r"[^0-9a-zа-яё]+", " ", normalized).strip()
    if not normalized or normalized == "|":
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def canonical_channel_key(value: str) -> str:
    """Normalizes an application channel so one destination has one writer lock.

    Native forms are scoped by source (``hh.ru:123``); external ATS destinations
    are shared across every board that links to the same canonical URL.
    """
    key = str(value or "").strip()
    if not key:
        return ""
    if key.startswith("ats:"):
        raw = key[4:].strip()
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").lower()
        path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
        return f"ats:{host}{path}"
    if ":" in key:
        source, native = key.split(":", 1)
        return f"{source.strip().lower()}:{native.strip()}"
    return key


TERMINAL_STATUSES = {"success", "already_applied"}
BLOCKED_STATUSES = TERMINAL_STATUSES | {"unknown", "submitting"}
# Explicit attempt transitions.  A numeric rank is deliberately not used:
# ``submitting -> unknown`` is a valid outcome, while ``unknown -> success``
# requires an evidence-backed reconciliation call.
APPLICATION_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"prepared", "submitting", "failed_before_submit", "needs_manual",
           "skipped", "already_applied", "unknown"},
    "prepared": {"submitting", "failed_before_submit", "needs_manual", "skipped"},
    "submitting": {"success", "unknown", "failed_before_submit", "needs_manual"},
    "unknown": set(),
    "failed_before_submit": {"prepared", "submitting", "needs_manual", "skipped"},
    "needs_manual": {"prepared", "submitting", "success", "unknown", "skipped"},
    "success": set(),
    "already_applied": set(),
    "skipped": {"prepared", "submitting"},
}
APPLICATION_STATUSES = set(APPLICATION_TRANSITIONS) - {None} | {"prepared"}
# Reservations are transient in-flight locks.  Once an outcome is recorded the
# attempt row itself is the durable guard, so the locks can be released.
RELEASABLE_AFTER = {"success", "already_applied", "unknown", "failed_before_submit",
                    "needs_manual", "skipped"}
STATUS_RANK = {
    "prepared": 0,
    "skipped": 0,
    "failed_before_submit": 1,
    "needs_manual": 1,
    "unknown": 2,
    "submitting": 3,
    "already_applied": 4,
    "success": 4,
}
NEGOTIATION_STATUSES = {
    "not_viewed", "viewed", "invitation", "discard", "phone_interview", "interview",
}
LEGACY_STATUS_MAP = {
    "timeout": "unknown",
    "error": "failed_before_submit",
    "no_button": "needs_manual",
    "closed": "skipped",
    "dry-run": "prepared",
}
REVIEW_STATES = {"UNREVIEWED", "FIT", "MAYBE", "SKIP", "APPROVED", "DEFERRED"}
CAMPAIGN_VIEWS = {
    "all", "new", "top", "review", "deferred", "approved", "ready", "needs_letter",
    "needs_form", "external_ats", "unknown", "applied", "skip",
}


def campaign_application_state(status: str, note: str = "") -> str | None:
    """Переводит технический результат попытки в отдельную ось состояния отклика."""
    normalized = str(status or "").strip().lower()
    if normalized == "success":
        return "APPLIED_CONFIRMED"
    if normalized == "already_applied":
        return "ALREADY_APPLIED"
    if normalized == "unknown":
        return "UNKNOWN"
    if normalized == "failed_before_submit":
        return "FAILED_BEFORE_SUBMIT"
    if normalized == "needs_manual":
        reason = str(note or "").casefold()
        if "external ats" in reason or "внешн" in reason:
            return "EXTERNAL_ATS"
        if "letter" in reason or "cover" in reason or "сопровод" in reason:
            return "NEEDS_LETTER"
        if "form" in reason or "question" in reason or "screening" in reason or "анкет" in reason:
            return "NEEDS_FORM"
    return None


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value] if isinstance(value, str) and value else []


@dataclass(frozen=True)
class ImportReport:
    source_sha256: str
    physical_data_lines: int
    logical_rows: int
    events_added: int
    already_imported: bool


def campaign_fit_bucket(filter_status: str, score_decision: str) -> str:
    """Разделяет FIT / MAYBE / SKIP без пересекающихся счётчиков."""
    if "reject" in {str(filter_status), str(score_decision)}:
        return "skip"
    if filter_status == "pass" and score_decision == "pass":
        return "fit"
    return "maybe"


def review_lane_for(filter_status: str, score_decision: str, *,
                    application_state: str = "", description_status: str = "") -> str:
    """Storage-side fallback for legacy rows without a persisted lane.

    Mirrors :func:`applypilot.multisource.review_lane` without importing the
    scanner (which would create a storage <-> multisource cycle).
    """
    if application_state in {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN"}:
        return "APPLIED"
    if "reject" in {str(filter_status), str(score_decision)}:
        return "SKIP"
    if filter_status == "pass" and score_decision == "pass":
        return "FIT"
    if score_decision == "pass" and filter_status == "unknown":
        return "GOOD_UNKNOWN"
    if score_decision == "maybe" and filter_status in {"pass", "unknown"}:
        return "NEEDS_ENRICHMENT"
    if description_status in {"missing", "provisional"}:
        return "NEEDS_ENRICHMENT"
    return "MANUAL_REVIEW"


def now() -> str:
    return datetime.now(UTC).isoformat()


def _legacy_status(value: str) -> str:
    status = value.strip() or "unknown"
    if status.startswith("skipped_"):
        return "skipped"
    return LEGACY_STATUS_MAP.get(status, status)


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._run_lock_depth = 0
        self._run_lock_owner: int | None = None

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        self._migrate(conn)
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sync_snapshots)")}
        if "account" not in columns:
            conn.execute("ALTER TABLE sync_snapshots ADD COLUMN account TEXT NOT NULL DEFAULT 'default'")
        negotiation_columns = {row["name"] for row in conn.execute("PRAGMA table_info(negotiation_statuses)")}
        if "resume" not in negotiation_columns:
            conn.execute("ALTER TABLE negotiation_statuses ADD COLUMN resume TEXT NOT NULL DEFAULT ''")
        telegram_columns = {row["name"] for row in conn.execute("PRAGMA table_info(telegram_messages)")}
        if "outgoing" not in telegram_columns:
            conn.execute("ALTER TABLE telegram_messages ADD COLUMN outgoing INTEGER NOT NULL DEFAULT -1")
        score_columns = {row["name"] for row in conn.execute("PRAGMA table_info(vacancy_track_scores)")}
        added_review_state = "review_state" not in score_columns
        for column, definition in (
            ("review_state", "TEXT NOT NULL DEFAULT 'UNREVIEWED'"),
            ("review_reason", "TEXT NOT NULL DEFAULT ''"),
            ("review_notes", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in score_columns:
                conn.execute(f"ALTER TABLE vacancy_track_scores ADD COLUMN {column} {definition}")
        if added_review_state:
            # До появления трековой оси review хранился на вакансии целиком.
            # Переносим его в уже существующие треки и очищаем legacy-поля,
            # чтобы новый трек не унаследовал решение от другого профиля.
            conn.execute(
                """UPDATE vacancy_track_scores AS s SET review_state=(
                       SELECT v.review_state FROM vacancies AS v
                       WHERE v.account=s.account AND v.source=s.source AND v.vacancy_id=s.vacancy_id),
                   review_reason=(SELECT v.review_reason FROM vacancies AS v
                       WHERE v.account=s.account AND v.source=s.source AND v.vacancy_id=s.vacancy_id),
                   review_notes=(SELECT v.review_notes FROM vacancies AS v
                       WHERE v.account=s.account AND v.source=s.source AND v.vacancy_id=s.vacancy_id)
                   WHERE EXISTS (SELECT 1 FROM vacancies AS v WHERE v.account=s.account
                       AND v.source=s.source AND v.vacancy_id=s.vacancy_id
                       AND v.review_state!='UNREVIEWED')"""
            )
            conn.execute(
                "UPDATE vacancies SET review_state='UNREVIEWED',review_reason='',review_notes='' "
                "WHERE review_state!='UNREVIEWED'"
            )

    def read_statuses(self, account: str = "default") -> dict[str, str]:
        """Читает существующий журнал без создания базы или схемы."""
        if not self.path.exists():
            return {}
        uri = f"file:{self.path.resolve()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            return {}
        conn.row_factory = sqlite3.Row
        try:
            return {row["vacancy_id"]: row["status"] for row in conn.execute(
                "SELECT vacancy_id,status FROM attempts WHERE account=?", (account,))}
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    def journaled_vacancy_ids(self) -> set[str]:
        """Читает все vacancy ID, когда-либо записанные в журнал откликов."""
        if not self.path.exists():
            return set()
        uri = f"file:{self.path.resolve()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            return set()
        try:
            found: set[str] = set()
            for table in ("attempts", "events", "run_items", "negotiation_statuses"):
                try:
                    found.update(str(row[0]) for row in conn.execute(
                        f"SELECT DISTINCT vacancy_id FROM {table}") if row[0])
                except sqlite3.Error:
                    continue
            return found
        finally:
            conn.close()

    def import_csv(self, path: Path, account: str = "default") -> ImportReport:
        """Импортирует один legacy CSV ровно один раз на account и digest источника."""
        with path.open("rb") as fh:
            digest = hashlib.file_digest(fh, "sha256").hexdigest()
        physical_data_lines = max(0, path.read_bytes().count(b"\n") - 1)
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        with self.connect() as conn:
            imported = conn.execute(
                "SELECT 1 FROM legacy_imports WHERE account=? AND source_sha256=?", (account, digest)
            ).fetchone()
            if imported:
                return ImportReport(digest, physical_data_lines, len(rows), 0, True)
            events_added = 0
            for row_number, row in enumerate(rows, 1):
                vacancy_id = str(row.get("vacancy_id") or row.get("id") or "").strip()
                if not vacancy_id:
                    continue
                status = _legacy_status(str(row.get("status") or "unknown"))
                timestamp = str(row.get("timestamp") or "1970-01-01T00:00:00+00:00")
                run_id = f"legacy-import:{digest[:16]}:{row_number}"
                existing = conn.execute(
                    "SELECT status FROM attempts WHERE account=? AND vacancy_id=?", (account, vacancy_id)
                ).fetchone()
                values = (
                    row.get("name", ""), row.get("company", ""), row.get("url", ""),
                    int(row.get("score") or 0), row.get("resume", ""), status, row.get("note", ""),
                    run_id, timestamp,
                )
                if existing is None:
                    conn.execute(
                        """INSERT INTO attempts
                        (account,vacancy_id,name,company,url,score,resume,status,note,run_id,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (account, vacancy_id, *values[:8], timestamp, values[8]),
                    )
                elif STATUS_RANK.get(status, 0) >= STATUS_RANK.get(existing["status"], 0):
                    conn.execute(
                        """UPDATE attempts SET name=?,company=?,url=?,score=?,resume=?,status=?,note=?,
                        run_id=?,updated_at=? WHERE account=? AND vacancy_id=?""",
                        (*values, account, vacancy_id),
                    )
                inserted = conn.execute(
                    """INSERT OR IGNORE INTO events(account,vacancy_id,status,note,run_id,created_at)
                    VALUES(?,?,?,?,?,?)""",
                    (account, vacancy_id, status, row.get("note", ""), run_id, timestamp),
                )
                events_added += int(inserted.rowcount > 0)
            conn.execute(
                """INSERT INTO legacy_imports
                (account,source_sha256,source_path,physical_data_lines,logical_rows,imported_at)
                VALUES(?,?,?,?,?,?)""",
                (account, digest, str(path.resolve()), physical_data_lines, len(rows), now()),
            )
        return ImportReport(digest, physical_data_lines, len(rows), events_added, False)

    def statuses(self, account: str = "default") -> dict[str, str]:
        return self.read_statuses(account) if self.path.exists() else {}

    def negotiation_ids(self, account: str = "default") -> set[str]:
        """Читает vacancy ID из HH negotiations в read-only режиме.

        Любая вакансия в negotiation ledger уже имеет отклик от этого account,
        независимо от HH-статуса, поэтому автоматически повторять её нельзя.
        """
        if not self.path.exists():
            return set()
        uri = f"file:{self.path.resolve()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            return set()
        conn.row_factory = sqlite3.Row
        try:
            return {row["vacancy_id"] for row in conn.execute(
                "SELECT vacancy_id FROM negotiation_statuses WHERE account=?", (account,))}
        except sqlite3.Error:
            return set()
        finally:
            conn.close()

    def negotiation_details(self, account: str = "default") -> dict[str, dict[str, str]]:
        """Читает последний синхронизированный HH-статус и резюме каждой вакансии."""
        if not self.path.exists():
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT vacancy_id,status,resume,updated_at FROM negotiation_statuses WHERE account=?",
                (account,),
            ).fetchall()
            return {row["vacancy_id"]: {"status": row["status"], "resume": row["resume"],
                                        "updated_at": row["updated_at"]} for row in rows}

    def attempt_details(self, account: str = "default") -> dict[str, dict[str, str]]:
        """Читает сохранённые результаты откликов, включая резюме, выбранное при отправке."""
        if not self.path.exists():
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT vacancy_id,status,resume,note,updated_at FROM attempts WHERE account=?",
                (account,),
            ).fetchall()
            return {row["vacancy_id"]: {"status": row["status"], "resume": row["resume"],
                                        "note": row["note"], "updated_at": row["updated_at"]}
                    for row in rows}

    def blocked_ids(self, account: str = "default") -> set[str]:
        blocked = {
            vacancy_id for vacancy_id, status in self.read_statuses(account).items()
            if status in BLOCKED_STATUSES
        }
        # Всё, что уже есть в HH negotiations после ручного или прошлого отклика, блокируется.
        blocked |= self.negotiation_ids(account)
        return blocked

    def start_run(self, run_id: str, account: str, mode: str, input_path: Path,
                  requested_limit: int, items: list[dict[str, Any]]) -> None:
        timestamp = now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO runs(run_id,account,mode,input_path,requested_limit,selected_count,status,started_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, account, mode, str(input_path.resolve()), requested_limit, len(items), "running", timestamp),
            )
            for ordinal, item in enumerate(items, 1):
                conn.execute(
                    """INSERT INTO run_items
                    (run_id,vacancy_id,ordinal,name,company,url,score,resume,status,note,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, str(item.get("id", "")), ordinal, item.get("name", ""), item.get("company", ""),
                     item.get("url", ""), int(item.get("score", 0)), item.get("resume", ""), "prepared", "",
                     timestamp, timestamp),
                )

    def finish_run(self, run_id: str, status: str, stop_reason: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET status=?,stop_reason=?,finished_at=? WHERE run_id=?",
                (status, stop_reason, now(), run_id),
            )

    def mark_run_item(self, run_id: str, vacancy_id: str, status: str, note: str = "") -> None:
        if not run_id:
            return
        with self.connect() as conn:
            conn.execute(
                "UPDATE run_items SET status=?,note=?,updated_at=? WHERE run_id=? AND vacancy_id=?",
                (status, note, now(), run_id, vacancy_id),
            )

    def run_summary(self, run_id: str) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                return None
            counts = {
                row["status"]: row["count"] for row in conn.execute(
                    "SELECT status,COUNT(*) AS count FROM run_items WHERE run_id=? GROUP BY status", (run_id,)
                )
            }
        return {"run": dict(run), "counts": counts}

    def record(self, item: dict[str, Any], status: str, note: str = "", run_id: str = "",
               account: str = "default") -> None:
        timestamp = now()
        vacancy_id = str(item.get("id", ""))
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO attempts
                (account,vacancy_id,name,company,url,score,resume,status,note,run_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account,vacancy_id) DO UPDATE SET
                name=excluded.name, company=excluded.company, url=excluded.url,
                score=excluded.score, resume=excluded.resume, status=excluded.status,
                note=excluded.note, run_id=excluded.run_id, updated_at=excluded.updated_at""",
                (account, vacancy_id, item.get("name", ""), item.get("company", ""), item.get("url", ""),
                 int(item.get("score", 0)), item.get("resume", ""), status, note, run_id, timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO events(account,vacancy_id,status,note,run_id,created_at) VALUES(?,?,?,?,?,?)",
                (account, vacancy_id, status, note, run_id, timestamp),
            )
            if run_id:
                conn.execute(
                    "UPDATE run_items SET status=?,note=?,updated_at=? WHERE run_id=? AND vacancy_id=?",
                    (status, note, timestamp, run_id, vacancy_id),
                )
            if status != "submitting":
                conn.execute(
                    "DELETE FROM reservations WHERE account=? AND vacancy_id=?",
                    (account, vacancy_id),
                )
            campaign_state = campaign_application_state(status, note)
            if campaign_state:
                conn.execute(
                    "UPDATE vacancies SET application_state=?,updated_at=? "
                    "WHERE account=? AND source='hh.ru' AND vacancy_id=?",
                    (campaign_state, timestamp, account, vacancy_id),
                )
                task_type = {"NEEDS_LETTER": "letter", "NEEDS_FORM": "form"}.get(campaign_state)
                if task_type:
                    track = str(item.get("track") or "").strip()
                    tracks = conn.execute(
                        "SELECT track FROM vacancy_track_scores WHERE account=? AND source='hh.ru' "
                        "AND vacancy_id=? AND (?='' OR track=?)",
                        (account, vacancy_id, track, track),
                    ).fetchall()
                    for track_row in tracks:
                        track = str(track_row["track"])
                        task_id = hashlib.sha256(
                            f"{account}:hh.ru:{vacancy_id}:{track}:{task_type}".encode()
                        ).hexdigest()[:32]
                        payload = json.dumps({"reason": note, "manual_review_required": True},
                                             ensure_ascii=False)
                        conn.execute(
                            """INSERT INTO workflow_tasks
                            (task_id,account,source,vacancy_id,track,task_type,status,payload_json,
                             created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(task_id) DO UPDATE SET payload_json=excluded.payload_json,
                              updated_at=excluded.updated_at
                              WHERE workflow_tasks.status NOT IN ('APPROVED','SENT_CONFIRMED')""",
                            (task_id, account, "hh.ru", vacancy_id, track, task_type,
                             "NEEDS_MANUAL", payload, timestamp, timestamp),
                        )
            if status == "success" and item.get("cover_letter_submitted") is True:
                track = str(item.get("track") or "").strip()
                conn.execute(
                    "UPDATE workflow_tasks SET status='SENT_CONFIRMED',updated_at=? "
                    "WHERE account=? AND source='hh.ru' AND vacancy_id=? AND task_type='letter' "
                    "AND (?='' OR track=?) AND status='APPROVED'",
                    (timestamp, account, vacancy_id, track, track),
                )

    def reserve(self, item: dict[str, Any], run_id: str, per_run: int, per_day: int,
                account: str = "default") -> tuple[bool, str]:
        """Атомарно выполняет dedupe и резервирует одну потенциальную отправку."""
        vacancy_id = str(item.get("id", ""))
        if not vacancy_id:
            return False, "missing vacancy id"
        timestamp = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status FROM attempts WHERE account=? AND vacancy_id=?", (account, vacancy_id)
            ).fetchone()
            if existing and existing["status"] != "submitting":
                # Завершённый результат при необходимости защищён таблицей ``attempts``;
                # временная in-flight блокировка не должна мешать будущей ручной попытке.
                conn.execute(
                    "DELETE FROM reservations WHERE account=? AND vacancy_id=?",
                    (account, vacancy_id),
                )
            if existing and existing["status"] in BLOCKED_STATUSES:
                return False, f"already handled ({existing['status']})"
            day_prefix = datetime.now(UTC).date().isoformat()
            run_count = conn.execute(
                """SELECT COUNT(DISTINCT vacancy_id) FROM events
                WHERE account=? AND run_id=? AND status='submitting'""",
                (account, run_id),
            ).fetchone()[0]
            day_count = conn.execute(
                """SELECT COUNT(DISTINCT vacancy_id) FROM events
                WHERE account=? AND status='submitting' AND created_at LIKE ?""",
                (account, f"{day_prefix}%"),
            ).fetchone()[0]
            if run_count >= per_run:
                return False, f"run budget exhausted ({per_run})"
            if day_count >= per_day:
                return False, f"daily budget exhausted ({per_day})"
            try:
                conn.execute(
                    "INSERT INTO reservations(account,vacancy_id,run_id,created_at) VALUES(?,?,?,?)",
                    (account, vacancy_id, run_id, timestamp),
                )
            except sqlite3.IntegrityError:
                return False, "already reserved"
            values = (
                account, vacancy_id, item.get("name", ""), item.get("company", ""), item.get("url", ""),
                int(item.get("score", 0)), item.get("resume", ""), "submitting", "submission started",
                run_id, timestamp, timestamp,
            )
            conn.execute(
                """INSERT INTO attempts
                (account,vacancy_id,name,company,url,score,resume,status,note,run_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account,vacancy_id) DO UPDATE SET status='submitting',run_id=excluded.run_id,
                note=excluded.note,updated_at=excluded.updated_at""",
                values,
            )
            conn.execute(
                "INSERT INTO events(account,vacancy_id,status,note,run_id,created_at) VALUES(?,?,?,?,?,?)",
                (account, vacancy_id, "submitting", "submission started", run_id, timestamp),
            )
            conn.execute(
                "UPDATE run_items SET status='submitting',note='submission started',updated_at=? "
                "WHERE run_id=? AND vacancy_id=?",
                (timestamp, run_id, vacancy_id),
            )
        return True, "reserved"

    def count(self, account: str = "default") -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) n FROM attempts WHERE account=? GROUP BY status", (account,)
            )
            return {row["status"]: row["n"] for row in rows}

    def event_count(self, account: str = "default") -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM events WHERE account=?", (account,)).fetchone()[0])

    def save_sync_snapshot(self, source: str, status: str, item_count: int = 0,
                           error: str = "", account: str = "default") -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO sync_snapshots(account,source,status,item_count,error,created_at)
                VALUES(?,?,?,?,?,?)""",
                (account, source, status, item_count, error, now()),
            )

    def replace_negotiation_statuses(self, rows: list[dict[str, Any]], account: str = "default",
                                     *, complete: bool = True, snapshot_status: str | None = None,
                                     snapshot_source: str = "hh.ru", snapshot_error: str = "") -> None:
        """Атомарно сохраняет набор страниц negotiations и при необходимости sync snapshot."""
        fetched_at = now()
        normalized: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            vacancy_id = str(row.get("vacancy_id") or row.get("id") or "")
            if vacancy_id:
                normalized.append((vacancy_id, row))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if complete:
                conn.execute("DELETE FROM negotiation_statuses WHERE account=?", (account,))
            for vacancy_id, row in normalized:
                conn.execute(
                    """INSERT INTO negotiation_statuses
                    (account,vacancy_id,status,name,company,resume,updated_at,fetched_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(account,vacancy_id) DO UPDATE SET status=excluded.status,
                    name=excluded.name,company=excluded.company,resume=excluded.resume,updated_at=excluded.updated_at,
                    fetched_at=excluded.fetched_at""",
                    (account, vacancy_id, str(row.get("status", "")), row.get("name", ""),
                     row.get("company", ""), str(row.get("resume", "")),
                     str(row.get("updated_at", "")), fetched_at),
                )
                conn.execute(
                    """UPDATE vacancies SET application_state='APPLIED_CONFIRMED',
                         application_note=?,last_checked_at=?,updated_at=?
                       WHERE account=? AND source='hh.ru' AND vacancy_id=?
                         AND application_state NOT IN ('UNKNOWN','ALREADY_APPLIED')""",
                    (f"confirmed by HH negotiations ({row.get('status', 'unknown')})",
                     fetched_at, fetched_at, account, vacancy_id),
                )
            if snapshot_status is not None:
                conn.execute(
                    """INSERT INTO sync_snapshots(account,source,status,item_count,error,created_at)
                    VALUES(?,?,?,?,?,?)""",
                    (account, snapshot_source, snapshot_status, len(normalized), snapshot_error, now()),
                )

    def reconcile_unknowns_from_negotiations(self, account: str = "default") -> list[str]:
        """Переводит неоднозначные отправки в подтверждённые по журналу HH negotiations."""
        timestamp = now()
        reconciled: list[str] = []
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT a.vacancy_id,a.run_id,n.status AS negotiation_status
                FROM attempts a JOIN negotiation_statuses n
                  ON n.account=a.account AND n.vacancy_id=a.vacancy_id
                WHERE a.account=? AND a.status='unknown'
                  AND n.status IN (?, ?, ?, ?, ?, ?)
                  AND n.fetched_at >= a.updated_at""",
                (account, *sorted(NEGOTIATION_STATUSES)),
            ).fetchall()
            for row in rows:
                vacancy_id = row["vacancy_id"]
                run_id = row["run_id"]
                note = f"confirmed by HH negotiations ({row['negotiation_status']})"
                conn.execute(
                    """UPDATE attempts SET status='success',note=?,updated_at=?
                    WHERE account=? AND vacancy_id=? AND status='unknown'""",
                    (note, timestamp, account, vacancy_id),
                )
                conn.execute(
                    "INSERT INTO events(account,vacancy_id,status,note,run_id,created_at) VALUES(?,?,?,?,?,?)",
                    (account, vacancy_id, "success", note, run_id, timestamp),
                )
                # HH evidence resolved the ambiguity: the persistent vacancy
                # must be marked confirmed, not left in an UNKNOWN quarantine.
                conn.execute(
                    """UPDATE vacancies
                    SET application_state='APPLIED_CONFIRMED', application_note=?,
                        last_checked_at=?,updated_at=?
                    WHERE account=? AND source='hh.ru' AND vacancy_id=?
                      AND application_state='UNKNOWN'""",
                    (note, timestamp, timestamp, account, vacancy_id),
                )
                if run_id:
                    conn.execute(
                        """UPDATE run_items SET status='success',note=?,updated_at=?
                        WHERE run_id=? AND vacancy_id=? AND status='unknown'""",
                        (note, timestamp, run_id, vacancy_id),
                    )
                    remaining = conn.execute(
                        "SELECT COUNT(*) FROM run_items WHERE run_id=? AND status='prepared'",
                        (run_id,),
                    ).fetchone()[0]
                    stop_note = (f"unknown vacancy {vacancy_id} confirmed by negotiations; "
                                 f"unprocessed candidates={remaining}")
                    conn.execute(
                        """UPDATE runs SET status='stopped_reconciled',stop_reason=?
                        WHERE run_id=? AND status='stopped_unknown'""",
                        (stop_note, run_id),
                    )
                reconciled.append(vacancy_id)
        return reconciled

    def recover_interrupted_runs(self, account: str = "default") -> list[str]:
        """Восстанавливает попытки submitting после получения вызывающим кодом run lock."""
        if self._run_lock_depth <= 0 or self._run_lock_owner != threading.get_ident():
            raise RuntimeError("run lock required")
        timestamp = now()
        recovered: list[str] = []
        run_ids: set[str] = set()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT vacancy_id,run_id FROM attempts
                WHERE account=? AND status='submitting' ORDER BY vacancy_id""",
                (account,),
            ).fetchall()
            for row in rows:
                vacancy_id = row["vacancy_id"]
                run_id = row["run_id"]
                note = "submission interrupted before outcome"
                conn.execute(
                    """UPDATE attempts SET status='unknown',note=?,updated_at=?
                    WHERE account=? AND vacancy_id=? AND status='submitting'""",
                    (note, timestamp, account, vacancy_id),
                )
                conn.execute(
                    """INSERT INTO events(account,vacancy_id,status,note,run_id,created_at)
                    VALUES(?,?,?,?,?,?)""",
                    (account, vacancy_id, "unknown", note, run_id, timestamp),
                )
                if run_id:
                    run_ids.add(run_id)
                    conn.execute(
                        """UPDATE run_items SET status='unknown',note=?,updated_at=?
                        WHERE run_id=? AND vacancy_id=?""",
                        (note, timestamp, run_id, vacancy_id),
                    )
                conn.execute(
                    "DELETE FROM reservations WHERE account=? AND vacancy_id=?",
                    (account, vacancy_id),
                )
                recovered.append(vacancy_id)
            for run_id in run_ids:
                conn.execute(
                    """UPDATE runs SET status='interrupted',stop_reason=?,finished_at=?
                    WHERE run_id=? AND account=? AND status='running'""",
                    ("interrupted submissions recovered", timestamp, run_id, account),
                )
        return recovered

    def latest_sync(self, account: str = "default", source: str = "hh.ru") -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.connect() as conn:
            row = conn.execute(
                """SELECT source,status,item_count,error,created_at FROM sync_snapshots
                WHERE account=? AND source=? ORDER BY id DESC LIMIT 1""",
                (account, source),
            ).fetchone()
            return dict(row) if row else None

    def vacancy_payload(self, account: str, source: str, vacancy_id: str) -> dict[str, Any]:
        """Возвращает последний сохранённый payload вакансии без изменения БД."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM vacancies WHERE account=? AND source=? AND vacancy_id=?",
                (account, source, str(vacancy_id)),
            ).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def vacancy_payloads(self, account: str, source: str,
                         vacancy_ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(str(value) for value in vacancy_ids if str(value)))
        if not ids:
            return {}
        found: dict[str, dict[str, Any]] = {}
        with self.connect() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT vacancy_id,payload_json FROM vacancies "
                    f"WHERE account=? AND source=? AND vacancy_id IN ({marks})",
                    (account, source, *chunk),
                ).fetchall()
                for row in rows:
                    try:
                        item = json.loads(row["payload_json"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(item, dict):
                        found[str(row["vacancy_id"])] = item
        return found

    @staticmethod
    def _channel_is_applied(conn: sqlite3.Connection, account: str, channel_key: str,
                            *, source: str = "", vacancy_id: str = "",
                            mirror_source: str = "", mirror_native_id: str = "") -> tuple[str, str]:
        """Returns the existing source/id that already owns a blocking channel.

        This is deliberately a channel check, not a company/title check.  Two
        boards may legitimately expose the same role through independent forms.
        """
        row = conn.execute(
            "SELECT source,vacancy_id FROM source_application_attempts "
            "WHERE account=? AND channel_key=? AND status IN "
            "('success','already_applied','unknown','submitting') "
            "ORDER BY updated_at DESC LIMIT 1",
            (account, channel_key),
        ).fetchone()
        if row:
            return str(row["source"]), str(row["vacancy_id"])
        hh_source = mirror_source or ("hh.ru" if channel_key.startswith("hh.ru:") else "")
        hh_id = mirror_native_id or (channel_key.split(":", 1)[1] if channel_key.startswith("hh.ru:") else "")
        if hh_source == "hh.ru" and hh_id:
            legacy = conn.execute(
                "SELECT status FROM attempts WHERE account=? AND vacancy_id=?",
                (account, hh_id),
            ).fetchone()
            negotiation = conn.execute(
                "SELECT 1 FROM negotiation_statuses WHERE account=? AND vacancy_id=?",
                (account, hh_id),
            ).fetchone()
            if negotiation or (legacy and legacy["status"] in BLOCKED_STATUSES):
                return "hh.ru", hh_id
        if source and vacancy_id:
            known = conn.execute(
                "SELECT application_state FROM vacancies WHERE account=? AND source=? AND vacancy_id=?",
                (account, source, vacancy_id),
            ).fetchone()
            if known and known["application_state"] in {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN"}:
                return source, vacancy_id
        return "", ""

    def save_vacancy_links(self, account: str, source: str,
                           items: list[dict[str, Any]]) -> int:
        """Persists job groups and application-channel links without hiding rows."""
        timestamp = now()
        saved = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for item in items:
                if not isinstance(item, dict):
                    continue
                vacancy_id = str(item.get("id") or item.get("vacancy_id") or "").strip()
                if not vacancy_id:
                    continue
                channel_key = str(item.get("application_channel") or f"{source}:{vacancy_id}")
                mirror_source = str(item.get("mirror_source") or "")
                mirror_id = str(item.get("mirror_native_id") or "")
                duplicate_source, duplicate_id = self._channel_is_applied(
                    conn, account, channel_key, source=source, vacancy_id=vacancy_id,
                    mirror_source=mirror_source, mirror_native_id=mirror_id,
                )
                conn.execute(
                    """INSERT INTO vacancy_links
                    (account,source,vacancy_id,group_key,channel_key,channel_type,application_url,
                     mirror_source,mirror_native_id,duplicate_of_source,duplicate_of_vacancy_id,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(account,source,vacancy_id) DO UPDATE SET
                      group_key=excluded.group_key,channel_key=excluded.channel_key,
                      channel_type=excluded.channel_type,application_url=excluded.application_url,
                      mirror_source=excluded.mirror_source,mirror_native_id=excluded.mirror_native_id,
                      duplicate_of_source=excluded.duplicate_of_source,
                      duplicate_of_vacancy_id=excluded.duplicate_of_vacancy_id,updated_at=excluded.updated_at""",
                    (account, source, vacancy_id,
                     str(item.get("job_group_key") or _vacancy_group_key(
                         str(item.get("company") or ""), str(item.get("name") or item.get("title") or "")
                     )),
                     channel_key, str(item.get("application_channel_type") or "native_form"),
                     str(item.get("application_url") or ""), mirror_source, mirror_id,
                     duplicate_source, duplicate_id, timestamp),
                )
                saved += 1
        return saved

    def vacancy_link(self, account: str, source: str, vacancy_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM vacancy_links WHERE account=? AND source=? AND vacancy_id=?",
                (account, source, str(vacancy_id)),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _release_source_locks(conn: sqlite3.Connection, account: str, source: str,
                              vacancy_id: str, track: str, channel_key: str = "") -> None:
        conn.execute(
            "DELETE FROM source_reservations WHERE account=? AND source=? AND vacancy_id=? AND track=?",
            (account, source, str(vacancy_id), track),
        )
        channel = canonical_channel_key(channel_key)
        if channel:
            conn.execute(
                "DELETE FROM channel_reservations WHERE account=? AND channel_key=? "
                "AND source=? AND vacancy_id=? AND track=?",
                (account, channel, source, str(vacancy_id), track),
            )
        else:
            conn.execute(
                "DELETE FROM channel_reservations WHERE account=? AND source=? AND vacancy_id=? AND track=?",
                (account, source, str(vacancy_id), track),
            )

    def channel_conflict(self, account: str, source: str, vacancy_id: str,
                         channel_key: str, *, mirror_source: str = "",
                         mirror_native_id: str = "") -> dict[str, Any] | None:
        """Returns a *current* duplicate/reservation conflict, never a cached flag."""
        channel = canonical_channel_key(channel_key)
        if not channel:
            return {"kind": "missing_channel", "source": source, "vacancy_id": str(vacancy_id)}
        with self.connect() as conn:
            duplicate_source, duplicate_id = self._channel_is_applied(
                conn, account, channel, source=source, vacancy_id=str(vacancy_id),
                mirror_source=mirror_source, mirror_native_id=mirror_native_id,
            )
            if duplicate_source:
                return {"kind": "applied_or_quarantined", "source": duplicate_source,
                        "vacancy_id": duplicate_id, "channel_key": channel}
            reservation = conn.execute(
                "SELECT source,vacancy_id,track,run_id FROM channel_reservations "
                "WHERE account=? AND channel_key=?",
                (account, channel),
            ).fetchone()
            if reservation:
                same = (str(reservation["source"]) == source
                        and str(reservation["vacancy_id"]) == str(vacancy_id))
                return {"kind": "same_run" if same else "reserved",
                        "source": str(reservation["source"]),
                        "vacancy_id": str(reservation["vacancy_id"]),
                        "track": str(reservation["track"]),
                        "run_id": str(reservation["run_id"]),
                        "channel_key": channel}
        return None

    def reserve_source_application(self, account: str, source: str, vacancy_id: str,
                                   track: str, channel_key: str, run_id: str,
                                   *, mirror_source: str = "",
                                   mirror_native_id: str = "",
                                   per_run: int = 100,
                                   per_day: int = 300) -> tuple[bool, str]:
        """Atomically reserves one canonical channel writer lock.

        The lock is ``(account, canonical channel)`` and is independent of the
        source/track that discovered the vacancy.  Two boards linking to the
        same ATS destination therefore cannot both reserve it, while HH and
        Habr native forms remain separate channels.  Source budgets are
        checked in the same write transaction as the reservation. Submitting,
        unknown, successful and already-applied attempts consume budget;
        known pre-submit failures do not.
        """
        channel = canonical_channel_key(channel_key)
        if not channel:
            return False, "missing channel key"
        try:
            per_run = int(per_run)
            per_day = int(per_day)
        except (TypeError, ValueError):
            return False, "invalid source application budget"
        if per_run < 1 or per_day < 1:
            return False, "invalid source application budget"
        timestamp = now()
        try:
            current_time = datetime.fromisoformat(timestamp)
        except ValueError:
            current_time = datetime.now(UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        day_start = current_time.astimezone(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()
        consuming_statuses = ("submitting", "unknown", "success", "already_applied")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            link = conn.execute(
                "SELECT mirror_source,mirror_native_id FROM vacancy_links "
                "WHERE account=? AND source=? AND vacancy_id=?",
                (account, source, str(vacancy_id)),
            ).fetchone()
            if link:
                mirror_source = mirror_source or str(link["mirror_source"] or "")
                mirror_native_id = mirror_native_id or str(link["mirror_native_id"] or "")
            duplicate_source, duplicate_id = self._channel_is_applied(
                conn, account, channel, source=source, vacancy_id=str(vacancy_id),
                mirror_source=mirror_source, mirror_native_id=mirror_native_id,
            )
            if duplicate_source:
                return False, f"channel already used by {duplicate_source}:{duplicate_id}"
            existing_channel = conn.execute(
                "SELECT source,vacancy_id,track,run_id FROM channel_reservations "
                "WHERE account=? AND channel_key=?",
                (account, channel),
            ).fetchone()
            if existing_channel:
                same = (str(existing_channel["source"]) == source
                        and str(existing_channel["vacancy_id"]) == str(vacancy_id)
                        and str(existing_channel["track"]) == track
                        and str(existing_channel["run_id"]) == str(run_id))
                if same:
                    return True, "already reserved for this run"
                return False, (
                    "channel reserved by "
                    f"{existing_channel['source']}:{existing_channel['vacancy_id']} "
                    f"run {existing_channel['run_id']}"
                )
            existing_source = conn.execute(
                "SELECT run_id FROM source_reservations WHERE account=? AND source=? "
                "AND vacancy_id=? AND track=?",
                (account, source, str(vacancy_id), track),
            ).fetchone()
            if existing_source:
                if str(existing_source["run_id"]) == str(run_id):
                    conn.execute(
                        "INSERT OR IGNORE INTO channel_reservations"
                        "(account,channel_key,source,vacancy_id,track,run_id,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (account, channel, source, str(vacancy_id), track, run_id, timestamp),
                    )
                    return True, "already reserved for this run"
                return False, "reserved by another run"

            # Count the union of durable attempts and active reservations so
            # the short interval between reserve() and recording submitting
            # still consumes capacity. UNION deduplicates that in-flight item.
            run_count = int(conn.execute(
                """SELECT COUNT(*) FROM (
                     SELECT vacancy_id,track,channel_key FROM source_application_attempts
                     WHERE account=? AND source=? AND run_id=? AND status IN (?,?,?,?)
                     UNION
                     SELECT vacancy_id,track,channel_key FROM source_reservations
                     WHERE account=? AND source=? AND run_id=?
                   )""",
                (account, source, run_id, *consuming_statuses,
                 account, source, run_id),
            ).fetchone()[0])
            if run_count >= per_run:
                return False, f"source per-run limit reached ({run_count}/{per_run})"
            day_count = int(conn.execute(
                """SELECT COUNT(*) FROM (
                     SELECT vacancy_id,track,channel_key FROM source_application_attempts
                     WHERE account=? AND source=? AND updated_at>=? AND status IN (?,?,?,?)
                     UNION
                     SELECT vacancy_id,track,channel_key FROM source_reservations
                     WHERE account=? AND source=? AND created_at>=?
                   )""",
                (account, source, day_start, *consuming_statuses,
                 account, source, day_start),
            ).fetchone()[0])
            if day_count >= per_day:
                return False, f"source per-day limit reached ({day_count}/{per_day})"
            try:
                conn.execute(
                    "INSERT INTO channel_reservations"
                    "(account,channel_key,source,vacancy_id,track,run_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (account, channel, source, str(vacancy_id), track, run_id, timestamp),
                )
                conn.execute(
                    "INSERT INTO source_reservations"
                    "(account,source,vacancy_id,track,channel_key,run_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (account, source, str(vacancy_id), track, channel, run_id, timestamp),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return False, "channel already reserved"
        return True, "reserved"

    def record_source_application(self, account: str, source: str, vacancy_id: str,
                                  track: str, channel_key: str, status: str,
                                  *, note: str = "", run_id: str = "",
                                  mode: str = "run") -> bool:
        """Records an explicit state transition; dry-run never calls this.

        ``unknown`` is a quarantine: it blocks an automatic retry and can only
        become ``success`` through :meth:`reconcile_source_application` with
        evidence.  ``success`` is immutable.
        """
        normalized = str(status or "").strip().lower()
        if normalized not in APPLICATION_STATUSES:
            raise ValueError(f"unknown application status: {status}")
        channel = canonical_channel_key(channel_key)
        if not channel:
            raise ValueError("channel_key is required")
        timestamp = now()
        attempt_id = hashlib.sha256(
            f"{account}:{source}:{vacancy_id}:{track}:{channel}".encode()
        ).hexdigest()[:32]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT status FROM source_application_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            prior_status = str(prior["status"]) if prior else None
            if prior_status == normalized:
                conn.execute(
                    "UPDATE source_application_attempts SET note=?,mode=?,run_id=?,updated_at=? "
                    "WHERE attempt_id=?",
                    (str(note)[:500], mode, str(run_id), timestamp, attempt_id),
                )
                if normalized in RELEASABLE_AFTER:
                    self._release_source_locks(conn, account, source, vacancy_id, track, channel)
                return True
            allowed = APPLICATION_TRANSITIONS.get(prior_status, set())
            if normalized not in allowed:
                return False
            conn.execute(
                """INSERT INTO source_application_attempts
                (attempt_id,account,source,vacancy_id,track,channel_key,channel_type,status,mode,note,
                 run_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(attempt_id) DO UPDATE SET status=excluded.status,mode=excluded.mode,
                  note=excluded.note,run_id=excluded.run_id,updated_at=excluded.updated_at""",
                (attempt_id, account, source, str(vacancy_id), track, channel, "native_form",
                 normalized, mode, str(note)[:500], str(run_id), timestamp, timestamp),
            )
            if normalized in RELEASABLE_AFTER:
                self._release_source_locks(conn, account, source, vacancy_id, track, channel)
            application_state = campaign_application_state(normalized, note)
            if application_state:
                conn.execute(
                    "UPDATE vacancies SET application_state=?,application_note=?,last_checked_at=?,updated_at=? "
                    "WHERE account=? AND source=? AND vacancy_id=?",
                    (application_state, str(note)[:500], timestamp, timestamp,
                     account, source, str(vacancy_id)),
                )
        return True

    def reconcile_source_application(self, account: str, source: str, vacancy_id: str,
                                     track: str, channel_key: str, evidence: str,
                                     *, note: str = "", run_id: str = "") -> bool:
        """Promotes a quarantined ``unknown`` to ``success`` only with evidence."""
        proof = str(evidence or "").strip()
        if len(proof) < 3:
            return False
        channel = canonical_channel_key(channel_key)
        attempt_id = hashlib.sha256(
            f"{account}:{source}:{vacancy_id}:{track}:{channel}".encode()
        ).hexdigest()[:32]
        timestamp = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT status FROM source_application_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if not prior or str(prior["status"]) != "unknown":
                return False
            proof_note = f"reconciled: {proof}"[:500]
            conn.execute(
                "UPDATE source_application_attempts SET status='success',note=?,run_id=?,updated_at=? "
                "WHERE attempt_id=? AND status='unknown'",
                (proof_note, str(run_id), timestamp, attempt_id),
            )
            self._release_source_locks(conn, account, source, vacancy_id, track, channel)
            conn.execute(
                "UPDATE vacancies SET application_state='APPLIED_CONFIRMED',application_note=?,"
                "last_checked_at=?,updated_at=? WHERE account=? AND source=? AND vacancy_id=?",
                (proof_note, timestamp, timestamp, account, source, str(vacancy_id)),
            )
        return True

    def recover_stale_source_reservations(self, max_age_seconds: int = 900,
                                          account: str | None = None) -> list[dict[str, Any]]:
        """Quarantines interrupted submissions instead of blindly retrying them."""
        cutoff = (datetime.now(UTC) - timedelta(seconds=max(1, int(max_age_seconds)))).isoformat()
        recovered: list[dict[str, Any]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            query = ("SELECT account,source,vacancy_id,track,channel_key,run_id,created_at "
                     "FROM source_reservations WHERE created_at<?")
            params: list[Any] = [cutoff]
            if account:
                query += " AND account=?"
                params.append(account)
            for row in conn.execute(query, params).fetchall():
                channel = canonical_channel_key(str(row["channel_key"]))
                attempt_id = hashlib.sha256(
                    f"{row['account']}:{row['source']}:{row['vacancy_id']}:"
                    f"{row['track']}:{channel}".encode()
                ).hexdigest()[:32]
                attempt = conn.execute(
                    "SELECT status FROM source_application_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                if attempt and str(attempt["status"]) == "submitting":
                    conn.execute(
                        "UPDATE source_application_attempts SET status='unknown',"
                        "note='interrupted: stale reservation recovered; quarantined',"
                        "updated_at=? WHERE attempt_id=? AND status='submitting'",
                        (now(), attempt_id),
                    )
                self._release_source_locks(
                    conn, str(row["account"]), str(row["source"]), str(row["vacancy_id"]),
                    str(row["track"]), channel,
                )
                recovered.append({
                    "account": str(row["account"]), "source": str(row["source"]),
                    "vacancy_id": str(row["vacancy_id"]), "track": str(row["track"]),
                    "channel_key": channel, "run_id": str(row["run_id"]),
                    "status": "quarantined",
                })
            # A crash may leave a submitting attempt after its reservation was
            # already cleaned.  Quarantine those too, never auto-retry.
            stale_query = ("SELECT attempt_id,account,source,vacancy_id,track,channel_key,run_id "
                           "FROM source_application_attempts WHERE status='submitting' AND updated_at<?")
            stale_params: list[Any] = [cutoff]
            if account:
                stale_query += " AND account=?"
                stale_params.append(account)
            for row in conn.execute(stale_query, stale_params).fetchall():
                conn.execute(
                    "UPDATE source_application_attempts SET status='unknown',"
                    "note='interrupted: stale submitting attempt; quarantined',updated_at=? "
                    "WHERE attempt_id=? AND status='submitting'",
                    (now(), row["attempt_id"]),
                )
                self._release_source_locks(
                    conn, str(row["account"]), str(row["source"]), str(row["vacancy_id"]),
                    str(row["track"]), str(row["channel_key"]),
                )
                recovered.append({
                    "account": str(row["account"]), "source": str(row["source"]),
                    "vacancy_id": str(row["vacancy_id"]), "track": str(row["track"]),
                    "channel_key": str(row["channel_key"]), "run_id": str(row["run_id"]),
                    "status": "quarantined",
                })
        return recovered

    def source_attempts(self, account: str = "default", source: str | None = None,
                        limit: int = 500) -> list[dict[str, Any]]:
        query = "SELECT * FROM source_application_attempts WHERE account=?"
        params: list[Any] = [account]
        if source:
            query += " AND source=?"
            params.append(source)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(5000, int(limit))))
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def start_campaign(self, campaign_id: str, account: str, track: str,
                       config: dict[str, Any], source: str = "hh.ru") -> str:
        """Начинает ручной scan и возвращает последний полный запуск для сравнения."""
        timestamp = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT campaign_id FROM campaign_runs WHERE account=? AND source=? AND track=? "
                "AND status IN ('ok','empty') ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (account, source, track),
            ).fetchone()
            previous_id = str(previous["campaign_id"]) if previous else ""
            conn.execute(
                """INSERT INTO campaign_runs
                (campaign_id,account,source,track,status,config_json,previous_campaign_id,started_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (campaign_id, account, source, track, "running",
                 json.dumps(config, ensure_ascii=False, sort_keys=True), previous_id, timestamp),
            )
        return previous_id

    def save_campaign_items(self, campaign_id: str, account: str, track: str,
                            items: list[dict[str, Any]], *, status: str,
                            snapshot_path: str, error: str = "", error_count: int = 0,
                            source: str = "hh.ru") -> dict[str, Any]:
        """Сохраняет все результаты успешной или частичной выдачи без удаления старых карточек."""
        observed_at = now()
        counts = {key: 0 for key in ("first_seen", "new_since_previous", "updated", "known")}
        matched_count = 0
        maybe_count = 0
        rejected_count = 0
        applied_count = 0
        stored_count = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT previous_campaign_id FROM campaign_runs WHERE campaign_id=? AND account=?",
                (campaign_id, account),
            ).fetchone()
            if run is None:
                raise ValueError(f"unknown campaign_id: {campaign_id}")
            previous_campaign_id = str(run["previous_campaign_id"] or "")
            for item in items:
                if not isinstance(item, dict):
                    continue
                vacancy_id = str(item.get("id") or item.get("vacancyId") or "").strip()
                if not vacancy_id:
                    continue
                stored_count += 1
                payload = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                stable_fields = (
                    "id", "name", "company", "url", "description", "description_status", "salary",
                    "experience", "area", "schedule", "work_format", "published", "employment",
                )
                stable = {key: item.get(key) for key in stable_fields if key in item}
                content_hash = hashlib.sha256(
                    json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
                prior = conn.execute(
                    "SELECT content_hash,first_seen_at FROM vacancies "
                    "WHERE account=? AND source=? AND vacancy_id=?",
                    (account, source, vacancy_id),
                ).fetchone()
                track_prior = conn.execute(
                    "SELECT 1 FROM vacancy_track_scores WHERE account=? AND source=? AND vacancy_id=? AND track=?",
                    (account, source, vacancy_id, track),
                ).fetchone()
                previous_observation = None
                if previous_campaign_id:
                    previous_observation = conn.execute(
                        "SELECT 1 FROM vacancy_observations WHERE campaign_id=? AND account=? AND source=? "
                        "AND vacancy_id=? AND track=?",
                        (previous_campaign_id, account, source, vacancy_id, track),
                    ).fetchone()
                ever_observed = conn.execute(
                    "SELECT 1 FROM vacancy_observations WHERE account=? AND source=? AND vacancy_id=? "
                    "AND track=? LIMIT 1",
                    (account, source, vacancy_id, track),
                ).fetchone()
                if prior is None:
                    legacy_first_seen = str(item.get("first_seen") or "").strip()
                    if legacy_first_seen and item.get("is_new") is False:
                        # На самом первом campaign не было предыдущего campaign:
                        # известные из старых scan вакансии не становятся "новыми после него".
                        change = "new_since_previous" if previous_campaign_id else "known"
                    else:
                        change = "first_seen"
                elif ((previous_campaign_id and previous_observation is None)
                      or (track_prior is None and ever_observed is None)):
                    change = "new_since_previous"
                elif str(prior["content_hash"] or "") != content_hash:
                    change = "updated"
                else:
                    change = "known"
                counts[change] += 1

                title = str(item.get("name") or item.get("title") or "")
                company = item.get("company") or ""
                if isinstance(company, dict):
                    company = company.get("visibleName") or company.get("name") or ""
                url = str(item.get("url") or item.get("alternate_url") or "")
                first_seen_at = str(prior["first_seen_at"]) if prior else observed_at
                if prior is None and item.get("first_seen") and item.get("is_new") is False:
                    first_seen_at = str(item.get("first_seen"))
                conn.execute(
                    """INSERT INTO vacancies
                    (account,source,vacancy_id,title,company,url,content_hash,payload_json,created_at,
                     first_seen_at,last_seen_at,last_campaign_id,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(account,source,vacancy_id) DO UPDATE SET
                      title=excluded.title,company=excluded.company,url=excluded.url,
                      content_hash=excluded.content_hash,payload_json=excluded.payload_json,
                      last_seen_at=excluded.last_seen_at,last_campaign_id=excluded.last_campaign_id,
                      updated_at=excluded.updated_at""",
                    (account, source, vacancy_id, title, str(company), url, content_hash, payload,
                     observed_at, first_seen_at, observed_at, campaign_id, observed_at),
                )
                try:
                    score = int(item.get("score") or item.get("fit_score") or 0)
                except (TypeError, ValueError):
                    score = 0
                reasons = item.get("reasons") or item.get("score_reasons") or []
                problems = list(dict.fromkeys([
                    *_json_list(item.get("hard_killers")),
                    *_json_list(item.get("soft_killers")),
                    *_json_list(item.get("problems")),
                ]))
                filter_reasons = item.get("filter_reasons") or []
                conn.execute(
                    """INSERT INTO vacancy_track_scores
                    (account,source,vacancy_id,track,score,score_decision,filter_status,reasons_json,
                     problems_json,filter_reasons_json,resume,campaign_id,latest_change,last_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(account,source,vacancy_id,track) DO UPDATE SET
                      score=excluded.score,score_decision=excluded.score_decision,
                      filter_status=excluded.filter_status,reasons_json=excluded.reasons_json,
                      problems_json=excluded.problems_json,filter_reasons_json=excluded.filter_reasons_json,
                      resume=excluded.resume,campaign_id=excluded.campaign_id,
                      latest_change=excluded.latest_change,last_seen_at=excluded.last_seen_at""",
                    (account, source, vacancy_id, track, score,
                     str(item.get("score_decision") or item.get("decision") or "unknown"),
                     str(item.get("filter_status") or "unknown"),
                     json.dumps(reasons, ensure_ascii=False), json.dumps(problems, ensure_ascii=False),
                     json.dumps(filter_reasons, ensure_ascii=False), str(item.get("resume") or ""),
                     campaign_id, change, observed_at),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO vacancy_observations
                    (campaign_id,account,source,vacancy_id,track,change_kind,content_hash,payload_json,observed_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (campaign_id, account, source, vacancy_id, track, change, content_hash, payload, observed_at),
                )
                bucket = campaign_fit_bucket(
                    str(item.get("filter_status") or "unknown"),
                    str(item.get("score_decision") or item.get("decision") or "unknown"),
                )
                if bucket == "fit":
                    matched_count += 1
                elif bucket == "maybe":
                    maybe_count += 1
                else:
                    rejected_count += 1
                if source == "hh.ru":
                    # Legacy HH tables have no source column and are intentionally
                    # kept untouched.  They must never influence a non-HH row that
                    # happens to reuse the same numeric vacancy ID.
                    attempt = conn.execute(
                        "SELECT status,note FROM attempts WHERE account=? AND vacancy_id=?",
                        (account, vacancy_id),
                    ).fetchone()
                    negotiation = conn.execute(
                        "SELECT 1 FROM negotiation_statuses WHERE account=? AND vacancy_id=?",
                        (account, vacancy_id),
                    ).fetchone()
                    if attempt:
                        app_state = campaign_application_state(attempt["status"], attempt["note"])
                        if app_state:
                            conn.execute(
                                "UPDATE vacancies SET application_state=? "
                                "WHERE account=? AND source=? AND vacancy_id=?",
                                (app_state, account, source, vacancy_id),
                            )
                    elif negotiation:
                        conn.execute(
                            "UPDATE vacancies SET application_state='APPLIED_CONFIRMED' "
                            "WHERE account=? AND source=? AND vacancy_id=?",
                            (account, source, vacancy_id),
                        )
                    if negotiation or (attempt and attempt["status"] in BLOCKED_STATUSES):
                        applied_count += 1
                else:
                    source_attempt = conn.execute(
                        "SELECT status,note FROM source_application_attempts "
                        "WHERE account=? AND source=? AND vacancy_id=? AND track=? "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (account, source, vacancy_id, track),
                    ).fetchone()
                    if source_attempt:
                        app_state = campaign_application_state(
                            source_attempt["status"], source_attempt["note"]
                        )
                        if app_state:
                            conn.execute(
                                "UPDATE vacancies SET application_state=? "
                                "WHERE account=? AND source=? AND vacancy_id=?",
                                (app_state, account, source, vacancy_id),
                            )
                    if source_attempt and source_attempt["status"] in BLOCKED_STATUSES:
                        applied_count += 1

            conn.execute(
                """UPDATE campaign_runs SET status=?,snapshot_path=?,item_count=?,first_seen_count=?,new_count=?,
                   updated_count=?,known_count=?,matched_count=?,maybe_count=?,rejected_count=?,applied_count=?,
                   error_count=?,error=?,finished_at=?
                   WHERE campaign_id=? AND account=?""",
                (status, snapshot_path, stored_count, counts["first_seen"], counts["new_since_previous"],
                 counts["updated"], counts["known"], matched_count, maybe_count, rejected_count,
                 applied_count, max(0, int(error_count)),
                 error[:1000], now(), campaign_id, account),
            )
        self.save_vacancy_links(account, source, items)
        return {"items": stored_count, **counts, "matched": matched_count,
                "maybe": maybe_count, "rejected": rejected_count,
                "applied_before": applied_count, "errors": max(0, int(error_count))}

    def fail_campaign(self, campaign_id: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE campaign_runs SET status='failed',error=?,finished_at=? "
                "WHERE campaign_id=? AND status='running'",
                (str(error)[:1000], now(), campaign_id),
            )

    def campaign_history(self, account: str = "default", track: str | None = None,
                         limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            query = "SELECT * FROM campaign_runs WHERE account=?"
            params: list[Any] = [account]
            if track:
                query += " AND track=?"
                params.append(track)
            query += " ORDER BY started_at DESC,rowid DESC LIMIT ?"
            params.append(max(1, min(200, int(limit))))
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            try:
                value["config"] = json.loads(value.pop("config_json"))
            except (json.JSONDecodeError, TypeError):
                value["config"] = {}
            result.append(value)
        return result

    def list_campaign_vacancies(self, account: str = "default", track: str | None = None,
                                view: str = "all", limit: int = 100,
                                source: str | None = None) -> list[dict[str, Any]]:
        if view not in CAMPAIGN_VIEWS:
            raise ValueError(f"unknown campaign view: {view}")
        query = """SELECT v.*,s.track,s.score,s.score_decision,s.filter_status,s.reasons_json,
                          s.problems_json,s.filter_reasons_json,s.resume,s.campaign_id AS track_campaign_id,
                          s.review_state AS track_review_state,s.review_reason AS track_review_reason,
                          s.review_notes AS track_review_notes,
                          s.latest_change,s.last_seen_at AS track_last_seen_at,
                          l.group_key AS link_group_key,l.channel_key AS link_channel_key,
                          l.channel_type AS link_channel_type,l.application_url AS link_application_url,
                          l.mirror_source AS link_mirror_source,l.mirror_native_id AS link_mirror_native_id,
                          l.duplicate_of_source AS link_duplicate_of_source,
                          l.duplicate_of_vacancy_id AS link_duplicate_of_vacancy_id
                   FROM vacancies v JOIN vacancy_track_scores s
                     ON s.account=v.account AND s.source=v.source AND s.vacancy_id=v.vacancy_id
                   LEFT JOIN vacancy_links l
                     ON l.account=v.account AND l.source=v.source AND l.vacancy_id=v.vacancy_id
                   WHERE v.account=?"""
        params: list[Any] = [account]
        if source:
            query += " AND v.source=?"
            params.append(source)
        if track:
            query += " AND s.track=?"
            params.append(track)
        query += " ORDER BY s.score DESC,s.last_seen_at DESC,v.vacancy_id"
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(query, params).fetchall()]
            result = []
            for row in rows:
                if view == "new" and row["latest_change"] not in {"first_seen", "new_since_previous"}:
                    continue
                review_state = row["track_review_state"] or "UNREVIEWED"
                if view == "deferred" and review_state != "DEFERRED":
                    continue
                if view == "approved" and review_state != "APPROVED":
                    continue
                if view == "ready" and row["application_state"] != "READY_TO_APPLY":
                    continue
                if view == "needs_letter" and row["application_state"] != "NEEDS_LETTER":
                    continue
                if view == "needs_form" and row["application_state"] != "NEEDS_FORM":
                    continue
                if view == "external_ats" and row["application_state"] != "EXTERNAL_ATS":
                    continue
                if view == "unknown" and row["application_state"] != "UNKNOWN":
                    continue
                if view == "applied" and row["application_state"] not in {"APPLIED_CONFIRMED", "ALREADY_APPLIED"}:
                    continue
                if view == "skip" and review_state != "SKIP":
                    continue
                # Lane is computed after the payload is decoded, so a
                # placeholder here is replaced below.  The view filter itself
                # runs after lane computation.
                try:
                    item = json.loads(row["payload_json"])
                except (json.JSONDecodeError, TypeError):
                    item = {}
                try:
                    item.update({
                        "id": row["vacancy_id"], "source": row["source"], "track": row["track"],
                        "score": row["score"], "fit_score": row["score"],
                        "score_decision": row["score_decision"], "filter_status": row["filter_status"],
                        "reasons": json.loads(row["reasons_json"] or "[]"),
                        "problems": json.loads(row["problems_json"] or "[]"),
                        "filter_reasons": json.loads(row["filter_reasons_json"] or "[]"),
                    })
                except (json.JSONDecodeError, TypeError):
                    item["reasons"], item["problems"], item["filter_reasons"] = [], [], []
                item.update({
                    "name": row["title"], "company": row["company"], "url": row["url"],
                    "resume": row["resume"], "review_state": review_state,
                    "review_reason": row["track_review_reason"],
                    "review_notes": row["track_review_notes"],
                    "application_state": row["application_state"],
                    "application_note": row["application_note"],
                    "conversation_state": row["conversation_state"], "next_action": row["next_action"],
                    "owner": row["owner"], "first_seen_at": row["first_seen_at"],
                    "last_seen_at": row["last_seen_at"], "discovery_status": row["latest_change"],
                    "last_checked_at": row["last_checked_at"],
                    "job_group_key": row["link_group_key"] or item.get("job_group_key", ""),
                    "channel_key": row["link_channel_key"] or item.get("application_channel", ""),
                    "channel_type": row["link_channel_type"] or item.get("application_channel_type", ""),
                    "application_url": row["link_application_url"] or item.get("application_url", ""),
                    "mirror_source": row["link_mirror_source"] or item.get("mirror_source", ""),
                    "mirror_native_id": row["link_mirror_native_id"] or item.get("mirror_native_id", ""),
                    "duplicate_of_source": row["link_duplicate_of_source"] or "",
                    "duplicate_of_vacancy_id": row["link_duplicate_of_vacancy_id"] or "",
                    "channel_duplicate": bool(row["link_duplicate_of_source"]),
                })
                # The payload is a historical snapshot. Review lanes must
                # always derive from authoritative CURRENT track scores and
                # application state; otherwise rescored rejects appear as FIT.
                old_lane = str(item.get("review_lane") or "").strip()
                lane = review_lane_for(
                    str(row["filter_status"] or ""), str(row["score_decision"] or ""),
                    application_state=str(row["application_state"] or ""),
                    description_status=str(item.get("description_status") or ""),
                )
                item["review_lane"] = lane
                item["lane_reasons"] = (
                    list(item.get("lane_reasons") or [])
                    if lane == old_lane else []
                )
                if not item["lane_reasons"]:
                    item["lane_reasons"] = list(dict.fromkeys(
                        [*item.get("filter_reasons", []), *item.get("problems", [])]
                    )) or ["manual_review"]
                if view == "top" and (
                    lane not in {"FIT", "GOOD_UNKNOWN"}
                    or review_state == "SKIP"
                    or row["application_state"] in {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN"}
                ):
                    continue
                if view == "review" and (
                    lane in {"SKIP", "APPLIED"}
                    or review_state == "SKIP"
                    or row["application_state"] in {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN"}
                ):
                    continue
                if row["source"] == "hh.ru":
                    attempt = conn.execute(
                        "SELECT status FROM attempts WHERE account=? AND vacancy_id=?",
                        (account, row["vacancy_id"]),
                    ).fetchone()
                    negotiation = conn.execute(
                        "SELECT 1 FROM negotiation_statuses WHERE account=? AND vacancy_id=?",
                        (account, row["vacancy_id"]),
                    ).fetchone()
                    item["applied_before"] = bool(
                        negotiation or (attempt and attempt["status"] in BLOCKED_STATUSES)
                    )
                else:
                    source_attempt = conn.execute(
                        "SELECT status FROM source_application_attempts "
                        "WHERE account=? AND source=? AND vacancy_id=? AND track=? "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (account, row["source"], row["vacancy_id"], row["track"]),
                    ).fetchone()
                    item["applied_before"] = bool(
                        item["channel_duplicate"]
                        or (source_attempt and source_attempt["status"] in BLOCKED_STATUSES)
                    )
                result.append(item)
        result.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("id") or "")))
        if view in {"top", "review"}:
            lane_rank = {"FIT": 0, "GOOD_UNKNOWN": 1, "NEEDS_ENRICHMENT": 2, "MANUAL_REVIEW": 3}
            result.sort(key=lambda item: (
                lane_rank.get(str(item.get("review_lane") or ""), 4),
                -int(item.get("score") or 0),
                str(item.get("id") or ""),
            ))
        return result[:max(1, min(5000, int(limit)))]

    def set_review_state(self, account: str, source: str, vacancy_id: str, state: str,
                         *, reason: str, notes: str | None = None, track: str | None = None) -> None:
        normalized = str(state).strip().upper()
        if normalized not in REVIEW_STATES:
            raise ValueError(f"review state must be one of {', '.join(sorted(REVIEW_STATES))}")
        reason = str(reason or "").strip()
        if normalized != "UNREVIEWED" and not reason:
            raise ValueError("a reason is required for a reviewed vacancy")
        timestamp = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT application_state FROM vacancies WHERE account=? AND source=? AND vacancy_id=?",
                (account, source, str(vacancy_id)),
            ).fetchone()
            if row is None:
                raise ValueError(f"vacancy not found: {vacancy_id}")
            if normalized == "APPROVED" and row["application_state"] in {
                "APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN",
            }:
                raise ValueError(f"cannot approve a vacancy in {row['application_state']} state")
            if track is not None:
                score = conn.execute(
                    "SELECT 1 FROM vacancy_track_scores WHERE account=? AND source=? AND vacancy_id=? AND track=?",
                    (account, source, str(vacancy_id), track),
                ).fetchone()
                if score is None:
                    raise ValueError(f"vacancy {vacancy_id} is not present in track {track}")
                conn.execute(
                    """UPDATE vacancy_track_scores SET review_state=?,review_reason=?,
                       review_notes=COALESCE(?,review_notes) WHERE account=? AND source=?
                       AND vacancy_id=? AND track=?""",
                    (normalized, reason, notes, account, source, str(vacancy_id), track),
                )
            else:
                # Compatibility for older callers: an unscoped decision is copied
                # to all tracks that already contain this vacancy.
                conn.execute(
                    """UPDATE vacancy_track_scores SET review_state=?,review_reason=?,
                       review_notes=COALESCE(?,review_notes) WHERE account=? AND source=? AND vacancy_id=?""",
                    (normalized, reason, notes, account, source, str(vacancy_id)),
                )
            application_state = row["application_state"]
            if normalized == "APPROVED" and application_state in {"NOT_STARTED", "FAILED_BEFORE_SUBMIT"}:
                application_state = "READY_TO_APPLY"
            conn.execute(
                """UPDATE vacancies SET application_state=?,updated_at=?
                   WHERE account=? AND source=? AND vacancy_id=?""",
                (application_state, timestamp, account, source, str(vacancy_id)),
            )

    def set_review_state_batch(self, account: str, source: str, vacancy_ids: list[str],
                               state: str, *, reason: str, track: str) -> int:
        """Атомарно применяет одно явное решение к пакету ID без частичного подтверждения."""
        normalized = str(state).strip().upper()
        if normalized not in REVIEW_STATES:
            raise ValueError("invalid review state")
        reason = str(reason or "").strip()
        if normalized != "UNREVIEWED" and not reason:
            raise ValueError("a reason is required")
        ids = list(dict.fromkeys(str(value).strip() for value in vacancy_ids if str(value).strip()))
        if not ids or len(ids) > 100:
            raise ValueError("batch must contain between 1 and 100 nonempty vacancy IDs")
        if not str(track).strip():
            raise ValueError("track must be specified")
        timestamp = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for vacancy_id in ids:
                row = conn.execute(
                    """SELECT v.application_state,s.score_decision,s.filter_status
                    FROM vacancies v JOIN vacancy_track_scores s
                      ON s.account=v.account AND s.source=v.source AND s.vacancy_id=v.vacancy_id
                    WHERE v.account=? AND v.source=? AND v.vacancy_id=? AND s.track=?""",
                    (account, source, vacancy_id, track),
                ).fetchone()
                if row is None:
                    raise ValueError(f"vacancy {vacancy_id} is not present in track {track}")
                app_state = str(row["application_state"])
                if normalized == "APPROVED":
                    if app_state not in {"NOT_STARTED", "FAILED_BEFORE_SUBMIT", "READY_TO_APPLY"}:
                        raise ValueError(f"cannot batch approve {vacancy_id} in {app_state}")
                    if row["score_decision"] != "pass" or row["filter_status"] not in {"pass", "unknown"}:
                        raise ValueError(f"vacancy {vacancy_id} did not pass screening rules")
                    # Legacy HH attempts are keyed by HH vacancy ID, even when
                    # the discovered apply channel points to an external ATS.
                    # Never let a new channel identity bypass prior HH attempts.
                    if source == "hh.ru":
                        legacy = conn.execute(
                            "SELECT status FROM attempts WHERE account=? AND vacancy_id=?",
                            (account, vacancy_id),
                        ).fetchone()
                        negotiation = conn.execute(
                            "SELECT 1 FROM negotiation_statuses "
                            "WHERE account=? AND vacancy_id=?",
                            (account, vacancy_id),
                        ).fetchone()
                        if negotiation or (legacy and legacy["status"] in BLOCKED_STATUSES):
                            raise ValueError(
                                f"vacancy {vacancy_id} has an existing HH application/uncertain attempt"
                            )
                    # Check the actual channel, not a coincidentally equal ID on HH.
                    # A DreamJob mirror shares the HH channel, while an independent
                    # Getmatch/Habr form must not inherit unrelated HH attempts.
                    link = conn.execute(
                        "SELECT channel_key,mirror_source,mirror_native_id FROM vacancy_links "
                        "WHERE account=? AND source=? AND vacancy_id=?",
                        (account, source, vacancy_id),
                    ).fetchone()
                    channel = canonical_channel_key(
                        str(link["channel_key"] if link else f"{source}:{vacancy_id}")
                    )
                    duplicate_source, duplicate_id = self._channel_is_applied(
                        conn, account, channel, source=source, vacancy_id=vacancy_id,
                        mirror_source=str(link["mirror_source"] or "") if link else "",
                        mirror_native_id=str(link["mirror_native_id"] or "") if link else "",
                    )
                    if duplicate_source:
                        raise ValueError(
                            f"vacancy {vacancy_id} channel already used by "
                            f"{duplicate_source}:{duplicate_id}"
                        )
                    reserved = conn.execute(
                        "SELECT source,vacancy_id FROM channel_reservations "
                        "WHERE account=? AND channel_key=?",
                        (account, channel),
                    ).fetchone()
                    if reserved:
                        raise ValueError(
                            f"vacancy {vacancy_id} channel reserved by "
                            f"{reserved['source']}:{reserved['vacancy_id']}"
                        )
            for vacancy_id in ids:
                conn.execute(
                    """UPDATE vacancy_track_scores SET review_state=?,review_reason=?
                    WHERE account=? AND source=? AND vacancy_id=? AND track=?""",
                    (normalized, reason, account, source, vacancy_id, track),
                )
                if normalized == "APPROVED":
                    conn.execute(
                        """UPDATE vacancies SET application_state='READY_TO_APPLY',updated_at=?
                        WHERE account=? AND source=? AND vacancy_id=?
                          AND application_state IN ('NOT_STARTED','FAILED_BEFORE_SUBMIT')""",
                        (timestamp, account, source, vacancy_id),
                    )
        return len(ids)

    def record_manual_application(self, account: str, source: str, vacancy_id: str,
                                  reason: str = "owner marked as applied") -> bool:
        """Записывает явное подтверждение владельца отдельно от HH negotiation sync."""
        timestamp = now()
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE vacancies SET application_state='APPLIED_CONFIRMED',application_note=?,
                   last_checked_at=?,updated_at=? WHERE account=? AND source=? AND vacancy_id=?
                   AND application_state NOT IN ('UNKNOWN','ALREADY_APPLIED')""",
                (str(reason)[:500], timestamp, timestamp, account, source, str(vacancy_id)),
            )
        return cursor.rowcount > 0

    def approved_campaign_items(self, account: str, track: str, limit: int = 10,
                                source: str = "hh.ru") -> list[dict[str, Any]]:
        rows = self.list_campaign_vacancies(account, track, "approved", limit=1000, source=source)
        result = []
        with self.connect() as conn:
            for row in rows:
                if needs_manual_reapproval(row):
                    continue
                # APPROVED is a track-scoped human override for reviewable unknown
                # filters (for example, unpublished salary), never for rejected
                # filters or an uncertain mechanical fit score.
                if (row.get("applied_before") or row.get("filter_status") not in {"pass", "unknown"}
                        or row.get("score_decision") != "pass"):
                    continue
                if row.get("application_state") == "READY_TO_APPLY":
                    task_id = hashlib.sha256(
                        f"{account}:{source}:{row['id']}:{track}:letter".encode()
                    ).hexdigest()[:32]
                    task = conn.execute(
                        "SELECT status,draft_text FROM workflow_tasks WHERE task_id=? AND account=?",
                        (task_id, account),
                    ).fetchone()
                    if task and task["status"] == "APPROVED" and str(task["draft_text"]).strip():
                        result.append({**row, "approved_cover_letter": task["draft_text"],
                                       "approved_letter_task_id": task_id})
                    else:
                        result.append(row)
                    continue
                if row.get("application_state") != "NEEDS_LETTER":
                    continue
                task_id = hashlib.sha256(
                    f"{account}:{source}:{row['id']}:{track}:letter".encode()
                ).hexdigest()[:32]
                task = conn.execute(
                    "SELECT status,draft_text FROM workflow_tasks WHERE task_id=? AND account=?",
                    (task_id, account),
                ).fetchone()
                if task and task["status"] == "APPROVED" and str(task["draft_text"]).strip():
                    result.append({**row, "approved_cover_letter": task["draft_text"],
                                   "approved_letter_task_id": task_id})
        return result[:max(1, min(100, int(limit)))]

    def create_workflow_task(self, account: str, source: str, vacancy_id: str, track: str,
                             task_type: str, status: str, payload: dict[str, Any] | None = None,
                             draft_text: str = "", task_id: str = "") -> str:
        if task_type not in {"letter", "form"}:
            raise ValueError("task_type must be letter or form")
        timestamp = now()
        task_id = task_id or hashlib.sha256(
            f"{account}:{source}:{vacancy_id}:{track}:{task_type}".encode()
        ).hexdigest()[:32]
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO workflow_tasks
                (task_id,account,source,vacancy_id,track,task_type,status,payload_json,draft_text,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,payload_json=excluded.payload_json,
                  draft_text=excluded.draft_text,updated_at=excluded.updated_at
                  WHERE workflow_tasks.status NOT IN ('DRAFT','APPROVED','SENT_CONFIRMED')""",
                (task_id, account, source, str(vacancy_id), track, task_type, str(status),
                 json.dumps(payload or {}, ensure_ascii=False), str(draft_text), timestamp, timestamp),
            )
        return task_id

    def update_workflow_task(self, task_id: str, account: str, *, draft_text: str | None = None,
                             action: str = "edit") -> dict[str, Any]:
        """Изменяет черновик или подтверждает его без отправки сообщения работодателю."""
        if action not in {"edit", "approve", "sent_confirmed"}:
            raise ValueError("workflow action must be edit, approve or sent_confirmed")
        timestamp = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM workflow_tasks WHERE task_id=? AND account=?", (task_id, account)
            ).fetchone()
            if row is None:
                raise ValueError(f"workflow task not found: {task_id}")
            if row["status"] == "SENT_CONFIRMED":
                raise ValueError("a confirmed task cannot be edited or re-approved")
            if row["task_type"] == "form" and action in {"approve", "sent_confirmed"}:
                raise ValueError("form answers need a supported question capture and owner review")
            if action == "approve" and not str(row["draft_text"] or "").strip():
                raise ValueError("cannot approve an empty draft")
            if action == "sent_confirmed" and row["status"] != "APPROVED":
                raise ValueError("only an approved task can be marked sent")
            status = {"edit": "DRAFT", "approve": "APPROVED", "sent_confirmed": "SENT_CONFIRMED"}[action]
            text = str(row["draft_text"] or "") if draft_text is None else str(draft_text)
            if action == "approve" and draft_text is not None:
                text = str(draft_text)
            if action == "approve" and not text.strip():
                raise ValueError("cannot approve an empty draft")
            conn.execute(
                "UPDATE workflow_tasks SET status=?,draft_text=?,updated_at=? WHERE task_id=? AND account=?",
                (status, text, timestamp, task_id, account),
            )
            updated = conn.execute(
                "SELECT * FROM workflow_tasks WHERE task_id=? AND account=?", (task_id, account)
            ).fetchone()
        result = dict(updated)
        try:
            result["payload"] = json.loads(result.pop("payload_json"))
        except (json.JSONDecodeError, TypeError):
            result["payload"] = {}
        return result

    def workflow_task(self, task_id: str, account: str = "default") -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_tasks WHERE task_id=? AND account=?", (task_id, account)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["payload"] = json.loads(result.pop("payload_json"))
        except (json.JSONDecodeError, TypeError):
            result["payload"] = {}
        return result

    def workflow_tasks(self, account: str = "default", task_type: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM workflow_tasks WHERE account=?"
        params: list[Any] = [account]
        if task_type:
            query += " AND task_type=?"
            params.append(task_type)
        query += " ORDER BY updated_at DESC"
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(query, params).fetchall()]
        for row in rows:
            try:
                row["payload"] = json.loads(row.pop("payload_json"))
            except (json.JSONDecodeError, TypeError):
                row["payload"] = {}
        return rows

    def save_inbox_messages(self, account: str, source: str, messages: list[dict[str, Any]]) -> int:
        timestamp = now()
        saved = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for message in messages:
                thread_id = str(message.get("thread_id") or "").strip()
                message_id = str(message.get("message_id") or "").strip()
                if not thread_id or not message_id:
                    continue
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO inbox_messages
                    (account,source,thread_id,message_id,vacancy_id,sent_at,sender_type,sender_name,
                     body,raw_json,first_seen_at,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (account, source, thread_id, message_id, str(message.get("vacancy_id") or ""),
                     str(message.get("sent_at") or ""), str(message.get("sender_type") or "unknown"),
                     str(message.get("sender_name") or ""), str(message.get("body") or ""),
                     json.dumps(message.get("raw") or {}, ensure_ascii=False), timestamp, timestamp),
                )
                saved += int(cursor.rowcount > 0)
        return saved

    def inbox_messages(self, account: str = "default", thread_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM inbox_messages WHERE account=?"
        params: list[Any] = [account]
        if thread_id:
            query += " AND thread_id=?"
            params.append(thread_id)
        query += " ORDER BY sent_at,thread_id,message_id"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def reconcile(self, path: Path, account: str = "default") -> int:
        """Применяет только явно подтверждённые статусы и никогда не повторяет unknown-попытку."""
        if path.suffix.lower() == ".json":
            rows = json.loads(path.read_text(encoding="utf-8"))
            rows = rows if isinstance(rows, list) else rows.get("items", [])
        else:
            with path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        changed = 0
        for row in rows:
            confirmed = str(row.get("confirmed", "")).lower() in {"1", "true", "yes", "да"}
            status = str(row.get("status", ""))
            if not confirmed or status not in TERMINAL_STATUSES:
                continue
            item = {
                "id": row.get("vacancy_id") or row.get("id", ""), "name": row.get("name", ""),
                "company": row.get("company", ""), "url": row.get("url", ""),
                "score": row.get("score", 0), "resume": row.get("resume", ""),
            }
            self.record(item, status, "explicit reconciliation", "reconcile", account)
            changed += 1
        return changed

    def save_external_account(self, provider: str, account: str, status: str,
                              detail: str = "", metadata: dict[str, Any] | None = None) -> bool:
        """Persists the last known status of a local external account."""
        timestamp = now()
        payload = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO external_accounts
                (provider,account,status,detail,metadata_json,checked_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(provider,account) DO UPDATE SET
                  status=excluded.status,detail=excluded.detail,
                  metadata_json=excluded.metadata_json,checked_at=excluded.checked_at""",
                (str(provider), str(account), str(status), str(detail)[:500],
                 payload, timestamp),
            )
        return True

    def external_accounts(self, provider: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM external_accounts"
        params: list[Any] = []
        if provider:
            query += " WHERE provider=?"
            params.append(provider)
        query += " ORDER BY provider, account"
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(query, params).fetchall()]
        for row in rows:
            try:
                row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
            except json.JSONDecodeError:
                row["metadata"] = {}
        return rows

    def external_account(self, provider: str, account: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM external_accounts WHERE provider=? AND account=?",
                (str(provider), str(account)),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {}
        return item

    def link_telegram_message(self, account: str, source: str, vacancy_id: str,
                              link: dict[str, Any]) -> bool:
        """Attaches a saved bot message to an existing scanned vacancy.

        The scanner payload is preserved; only the ``telegram`` metadata is
        merged.  Raw callback payloads are never stored, only their public
        classification and button text.
        """
        timestamp = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload_json FROM vacancies WHERE account=? AND source=? AND vacancy_id=?",
                (account, source, str(vacancy_id)),
            ).fetchone()
            if row is None:
                return False
            try:
                payload = json.loads(row["payload_json"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload["telegram"] = link
            payload["telegram_message_id"] = link.get("message_id")
            payload["telegram_chat"] = link.get("chat")
            payload["telegram_sent_at"] = link.get("sent_at")
            payload["telegram_buttons"] = link.get("buttons") or []
            conn.execute(
                "UPDATE vacancies SET payload_json=?, updated_at=? "
                "WHERE account=? AND source=? AND vacancy_id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), timestamp,
                 account, source, str(vacancy_id)),
            )
        return True

    def save_imported_vacancy(self, account: str, source: str, vacancy_id: str,
                              item: dict[str, Any], track: str) -> bool:
        """Creates a review-only placeholder for a Telegram ref not yet scanned.

        It deliberately has ``score_decision='maybe'`` so it cannot enter the
        live queue until the real scanner enriches and scores it.
        """
        timestamp = now()
        title = str(item.get("name") or item.get("title") or "")
        company = str(item.get("company") or "")
        url = str(item.get("url") or "")
        payload = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO vacancies
                (account,source,vacancy_id,title,company,url,content_hash,payload_json,created_at,
                 first_seen_at,last_seen_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account,source,vacancy_id) DO UPDATE SET
                  title=excluded.title,company=excluded.company,url=excluded.url,
                  payload_json=excluded.payload_json,last_seen_at=excluded.last_seen_at,
                  updated_at=excluded.updated_at""",
                (account, source, str(vacancy_id), title, company, url, "",
                 payload, timestamp, timestamp, timestamp, timestamp),
            )
            conn.execute(
                """INSERT INTO vacancy_track_scores
                (account,source,vacancy_id,track,score,score_decision,filter_status,
                 reasons_json,problems_json,filter_reasons_json,resume,campaign_id,
                 latest_change,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account,source,vacancy_id,track) DO UPDATE SET
                  latest_change=excluded.latest_change,last_seen_at=excluded.last_seen_at""",
                (account, source, str(vacancy_id), track, 0, "maybe", "unknown",
                 "[]", "[]", "[]", "", "", "telegram_import", timestamp),
            )
        return True

    def save_telegram_messages(self, account: str, messages: list[dict[str, Any]]) -> int:
        """Upserts locally read bot messages; never stores credentials."""
        timestamp = now()
        saved = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for message in messages:
                try:
                    message_id = int(message.get("message_id") or 0)
                except (TypeError, ValueError):
                    continue
                if message_id <= 0:
                    continue
                chat = str(message.get("chat") or "@g_jobbot")
                buttons = json.dumps(message.get("buttons") or [], ensure_ascii=False)
                refs = json.dumps(message.get("vacancy_refs") or [], ensure_ascii=False)
                conn.execute(
                    """INSERT INTO telegram_messages
                    (account,chat,message_id,sent_at,text,buttons_json,vacancy_refs_json,
                     application_likely,has_callback,outgoing,first_seen_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(account,chat,message_id) DO UPDATE SET
                      sent_at=excluded.sent_at,text=excluded.text,
                      buttons_json=excluded.buttons_json,
                      vacancy_refs_json=excluded.vacancy_refs_json,
                      application_likely=excluded.application_likely,
                      has_callback=excluded.has_callback,
                      outgoing=excluded.outgoing,updated_at=excluded.updated_at""",
                    (
                        str(account), chat, message_id, str(message.get("sent_at") or ""),
                        str(message.get("text") or ""), buttons, refs,
                        1 if message.get("application_likely") else 0,
                        1 if message.get("has_callback") else 0,
                        (1 if message["outgoing"] else 0)
                        if isinstance(message.get("outgoing"), bool) else -1,
                        timestamp, timestamp,
                    ),
                )
                saved += 1
        return saved

    def telegram_messages(self, account: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM telegram_messages WHERE account=? "
                "ORDER BY sent_at DESC, message_id DESC LIMIT ?",
                (str(account), max(1, min(1000, int(limit)))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["buttons"] = json.loads(item.pop("buttons_json") or "[]")
            except json.JSONDecodeError:
                item["buttons"] = []
            try:
                item["vacancy_refs"] = json.loads(item.pop("vacancy_refs_json") or "[]")
            except json.JSONDecodeError:
                item["vacancy_refs"] = []
            item["application_likely"] = bool(item.get("application_likely"))
            item["has_callback"] = bool(item.get("has_callback"))
            item["outgoing"] = (
                bool(item["outgoing"]) if item.get("outgoing") in (0, 1)
                else None
            )
            result.append(item)
        return result

    def vacancy_row(self, account: str, source: str, vacancy_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM vacancies WHERE account=? AND source=? AND vacancy_id=?",
                (str(account), str(source), str(vacancy_id)),
            ).fetchone()
        return dict(row) if row else None

    def vacancy_track_row(self, account: str, source: str, vacancy_id: str,
                          track: str) -> dict[str, Any] | None:
        """One vacancy joined with its score/review row for a specific track."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT v.vacancy_id AS id,v.title AS name,v.company,v.url,v.source,
                          v.application_state,s.score,s.score_decision,s.filter_status,
                          s.review_state,s.review_reason,s.review_notes
                   FROM vacancies v
                   LEFT JOIN vacancy_track_scores s
                     ON s.account=v.account AND s.source=v.source
                    AND s.vacancy_id=v.vacancy_id AND s.track=?
                   WHERE v.account=? AND v.source=? AND v.vacancy_id=?""",
                (str(track), str(account), str(source), str(vacancy_id)),
            ).fetchone()
        return dict(row) if row else None

    @contextlib.contextmanager
    def run_lock(self):
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            owner = threading.get_ident()
            self._run_lock_owner = owner
            self._run_lock_depth += 1
            try:
                yield
            finally:
                self._run_lock_depth -= 1
                if self._run_lock_depth == 0:
                    self._run_lock_owner = None
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
