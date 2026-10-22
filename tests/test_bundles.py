import json

from applypilot.bundles import compile_bundle
from applypilot.storage import Store


def _save(store, tmp_path, track, rows):
    run_id = "scan-" + track
    store.start_campaign(run_id, "account", track, {"query": "LLM"})
    store.save_campaign_items(
        run_id, "account", track, rows, status="ok", snapshot_path=str(tmp_path / "ignored.json"),
    )
    snap = tmp_path / f"{track}.json"
    snap.write_text(json.dumps({"items": rows}), encoding="utf-8")
    return {"snapshot": str(snap), "status": "ok",
            "summary": {"items": len(rows), "first_seen": len(rows), "new_since_previous": 0}}


def test_bundle_selects_unique_relevant_and_preserves_tracking(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    ai = [
        {"id": "one", "name": "AI Agent Engineer", "score": 80, "score_decision": "pass",
         "filter_status": "unknown", "resume": "AI", "description": "RAG and Python",
         "filter_reasons": ["salary:unpublished"]},
        {"id": "two", "name": "AI Agent Engineer", "score": 65, "score_decision": "pass",
         "filter_status": "pass", "resume": "AI"},
        {"id": "excluded", "name": "Java AI marketing", "score": 91, "score_decision": "reject",
         "filter_status": "pass", "resume": "AI"},
        {"id": "sent", "name": "AI Engineer", "score": 88, "score_decision": "pass",
         "filter_status": "pass", "resume": "AI"},
    ]
    backend = [
        {"id": "one", "name": "AI Backend Engineer", "score": 64, "score_decision": "pass",
         "filter_status": "pass", "resume": "Backend"},
        {"id": "back", "name": "Python FastAPI Developer", "score": 66, "score_decision": "pass",
         "filter_status": "pass", "resume": "Backend"},
    ]
    ra = _save(store, tmp_path, "ai", ai)
    rb = _save(store, tmp_path, "backend", backend)
    store.record(ai[-1], "success", account="account")
    bundle = compile_bundle(store, "account", {"ai": ra, "backend": rb}, limit=100)
    ids = {v["id"] for v in bundle["items"]}
    assert ids == {"one", "two", "back"}
    assert bundle["review_count"] == bundle["unique_candidates"] == 3
    assert bundle["no_applications_sent"] and not bundle["ready_to_send"]
    assert len([v for v in bundle["items"] if v["id"] == "one"]) == 1
    assert bundle["skipped"]["already_applied_or_uncertain"] == 1
    assert "salary:unpublished" in next(v for v in bundle["items"] if v["id"] == "one")["flags"]
    assert len(store.list_campaign_vacancies("account", "ai", "all")) == 4


def test_bundle_does_not_use_stale_candidates_not_in_current_snapshot(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    rows = [
        {"id": "old", "name": "AI Engineer", "score": 77, "score_decision": "pass",
         "filter_status": "pass", "resume": "AI"},
        {"id": "fresh", "name": "RAG Engineer", "score": 70, "score_decision": "pass",
         "filter_status": "pass", "resume": "AI"},
    ]
    data = _save(store, tmp_path, "ai", rows)
    snap = tmp_path / "ai.json"
    snap.write_text(json.dumps({"items": [rows[-1]]}), encoding="utf-8")
    result = compile_bundle(store, "account", {"ai": data})
    assert [x["id"] for x in result["items"]] == ["fresh"]


def test_bundle_filters_non_target_qa_php_and_primary_cv_titles(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    rows = [
        {"id": key, "name": name, "score": 95, "score_decision": "pass",
         "filter_status": "pass", "resume": "Backend"}
        for key, name in [
            ("qa", "AQA инженер (Python)"),
            ("php", "Fullstack-разработчик PHP/Python"),
            ("cv", "Computer Vision Engineer"),
            ("good", "Python Backend Developer"),
        ]
    ]
    report = _save(store, tmp_path, "backend", rows)
    result = compile_bundle(store, "account", {"backend": report})
    assert [row["id"] for row in result["items"]] == ["good"]
    assert result["skipped"]["out_of_scope_title"] == 3


def test_bundle_deduplicates_same_employer_and_role_but_not_different_roles(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    items = [
        {"id": "201", "company": "Example Inc", "name": "AI-инженер по автоматизации",
         "description": "LLM Python RAG", "description_status": "ok",
         "score": 85, "score_decision": "pass", "filter_status": "pass", "resume": "AI"},
        {"id": "202", "company": "Example Inc", "name": "AI-инженер по автоматизации",
         "description": "LLM Python RAG", "description_status": "ok",
         "score": 80, "score_decision": "pass", "filter_status": "pass", "resume": "AI"},
        {"id": "203", "company": "Example Inc", "name": "RAG Backend Developer",
         "description": "RAG Python FastAPI", "description_status": "ok",
         "score": 68, "score_decision": "pass", "filter_status": "pass", "resume": "AI"},
    ]
    report = _save(store, tmp_path, "ai", items)
    result = compile_bundle(store, "account", {"ai": report})
    assert result["eligible_postings"] == 3
    assert result["unique_candidates"] == 2
    assert result["duplicate_postings_suppressed"] == 1
    assert [v["id"] for v in result["items"]] == ["201", "203"]
    assert result["items"][0]["duplicate_posting_ids"] == ["202"]


def test_bundle_keeps_forms_in_separate_workflow_queue(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    items = [
        {"id": "201", "company": "Example", "name": "AI Engineer",
         "score": 80, "score_decision": "pass", "filter_status": "pass",
         "description_status": "ok", "description": "Python LLM"},
    ]
    report = _save(store, tmp_path, "ai", items)
    store.record({**items[0], "track": "ai"}, "needs_manual",
                 "screening questions require manual review", account="account")
    result = compile_bundle(store, "account", {"ai": report})
    assert result["review_count"] == 0
    assert result["skipped"]["awaiting_form_or_letter"] == 1
    assert len(store.workflow_tasks("account", "form")) == 1


def test_publication_last_24h_is_relative_to_snapshot_time_not_replay_time(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    items = [
        {"id": "one", "company": "Lab", "name": "AI Engineer",
         "published": "2026-10-21T10:00:00+00:00",
         "score": 80, "score_decision": "pass", "filter_status": "pass",
         "description": "LLM Python", "description_status": "ok", "resume": "AI"},
    ]
    report = _save(store, tmp_path, "ai", items)
    snap = tmp_path / "ai.json"
    snap.write_text(json.dumps({
        "observed_at": "2026-10-21T19:00:00+00:00", "items": items
    }), encoding="utf-8")
    result = compile_bundle(store, "account", {"ai": report})
    assert result["published_last_24h_unique"] == 1
