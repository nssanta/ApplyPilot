from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from applypilot.campaigns import _published_status, run_campaign, write_approved_input
from applypilot.config import AppConfig, effective_search
from applypilot.parser import ScanSegment
from applypilot.storage import AUTO_APPROVAL_REASON, Store


@pytest.fixture(autouse=True)
def fixed_campaign_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 10, 22, 12, tzinfo=UTC)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr("applypilot.campaigns.datetime", Clock)


def test_published_window_uses_exact_utc_cutoff_and_unknown_is_not_pass():
    cutoff = datetime(2026, 10, 21, 12, tzinfo=UTC)

    assert _published_status("2026-10-21T12:00:00+00:00", cutoff) == "within_window"
    assert _published_status("2026-10-21T11:59:59Z", cutoff) == "outside_window"
    assert _published_status("not-a-date", cutoff) == "unknown"
    assert _published_status(None, cutoff) == "unknown"


def test_campaign_scan_persists_queue_and_keeps_review_state_on_partial_retry(tmp_path, monkeypatch):
    config = AppConfig.discover(root=tmp_path, data_dir=str(tmp_path / "private/data"))
    search = effective_search({
        "queries": ["AI Engineer"], "areas": [113], "max_pages": 1,
        "request_budget": 4, "details_limit": 5,
    })
    profile = {"role_terms": ["ai"], "title_role_terms": ["ai"], "search": search}
    observed = [
        {"id": "one", "name": "AI Engineer", "company": "A", "url": "https://hh.ru/vacancy/one",
         "description": "AI and Python", "description_status": "ok", "published": "2026-10-21T00:00:00Z"},
    ]
    segment_status = "ok"

    def discover(_adapter, filters):
        assert filters["request_budget"] > 0
        return [dict(item) for item in observed], [ScanSegment(
            "AI Engineer", 113, 1, segment_status, len(observed), "partial" if segment_status == "failed" else "",
            requests=1,
        )]

    monkeypatch.setattr("applypilot.sources.HhSearchAdapter.discover", discover)
    monkeypatch.setattr("applypilot.campaigns.prioritize_for_enrichment",
                        lambda items, _profile, _limit: [SimpleNamespace(id=str(item["id"])) for item in items])
    monkeypatch.setattr("applypilot.campaigns.enrich_items",
                        lambda items, _limit, **_kwargs: ([dict(item) for item in items], []))

    first = run_campaign(config, "ai", profile, search, days=3, request_budget=2, limit=10,
                         track_resume="AI Agent Engineer")
    assert first["status"] == "ok"
    assert first["summary"]["first_seen"] == 1
    assert len(first["top"]) == 1

    store = Store(config.db_path)
    two = {"id": "two", "name": "AI/LLM Engineer", "company": "B", "url": "https://hh.ru/vacancy/two",
           "description": "LLM and Python", "description_status": "ok", "published": "2026-10-21T01:00:00Z"}
    observed.append(two)
    second = run_campaign(config, "ai", profile, search, days=3, request_budget=2, limit=10,
                          track_resume="AI Agent Engineer")
    assert second["summary"]["known"] == 1
    assert second["summary"]["first_seen"] == 1
    store.set_review_state("default", "hh.ru", "one", "APPROVED", reason="релевантный опыт",
                           notes="проверить зарплату", track="ai")

    observed[:] = [dict(observed[0])]
    third = run_campaign(config, "ai", profile, search, days=3, request_budget=2, limit=10,
                         track_resume="AI Agent Engineer")
    assert third["summary"]["known"] == 1

    observed.append(two)
    fourth = run_campaign(config, "ai", profile, search, days=3, request_budget=2, limit=10,
                          track_resume="AI Agent Engineer")
    assert fourth["summary"]["known"] == 1
    assert fourth["summary"]["new_since_previous"] == 1

    segment_status = "failed"
    observed[:] = [dict(observed[0])]
    fifth = run_campaign(config, "ai", profile, search, days=3, request_budget=1, limit=10,
                         track_resume="AI Agent Engineer")
    queue = store.list_campaign_vacancies("default", "ai", "all", 20)
    by_id = {row["id"]: row for row in queue}
    assert fifth["status"] == "partial"
    assert set(by_id) == {"one", "two"}
    assert by_id["one"]["review_state"] == "APPROVED"
    assert by_id["one"]["review_notes"] == "проверить зарплату"
    assert store.campaign_history("default", "ai", 10)[0]["campaign_id"] == fifth["campaign_id"]


def test_hh_campaign_persists_private_auto_apply_title_gate(tmp_path, monkeypatch):
    config = AppConfig.discover(root=tmp_path, data_dir=str(tmp_path / "private/data"))
    search = effective_search({
        "queries": ["AI agents"], "areas": [113], "max_pages": 1,
        "request_budget": 1, "details_limit": 0,
        "auto_apply_title_terms": ["agent", "llm", "rag"],
    })
    profile = {
        "role_terms": ["llm"], "primary_role_terms": ["llm"],
        "title_role_terms": ["ai"], "required_role_terms": ["engineer"],
        "resumes": {"default": "Example"},
    }
    raw = {
        "id": "generic-ai-role", "name": "AI Engineer",
        "description": "Build production LLM agent applications with RAG.",
        "description_status": "ok", "is_remote": True,
        "published": "2026-10-21T10:00:00Z",
        "salary": {"from": 200000, "currency": "RUR"},
    }
    monkeypatch.setattr("applypilot.sources.HhSearchAdapter.discover", lambda *_args, **_kwargs: (
        [dict(raw)], [ScanSegment("AI agents", 113, 1, "ok", 1, requests=1)],
    ))

    result = run_campaign(
        config, "ai_core", profile, search, days=30, request_budget=1, limit=10,
        track_resume="Example",
    )
    rows = Store(config.db_path).list_campaign_vacancies(
        "default", "ai_core", "all", 10, source="hh.ru",
    )

    assert result["status"] == "ok"
    assert rows[0]["review_lane"] == "FIT"
    assert rows[0]["auto_apply_eligible"] is False


