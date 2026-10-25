"""Regression tests for source-specific review, channel identity and form readiness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from applypilot import cli
from applypilot.config import AppConfig
from applypilot.multisource import queue_plan
from applypilot.storage import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/public"


@pytest.mark.parametrize("operation", ["set", "batch-set"])
def test_cli_queue_routes_independent_source_accounts(tmp_path, monkeypatch, capsys, operation):
    config = AppConfig(tmp_path, tmp_path / "private/data",
                       FIXTURES / "profile.toml", FIXTURES / "search.toml")
    config_dir = tmp_path / "private/config"
    config_dir.mkdir(parents=True)
    (config_dir / "sources.toml").write_text(
        '[accounts]\n"hh.ru"="default"\n"career.habr.com"="habr-owner"\n',
    )
    store = Store(config.db_path)
    _vacancy(store, "default", "hh.ru", "101", score=80)
    _vacancy(store, "habr-owner", "career.habr.com", "101", score=95)
    _vacancy(store, "other-owner", "career.habr.com", "999", score=100)
    monkeypatch.setattr(cli, "_config", lambda args: config)
    monkeypatch.setattr(cli, "_track_config", lambda *args, **kwargs: (config, {}))
    assert cli.main(["queue", "list", "--track", "ai_core", "--view", "review", "--json"]) == 0
    combined = json.loads(capsys.readouterr().out)
    assert {(x["source"], x["id"]) for x in combined["items"]} == {
        ("hh.ru", "101"), ("career.habr.com", "101"),
    }
    assert cli.main(["queue", "list", "--track", "ai_core", "--source", "career.habr.com",
                     "--view", "review", "--json"]) == 0
    source = json.loads(capsys.readouterr().out)
    assert source["account"] == "habr-owner" and source["count"] == 1
    assert cli.main(["queue", operation, "--track", "ai_core", "--source", "career.habr.com",
                     "--id", "101", "--state", "APPROVED", "--reason", "source review"]) == 0
    capsys.readouterr()
    assert store.list_campaign_vacancies("default", "ai_core", "approved", 10) == []
    assert len(store.list_campaign_vacancies("habr-owner", "ai_core", "approved", 10)) == 1
    assert cli.main(["queue", "apply", "--track", "ai_core", "--source", "career.habr.com",
                     "--dry-run"]) == 0
    assert "blocked=1" in capsys.readouterr().out


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "data" / "applypilot.sqlite3")


def _vacancy(
    store: Store, account: str, source: str, vacancy_id: str, *,
    score: int = 85, state: str = "NOT_STARTED", track: str = "ai_core",
) -> None:
    stamp = "2026-10-22T00:00:00+00:00"
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO vacancies
            (account,source,vacancy_id,title,company,url,application_state,created_at,
             first_seen_at,last_seen_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (account, source, vacancy_id, "LLM Engineer", "Example",
             f"https://{source}/vacancy/{vacancy_id}", state, stamp, stamp, stamp, stamp),
        )
        conn.execute(
            """INSERT INTO vacancy_track_scores
            (account,source,vacancy_id,track,score,score_decision,filter_status,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (account, source, vacancy_id, track, score, "pass", "pass", stamp),
        )


