"""Compact, read-only agent review queue from already discovered vacancies.

Score orders reviews; it never approves applications. Source ledgers and explicit
owner decisions remain authoritative.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping


_CLOSED = {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN", "SUBMITTING"}
_WORKFLOW = {"NEEDS_FORM", "NEEDS_LETTER", "NEEDS_MANUAL", "EXTERNAL_ATS"}
_VETO = {"SKIP", "DEFERRED", "MAYBE"}


def build_review_shortlist(
    rows: Iterable[Mapping[str, Any]],
    *,
    limit: int = 100,
    min_score: int = 45,
    description_chars: int = 3500,
) -> dict[str, Any]:
    """Return reviewable *complete* cards, without mutating state or granting approval."""
    if not 1 <= limit <= 500 or not 0 <= min_score <= 100:
        raise ValueError("limit must be 1..500 and min_score must be 0..100")
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    ready_to_check: dict[tuple[str, str], dict[str, Any]] = {}
    counts = {"total_rows": 0, "missing_description": 0, "awaiting_review": 0,
              "already_ready": 0, "closed_or_vetoed": 0}
    for row in rows:
        counts["total_rows"] += 1
        source = str(row.get("source") or "")
        vacancy_id = str(row.get("id") or "")
        if not (source and vacancy_id):
            continue
        global_state = str(row.get("global_application_state") or "NOT_STARTED").upper()
        app_state = str(row.get("application_state") or "NOT_STARTED").upper()
        review = str(row.get("review_state") or "UNREVIEWED").upper()
        decision = str(row.get("decision") or "").upper()
        if (global_state in _CLOSED | _WORKFLOW
                or app_state in _CLOSED | _WORKFLOW
                or review in _VETO or row.get("applied_before")
                or row.get("channel_duplicate")):
            counts["closed_or_vetoed"] += 1
            continue
        ready = decision == "APPLY" and review == "APPROVED" and app_state == "READY_TO_APPLY"
        if not ready and (decision != "REVIEW" or review == "APPROVED"):
            continue
        if str(row.get("description_status") or "") != "ok" or not str(row.get("description") or "").strip():
            counts["missing_description"] += 1
            continue
        score = int(row.get("score") or 0)
        if not ready and (score < min_score or str(row.get("filter_status") or "") == "reject"):
            continue
        if ready:
            counts["already_ready"] += 1
        else:
            counts["awaiting_review"] += 1
        description = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(row.get("description") or ""))).strip()
        item = {
            "source": source, "track": str(row.get("track") or ""),
            "stage": "pre_send_recheck" if ready else "needs_agent_review",
            "id": vacancy_id, "score": score, "title": str(row.get("name") or ""),
            "company": str(row.get("company") or ""), "url": str(row.get("url") or ""),
            "filter": str(row.get("filter_status") or ""),
            "reason": str(row.get("decision_reason") or "")[:350],
            "requirements_to_check": list(row.get("problems") or [])[:10],
            "salary": row.get("salary") or {},
            "experience": row.get("experience") or "",
            "remote": row.get("is_remote"),
            "description": description[:description_chars],
            "description_truncated": len(description) > description_chars,
        }
        key = source, vacancy_id
        target = ready_to_check if ready else selected
        previous = target.get(key)
        if previous is None or (item["score"], item["track"] == "ai") > (
            previous["score"], previous["track"] == "ai",
        ):
            target[key] = item
    items = sorted(selected.values(), key=lambda i: (-i["score"], i["source"], i["id"]))[:limit]
    ready_items = sorted(ready_to_check.values(), key=lambda i: (-i["score"], i["source"], i["id"]))[:limit]
    return {"kind": "review_shortlist", "approved": False, "sendable": False,
            "note": "Agent must read full requirements and explicitly approve exact IDs before dispatch.",
            "counts": {**counts, "unique_reviewable": len(selected), "returned": len(items),
                       "unique_pre_send_recheck": len(ready_to_check)},
            "ready_items": ready_items, "items": items}