"""Регрессии по реальным типам HH-заголовков и бюджету поиска, без сетевых вызовов."""

from __future__ import annotations

from datetime import UTC, datetime

from applypilot.bundles import compile_bundle
from applypilot.campaigns import run_campaign
from applypilot.config import AppConfig, effective_search
from applypilot.parser import ScanSegment
from applypilot.presets import ROLE_PRESETS
from applypilot.scoring import prioritize_for_enrichment, score_vacancy
from applypilot.sources import HhSearchAdapter
from applypilot.storage import Store


def test_true_ai_developer_and_vibecoder_titles_reach_review():
    cfg = ROLE_PRESETS["ai-agents-llmops"]
    profile = {**cfg, "search": cfg, "rescore": True}
    positive_titles = [
        "AI-разработчик / Вайбкодер (Vibe Coding)",
        "Инженер по автоматизации AI-агентов",
        "Разработчик ИИ-решений",
        "ИИ-инженер",
        "Ведущий AI-инженер / AI Tech Lead (Python, LLM, AI Agents)",
    ]
    for title in positive_titles:
        item = score_vacancy(
            {"id": "fixture", "name": title, "description":
             "Создавать и поддерживать LLM AI-агентов, Python FastAPI MCP, API-интеграции."},
            profile,
        )
        assert item.decision == "pass", (title, item.hard_killers, item.reasons)
    for title in ("Vibe Coding Mentor", "AI Recruiter", "AI Sales Manager", "QA Engineer LLM"):
        item = score_vacancy(
            {"id": "fixture", "name": title, "description": "AI LLM Python MCP"},
            profile,
        )
        assert item.decision == "reject", title


def test_enrichment_uses_preliminary_evidence_before_legacy_id_order():
    cfg = ROLE_PRESETS["ai-agents-llmops"]
    profile = {**cfg, "search": cfg, "rescore": True}
    candidates = [
        {"id": "a", "name": "AI Engineer", "description": "",
         "work_format": [], "salary": None},
        {"id": "z", "name": "AI/LLM Agent Engineer",
         "description": "AI, LLM, RAG, MCP, Python, Docker, FastAPI, PostgreSQL",
         "salary": {"currency": "RUR", "from": 180000},
         "is_remote": True},
    ]
    # no full vacancy description; both can be capped to a provisional score
    selected = prioritize_for_enrichment(candidates, profile, limit=1)
    assert [x.id for x in selected] == ["z"]


def test_hh_adapter_visits_all_first_pages_before_second_page(monkeypatch):
    calls = []

    def scan_fake(queries, areas, pages, **kwargs):
        calls.append((tuple(queries), kwargs.get("start_page", 0), pages, kwargs.get("request_budget")))
        return (
            [{"id": f"{queries[0]}-{kwargs.get('start_page', 0)}",
              "query_sources": list(queries), "area_sources": list(areas)}],
            [ScanSegment(str(query), int(areas[0]), pages, "truncated", 50,
                         requests=1, order_by=kwargs["order_by"]) for query in queries],
        )

    monkeypatch.setattr("applypilot.sources.scan_many", scan_fake)
    got, segments = HhSearchAdapter().discover(
        {"queries": ["AI Agents", "Вайбкодер", "RAG"], "areas": [113],
         "pages": 2, "remote": True, "request_budget": 5, "order_by": "publication_time",
         "pause_seconds": 0},
    )
    assert [(row[0], row[1], row[2]) for row in calls] == [
        (("AI Agents",), 0, 1), (("Вайбкодер",), 0, 1), (("RAG",), 0, 1),
        (("AI Agents",), 1, 1), (("Вайбкодер",), 1, 1),
    ]
    assert [row[3] for row in calls] == [5, 4, 3, 2, 1]
    assert len(segments) == 5
    assert len({x["id"] for x in got}) == 5


def test_bundle_includes_preliminary_title_evidence_without_authorizing_apply(tmp_path):
    store = Store(tmp_path / "data.sqlite3")
    run = "b1"
    store.start_campaign(run, "account", "ai", {})
    rows = [
        {"id": "new-technical", "name": "Разработчик ИИ-решений",
         "description_status": "provisional", "description": "ИИ",
         "components": {"role": 35, "stack": 12, "duties": 0, "salary": 0, "format": 5},
         "score_decision": "unknown", "filter_status": "unknown", "score": 29,
         "resume": "AI"},
        {"id": "confirmed", "name": "AI Agent Developer",
         "description_status": "ok", "description": "Python LLM agent integration",
         "components": {"role": 35, "stack": 20}, "score": 58,
         "score_decision": "pass", "filter_status": "pass", "resume": "AI"},
    ]
    store.save_campaign_items(run, "account", "ai", rows, status="ok", snapshot_path="fixture.json")
    import json

    p = tmp_path / "fixture.json"
    p.write_text(json.dumps({"items": rows}), encoding="utf-8")
    report = {
        "snapshot": str(p), "status": "ok",
        "summary": {"items": 2, "first_seen": 2, "new_since_previous": 0},
    }
    bundle = compile_bundle(store, "account", {"ai": report})
    assert bundle["fit_candidates"] == 1
    assert bundle["needs_enrichment_candidates"] == 1
    assert bundle["items"][0]["id"] == "confirmed"
    pending = next(item for item in bundle["items"] if item["id"] == "new-technical")
    assert pending["auto_apply_eligible"] is False
    assert pending["review_lane"] == "needs_enrichment"


