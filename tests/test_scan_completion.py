from __future__ import annotations

from pathlib import Path

import pytest

from applypilot import multisource
from applypilot.config import AppConfig
from applypilot.providers import default_registry
from applypilot.storage import Store


def _config(root: Path) -> AppConfig:
    return AppConfig(root, root / "private" / "data", root / "profile.toml", root / "search.toml")


def test_until_complete_visits_every_query_group_and_drains_getmatch(monkeypatch, tmp_path):
    calls: list[tuple[str, str]] = []
    pass_counts: dict[str, int] = {}

    def fake_scan(_config, _track, _profile, search, source, **_kwargs):
        group = str(search["group_name"])
        calls.append((source, group))
        pass_counts[group] = pass_counts.get(group, 0) + 1
        turn = pass_counts[group]
        return {
            "source": source,
            "status": "partial" if turn == 1 else "ok",
            "items": 1,
            "candidate_ids": [f"{group}-{turn}"],
            "summary": {"items": 1},
            "errors": ["request budget reached"] if turn == 1 else [],
            "account": "getmatch-primary",
        }

    monkeypatch.setattr(multisource, "scan_source", fake_scan)
    search = {
        "queries": ["default"], "days": 30,
        "groups": [
            {"name": "agents", "queries": ["AI Agent Engineer"]},
            {"name": "mcp", "queries": ["MCP developer"]},
        ],
    }
    result = multisource.scan_sources(
        _config(tmp_path), "ai_core", {"account": "default"}, search,
        sources=["getmatch.ru"], until_complete=True, max_passes=4,
        registry=default_registry(),
    )

    row = result["sources"][0]
    assert calls == [
        ("getmatch.ru", "agents"), ("getmatch.ru", "agents"),
        ("getmatch.ru", "mcp"), ("getmatch.ru", "mcp"),
    ]
    assert row["status"] == "ok"
    assert row["candidate_ids"] == ["agents-1", "agents-2", "mcp-1", "mcp-2"]
    assert row["items"] == 4
    assert row["passes"] == 4
    assert result["partial_sources"] == []


def test_habr_page_cursor_survives_interruption_and_resume(monkeypatch, tmp_path):
    starts: list[int] = []

    def fake_scan(_config, _track, _profile, _search, source, **kwargs):
        start = int(kwargs["start_page"])
        starts.append(start)
        if start == 1:
            return {
                "source": source, "status": "truncated", "items": 1,
                "candidate_ids": ["habr-job-1"], "summary": {"items": 1},
                "errors": ["vacancies: maximum page window reached"],
                "campaign_id": "first",
            }
        return {
            "source": source, "status": "empty_verified", "items": 0,
            "candidate_ids": [], "summary": {"items": 0}, "errors": [],
            "campaign_id": "second",
        }

    monkeypatch.setattr(multisource, "scan_source", fake_scan)
    config = _config(tmp_path)
    profile = {"account": "habr-primary"}
    search = {"queries": ["AI agent"], "days": 30}
    registry = default_registry()

    first = multisource.scan_sources(
        config, "ai_core", profile, search, sources=["career.habr.com"],
        until_complete=True, resume=True, pages=None, max_passes=1, registry=registry,
    )
    assert first["sources"][0]["status"] == "truncated"
    assert starts == [1]

    second = multisource.scan_sources(
        config, "ai_core", profile, search, sources=["career.habr.com"],
        resume=True, pages=None, max_passes=1, registry=registry,
    )
    assert starts == [1, 2]
    assert second["sources"][0]["status"] == "empty_verified"
    assert second["sources"][0]["candidate_ids"] == []
    assert not list((config.data_dir / "providers" / "career-habr-com").rglob("scan-*.json"))


