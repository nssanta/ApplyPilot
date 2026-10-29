"""Operator-facing review annotations for the scan -> review -> apply flow.

The functions here are pure: they do not approve, reserve or submit anything.
They make the distinction between a relevance score and a real requirement
visible in one compact record, so a human/ChatGPT operator can select a batch
without relying on stale title or score gates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .decision import (
    decide_row,
    is_owner_approval,
    owner_hard_blockers,
)

_APPLIED_STATES = {"APPLIED_CONFIRMED", "ALREADY_APPLIED"}
_WORKFLOW_STATES = {"NEEDS_FORM", "NEEDS_LETTER", "NEEDS_MANUAL", "EXTERNAL_ATS"}


def _salary_text(salary: Any) -> str:
    if not isinstance(salary, Mapping) or not salary:
        return "not published"
    parts = []
    if salary.get("from") is not None:
        parts.append(f"from {salary['from']}")
    if salary.get("to") is not None:
        parts.append(f"to {salary['to']}")
    currency = str(salary.get("currency") or "").strip()
    return " ".join([*parts, currency]).strip() or "not published"


def suggested_action(row: Mapping[str, Any], *, hard: list[str],
                     decision: Any = None) -> tuple[str, str]:
    """Return one short operator label and its reason."""
    state = str(
        row.get("global_application_state") or row.get("application_state") or "NOT_STARTED"
    ).upper()
    if state == "UNKNOWN":
        return "VERIFY_UNKNOWN", "ambiguous submit is quarantined; verify in source history"
    if state in _APPLIED_STATES:
        return "DONE", "source already confirms an application"
    if state in _WORKFLOW_STATES:
        return "MANUAL", f"workflow state is {state}"
    if str(row.get("review_state") or "").upper() == "SKIP":
        return "SKIP", str(row.get("review_reason") or "owner skipped")
    if hard:
        return "SKIP", "; ".join(hard)[:300]
    if is_owner_approval(row):
        return "APPROVED", "operator approval is already saved"
    decision = decision or decide_row(row)
    if decision.action == "APPLY":
        return "APPROVE", decision.reason or "clean fit and allowed conditions"
    if decision.reason_code == "needs_description":
        return "CHECK_DESCRIPTION", decision.reason
    if decision.action == "REJECT":
        return "SKIP", decision.reason
    if decision.action == "BLOCKED":
        return "MANUAL", decision.reason
    return "REVIEW", decision.reason or decision.reason_code


def review_item(
    row: Mapping[str, Any],
    *,
    search: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    full_description: bool = False,
) -> dict[str, Any]:
    """Build a compact, auditable review record for one vacancy."""
    search = search or {}
    profile = profile or {}
    decision = decide_row(row, search=search, profile=profile, policy=policy)
    hard = owner_hard_blockers(row, search=search, profile=profile)
    action, action_reason = suggested_action(row, hard=hard, decision=decision)
    description = str(row.get("description") or "").strip()
    problems = [str(value) for value in row.get("problems", []) if str(value).strip()]
    filter_reasons = [
        str(value) for value in row.get("filter_reasons", []) if str(value).strip()
    ]
    hard_set = set(hard)
    warnings = [
        value for value in dict.fromkeys([*filter_reasons, *problems])
        if value not in hard_set
    ]
    return {
        "id": str(row.get("id") or ""),
        "source": str(row.get("source") or ""),
        "name": str(row.get("name") or ""),
        "company": str(row.get("company") or ""),
        "url": str(row.get("url") or ""),
        "score": int(row.get("score") or 0),
        "salary": _salary_text(row.get("salary")),
        "area": str(row.get("area") or ""),
        "experience": str(row.get("experience") or ""),
        "remote": bool(row.get("is_remote")),
        "published": str(row.get("published") or ""),
        "review_state": str(row.get("review_state") or "UNREVIEWED"),
        "application_state": str(
            row.get("global_application_state") or row.get("application_state") or ""
        ),
        "decision": decision.action,
        "decision_reason": decision.reason,
        "score_decision": str(row.get("score_decision") or "unknown"),
        "filter_status": str(row.get("filter_status") or "unknown"),
        "review_lane": str(row.get("review_lane") or ""),
        "resume": str(row.get("resume") or ""),
        "admit_reasons": [str(value) for value in row.get("reasons", []) if str(value)],
        "warnings": warnings,
        "hard_blockers": hard,
        "suggested_action": action,
        "action_reason": action_reason,
        "description_status": str(row.get("description_status") or ""),
        "description_chars": len(description),
        "description": description if full_description else description[:700],
    }


def review_counts(items: list[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get("suggested_action") or "REVIEW")
        counts[key] = counts.get(key, 0) + 1
    return counts