def test_malformed_salary_and_search_signals_stay_manual_review(tmp_path, monkeypatch):
    from applypilot.campaigns import _review_problems

    problems = _review_problems({"salary": {"from": "not-a-number"}, "is_remote": None}, {
        "salary": {"from": 140000, "currency": "RUR"}, "only_remote": True,
    })
    assert "salary:malformed" in problems
    assert "geo:remote_not_verified" in problems


def test_salary_that_satisfies_configured_policy_is_not_an_open_problem():
    from applypilot.campaigns import _review_problems, _salary_confidence

    search = {"salary": {"from": 120000, "currency": "RUR", "policy": "possible"}}
    guaranteed = {"salary": {"currency": "RUR", "from": 130000}}
    overlapping = {"salary": {"currency": "RUR", "from": 100000, "to": 140000}}

    assert "salary:lower_bound_meets_threshold" not in _review_problems(guaranteed, search)
    assert _salary_confidence(guaranteed, search) == "lower_bound_meets_threshold"
    assert "salary:range_overlaps_threshold" not in _review_problems(overlapping, search)
    assert _salary_confidence(overlapping, search) == "range_overlaps_threshold"


def test_stale_automatic_title_approval_is_filtered_from_hh_apply_input(tmp_path, monkeypatch):
    config = AppConfig.discover(root=tmp_path, data_dir=str(tmp_path / "private/data"))
    rows = [
        {"id": "stale-auto", "review_state": "APPROVED", "review_reason": AUTO_APPROVAL_REASON,
         "auto_apply_eligible": False},
        {"id": "manual-override", "review_state": "APPROVED",
         "review_reason": "operator reviewed full description", "auto_apply_eligible": False},
        {"id": "current-auto", "review_state": "APPROVED", "review_reason": AUTO_APPROVAL_REASON,
         "auto_apply_eligible": True},
    ]
    monkeypatch.setattr(Store, "approved_campaign_items", lambda *_args, **_kwargs: rows)

    _path, prepared = write_approved_input(config, "ai_core", "default")

    assert [item["id"] for item in prepared] == ["manual-override", "current-auto"]
    _selected_path, selected = write_approved_input(
        config, "ai_core", "default", vacancy_ids={"manual-override"})
    assert [item["id"] for item in selected] == ["manual-override"]


def test_unpublished_salary_is_maybe_until_owner_approves_it(tmp_path, monkeypatch):
    config = AppConfig.discover(root=tmp_path, data_dir=str(tmp_path / "private/data"))
    search = effective_search({
        "queries": ["AI Engineer"], "max_pages": 1, "request_budget": 1,
        "details_limit": 0, "salary": {"from": 140000, "currency": "RUR", "missing": "include"},
    })
    profile = {"role_terms": ["ai"], "title_role_terms": ["ai"], "search": search}
    monkeypatch.setattr("applypilot.sources.HhSearchAdapter.discover", lambda *_args, **_kwargs: (
        [{"id": "one", "name": "AI Engineer", "company": "A", "url": "https://hh.ru/vacancy/one",
          "salary": None, "description": "AI and LLM Python RAG", "description_status": "ok",
          "published": "2026-10-21T00:00:00Z"}],
        [ScanSegment("AI Engineer", 113, 1, "ok", 1, requests=1)],
    ))
    monkeypatch.setattr("applypilot.campaigns.prioritize_for_enrichment", lambda *_args: [])
    result = run_campaign(config, "ai", profile, search, days=3, request_budget=1, limit=10)
    store = Store(config.db_path)
    item = store.list_campaign_vacancies("default", "ai", "all", 10)[0]

    assert result["summary"]["matched"] == 0
    assert result["summary"]["maybe"] == 1
    assert item["filter_status"] == "unknown"
    assert "salary:unpublished_not_verified" in item["filter_reasons"]
    store.set_review_state("default", "hh.ru", "one", "APPROVED",
                           reason="checked salary manually", track="ai")
    assert len(store.approved_campaign_items("default", "ai")) == 1


def test_campaign_does_not_require_exactly_limit_results(tmp_path, monkeypatch):
    config = AppConfig.discover(root=tmp_path, data_dir=str(tmp_path / "private/data"))
    search = effective_search({"queries": ["AI Engineer"], "max_pages": 1,
                               "request_budget": 1, "details_limit": 0})
    profile = {"role_terms": ["ai"], "title_role_terms": ["ai"], "search": search}
    monkeypatch.setattr("applypilot.sources.HhSearchAdapter.discover", lambda *_args, **_kwargs: (
        [{"id": "one", "name": "AI Engineer", "description": "AI", "published": "2026-10-21T00:00:00Z"}],
        [ScanSegment("AI Engineer", 113, 1, "ok", 1, requests=1)],
    ))
    result = run_campaign(config, "ai", profile, search, days=3, request_budget=1, limit=100)

    assert result["summary"]["items"] == 1
    assert len(result["top"]) <= 100
