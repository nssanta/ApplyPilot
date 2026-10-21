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