def test_until_complete_uses_configured_bounded_pass_budgets(monkeypatch, tmp_path):
    observed: dict[str, object] = {}

    def fake_scan(_config, _track, _profile, _search, source, **kwargs):
        observed.update(kwargs)
        return {
            "source": source, "status": "empty_verified", "items": 0,
            "candidate_ids": [], "summary": {"items": 0}, "errors": [],
        }

    monkeypatch.setattr(multisource, "scan_source", fake_scan)
    multisource.scan_sources(
        _config(tmp_path), "ai_core", {}, {"queries": ["AI agent"], "max_pages": 2},
        sources=["career.habr.com"], until_complete=True,
        pages=None, request_budget=None, details_limit=None,
        registry=default_registry(),
    )

    assert observed["pages"] == 2
    assert observed["request_budget"] == 100
    assert observed["details_limit"] == 100


def test_until_complete_respects_configured_budget_for_each_source(monkeypatch, tmp_path):
    observed: dict[str, dict[str, object]] = {}

    def fake_scan(_config, _track, _profile, _search, source, **kwargs):
        observed[source] = kwargs
        return {
            "source": source, "status": "empty_verified", "items": 0,
            "candidate_ids": [], "summary": {"items": 0}, "errors": [],
        }

    monkeypatch.setattr(multisource, "scan_source", fake_scan)
    multisource.scan_sources(
        _config(tmp_path), "ai_core", {}, {
            "queries": ["AI agent"], "request_budget": 140, "details_limit": 140,
        }, sources=["hh.ru", "career.habr.com"], until_complete=True,
        pages=None, request_budget=None, details_limit=None,
        registry=default_registry(),
    )

    assert observed["hh.ru"]["request_budget"] == 140
    assert observed["hh.ru"]["details_limit"] == 140
    assert observed["hh.ru"]["pages"] == 1
    assert observed["career.habr.com"]["request_budget"] == 140
    assert observed["career.habr.com"]["details_limit"] == 140
    assert observed["career.habr.com"]["pages"] == 1


def test_until_complete_resumes_hh_queries_at_the_next_page(monkeypatch, tmp_path):
    from applypilot.sources import HhSearchAdapter

    calls: list[dict[str, object]] = []

    def fake_scan(_config, _track, _profile, search, source, **kwargs):
        queries = list(search["queries"])
        cursor = dict(kwargs.get("hh_start_pages") or {})
        calls.append({"queries": queries, "cursor": cursor})
        if len(calls) == 1:
            return {
                "source": source, "status": "truncated", "items": 2,
                "candidate_ids": ["job-q1-page0", "job-q2-page0"],
                "summary": {"items": 2}, "errors": [],
                "uncovered_queries": [],
                "segments_detail": [
                    {"query": query, "area": 113, "pages": 1, "status": "truncated",
                     "requests": 2, "error": "", "order_by": "publication_time"}
                    for query in queries
                ],
            }
        return {
            "source": source, "status": "ok", "items": 2,
            "candidate_ids": ["job-q1-page1", "job-q2-page1"],
            "summary": {"items": 2}, "errors": [], "uncovered_queries": [],
            "segments_detail": [
                {"query": query, "area": 113, "pages": 1, "status": "empty",
                 "requests": 2, "error": "", "order_by": "publication_time"}
                for query in queries
            ],
        }

    monkeypatch.setattr(multisource, "scan_source", fake_scan)
    result = multisource.scan_sources(
        _config(tmp_path), "ai_core", {}, {"queries": ["q1", "q2"]},
        sources=["hh.ru"], until_complete=True, max_passes=4,
        registry=default_registry(),
    )

    row = result["sources"][0]
    assert calls[0] == {
        "queries": ["q1", "q2"],
        "cursor": {
            HhSearchAdapter.page_key("q1", 113, "publication_time"): 0,
            HhSearchAdapter.page_key("q2", 113, "publication_time"): 0,
        },
    }
    assert calls[1]["queries"] == ["q1", "q2"]
    assert calls[1]["cursor"] == {
        HhSearchAdapter.page_key("q1", 113, "publication_time"): 1,
        HhSearchAdapter.page_key("q2", 113, "publication_time"): 1,
    }
    assert row["passes"] == 2
    assert row["candidate_ids"] == [
        "job-q1-page0", "job-q2-page0", "job-q1-page1", "job-q2-page1",
    ]
    assert row["query_count"] == 2
    assert row["uncovered_queries"] == []


