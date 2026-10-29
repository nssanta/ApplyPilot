"""Private at-most-once HH chat reply reservations, separate from applications."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

_STATES = {"submitting", "sent", "unknown", "stale"}


class ChatJournal:
    """Durable journal for one exact reply to one exact inbound message.

    The database is intentionally separate from the application ledger so a
    chat attempt can never rewrite application history or its dedupe state.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS chat_replies (
                    account TEXT NOT NULL, thread_id TEXT NOT NULL, inbound_key TEXT NOT NULL,
                    vacancy_id TEXT NOT NULL, reply_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(account, thread_id, inbound_key))"""
            )
        path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def reserve(self, account: str, thread_id: str, vacancy_id: str,
                inbound_key: str, reply: str) -> bool:
        """Persist before any send click. Existing/uncertain replies never retry."""
        digest = hashlib.sha256(reply.encode("utf-8")).hexdigest()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT INTO chat_replies
                       (account,thread_id,inbound_key,vacancy_id,reply_sha256,state)
                       VALUES (?,?,?,?,?,'submitting')""",
                    (account, thread_id, inbound_key, vacancy_id, digest),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
        return True

    def finish(self, account: str, thread_id: str, inbound_key: str, state: str) -> bool:
        if state not in _STATES - {"submitting"}:
            raise ValueError("Unsupported chat outcome")
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE chat_replies SET state=?,updated_at=CURRENT_TIMESTAMP
                   WHERE account=? AND thread_id=? AND inbound_key=? AND state='submitting'""",
                (state, account, thread_id, inbound_key),
            )
        return cursor.rowcount > 0

    def status(self, account: str, thread_id: str, inbound_key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT state FROM chat_replies
                   WHERE account=? AND thread_id=? AND inbound_key=?""",
                (account, thread_id, inbound_key),
            ).fetchone()
        # A crashed process may retain ``submitting``.  Both states block resend.
        if row is None:
            return None
        return "unknown" if row["state"] == "submitting" else str(row["state"])

    def entry(self, account: str, thread_id: str, inbound_key: str) -> dict[str, str] | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT vacancy_id,reply_sha256,state,updated_at FROM chat_replies
                   WHERE account=? AND thread_id=? AND inbound_key=?""",
                (account, thread_id, inbound_key),
            ).fetchone()
        if row is None:
            return None
        state = "unknown" if row["state"] == "submitting" else str(row["state"])
        return {
            "vacancy_id": str(row["vacancy_id"]),
            "reply_sha256": str(row["reply_sha256"]),
            "state": state,
            "updated_at": str(row["updated_at"]),
        }
