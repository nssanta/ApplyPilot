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


def test_risks_from_exact_vacancy_text_flag_moscow_only_and_6yrs():
    from applypilot.bundles import _description_risks

    text = ("Важно: Рассмотрим кандидатов ТОЛЬКО с Москвы, интервью ОЧНО в офисе. "
            "Опыт работы с ML от 6 лет.")
    risk = _description_risks(text, "ML Engineer Middle")
    assert "geo:only_moscow_applicants" in risk
    assert "geo:in_person_interview" in risk
    assert "grade:ml_6plus" in risk
    assert _description_risks("Удалённая работа по РФ", "AI Engineer") == []


def test_bundle_does_not_auto_authorize_only_moscow_vacancy(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    rows = [
        {"id": "201", "name": "ML Engineer", "company": "Lab",
         "description": "Рассмотрим кандидатов ТОЛЬКО с Москвы. Интервью ОЧНО.",
         "description_status": "ok", "score_decision": "pass",
         "filter_status": "pass", "score": 90, "resume": "ML"},
        {"id": "202", "name": "ML Engineer", "company": "Other",
         "description": "Удалённо по всей РФ", "description_status": "ok",
         "score_decision": "pass", "filter_status": "pass", "score": 65, "resume": "ML"},
    ]
    data = _save(store, tmp_path, "ml", rows)
    result = compile_bundle(store, "account", {"ml": data})
    assert result["manual_geo_check_candidates"] == 1
    assert result["items"][0]["id"] == "202"
    only_moscow = next(row for row in result["items"] if row["id"] == "201")
    assert only_moscow["review_lane"] == "manual_geo_check"
    assert only_moscow["auto_apply_eligible"] is False



def test_top_review_prefers_stronger_fit_to_previously_approved_but_weaker_role(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    rows = [
        {"id": "previous", "company": "Old", "name": "AI Engineer",
         "description_status": "ok", "description": "RAG Python agent",
         "score": 65, "score_decision": "pass", "filter_status": "pass", "resume": "AI"},
        {"id": "strong", "company": "New", "name": "AI Agent Engineer",
         "description_status": "ok", "description": "MCP LLM Python agents",
         "score": 95, "score_decision": "pass", "filter_status": "pass", "resume": "AI"},
    ]
    report = _save(store, tmp_path, "ai", rows)
    store.set_review_state("account", "hh.ru", "previous", "APPROVED",
                           reason="approved in an older campaign", track="ai")
    output = compile_bundle(store, "account", {"ai": report})
    assert [item["id"] for item in output["items"]] == ["strong", "previous"]
    assert output["items"][1]["review_state"] == "APPROVED"


def test_ai_coding_tools_and_higher_salary_are_ranking_bonuses(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    items = [
        {"id": "codex", "company": "A", "name": "Python Developer",
         "score": 76, "score_decision": "pass", "filter_status": "pass",
         "salary": {"currency": "RUR", "from": 200000},
         "problems": ["salary:lower_bound_meets_threshold"],
         "description_status": "ok",
         "description": "Python FastAPI APIs, пишем код совместно с Claude Code и Codex"},
        {"id": "ordinary", "company": "B", "name": "Python Developer",
         "score": 76, "score_decision": "pass", "filter_status": "pass",
         "salary": {"currency": "RUR", "from": 120000},
         "problems": ["salary:lower_bound_meets_threshold"],
         "description_status": "ok", "description": "Python FastAPI APIs"},
    ]
    report = _save(store, tmp_path, "ai", items)
    result = compile_bundle(store, "account", {"ai": report})
    assert result["items"][0]["id"] == "codex"
    assert "workflow:ai_assisted_coding" in result["items"][0]["flags"]
    assert result["items"][0]["review_rank"] > result["items"][1]["review_rank"]


def test_wide_stack_search_does_not_rank_embedded_hardware_above_ai_vibecoder(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    items = [
        {"id": "embedded", "name": "AI-First Embedded / Firmware Engineer", "company": "Electronics",
         "description": "Встраиваемые системы, микроконтроллеры, C++",
         "description_status": "ok", "score": 90, "score_decision": "pass",
         "filter_status": "pass", "resume": "AI",
         "salary": {"currency": "RUR", "from": 200000}},
        {"id": "coding", "name": "AI-разработчик / Вайбкодер", "company": "SaaS",
         "description": "Python, FastAPI, Claude Code, RAG, Cursor, Codex",
         "description_status": "ok", "score": 85, "score_decision": "pass",
         "filter_status": "pass", "resume": "AI",
         "salary": {"currency": "RUR", "from": 120000}},
    ]
    report = _save(store, tmp_path, "ai", items)
    result = compile_bundle(store, "account", {"ai": report})
    assert [row["id"] for row in result["items"]] == ["coding"]
    assert result["skipped"]["out_of_scope_title"] == 1


def test_salary_ceiling_and_primary_rust_get_manual_review(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    items = [
        {"id": "cheap", "name": "Python Backend Developer", "company": "One",
         "salary": {"from": 80000, "to": 120000, "currency": "RUR"},
         "score": 93, "score_decision": "pass", "filter_status": "pass",
         "description_status": "ok", "description": "FastAPI Python",
         "resume": "AI"},
        {"id": "rust", "name": "Middle Backend разработчик (Rust/С++)", "company": "Two",
         "salary": {"from": 250000, "currency": "RUR"},
         "score": 85, "score_decision": "pass", "filter_status": "pass",
         "description_status": "ok", "description": "Backend Rust C++ Python",
         "resume": "AI"},
    ]
    report = _save(store, tmp_path, "ai", items)
    result = compile_bundle(store, "account", {"ai": report})
    by_id = {v["id"]: v for v in result["items"]}
    assert by_id["cheap"]["review_lane"] == "manual_salary_check"
    assert by_id["rust"]["review_lane"] == "manual_specialist_check"
    assert not any(v["auto_apply_eligible"] for v in result["items"])


def test_ai_agent_title_outweighs_same_score_generic_fullstack(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    rows = [
        {"id": "agent", "name": "AI-инженер мультиагентных систем", "company": "Product",
         "description": "LLM RAG tool calling Python", "description_status": "ok",
         "score": 72, "score_decision": "pass", "filter_status": "pass", "resume": "AI"},
        {"id": "generic", "name": "Fullstack-разработчик", "company": "Web",
         "description": "TypeScript React Node.js API", "description_status": "ok",
         "score": 72, "score_decision": "pass", "filter_status": "pass", "resume": "AI"},
    ]
    result = compile_bundle(store, "account", {"ai": _save(store, tmp_path, "ai", rows)})
    assert result["items"][0]["id"] == "agent"
    assert "role:agent_llm_priority" in result["items"][0]["flags"]