def test_hh_budget_exhaustion_keeps_unstarted_queries_in_cursor(monkeypatch, tmp_path):
    from applypilot.sources import HhSearchAdapter

    requested: list[tuple[str, int]] = []

    def fake_scan(_config, _track, _profile, search, source, **kwargs):
        cursor = dict(kwargs.get("hh_start_pages") or {})
        remaining = 1
        segments = []
        candidate_ids = []
        for query in search["queries"]:
            key = HhSearchAdapter.page_key(query, 113, "publication_time")
            page = cursor.get(key, 0)
            if page is None:
                segments.append({
                    "query": query, "area": 113, "pages": 0,
                    "status": "empty", "requests": 0, "error": "",
                    "order_by": "publication_time",
                })
            elif remaining:
                remaining -= 1
                requested.append((query, page))
                done = page == 1
                segments.append({
                    "query": query, "area": 113, "pages": 1,
                    "status": "empty" if done else "truncated", "requests": 1,
                    "error": "", "order_by": "publication_time",
                })
                if page == 0:
                    candidate_ids.append(f"{query}-page0")
            else:
                segments.append({
                    "query": query, "area": 113, "pages": 0,
                    "status": "truncated", "requests": 0,
                    "error": "search request budget reached",
                    "order_by": "publication_time",
                })
        status = "truncated" if any(row["status"] == "truncated" for row in segments) else "ok"
        return {
            "source": source, "status": status, "items": len(candidate_ids),
            "candidate_ids": candidate_ids, "summary": {"items": len(candidate_ids)},
            "errors": [], "uncovered_queries": [], "segments_detail": segments,
        }

    monkeypatch.setattr(multisource, "scan_source", fake_scan)
    result = multisource.scan_sources(
        _config(tmp_path), "ai_core", {}, {"queries": ["q1", "q2", "q3"]},
        sources=["hh.ru"], until_complete=True, max_passes=8,
        request_budget=1, registry=default_registry(),
    )

    assert requested == [("q1", 0), ("q1", 1), ("q2", 0), ("q2", 1), ("q3", 0), ("q3", 1)]
    row = result["sources"][0]
    assert row["status"] == "ok"
    assert row["passes"] == 6
    assert row["candidate_ids"] == ["q1-page0", "q2-page0", "q3-page0"]


def test_hh_completed_cursor_aggregates_coverage_across_passes(monkeypatch, tmp_path):
    from applypilot.sources import HhSearchAdapter

    calls = 0

    def fake_scan(_config, _track, _profile, _search, source, **kwargs):
        nonlocal calls
        calls += 1
        cursor = dict(kwargs["hh_start_pages"])
        if calls == 1:
            q1_key = HhSearchAdapter.page_key("q1", 113, "publication_time")
            q2_key = HhSearchAdapter.page_key("q2", 113, "publication_time")
            assert cursor == {q1_key: 0, q2_key: 0}
            return {
                "source": source, "status": "truncated", "items": 1,
                "candidate_ids": ["q1-job"], "summary": {"items": 1},
                "errors": ["q2 area 113: search request budget reached"],
                "uncovered_queries": ["q2"],
                "segments_detail": [
                    {"query": "q1", "area": 113, "pages": 1, "status": "empty",
                     "requests": 1, "error": "", "order_by": "publication_time"},
                    {"query": "q2", "area": 113, "pages": 0, "status": "truncated",
                     "requests": 0, "error": "search request budget reached",
                     "order_by": "publication_time"},
                ],
            }
        assert cursor[HhSearchAdapter.page_key("q1", 113, "publication_time")] is None
        assert cursor[HhSearchAdapter.page_key("q2", 113, "publication_time")] == 0
        return {
            "source": source, "status": "truncated", "items": 0,
            "candidate_ids": [], "summary": {"items": 0}, "errors": [],
            "uncovered_queries": ["q1"],
            "segments_detail": [
                {"query": "q2", "area": 113, "pages": 1, "status": "empty",
                 "requests": 1, "error": "", "order_by": "publication_time"},
            ],
        }

    monkeypatch.setattr(multisource, "scan_source", fake_scan)
    result = multisource.scan_sources(
        _config(tmp_path), "ai_core", {}, {"queries": ["q1", "q2"]},
        sources=["hh.ru"], until_complete=True, max_passes=4,
        request_budget=1, registry=default_registry(),
    )

    row = result["sources"][0]
    assert calls == 2
    assert row["status"] == "ok"
    assert row["coverage_complete"] is True
    assert row["covered_query_count"] == row["query_count"] == 2
    assert row["uncovered_queries"] == []
    assert len(row["segments_detail"]) == 3
    assert row["errors"] == []
    assert result["partial_sources"] == []


