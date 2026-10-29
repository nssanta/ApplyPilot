"""Relevance and dispatch regression checks for AI/Python campaigns."""
from __future__ import annotations

from pathlib import Path

from applypilot.decision import decide_row
from applypilot.scoring import auto_apply_title_eligible
from applypilot.storage import effective_track_application_state


def test_manual_review_pattern_precedes_positive_title_match() -> None:
    search = {
        "auto_apply_title_terms": ["ai engineer", "python"],
        "auto_apply_review_title_patterns": [r"(?i)\bn8n\b"],
        "auto_apply_review_description_patterns": [r"(?i)required.{0,30}\bphp\b"],
    }
    assert not auto_apply_title_eligible(
        {"name": "AI Engineer (n8n)", "description": "Python LLM RAG"}, search
    )
    assert not auto_apply_title_eligible(
        {"name": "Python Developer", "description": "Required PHP for production"}, search
    )
    assert auto_apply_title_eligible(
        {"name": "Python Developer", "description": "Python LLM RAG FastAPI"}, search
    )


def test_broken_review_regex_blocks_auto_send() -> None:
    assert not auto_apply_title_eligible(
        {"name": "Python developer"},
        {"auto_apply_title_terms": ["python"],
         "auto_apply_review_title_patterns": ["[broken"]},
    )


def test_low_score_remains_in_review_without_losing_discovery() -> None:
    item = {
        "name": "AI Engineer", "description": "Python LLM RAG agents",
        "description_status": "ok", "score": 57,
        "score_decision": "pass", "filter_status": "pass",
        "review_state": "UNREVIEWED", "problems": [],
    }
    decision = decide_row(
        item, search={"auto_apply_title_terms": ["ai engineer"],
                      "auto_apply_min_score": 65},
    )
    assert decision.action == "REVIEW"
    assert decision.reason_code == "score_requires_manual_review"


def test_stale_auto_approval_is_not_dispatchable_after_relevance_review() -> None:
    assert effective_track_application_state(
        "READY_TO_APPLY", "APPROVED", "pass", "pass", "REVIEW",
    ) == "NOT_STARTED"
    assert effective_track_application_state(
        "READY_TO_APPLY", "APPROVED", "pass", "pass", "REJECT",
    ) == "NOT_STARTED"
    assert effective_track_application_state(
        "READY_TO_APPLY", "APPROVED", "pass", "pass", "APPLY",
    ) == "READY_TO_APPLY"


def test_clicker_queue_skips_old_approval_after_track_review(tmp_path: Path) -> None:
    from applypilot.storage import Store

    store = Store(tmp_path / "queue.sqlite3")
    store.start_campaign("run-1", "account", "ai", {})
    store.save_campaign_items(
        "run-1", "account", "ai",
        [{
            "id": "off-target", "name": "Fullstack Developer", "company": "X",
            "description": "Native mobile apps in Java and Kotlin",
            "description_status": "ok", "score": 85,
            "score_decision": "pass", "filter_status": "pass",
            "problems": [], "resume": "AI",
        }],
        status="ok", snapshot_path="x.json", source="hh.ru",
    )
    store.set_review_state(
        "account", "hh.ru", "off-target", "APPROVED",
        reason="auto-approved", track="ai",
    )
    assert len(store.approved_campaign_items("account", "ai")) == 1
    store.set_track_decision(
        "account", "hh.ru", "off-target", "ai", "REVIEW", "wrong stack",
    )
    assert store.approved_campaign_items("account", "ai") == []


def test_superseded_bulk_approval_must_not_override_new_role_gate() -> None:
    row = {
        "name": "Fullstack Developer", "description": "Web frontend in React",
        "description_status": "ok", "score": 88, "score_decision": "pass",
        "filter_status": "pass", "review_state": "APPROVED",
        "review_reason": "Legacy bulk approval",
        "global_application_state": "READY_TO_APPLY",
        "application_state": "READY_TO_APPLY", "problems": [],
    }
    search = {
        "auto_apply_title_terms": ["ai engineer", "python", "llm"],
        "superseded_bulk_approval_patterns": [r"^legacy bulk approval"],
    }
    old_batch = decide_row(row, search=search)
    assert old_batch.action == "REVIEW"
    assert old_batch.reason_code == "title_requires_manual_role_review"
    new_specific_approval = decide_row(
        {**row, "review_reason": "Owner directly approved this exact vacancy"},
        search=search,
    )
    assert new_specific_approval.action == "APPLY"
    assert new_specific_approval.reason_code == "owner_approved"
