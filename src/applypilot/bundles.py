"""Единая on-demand сводка нескольких кампаний без автоматической рассылки."""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .storage import Store

OUT_OF_SCOPE_TITLES = re.compile(
    r"\b(?:aqa|qa|sdet|php|bim|bitrix|computer vision|cv engineer|quality assurance|"
    r"test automation|test engineer|software development engineer in test)\b"
    r"|тестиров|битрикс|1с", re.IGNORECASE,
)


def compile_bundle(
    store: Store, account: str, reports: dict[str, dict[str, Any]], *,
    limit: int = 100,
) -> dict[str, Any]:
    """Дедуп по vacancy_id между треками; использует только текущие snapshots."""
    if not 1 <= limit <= 100:
        raise ValueError("review limit must be between 1 and 100")
    observed_ids: set[str] = set()
    first_seen_ids: set[str] = set()
    new_since_previous_ids: set[str] = set()
    recent_published_ids: set[str] = set()
    selected: dict[str, dict[str, Any]] = {}
    skipped = {
        "already_applied_or_uncertain": 0, "screening_rejected": 0,
        "operator_skipped": 0, "out_of_scope_title": 0,
        "awaiting_form_or_letter": 0,
    }
    totals: dict[str, dict[str, Any]] = {}
    priority = {"ai": 0, "backend": 1, "ml": 2}
    for track, report in reports.items():
        snapshot = Path(str(report["snapshot"]))
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        # Publication freshness is anchored to the scan timestamp, not to
        # when an archived bundle is replayed the following morning.
        observed_text = str(payload.get("observed_at") or payload.get("fetched_at") or "")
        try:
            observed_now = datetime.fromisoformat(observed_text)
            if observed_now.tzinfo is None:
                observed_now = observed_now.replace(tzinfo=UTC)
            observed_now = observed_now.astimezone(UTC)
        except ValueError:
            observed_now = datetime.now(UTC)
        day_cutoff = observed_now - timedelta(hours=24)
        current_ids = {str(row.get("id")) for row in payload.get("items", []) if row.get("id")}
        observed_ids.update(current_ids)
        for row in payload.get("items", []):
            vid = str(row.get("id") or "")
            if not vid:
                continue
            published = str(row.get("published") or "").strip()
            if published:
                try:
                    parsed = datetime.fromisoformat(published)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    if day_cutoff <= parsed.astimezone(UTC) <= observed_now:
                        recent_published_ids.add(vid)
                except ValueError:
                    pass
        # Use persistent campaign observations, NOT legacy seen.json:is_new:
        # those two histories can disagree and were measured to differ in HH.
        campaign_id = str(report.get("campaign_id") or payload.get("campaign_id") or "")
        if campaign_id:
            with store.connect() as conn:
                observations = conn.execute(
                    """SELECT vacancy_id,change_kind FROM vacancy_observations
                    WHERE campaign_id=? AND account=? AND track=?""",
                    (campaign_id, account, track),
                ).fetchall()
            for observed in observations:
                if observed["change_kind"] == "first_seen":
                    first_seen_ids.add(str(observed["vacancy_id"]))
                elif observed["change_kind"] == "new_since_previous":
                    new_since_previous_ids.add(str(observed["vacancy_id"]))
        rows = store.list_campaign_vacancies(account, track, "all", limit=5000)
        totals[track] = {
            "found": report["summary"]["items"],
            "first_seen": report["summary"]["first_seen"],
            "new_since_previous": report["summary"]["new_since_previous"],
            "queries_covered": report.get("covered_query_count"),
            "queries_total": report.get("query_count"),
            "uncovered_queries": report.get("uncovered_queries", []),
            "cached_descriptions": report.get("cached_descriptions", 0),
            "detail_fetch_attempts": report.get("detail_fetch_attempts", 0),
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
            if item.get("application_state") in {"NEEDS_FORM", "NEEDS_LETTER", "EXTERNAL_ATS"}:
                skipped["awaiting_form_or_letter"] += 1
                continue
            if OUT_OF_SCOPE_TITLES.search(str(item.get("name") or "")):
                skipped["out_of_scope_title"] += 1
                continue
            if item.get("filter_status") not in {"pass", "unknown"}:
                skipped["screening_rejected"] += 1
                continue
            decision = str(item.get("score_decision") or "unknown")
            evidence = item.get("components") or {}
            preliminary = sum(int(v) for v in evidence.values()) if isinstance(evidence, dict) else 0
            needs_enrichment = item.get("description_status") != "ok" and (
                decision == "pass"
                or (
                    decision == "unknown"
                    and not item.get("hard_killers")
                    and int(evidence.get("role", 0) or 0) >= 20
                    and preliminary >= 45
                )
            )
            if decision != "pass" and not needs_enrichment:
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
            # High confidence first: salary guaranteed >= target, relevant
            # stack, and no unsupported seniority/English. Keep overlaps and
            # undisclosed salaries, but mark them as uncertain.
            adjustments = 10 if item.get("filter_status") == "pass" else -5
            if "salary:lower_bound_meets_threshold" in flags:
                adjustments += 12
            elif "salary:range_overlaps_threshold" in flags:
                adjustments += 3
            if any(str(v) in str(item.get("name", "")).casefold() for v in
                   ("agent", "агент", "ai-native", "ai-first", "вайб", "vibe", "llm", "rag")):
                adjustments += 7
            if any("seniority" in str(f) for f in flags):
                adjustments -= 18
            if any("english_requirement" in str(f) for f in flags):
                adjustments -= 12
            review_rank = score + adjustments if not needs_enrichment else min(74, preliminary - 15)
            if needs_enrichment:
                flags.append("review:needs_full_description")
            item_copy = {
                "id": vacancy_id, "track": track, "name": item.get("name", ""),
                "company": item.get("company", ""), "url": item.get("url", ""),
                "score": score, "review_rank": review_rank,
                "review_lane": "needs_enrichment" if needs_enrichment else "fit",
                "auto_apply_eligible": not needs_enrichment,
                "resume": item.get("resume", ""), "salary": item.get("salary"),
                "review_state": item.get("review_state", "UNREVIEWED"),
                "application_state": item.get("application_state", "NOT_STARTED"),
                "filter_status": item.get("filter_status", "unknown"),
                "reasons": item.get("reasons", []), "flags": list(dict.fromkeys(flags)),
                "description_status": item.get("description_status", "unknown"),
            }
            existing = selected.get(vacancy_id)
            if existing is None or (
                item_copy["review_lane"] == "fit",
                item_copy["review_rank"], -priority.get(track, 9)
            ) > (
                existing["review_lane"] == "fit",
                existing["review_rank"], -priority.get(existing["track"], 9)
            ):
                selected[vacancy_id] = item_copy
    ordered = sorted(
        selected.values(),
        key=lambda v: (
            v["review_state"] != "APPROVED",
            v["review_lane"] != "fit",
            -v["review_rank"],
            priority.get(v["track"], 9),
            v["id"],
        ),
    )
    # HH can repost the exact same job multiple times under distinct IDs.
    # Keep the best/approved instance for action; retain all other IDs in
    # the report so the system never silently counts duplicates as reach.
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    duplicates_suppressed = 0
    for item in ordered:
        company = " ".join(html.unescape(str(item["company"])).casefold().split())
        title = " ".join(html.unescape(str(item["name"])).casefold().split())
        # Do not merge unknown companies, or different positions at one company.
        key = (company, title) if company and title else ("ID", item["id"])
        if key not in groups:
            groups[key] = item
            item["duplicate_posting_ids"] = []
        else:
            groups[key]["duplicate_posting_ids"].append(item["id"])
            duplicates_suppressed += 1
    unique_roles = list(groups.values())
    ranked = unique_roles[:limit]
    return {
        "kind": "on_demand_review_bundle", "ready_to_send": False,
        "no_applications_sent": True,
        "tracks": totals, "raw_rows_by_track": sum(x["found"] for x in totals.values()),
        "unique_raw_vacancies": len(observed_ids),
        "first_seen_sqlite_unique": len(first_seen_ids),
        "new_since_previous_unique": len(new_since_previous_ids),
        "published_last_24h_unique": len(recent_published_ids),
        "eligible_postings": len(selected),
        "unique_candidates": len(unique_roles),
        "duplicate_postings_suppressed": duplicates_suppressed,
        "review_count": len(ranked),
        "fit_candidates": sum(v["review_lane"] == "fit" for v in unique_roles),
        "needs_enrichment_candidates": sum(
            v["review_lane"] == "needs_enrichment" for v in unique_roles
        ),
        "skipped": skipped,
        "items": ranked,
        "note": "FIT и NEEDS_ENRICHMENT разделены. Только подтверждённый FIT допускается до отдельного согласованного apply.",
    }