def test_autoapproval_requires_complete_fit_and_no_open_checks(tmp_path):
    store = Store(tmp_path / "applypilot.sqlite3")
    account, track, source = "habr-primary", "ai_core", "career.habr.com"
    campaign = "scan-test"
    store.start_campaign(campaign, account, track, {"queries": ["AI agents"]}, source=source)
    base = {
        "source": source, "name": "AI Agent Engineer", "company": "Example",
        "url": "https://career.habr.com/vacancies/123", "application_channel": f"{source}:123",
        "application_channel_type": "native_form", "score": 95, "score_decision": "pass",
        "filter_status": "pass", "review_lane": "FIT", "description_status": "ok",
        "description": "Build AI agents and RAG services", "problems": [], "filter_reasons": [],
    }
    items = [
        {**base, "id": "123"},
        {**base, "id": "124", "problems": ["education:requirement_check"]},
        {**base, "id": "125", "filter_status": "unknown"},
    ]
    store.save_campaign_items(
        campaign, account, track, items, status="ok", snapshot_path="", source=source,
    )

    result = multisource.approve_scanned_fits(
        store, account, track, source, ["123", "124", "125"],
    )

    assert result["approved"] == 1
    assert result["blocked"] == {
        "124": "education:requirement_check",
        "125": "role or search conditions are not fully verified",
    }
    approved = store.list_campaign_vacancies(account, track, "approved", 10, source=source)
    assert [row["id"] for row in approved] == ["123"]


def test_autoapproval_preserves_manual_review_decisions(tmp_path):
    store = Store(tmp_path / "applypilot.sqlite3")
    account, track, source = "hh-primary", "ai_core", "hh.ru"
    store.start_campaign("review-veto", account, track, {"queries": ["AI agents"]}, source=source)
    base = {
        "source": source, "name": "AI Agent Engineer", "company": "Example",
        "url": "https://hh.ru/vacancy/123", "score": 90,
        "score_decision": "pass", "filter_status": "pass",
        "description_status": "ok", "description": "Production LLM agents and RAG",
        "review_lane": "FIT", "problems": [], "filter_reasons": [],
        "auto_apply_eligible": True,
    }
    ids = ["fresh", "skip", "deferred", "maybe", "manual-approved"]
    store.save_campaign_items(
        "review-veto", account, track,
        [{**base, "id": vacancy_id} for vacancy_id in ids],
        status="ok", snapshot_path="", source=source,
    )
    for vacancy_id, state in [
        ("skip", "SKIP"), ("deferred", "DEFERRED"),
        ("maybe", "MAYBE"), ("manual-approved", "APPROVED"),
    ]:
        store.set_review_state_batch(
            account, source, [vacancy_id], state,
            reason=f"owner decided {state}", track=track,
        )

    result = multisource.approve_scanned_fits(store, account, track, source, ids)
    assert result["approved"] == 1
    assert result["blocked"] == {
        vacancy_id: f"existing review state is {state}"
        for vacancy_id, state in [
            ("skip", "SKIP"), ("deferred", "DEFERRED"),
            ("maybe", "MAYBE"), ("manual-approved", "APPROVED"),
        ]
    }
    rows = {
        row["id"]: row
        for row in store.list_campaign_vacancies(account, track, "all", 10, source=source)
    }
    assert rows["fresh"]["review_state"] == "APPROVED"
    for vacancy_id in ids[1:]:
        assert rows[vacancy_id]["review_reason"].startswith("owner decided ")

    again = multisource.approve_scanned_fits(store, account, track, source, ids)
    assert again["approved"] == 0
    assert again["blocked"]["fresh"] == "existing review state is APPROVED"


