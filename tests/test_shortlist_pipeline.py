"""Regressions for the agent-review -> exact-batch -> dispatcher boundary."""
from __future__ import annotations

import json
from pathlib import Path

from applypilot import cli
from applypilot.config import AppConfig
from applypilot.shortlist import build_review_shortlist

FIXTURES = Path(__file__).parent / "fixtures" / "public"


def _row(vacancy_id: str, **overrides):
    row = {
        "source": "hh.ru", "track": "ai", "id": vacancy_id,
        "name": "Python LLM Developer", "company": "Example",
        "score": 82, "description_status": "ok",
        "description": "Python backend, LLM agents, FastAPI, PostgreSQL, remote.",
        "decision": "REVIEW", "review_state": "UNREVIEWED",
        "global_application_state": "NOT_STARTED",
        "application_state": "NOT_STARTED", "filter_status": "pass",
        "decision_reason": "needs_evidence: grade", "is_remote": True,
    }
    row.update(overrides)
    return row


def test_shortlist_includes_described_review_not_approvals() -> None:
    rows = [
        _row("101"),
        _row("101", track="backend", score=71),  # one vacancy, two tracks
        _row("102", score=90, description_status="provisional"),
        _row("103", score=96, global_application_state="UNKNOWN"),
        _row("104", score=99, review_state="DEFERRED"),
        _row("105", score=92, decision="APPLY", review_state="APPROVED",
             application_state="READY_TO_APPLY"),
        _row("106", score=60, filter_status="reject"),
        _row("107", score=20),
    ]
    result = build_review_shortlist(rows, limit=100, min_score=45)
    assert result["approved"] is False and result["sendable"] is False
    assert [(r["id"], r["track"]) for r in result["items"]] == [("101", "ai")]
    assert result["counts"]["already_ready"] == 1
    assert [(r["id"], r["stage"]) for r in result["ready_items"]] == [("105", "pre_send_recheck")]
    assert result["counts"]["missing_description"] == 1


def test_dispatch_cli_unrestricted_ids_are_none_but_explicit_ids_stay_exact(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    cfg = AppConfig(
        tmp_path, tmp_path / "data",
        FIXTURES / "profile.toml", FIXTURES / "search.toml",
    )
    monkeypatch.setattr(cli, "_config", lambda _args: cfg)
    monkeypatch.setattr(cli, "_track_config", lambda *_args, **_kwargs: (
        cfg, {"resume": "AI Engineer"},
    ))

    calls = []

    def fake_dispatch(*args, **kwargs):
        calls.append(kwargs["vacancy_ids"])
        return {
            "mode": "dry_run", "sources": ["getmatch.ru"],
            "plans": {}, "planned_count": 0, "blocked_count": 0,
            "candidate_count": 0, "planned": [], "blocked": [],
            "executed": 0, "results": [],
        }

    monkeypatch.setattr("applypilot.dispatch.dispatch_apply", fake_dispatch)

    base = ["sources", "dispatch-apply", "--track", "ai",
            "--source", "getmatch.ru", "--json"]
    assert cli.main(base) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry_run"
    assert cli.main(base + ["--id", "101,202"]) == 0
    capsys.readouterr()
    assert calls == [None, ["101", "202"]]


def test_plan_unfiltered_is_nonempty_but_explicit_empty_is_empty(
    tmp_path: Path, monkeypatch,
) -> None:
    from applypilot.multisource import queue_plan
    from applypilot.storage import Store

    store = Store(tmp_path / "plan.sqlite3")
    ready = _row(
        "789", source="getmatch.ru", decision="APPLY",
        review_state="APPROVED", application_state="READY_TO_APPLY",
    )
    monkeypatch.setattr(
        store, "list_campaign_vacancies", lambda *_args, **_kwargs: [ready],
    )
    normal = queue_plan(
        store, "default", "backend", "getmatch.ru", 100,
        verified_driver=True, vacancy_ids=None,
    )
    empty = queue_plan(
        store, "default", "backend", "getmatch.ru", 100,
        verified_driver=True, vacancy_ids=[],
    )
    assert [row["id"] for row in normal["planned"]] == ["789"]
    assert empty["candidate_count"] == empty["planned_count"] == 0