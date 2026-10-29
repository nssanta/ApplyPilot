"""End-to-end contracts for scan -> review -> apply operator commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from applypilot import cli, multisource
from applypilot.config import AppConfig
from applypilot.decision import decide_row
from applypilot.multisource import queue_plan
from applypilot.storage import AUTO_APPROVAL_REASON, Store


def _config(tmp_path: Path) -> AppConfig:
    config_dir = tmp_path / "private/config"
    config_dir.mkdir(parents=True)
    (config_dir / "profile.toml").write_text(
        '[resumes]\ndefault="AI Engineer"\nai_agent="AI Engineer"\n'
        'account="default"\nreviewed=true\n[limits]\nper_run=100\nper_day=0\n',
        encoding="utf-8",
    )
    (config_dir / "search.toml").write_text(
        'queries=["AI Engineer"]\ndays=3\nmin_score=0\n'
        'auto_apply_title_terms=["ai"]\n'
        '[salary]\nfrom=120000\nmissing="include"\n',
        encoding="utf-8",
    )
    (config_dir / "operating-policy.toml").write_text(
        '[policy]\nlive_apply=true\nauto_approve=true\n'
        'allow_missing_salary=true\nallow_unknown_publication=true\n',
        encoding="utf-8",
    )
    return AppConfig(tmp_path, tmp_path / "private/data",
                     config_dir / "profile.toml", config_dir / "search.toml")


def _vacancy(vacancy_id: str, **overrides):
    item = {
        "id": vacancy_id,
        "name": "AI/LLM Engineer",
        "company": "Example",
        "url": f"https://hh.ru/vacancy/{vacancy_id}",
        "description": "Build Python LLM agents, RAG services and FastAPI APIs.",
        "description_status": "ok",
        "score": 80,
        "score_decision": "pass",
        "filter_status": "pass",
        "reasons": ["keyword:python", "keyword:llm"],
        "problems": [],
        "filter_reasons": [],
        "is_remote": True,
        "published": datetime.now(UTC).isoformat(),
        "resume": "AI Engineer",
        "salary": {"from": 180000, "to": 220000, "currency": "RUR"},
    }
    item.update(overrides)
    return item


def _seed(config: AppConfig, items: list[dict], source: str = "hh.ru",
          campaign: str = "scan-campaign", track: str = "ai") -> Store:
    store = Store(config.db_path)
    store.start_campaign(campaign, "default", track, {"source": source}, source=source)
    store.save_campaign_items(
        campaign, "default", track, items, status="ok", snapshot_path="snapshot.json",
        source=source,
    )
    return store


def test_owner_approval_overrides_stale_relevance_but_not_profile_contradiction() -> None:
    approved = {
        **_vacancy("approved"),
        "review_state": "APPROVED",
        "review_reason": "Operator approved after reading the full card",
        "application_state": "NOT_STARTED",
        "global_application_state": "READY_TO_APPLY",
        "score_decision": "reject",
        "filter_status": "reject",
        "filter_reasons": [
            "publication:outside_requested_window",
            "salary:missing_allowed",
            "role:title_not_target",
        ],
        "problems": ["role:title_not_target"],
    }
    decision = decide_row(approved, search={"days": 3})
    assert decision.action == "APPLY"
    assert decision.reason_code == "owner_approved"

    hard = {
        **approved,
        "id": "hard",
        "filter_reasons": ["geo:configured_exclusion"],
    }
    decision = decide_row(hard, search={"days": 3})
    assert decision.action == "REJECT"
    assert decision.reason_code == "owner_approval_hard_blocker"


def test_saved_owner_approval_is_immediately_dispatchable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _seed(config, [_vacancy(
        "101", score_decision="reject", filter_status="reject",
        problems=["role:title_not_target"],
        filter_reasons=["publication:outside_requested_window"],
    )])
    store.set_review_state(
        "default", "hh.ru", "101", "APPROVED",
        reason="Operator checked the duties and approved", track="ai",
        allow_relevance_override=True,
    )
    row = store.list_campaign_vacancies("default", "ai", "all", 10)[0]
    assert row["review_state"] == "APPROVED"
    assert row["decision"] == "APPLY"
    assert row["application_state"] == "READY_TO_APPLY"
    assert [item["id"] for item in store.approved_campaign_items(
        "default", "ai", 10,
    )] == ["101"]
    assert queue_plan(store, "default", "ai", "hh.ru")["planned_count"] == 1


def test_scan_manifest_limits_review_and_apply_package(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _seed(config, [_vacancy("101"), _vacancy("202")])
    store.create_execution("scan-1", {"mode": "scan", "track": "ai"})
    rows = store.list_campaign_vacancies("default", "ai", "all", 10)
    store.observe_execution("scan-1", "default", "hh.ru", "ai",
                            [row for row in rows if row["id"] == "101"])
    store.set_review_state(
        "default", "hh.ru", "101", "APPROVED", reason="operator", track="ai",
    )
    store.set_review_state(
        "default", "hh.ru", "202", "APPROVED", reason="operator", track="ai",
    )
    assert [row["id"] for row in store.list_campaign_vacancies(
        "default", "ai", "all", 10, scan_id="scan-1",
    )] == ["101"]
    assert [row["id"] for row in store.approved_campaign_items(
        "default", "ai", 10, scan_id="scan-1",
    )] == ["101"]
    plan = queue_plan(store, "default", "ai", "hh.ru", scan_id="scan-1")
    assert [row["id"] for row in plan["planned"]] == ["101"]


def test_review_and_apply_cli_use_the_same_scan_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    store = _seed(config, [_vacancy("101"), _vacancy(
        "202", name="Java AI Engineer",
        filter_reasons=["preset_noise:java"],
        problems=["preset_noise:java"],
    )])
    store.create_execution("scan-1", {"mode": "scan", "track": "ai"})
    rows = store.list_campaign_vacancies("default", "ai", "all", 10)
    store.observe_execution("scan-1", "default", "hh.ru", "ai", rows)
    monkeypatch.setattr(cli, "_config", lambda _args: config)
    monkeypatch.setattr(cli, "_track_config", lambda *args, **kwargs: (config, {
        "resume": "AI Engineer",
    }))

    assert cli.main([
        "review", "--track", "ai", "--scan", "scan-1", "--limit", "10", "--json",
    ]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["count"] == 2
    assert {item["id"] for item in listing["items"]} == {"101", "202"}

    assert cli.main([
        "review", "--track", "ai", "--scan", "scan-1", "--limit", "10",
        "--approve", "101", "--approve", "202",
        "--reason", "Checked duties, remote format and requirements", "--json",
    ]) == 2
    decision = json.loads(capsys.readouterr().out)
    assert decision["actions"]["updated"] == {"hh.ru:101": "APPROVED"}
    assert "hh.ru:202" in decision["actions"]["blocked"]
    assert "hard requirement" in decision["actions"]["blocked"]["hh.ru:202"]

    assert cli.main([
        "apply", "--track", "ai", "--scan", "scan-1", "--dry-run", "--json",
    ]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["package_count"] == 1
    assert plan["sources"]["hh.ru"]["package"][0]["id"] == "101"
    assert plan["sources"]["hh.ru"]["planned"] == 1


def test_scan_command_records_scan_id_and_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)

    def fake_scan(config, track, profile, search, **kwargs):
        store = Store(config.db_path)
        store.start_campaign("inner", "default", track, {}, source="hh.ru")
        store.save_campaign_items(
            "inner", "default", track, [_vacancy("101")], status="ok",
            snapshot_path="snapshot.json", source="hh.ru",
        )
        return {
            "sources": [{
                "source": "hh.ru", "status": "ok", "candidate_ids": ["101"],
                "items": 1, "passes": 1, "query_count": 1,
                "covered_query_count": 1, "coverage_complete": True,
                "uncovered_queries": [], "errors": [], "campaign_ids": ["inner"],
            }],
            "partial_sources": [], "failed_sources": [],
        }

    monkeypatch.setattr(multisource, "scan_sources", fake_scan)
    monkeypatch.setattr(cli, "_config", lambda _args: config)
    monkeypatch.setattr(cli, "_track_config", lambda *args, **kwargs: (config, {
        "resume": "AI Engineer",
    }))
    assert cli.main([
        "scan", "--track", "ai", "--sources", "hh.ru", "--days", "3",
        "--scan-id", "scan-live-1", "--json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scan_id"] == "scan-live-1"
    assert result["status"] == "scan_complete"
    assert result["sources"]["hh.ru"]["coverage_complete"] is True
    execution = Store(config.db_path).execution("scan-live-1")
    assert execution is not None and execution["status"] == "scan_complete"
    assert cli.main([
        "scan", "--track", "ai", "--sources", "hh.ru", "--days", "3",
        "--scan-id", "scan-live-1", "--resume", "--json",
    ]) == 2
    assert "already complete" in capsys.readouterr().err


def test_status_reports_scan_and_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path)
    store = _seed(config, [_vacancy("101")])
    store.create_execution("scan-1", {"mode": "scan", "track": "ai"})
    store.observe_execution("scan-1", "default", "hh.ru", "ai",
                            store.list_campaign_vacancies("default", "ai", "all", 10))
    store.update_execution("scan-1", "scan_complete", "", {"counts": {"hh.ru": 1}})
    monkeypatch.setattr(cli, "_config", lambda _args: config)
    monkeypatch.setattr(cli, "_track_config", lambda *args, **kwargs: (config, {}))
    assert cli.main([
        "status", "--track", "ai", "--scan", "scan-1", "--source", "hh.ru", "--json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scan_status"]["status"] == "scan_complete"
    assert result["sources"]["hh.ru"]["total"] == 1


def test_qa_title_is_rejected_even_when_python_and_ai_are_present() -> None:
    from applypilot.config import effective_search
    from applypilot.scoring import score_vacancy

    search = effective_search({}, "ai-llm")
    search["exclude_titles"] = [*search.get("exclude_titles", []), "qa"]
    candidate = score_vacancy({
        **_vacancy("qa"),
        "name": "Junior Software Development Engineer in Test (Quality & AI focus)",
    }, {"search": search, "rescore": True})
    assert candidate.decision == "reject"
    assert any(reason.startswith("preset_noise:qa") for reason in candidate.hard_killers)


def test_auto_approval_reason_is_not_treated_as_owner_override() -> None:
    row = {
        **_vacancy("auto"),
        "review_state": "APPROVED",
        "review_reason": AUTO_APPROVAL_REASON,
        "application_state": "NOT_STARTED",
        "global_application_state": "READY_TO_APPLY",
        "score_decision": "reject",
        "filter_status": "reject",
    }
    decision = decide_row(row, search={"days": 3})
    assert decision.action != "APPLY"