def test_autoapproval_allows_only_configured_soft_unknowns(tmp_path):
    store = Store(tmp_path / "applypilot.sqlite3")
    account, track, source = "getmatch-primary", "ai_core", "getmatch.ru"
    store.start_campaign("soft-unknowns", account, track, {"queries": ["LLM Engineer"]}, source=source)
    base = {
        "source": source, "name": "LLM Engineer", "company": "Example",
        "url": "https://getmatch.ru/vacancies/123", "application_channel": f"{source}:123",
        "application_channel_type": "native_form", "score": 90, "score_decision": "pass",
        "review_lane": "GOOD_UNKNOWN", "description_status": "ok",
        "description": "Build remote LLM agents", "auto_apply_eligible": True,
        "application_state": "READY_TO_APPLY",
    }
    items = [
        {**base, "id": "salary-missing", "filter_status": "unknown",
         "filter_reasons": ["remote:verified", "salary:missing_allowed",
                            "salary:unpublished_not_verified"],
         "problems": ["salary:unpublished"]},
        {**base, "id": "date-unknown", "filter_status": "unknown",
         "filter_reasons": ["remote:verified", "salary:matching",
                            "publication:time_not_verified"], "problems": []},
        {**base, "id": "remote-unknown", "filter_status": "unknown",
         "filter_reasons": ["remote:not_verified", "publication:time_not_verified"],
         "problems": ["geo:remote_not_verified"]},
        {**base, "id": "currency-unknown", "filter_status": "unknown",
         "filter_reasons": ["remote:verified", "salary:currency_mismatch_check"],
         "problems": ["salary:currency_mismatch"]},
    ]
    store.save_campaign_items(
        "soft-unknowns", account, track, items, status="ok", snapshot_path="", source=source,
    )

    result = multisource.approve_scanned_fits(
        store, account, track, source, [item["id"] for item in items],
        search={"days": 30, "salary": {"from": 120000, "missing": "include"}},
    )

    assert result["approved"] == 2
    assert result["blocked"] == {
        "remote-unknown": "role or search conditions are not fully verified",
        "currency-unknown": "role or search conditions are not fully verified",
    }


def test_live_catalogue_scan_reuses_and_closes_one_http_session(monkeypatch, tmp_path):
    class Session:
        closed = False

        def close(self):
            self.closed = True

    session = Session()
    monkeypatch.setattr(multisource.requests, "Session", lambda: session)

    class Provider:
        capabilities = multisource.SourceCapabilities()

        def discover(self, filters):
            assert filters["session"] is session
            return [], []

        def normalize(self, item):
            return item

    class Registry:
        def get(self, _source):
            return Provider()

    result = multisource.scan_source(
        _config(tmp_path), "ai_core", {"account": "habr-primary", "resume": ""},
        {"queries": ["AI agent"], "days": 30}, "career.habr.com",
        registry=Registry(),
    )

    assert result["status"] == "empty_verified"
    assert session.closed is True


