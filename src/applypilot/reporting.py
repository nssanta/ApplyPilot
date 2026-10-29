"""Management-facing campaign report sections.

Receipt ledgers are the only source of confirmed sends.  Discovery counts,
historical negotiations and incoming-message observations are reported
separately so they can never inflate the current campaign target.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .queue_actions import next_action, problem_queue
from .storage import Store

INVITATION_STATUSES = {"invitation", "phone_interview", "interview"}


def _iso(value: Any) -> str:
    return str(value or "").strip()


def _parse(value: Any) -> datetime | None:
    text = _iso(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _since(value: Any) -> datetime:
    parsed = _parse(value)
    return parsed or datetime.now(UTC)


def management_report(
    store: Store,
    *,
    track: str,
    sources: list[str],
    accounts: Mapping[str, str],
    execution_id: str,
    campaign_started_at: str,
    states: Mapping[str, str],
    scan: Mapping[str, Any],
    live: Mapping[str, Any],
    stops: Mapping[str, str],
    queue: Mapping[str, Mapping[str, Any]],
    rows_by_source: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    started = _since(campaign_started_at)
    counts = store.execution_counts(execution_id) if execution_id else {}
    confirmed_by_source = {source: int(values.get("success", 0))
                           for source, values in counts.items()}
    new_confirmed = sum(confirmed_by_source.values())

    hh_account = str(accounts.get("hh.ru") or accounts.get("dreamjob.ru") or "default")
    negotiations = store.negotiation_details(hh_account)
    new_refusals: list[dict[str, Any]] = []
    new_invitations: list[dict[str, Any]] = []
    for vacancy_id, row in negotiations.items():
        detected = _parse(row.get("status_detected_at") or row.get("fetched_at"))
        if detected is None or detected < started:
            continue
        status = str(row.get("status") or "").casefold()
        event_at = _iso(row.get("updated_at"))
        item = {
            "vacancy_id": vacancy_id,
            "status": status,
            "detected_at": row.get("status_detected_at") or row.get("fetched_at"),
            "event_at": event_at,
            "event_time_source": "hh_last_modified" if event_at else "unknown",
        }
        if status == "discard":
            new_refusals.append(item)
        elif status in INVITATION_STATUSES:
            new_invitations.append(item)

    inbox_sync = store.latest_sync(hh_account, "hh.ru/messages")
    incoming: list[dict[str, Any]] = []
    inbox_checked_at = _parse(inbox_sync.get("created_at")) if inbox_sync else None
    inbox_checked = bool(inbox_sync and inbox_checked_at and inbox_checked_at >= started
                         and str(inbox_sync.get("status")) in {"ok", "partial"})
    if inbox_checked:
        for message in store.inbox_messages(hh_account):
            first_seen = _parse(message.get("first_seen_at"))
            if first_seen is None or first_seen < started:
                continue
            if str(message.get("sender_type") or "") not in {"recruiter", "bot"}:
                continue
            incoming.append({
                "thread_id": message.get("thread_id"),
                "message_id": message.get("message_id"),
                "vacancy_id": message.get("vacancy_id"),
                "sender": message.get("sender_name"),
                "detected_at": message.get("first_seen_at"),
                "sent_at": message.get("sent_at"),
                "event_time_source": "hh_creation_time" if message.get("sent_at") else "unknown",
            })
    inbox = {
        "checked": inbox_checked,
        "status": str(inbox_sync.get("status")) if inbox_checked else "not_checked",
        "checked_at": inbox_sync.get("created_at") if inbox_sync else "",
        "coverage": "official HH read-only API" if inbox_checked else
                    "not checked in this campaign; absence is not zero messages",
        "new_incoming": len(incoming),
        # The read-only API does not expose our own unread flag. Do not claim it.
        "unread_verified": False,
        "items": incoming[:20],
        "other_sources": {
            source: {"checked": False, "status": "not_checked",
                     "coverage": "use source history/CLI, then the authorized browser UI"}
            for source in sources if source != "hh.ru"
        },
    }

    all_rows = [row for rows in rows_by_source.values() for row in rows]
    problems = problem_queue(all_rows, source_states=states, limit=30)
    ready = sum(int(values.get("ready", 0) or 0) for values in queue.values())
    found = sum(int(row.get("items", 0) or 0) for row in scan.get("sources", []))
    checked = int(store.execution_funnel(execution_id).get("descriptions_checked", 0)) \
        if execution_id else 0

    source_rows: list[dict[str, Any]] = []
    for source in sources:
        source_scan = next((row for row in scan.get("sources", [])
                            if row.get("source") == source), {})
        state = str(states.get(source) or states.get(f"{track}:{source}") or "unknown")
        error = "; ".join(str(value) for value in source_scan.get("errors", []))[:500]
        source_rows.append({
            "source": source,
            "state": state,
            "scan_status": source_scan.get("status", "not_run"),
            "items": int(source_scan.get("items", 0) or 0),
            "passes": int(source_scan.get("passes", 0) or 0),
            "scan_attempts": int(source_scan.get("scan_attempts", 0) or 0),
            "stop_reason": stops.get(source, "") or error,
            "live": bool(live.get(source)),
            "ready": int(queue.get(source, {}).get("ready", 0) or 0),
        })

    return {
        "new_confirmed": new_confirmed,
        "new_confirmed_by_source": confirmed_by_source,
        "historical_confirmed_excluded": True,
        "found_cards": found,
        "descriptions_checked": checked,
        "ready_queue": ready,
        "refusals": {"new": len(new_refusals), "items": new_refusals[:20],
                     "event_time_note": "detected_at is when this campaign read the status"},
        "invitations": {"new": len(new_invitations), "items": new_invitations[:20],
                        "event_time_note": "event_at is the source timestamp when known"},
        "inbox": inbox,
        "problems": problems,
        "sources": source_rows,
        "unresolved_questions": problem_queue(
            [row for row in all_rows
             if next_action(row, states.get(str(row.get("source") or ""), ""))["requires_owner"]],
            source_states=states, limit=30,
        )["counts"],
        "unknown_quarantine": sum(
            1 for row in all_rows
            if str(row.get("global_application_state") or "").upper() == "UNKNOWN"
        ),
        "notes": [
            "new_confirmed comes from this campaign receipt ledgers only",
            "inbox not_checked is unknown coverage, never zero messages",
            "refusal/invitation detection time is separate from the source event time",
        ],
    }


def summarize_states(states: Mapping[str, str]) -> dict[str, int]:
    return dict(Counter(str(value) for value in states.values()))
