"""Actionable queue labels shared by the CLI, reports and diagnostics.

The scanner and the clicker are separate stages.  Every row that is not
terminal therefore needs one explicit next action, including the reason it
cannot advance.  These helpers are deliberately read-only: they never approve,
reserve or submit anything.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

ACTIONS = {
    "send",
    "enrich",
    "check_requirement",
    "ask_owner",
    "reject",
    "verify_unknown",
    "wait_source",
    "review",
    "no_progress",
}

_OWNER_MARKERS = (
    "english",
    "degree",
    "education",
    "owner decision",
    "owner approval",
    "unknown personal",
    "unknown_personal",
    "seniority",
    "grade",
    "experience is below",
    "неизвест",
    "образован",
    "опыт",
    "англий",
)


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def next_action(row: Mapping[str, Any], source_state: str = "") -> dict[str, Any]:
    """Returns the single safe next action for one queue row."""
    app_state = str(
        row.get("global_application_state") or row.get("application_state") or "NOT_STARTED"
    ).upper()
    decision = str(row.get("decision") or "").upper()
    reason_code = _text(row.get("decision_reason"))
    review_state = str(row.get("review_state") or "UNREVIEWED").upper()
    description_status = str(row.get("description_status") or "").lower()
    streak = int(row.get("decision_streak") or 0)
    low_reason = reason_code.casefold()
    source_state = str(source_state or "").casefold()

    if source_state in {"platform_blocked", "source_unavailable", "campaign_error"}:
        return {
            "next_action": "wait_source",
            "action_reason": f"source state is {source_state}; do not retry blindly",
            "requires_owner": False,
        }
    if app_state == "UNKNOWN":
        return {
            "next_action": "verify_unknown",
            "action_reason": "ambiguous submit is quarantined; verify it in source history first",
            "requires_owner": False,
        }
    if app_state in {"NEEDS_FORM", "NEEDS_LETTER", "NEEDS_MANUAL"}:
        owner = app_state != "NEEDS_FORM"
        return {
            "next_action": "ask_owner" if owner else "enrich",
            "action_reason": _text(row.get("application_note")) or f"workflow state {app_state}",
            "requires_owner": owner,
        }
    if row.get("applied_before"):
        return {
            "next_action": "reject",
            "action_reason": "channel already has an application or reservation",
            "requires_owner": False,
        }
    if streak >= 2:
        return {
            "next_action": "no_progress",
            "action_reason": (
                f"unchanged for {streak} decision passes ({decision or 'UNDECIDED'}: "
                f"{reason_code or 'no reason'}); inspect the gate or change the input"
            ),
            "requires_owner": False,
            "stagnant": True,
        }
    if decision == "APPLY" and app_state == "READY_TO_APPLY" and review_state == "APPROVED":
        return {
            "next_action": "send",
            "action_reason": "approved and ready; send before any new large scan",
            "requires_owner": False,
        }
    if decision == "APPLY" and app_state == "READY_TO_APPLY":
        return {
            "next_action": "review",
            "action_reason": f"ready but review_state is {review_state}",
            "requires_owner": False,
        }
    if description_status in {"missing", "provisional", "telegram", "network_error", "parse_error"} \
            or "needs_description" in low_reason:
        return {
            "next_action": "enrich",
            "action_reason": "full description/score is missing",
            "requires_owner": False,
        }
    if decision == "REJECT" or review_state == "SKIP":
        return {
            "next_action": "reject",
            "action_reason": reason_code or "rejected by the current criteria",
            "requires_owner": False,
        }
    if decision == "REVIEW":
        if any(marker in low_reason for marker in _OWNER_MARKERS):
            return {
                "next_action": "ask_owner",
                "action_reason": reason_code or "a personal fact or explicit owner decision is missing",
                "requires_owner": True,
            }
        if any(marker in low_reason for marker in (
            "remote", "unknown_filter", "needs_evidence", "title", "score", "filter",
            "insufficient_role", "approved_without_ready",
        )):
            return {
                "next_action": "check_requirement",
                "action_reason": reason_code or "a concrete listing requirement must be verified",
                "requires_owner": False,
            }
        return {
            "next_action": "review",
            "action_reason": reason_code or "manual queue review is required",
            "requires_owner": False,
        }
    if app_state in {"APPLIED_CONFIRMED", "ALREADY_APPLIED"}:
        return {
            "next_action": "review",
            "action_reason": "application already confirmed; check the conversation instead",
            "requires_owner": False,
        }
    if decision == "BLOCKED":
        return {
            "next_action": "review",
            "action_reason": reason_code or "channel or workflow is blocked",
            "requires_owner": False,
        }
    return {
        "next_action": "review",
        "action_reason": reason_code or "waiting for the next decision pass",
        "requires_owner": False,
    }


def problem_queue(
    rows: Iterable[Mapping[str, Any]],
    *,
    source_states: Mapping[str, str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Collects unresolved work without mixing it with confirmed sends."""
    states = {str(key): str(value) for key, value in (source_states or {}).items()}
    counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    for row in rows:
        source = str(row.get("source") or "")
        action = next_action(row, states.get(source, ""))
        if action["next_action"] in {"send", "reject"}:
            continue
        counts[action["next_action"]] += 1
        if len(items) < max(1, int(limit)):
            items.append({
                "source": source,
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "company": str(row.get("company") or ""),
                "score": int(row.get("score") or 0),
                "decision": str(row.get("decision") or ""),
                "application_state": str(
                    row.get("global_application_state") or row.get("application_state") or ""
                ),
                **action,
            })
    return {"counts": dict(counts), "items": items}
