"""Единая on-demand сводка нескольких кампаний без автоматической рассылки."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .storage import Store

OUT_OF_SCOPE_TITLES = re.compile(
    r"\b(?:aqa|qa|sdet|php|computer vision|cv engineer|quality assurance|"
    r"test automation|test engineer|software development engineer in test)\b"
    r"|тестиров", re.IGNORECASE,
)


def compile_bundle(
    store: Store, account: str, reports: dict[str, dict[str, Any]], *,
    limit: int = 100,
) -> dict[str, Any]:
    """Дедуп по vacancy_id между треками; использует только текущие snapshots."""
    if not 1 <= limit <= 100:
        raise ValueError("review limit must be between 1 and 100")
    selected: dict[str, dict[str, Any]] = {}
    skipped = {
        "already_applied_or_uncertain": 0, "screening_rejected": 0,
        "operator_skipped": 0, "out_of_scope_title": 0,
    }
    totals: dict[str, dict[str, Any]] = {}
    priority = {"ai": 0, "backend": 1, "ml": 2}
    for track, report in reports.items():
        snapshot = Path(str(report["snapshot"]))
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        current_ids = {str(row.get("id")) for row in payload.get("items", []) if row.get("id")}
        rows = store.list_campaign_vacancies(account, track, "all", limit=1000)
        totals[track] = {
            "found": report["summary"]["items"],
            "first_seen": report["summary"]["first_seen"],
            "new_since_previous": report["summary"]["new_since_previous"],
            "status": report["status"],
        }
        for item in rows:
            vacancy_id = str(item.get("id") or "")
            if vacancy_id not in current_ids:
                continue
            if item.get("application_state") in {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN"} or item.get("applied_before"):
                skipped["already_applied_or_uncertain"] += 1
                continue
            if item.get("review_state") == "SKIP":
                skipped["operator_skipped"] += 1
                continue
            if OUT_OF_SCOPE_TITLES.search(str(item.get("name") or "")):
                skipped["out_of_scope_title"] += 1
                continue
            if item.get("score_decision") != "pass" or item.get("filter_status") not in {"pass", "unknown"}:
                skipped["screening_rejected"] += 1
                continue
            flags = list(dict.fromkeys([*item.get("filter_reasons", []), *item.get("problems", [])]))
            if item.get("filter_status") == "unknown":
                flags.append("verify:uncertain-filter")
            if track != "ai" and re.search(
                r"\b(?:AI|LLM|RAG|Agent|GenAI)\b", str(item.get("name") or ""), re.IGNORECASE,
            ):
                flags.append("verify:check-ai-resume-selection")
            score = int(item.get("score") or 0)
            # Rank fit and reliable salary above speculative high seniority;
            # never silently reinterpret score as human-approved suitability.
            adjustments = 10 if item.get("filter_status") == "pass" else -5
            if any("seniority" in str(f) for f in flags):
                adjustments -= 18
            if any("english_requirement" in str(f) for f in flags):
                adjustments -= 12
            item_copy = {
                "id": vacancy_id, "track": track, "name": item.get("name", ""),
                "company": item.get("company", ""), "url": item.get("url", ""),
                "score": score, "review_rank": score + adjustments,
                "resume": item.get("resume", ""), "salary": item.get("salary"),
                "review_state": item.get("review_state", "UNREVIEWED"),
                "application_state": item.get("application_state", "NOT_STARTED"),
                "filter_status": item.get("filter_status", "unknown"),
                "reasons": item.get("reasons", []), "flags": list(dict.fromkeys(flags)),
                "description_status": item.get("description_status", "unknown"),
            }
            existing = selected.get(vacancy_id)
            if existing is None or (
                item_copy["review_rank"], -priority.get(track, 9)
            ) > (
                existing["review_rank"], -priority.get(existing["track"], 9)
            ):
                selected[vacancy_id] = item_copy
    ranked = sorted(
        selected.values(),
        key=lambda v: (
            v["review_state"] != "APPROVED",
            -v["review_rank"],
            priority.get(v["track"], 9),
            v["id"],
        ),
    )[:limit]
    return {
        "kind": "on_demand_review_bundle", "ready_to_send": False,
        "no_applications_sent": True,
        "tracks": totals, "unique_candidates": len(selected),
        "review_count": len(ranked), "skipped": skipped,
        "items": ranked,
        "note": "Механический shortlist. Для отправки нужны пакетное решение, проверка HH и resume.",
    }