def test_nonhh_approval_is_not_blocked_by_unrelated_hh_numeric_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _vacancy(store, "shared", "getmatch.ru", "123")
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO attempts(account,vacancy_id,status,created_at,updated_at)
            VALUES(?,?,?,?,?)""",
            ("shared", "123", "success", "2026-01-01", "2026-01-01"),
        )
    changed = store.set_review_state_batch(
        "shared", "getmatch.ru", ["123"], "APPROVED",
        reason="independent native Getmatch channel", track="ai_core",
    )
    assert changed == 1
    row = store.list_campaign_vacancies("shared", "ai_core", "approved", 10, source="getmatch.ru")
    assert len(row) == 1
    assert row[0]["application_state"] == "READY_TO_APPLY"


def test_batch_approval_rejects_real_shared_ats_channel(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _vacancy(store, "shared", "geekjob.ru", "200")
    store.save_vacancy_links("shared", "geekjob.ru", [{
        "id": "200", "application_channel": "ats:jobs.example.com/apply/777",
        "application_channel_type": "external_ats",
    }])
    assert store.record_source_application(
        "shared", "career.habr.com", "100", "ai_core",
        "ats:jobs.example.com/apply/777", "submitting",
    )
    assert store.record_source_application(
        "shared", "career.habr.com", "100", "ai_core",
        "ats:jobs.example.com/apply/777", "success",
    )
    with pytest.raises(ValueError, match="channel already used"):
        store.set_review_state_batch(
            "shared", "geekjob.ru", ["200"], "APPROVED",
            reason="same ATS", track="ai_core",
        )
    assert store.list_campaign_vacancies("shared", "ai_core", "approved", 10, source="geekjob.ru") == []


def test_hh_approved_needs_form_and_letter_are_not_ready_to_apply(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for vacancy_id, state in (
        ("301", "NEEDS_FORM"), ("302", "NEEDS_LETTER"), ("303", "READY_TO_APPLY"),
    ):
        _vacancy(store, "shared", "hh.ru", vacancy_id, state=state)
        store.set_review_state(
            "shared", "hh.ru", vacancy_id, "APPROVED", reason="previous operator review",
            track="ai_core",
        )
    plan = queue_plan(store, "shared", "ai_core", "hh.ru", 100)
    assert [r["id"] for r in plan["planned"]] == ["303"]
    blocked = {r["id"]: r["reason"] for r in plan["blocked"]}
    assert blocked == {
        "301": "needs_workflow:NEEDS_FORM",
        "302": "needs_workflow:NEEDS_LETTER",
    }


def test_cli_batch_from_view_is_filtered_by_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig(
        tmp_path, tmp_path / "data", FIXTURES / "profile.toml", FIXTURES / "search.toml",
    )
    store = Store(config.db_path)
    _vacancy(store, "default", "hh.ru", "901", score=99)
    _vacancy(store, "default", "getmatch.ru", "902", score=89)
    monkeypatch.setattr(cli, "_config", lambda args: config)
    monkeypatch.setattr(cli, "_track_config", lambda *args, **kwargs: (config, {}))
    code = cli.main([
        "sources", "approve-batch", "--track", "ai_core", "--source", "getmatch.ru",
        "--from-view", "top", "--limit", "1", "--state", "APPROVED",
        "--reason", "source-specific shortlist",
    ])
    assert code == 0
    approved = store.list_campaign_vacancies("default", "ai_core", "approved", 10)
    assert [(x["source"], x["id"]) for x in approved] == [("getmatch.ru", "902")]


def test_nonhh_batch_approval_blocks_existing_same_channel_reservation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _vacancy(store, "shared", "career.habr.com", "503")
    store.save_vacancy_links("shared", "career.habr.com", [{
        "id": "503", "application_channel": "ats:example.org/apply/503",
    }])
    assert store.reserve_source_application(
        "shared", "getmatch.ru", "804", "ai_core",
        "ats:example.org/apply/503", "run-other",
    )[0]
    with pytest.raises(ValueError, match="channel reserved"):
        store.set_review_state_batch(
            "shared", "career.habr.com", ["503"], "APPROVED",
            reason="pending competing ATS", track="ai_core",
        )


def test_hh_legacy_attempt_still_blocks_external_channel_alias(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _vacancy(store, "shared", "hh.ru", "802")
    store.save_vacancy_links("shared", "hh.ru", [{
        "id": "802", "application_channel": "ats:third-party.example/apply/802",
    }])
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO attempts(account,vacancy_id,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            ("shared", "802", "unknown", "2026-10-22", "2026-10-22"),
        )
    with pytest.raises(ValueError, match="existing HH"):
        store.set_review_state_batch(
            "shared", "hh.ru", ["802"], "APPROVED",
            reason="must not retry unknown HH", track="ai_core",
        )


def test_source_plan_prioritizes_ready_beyond_many_higher_scored_forms(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for n in range(101):
        vacancy_id = str(10_000 + n)
        _vacancy(
            store, "shared", "hh.ru", vacancy_id,
            score=95, state="NEEDS_FORM",
        )
        store.set_review_state(
            "shared", "hh.ru", vacancy_id, "APPROVED",
            reason="prior review, form pending", track="ai_core",
        )
    _vacancy(
        store, "shared", "hh.ru", "9999",
        score=40, state="READY_TO_APPLY",
    )
    store.set_review_state(
        "shared", "hh.ru", "9999", "APPROVED",
        reason="prior review, ready", track="ai_core",
    )
    plan = queue_plan(store, "shared", "ai_core", "hh.ru", limit=100)
    assert plan["planned_count"] == 1
    assert plan["planned"][0]["id"] == "9999"
    assert plan["blocked_count"] == 99
    assert all(x["reason"] == "needs_workflow:NEEDS_FORM" for x in plan["blocked"])
