from __future__ import annotations

import contextlib
import csv
import fcntl
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
"""

TERMINAL_STATUSES = {"success", "already_applied"}
BLOCKED_STATUSES = TERMINAL_STATUSES | {"unknown", "submitting"}
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
    "all", "new", "top", "deferred", "approved", "ready", "needs_letter", "needs_form",
    "external_ats", "unknown", "applied", "skip",
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
            if status == "success" and str(item.get("cover_letter") or "").strip():
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
                        change = "new_since_previous"
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
                problems = [*_json_list(item.get("hard_killers")), *_json_list(item.get("soft_killers"))]
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
                if item.get("filter_status") == "pass" and item.get("score_decision", item.get("decision")) == "pass":
                    matched_count += 1
                elif item.get("filter_status") == "unknown" or item.get("score_decision", item.get("decision")) == "unknown":
                    maybe_count += 1
                else:
                    rejected_count += 1
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
                            "UPDATE vacancies SET application_state=? WHERE account=? AND source=? AND vacancy_id=?",
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
                          s.latest_change,s.last_seen_at AS track_last_seen_at
                   FROM vacancies v JOIN vacancy_track_scores s
                     ON s.account=v.account AND s.source=v.source AND s.vacancy_id=v.vacancy_id
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
                if view == "top" and not (
                    row["filter_status"] == "pass" and row["score_decision"] == "pass"
                ):
                    continue
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
                })
                attempt = conn.execute(
                    "SELECT status FROM attempts WHERE account=? AND vacancy_id=?",
                    (account, row["vacancy_id"]),
                ).fetchone()
                negotiation = conn.execute(
                    "SELECT 1 FROM negotiation_statuses WHERE account=? AND vacancy_id=?",
                    (account, row["vacancy_id"]),
                ).fetchone()
                item["applied_before"] = bool(negotiation or (attempt and attempt["status"] in BLOCKED_STATUSES))
                result.append(item)
        result.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("id") or "")))
        if view == "top":
            result.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("id") or "")))
        return result[:max(1, min(1000, int(limit)))]

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