def test_campaign_reuses_full_cached_description_instead_of_second_fetch(tmp_path, monkeypatch):
    cfg = AppConfig.discover(root=tmp_path, data_dir=str(tmp_path / "private/data"))
    search = effective_search({"queries": ["AI Engineer"], "areas": [113], "request_budget": 2,
                               "max_pages": 1, "details_limit": 1})
    profile = {"search": search, "role_terms": ["ai"], "title_role_terms": ["ai"]}
    calls = {"details": 0}

    def discover(_adapter, _filters):
        return ([{"id": "one", "name": "AI Engineer", "url": "https://hh.ru/vacancy/one",
                  "description": "AI", "description_status": "provisional",
                  "published": datetime.now(UTC).isoformat()}],
                [ScanSegment("AI Engineer", 113, 1, "ok", 1, requests=1)])

    def enrich(items, limit, **kwargs):
        calls["details"] += len(items)
        return ([{**item, "description": "Full AI Engineer Python LLM RAG FastAPI",
                  "description_status": "ok"} for item in items], [])

    monkeypatch.setattr("applypilot.sources.HhSearchAdapter.discover", discover)
    monkeypatch.setattr("applypilot.campaigns.enrich_items", enrich)
    first = run_campaign(cfg, "ai", profile, search, request_budget=2, details_limit=1, days=3)
    assert calls["details"] == 1
    assert first["cached_descriptions"] == 0
    second = run_campaign(cfg, "ai", profile, search, request_budget=2, details_limit=1, days=3)
    assert calls["details"] == 1
    assert second["cached_descriptions"] == 1
    assert second["detail_fetch_attempts"] == 0
    assert second["uncovered_queries"] == []
    assert Store(cfg.db_path).list_campaign_vacancies("default", "ai", "all", 1)[0]["description_status"] == "ok"


def test_balanced_scan_moves_second_sort_to_different_queries(tmp_path, monkeypatch):
    cfg = AppConfig.discover(root=tmp_path, data_dir=str(tmp_path / "private/data"))
    queries = ["AI Agent", "LLM Engineer", "RAG Engineer", "Вайбкодер"]
    search = effective_search({
        "queries": queries, "areas": [113],
        "request_budget": 4, "max_pages": 1, "details_limit": 0,
    })
    profile = {"search": search, "role_terms": ["ai", "llm", "rag", "вайбкод"],
               "title_role_terms": ["ai", "llm", "rag", "вайбкод"]}
    captured = []

    def discover(_adapter, filters):
        captured.append((filters["order_by"], list(filters["queries"])))
        return [], [ScanSegment(filters["queries"][0], 113, 1, "empty", 0, requests=1)]

    monkeypatch.setattr("applypilot.sources.HhSearchAdapter.discover", discover)
    result = run_campaign(cfg, "ai", profile, search, days=3, request_budget=4,
                          details_limit=0, sort_mode="balanced")
    assert captured[0] == ("publication_time", queries)
    assert captured[1] == ("relevance", ["RAG Engineer", "Вайбкодер", "AI Agent", "LLM Engineer"])
    assert result["query_count"] == 4


def test_bundle_new_count_uses_sqlite_not_stale_legacy_is_new_flag(tmp_path):
    import json

    store = Store(tmp_path / "state.sqlite3")
    item = {
        "id": "known", "name": "AI Engineer", "company": "Example",
        "description": "Python LLM RAG", "description_status": "ok",
        "score": 80, "score_decision": "pass", "filter_status": "pass",
        "is_new": True, "published": datetime.now(UTC).isoformat(),
    }
    store.start_campaign("seed", "account", "ai", {})
    store.save_campaign_items("seed", "account", "ai", [item],
                              status="ok", snapshot_path="seed.json")
    store.start_campaign("later", "account", "ai", {})
    store.save_campaign_items("later", "account", "ai", [item],
                              status="ok", snapshot_path="later.json")
    path = tmp_path / "later.json"
    path.write_text(json.dumps({"campaign_id": "later", "items": [item]}), encoding="utf-8")
    report = compile_bundle(store, "account", {
        "ai": {"campaign_id": "later", "snapshot": str(path),
               "status": "ok",
               "summary": {"items": 1, "first_seen": 0, "new_since_previous": 0}}
    })
    assert report["first_seen_sqlite_unique"] == 0
    assert report["new_since_previous_unique"] == 0
    assert report["published_last_24h_unique"] == 1


def test_ai_devops_plus_development_is_not_false_negative():
    cfg = ROLE_PRESETS["ai-agents-llmops"]
    profile = {**cfg, "search": cfg, "rescore": True}
    engineer = score_vacancy(
        {"id": "one", "name": "AI инженер (devops и разработка)",
         "description": ("Проектировать и разрабатывать AI-агентов, Claude Code skills, "
                         "MCP, Python, LLM-шлюз, FastAPI, интеграции, Docker.")},
        profile,
    )
    assert engineer.decision == "pass", engineer.hard_killers
    pure_ops = score_vacancy(
        {"id": "two", "name": "DevOps / LLMOps Engineer (AI-платформа)",
         "description": "Администрировать Kubernetes кластеры и CI/CD"},
        profile,
    )
    # A mixed title without LLM duties is reviewable, not an automatic apply
    # or a definitive rejection based on the word DevOps alone.
    assert pure_ops.decision == "unknown"