def test_interrupted_catalogue_scan_closes_session_and_marks_campaign_failed(monkeypatch, tmp_path):
    class Session:
        closed = False

        def close(self):
            self.closed = True

    session = Session()
    monkeypatch.setattr(multisource.requests, "Session", lambda: session)

    class Provider:
        capabilities = multisource.SourceCapabilities()

        def discover(self, _filters):
            raise KeyboardInterrupt

    class Registry:
        def get(self, _source):
            return Provider()

    config = _config(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        multisource.scan_source(
            config, "ai_core", {"account": "habr-primary", "resume": ""},
            {"queries": ["AI agent"], "days": 30}, "career.habr.com",
            registry=Registry(),
        )

    assert session.closed is True
    [run] = Store(config.db_path).campaign_history("habr-primary", "ai_core", 1)
    assert run["status"] == "failed"
    assert "resume is safe" in run["error"]


def test_unified_campaign_dry_run_routes_selected_sources(monkeypatch, tmp_path, capsys):
    from applypilot import cli

    config = _config(tmp_path)
    monkeypatch.setattr(cli, "_config", lambda _args: config)
    monkeypatch.setattr(cli, "_track_config", lambda *_args, **_kwargs: (
        config, {"resume": "AI Agent Engineer"},
    ))
    seen: dict[str, object] = {}

    def fake_scan(_config, track, _profile, search, *, sources, **kwargs):
        seen.update({"track": track, "days": search["days"], "sources": list(sources), **kwargs})
        return {
            "partial_sources": [], "failed_sources": [],
            "sources": [{
                "source": "career.habr.com", "status": "ok", "items": 12,
                "groups": 2, "passes": 2, "candidate_ids": ["1"], "account": "habr-primary",
            }],
        }

    monkeypatch.setattr(multisource, "scan_sources", fake_scan)
    monkeypatch.setattr(multisource, "approve_scanned_fits", lambda *_args, **_kwargs: {
        "approved": 1, "blocked": {},
    })

    code = cli.main([
        "run", "campaign", "--track", "ai_core", "--source", "career.habr.com",
        "--days", "30", "--until-complete",
    ])

    assert code == 0
    assert seen["track"] == "ai_core"
    assert seen["days"] == 30
    assert seen["sources"] == ["career.habr.com"]
    assert seen["until_complete"] is True
    output = capsys.readouterr().out
    assert "SCAN career.habr.com: ok · unique=12" in output
    assert "FIT approved=1" in output
    assert "private_report:" in output


def test_unified_getmatch_live_opt_in_is_scoped_to_source_dispatch(
    monkeypatch, tmp_path, capsys,
):
    import json
    import os

    from applypilot import cli

    config = _config(tmp_path)
    monkeypatch.setattr(cli, "_config", lambda _args: config)
    monkeypatch.setattr(cli, "_track_config", lambda *_args, **_kwargs: (
        config, {"resume": "AI Agent Engineer"},
    ))
    monkeypatch.setattr(cli, "_profile", lambda *_args: {"reviewed": True, "limits": {}})
    monkeypatch.setattr(multisource, "scan_sources", lambda *_args, **_kwargs: {
        "partial_sources": [], "failed_sources": [], "sources": [],
    })
    monkeypatch.setattr(multisource, "approve_scanned_fits", lambda *_args: {
        "approved": 0, "blocked": {},
    })
    real_main = cli.main
    nested_calls: list[list[str]] = []

    def nested_main(argv=None):
        values = list(argv or [])
        if "dispatch-apply" in values:
            nested_calls.append(values)
            assert os.environ.get("APPLYPILOT_TELEGRAM_LIVE") == "1"
            print(json.dumps({"summary": {"success": 0}, "send_attempts": 0}))
            return 0
        return real_main(argv)

    monkeypatch.setattr(cli, "main", nested_main)
    monkeypatch.setenv("APPLYPILOT_TELEGRAM_LIVE", "0")

    code = cli.main([
        "run", "campaign", "--track", "ai_core", "--source", "getmatch.ru",
        "--apply", "--allow-live",
    ])

    assert code == 0
    assert len(nested_calls) == 1
    assert os.environ["APPLYPILOT_TELEGRAM_LIVE"] == "0"
    output = capsys.readouterr().out
    assert "APPLY getmatch.ru: sent=0 confirmed=0" in output


def test_campaign_apply_summary_reads_legacy_hh_run_counts():
    from applypilot.cli import _campaign_apply_summary

    assert _campaign_apply_summary({
        "exit_code": 0,
        "summary": {"counts": {
            "success": 4, "needs_manual": 1, "unknown": 0,
            "failed_before_submit": 0,
        }},
    }) == {
        "sent": 4, "confirmed": 4, "needs_manual": 1,
        "unknown": 0, "failures": 0, "status": "completed",
    }
