"""Local readiness inventory. No network, schema migration or state transitions."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .storage import effective_track_application_state

CHANNELS = {
    "hh.ru": "legacy_runner",
    "career.habr.com": "native_opt_in",
    "getmatch.ru": "native_opt_in",
    "geekjob.ru": "native_opt_in",
    "yandex.ru/jobs": "native_opt_in",
    "dreamjob.ru": "mirror_only",
}


def readiness_snapshot(db_path: Path, track: str, accounts: dict[str, str], *,
                       max_sync_age_hours: int = 24,
                       now: datetime | None = None) -> dict[str, Any]:
    """Report queue axes separately; READY is never a claim of live acceptance.

    Counts are track/account-specific, while a receipt is historical evidence.
    A fresh session, channel-conflict check and full driver preflight are still
    required. Missing databases are reported without creating private state.
    """
    now = now or datetime.now(UTC)
    result: dict[str, Any] = {"track": track, "checked_at": now.isoformat(),
                              "mode": "local_read_only", "sources": []}
    if not db_path.is_file():
        return {**result, "status": "database_missing"}
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        score_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(vacancy_track_scores)")
        }
        decision_expr = "s.decision" if "decision" in score_columns else "''"
        for source, channel in CHANNELS.items():
            account = accounts.get(source, "default")
            rows = conn.execute(
                f"""SELECT s.review_state,s.score_decision,s.filter_status,
                           {decision_expr} AS decision,v.application_state
                FROM vacancy_track_scores s JOIN vacancies v USING(account,source,vacancy_id)
                WHERE s.account=? AND s.source=? AND s.track=?""", (account, source, track),
            ).fetchall()
            counts = {"total": len(rows), "approved": 0, "approved_eligible_ready": 0,
                      "needs_workflow": 0, "unknown": 0, "confirmed": 0, "rejected": 0}
            for row in rows:
                state = effective_track_application_state(
                    str(row["application_state"] or ""),
                    str(row["review_state"] or ""),
                    str(row["score_decision"] or ""),
                    str(row["filter_status"] or ""),
                    str(row["decision"] or ""),
                )
                approved = row["review_state"] == "APPROVED"
                counts["approved"] += approved
                counts["approved_eligible_ready"] += (
                    approved and state == "READY_TO_APPLY" and row["score_decision"] == "pass"
                    and row["filter_status"] in {"pass", "unknown"}
                    and str(row["decision"] or "").upper() != "BLOCKED"
                )
                counts["needs_workflow"] += state in {"NEEDS_FORM", "NEEDS_LETTER", "NEEDS_MANUAL"}
                counts["unknown"] += state == "UNKNOWN"
                counts["confirmed"] += state in {"APPLIED_CONFIRMED", "ALREADY_APPLIED"}
                counts["rejected"] += row["filter_status"] == "reject" or row["score_decision"] == "reject"
            evidence = conn.execute(
                """SELECT status,updated_at FROM source_application_attempts
                WHERE account=? AND source=? AND track=? AND mode='run'
                AND status='success' ORDER BY updated_at DESC LIMIT 1""", (account, source, track),
            ).fetchone()
            gates = ["session_not_checked", "driver_preflight_required"]
            sync: dict[str, Any] | None = None
            if source == "hh.ru":
                saved = conn.execute(
                    """SELECT status,created_at FROM sync_snapshots WHERE account=? AND source=?
                    ORDER BY id DESC LIMIT 1""", (account, source),
                ).fetchone()
                sync = dict(saved) if saved else {"status": "missing"}
                fresh = False
                try:
                    stamp = datetime.fromisoformat(sync["created_at"])
                    fresh = (stamp.tzinfo is not None and sync["status"] == "ok"
                             and timedelta(0) <= now - stamp <= timedelta(hours=max_sync_age_hours))
                except (KeyError, TypeError, ValueError):
                    pass
                sync["fresh"] = fresh
                if not fresh:
                    gates.append("fresh_complete_hh_sync_required")
            if channel == "needs_live_validation":
                gates.append("native_submit_and_receipt_unverified")
            if not counts["approved_eligible_ready"]:
                gates.append("no_approved_eligible_ready_candidates")
            if channel == "mirror_only":
                gates = ["use_hh_channel_no_independent_submission"]
            result["sources"].append({
                "source": source, "account": account, "channel": channel, "queue": counts,
                "last_recorded_source_success": dict(evidence) if evidence else None,
                "hh_sync": sync, "gates": gates,
            })
    return {**result, "status": "preflight_required"}
