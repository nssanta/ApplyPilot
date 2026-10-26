from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.config import AppConfig, effective_search
from applypilot.dispatch import (
    ApplyOutcome,
    approve_batch,
    dispatch_apply,
    dispatch_plan,
    verified_success_receipt,
)
from applypilot.multisource import queue_plan, scan_sources
from applypilot.storage import AUTO_APPROVAL_REASON, Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/public"
SOURCES = ["career.habr.com", "geekjob.ru", "getmatch.ru", "yandex.ru/jobs", "dreamjob.ru"]


class FakeDriver:
    def __init__(self, outcome: ApplyOutcome | None = None, error: Exception | None = None) -> None:
        self.outcome = outcome or ApplyOutcome("success", "confirmed")
        self.error = error
        self.preflights = 0
        self.applies = 0

    def preflight(self, row):
        self.preflights += 1
        return ApplyOutcome("ready")

    def apply(self, row):
        self.applies += 1
        if self.error is not None:
            raise self.error
        return self.outcome


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(tmp_path, tmp_path / "data", FIXTURES / "profile.toml", FIXTURES / "search.toml")


def _seed(tmp_path: Path) -> tuple[AppConfig, Store]:
    config = _config(tmp_path)
    profile = config.load_profile()
    search = effective_search(config.load_search())
    scan_sources(
        config, "ai_core", profile, search, sources=SOURCES,
        fixture_dir=FIXTURES, limit=100,
    )
    return config, Store(config.db_path)


def _fake_plan(store: Store, source: str, vacancy_id: str = "36887") -> dict:
    return {
        "source": source, "candidate_count": 1,
        "planned_count": 1, "blocked_count": 0,
        "planned": [{
            "id": vacancy_id, "source": source, "name": "AI Engineer",
            "company": "Gamma AI", "channel_key": f"{source}:{vacancy_id}",
        }],
        "blocked": [], "apply_status": "needs_live_validation",
        "account": "default", "track": "ai_core",
    }


def test_dispatch_plan_is_dry_run_and_aggregates(tmp_path: Path) -> None:
    _config_obj, store = _seed(tmp_path)
    assert approve_batch(
        store, "default", "ai_core", "getmatch.ru", ["36887"],
        reason="public fixture review",
    )["updated"] == 1

    plan = dispatch_plan(store, "default", "ai_core", ["getmatch.ru"], 100)

    assert plan["mode"] == "dry_run"
    assert plan["candidate_count"] == 1
    assert plan["planned_count"] == 0
    assert plan["blocked_count"] >= 1
    assert plan["blocked"][0]["source"] == "getmatch.ru"
    assert plan["blocked"][0]["reason"].startswith("needs_live_validation")
    assert store.source_attempts("default", "getmatch.ru") == []


def test_source_queue_excludes_stale_automatic_approvals(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "jobs.sqlite3")
    row = {
        "id": "stale-auto", "source": "getmatch.ru", "review_state": "APPROVED",
        "review_reason": AUTO_APPROVAL_REASON, "auto_apply_eligible": False,
        "application_state": "READY_TO_APPLY", "filter_status": "pass",
        "score_decision": "pass", "applied_before": False,
    }
    monkeypatch.setattr(
        store, "list_campaign_vacancies", lambda *_args, **_kwargs: [row],
    )

    result = queue_plan(store, "default", "ai_core", "getmatch.ru")

    assert result["candidate_count"] == 0
    assert result["planned_count"] == 0


def test_dispatch_apply_dry_run_never_writes_attempts(tmp_path: Path) -> None:
    _config_obj, store = _seed(tmp_path)
    result = dispatch_apply(
        store, "default", "ai_core", ["getmatch.ru"], 100, allow_live=False,
    )
    assert result["mode"] == "dry_run"
    assert result["executed"] == 0
    assert store.source_attempts("default", "getmatch.ru") == []


def test_dispatch_apply_live_without_driver_skips_unverified(tmp_path: Path) -> None:
    _config_obj, store = _seed(tmp_path)
    approve_batch(store, "default", "ai_core", "getmatch.ru", ["36887"], reason="review")
    # A blocked source has no planned row, so this is a no-op by design.
    result = dispatch_apply(
        store, "default", "ai_core", ["getmatch.ru"], 100, allow_live=True,
    )
    assert result["mode"] == "live"
    assert result["executed"] == 0
    assert store.source_attempts("default", "getmatch.ru") == []


def test_dispatch_apply_live_driver_success_records_receipt(tmp_path: Path, monkeypatch) -> None:
    _config_obj, store = _seed(tmp_path)
    import applypilot.dispatch as dispatch_module

    monkeypatch.setattr(
        dispatch_module, "queue_plan",
        lambda *args, **kwargs: _fake_plan(store, "getmatch.ru"),
    )
    driver = FakeDriver(ApplyOutcome("success", "confirmed by test", receipt={"provider": "@g_jobbot", "vacancy_id": "36887",
                                                                    "confirmation_message_id": 42}))
    result = dispatch_apply(
        store, "default", "ai_core", ["getmatch.ru"], 100,
        allow_live=True, drivers={"getmatch.ru": driver}, run_id="run-success",
    )

    assert result["executed"] == 1
    assert result["summary"] == {"success": 1}
    assert driver.preflights == 1 and driver.applies == 1
    attempts = store.source_attempts("default", "getmatch.ru")
    assert len(attempts) == 1
    assert attempts[0]["status"] == "success"
    assert "receipt" in attempts[0]["note"]


def test_dispatch_stops_at_source_profile_run_limit(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "jobs.sqlite3")
    rows = [
        {"id": str(vacancy_id), "source": "getmatch.ru", "name": "AI Engineer",
         "company": "Example", "channel_key": f"getmatch.ru:{vacancy_id}"}
        for vacancy_id in (36887, 36888, 36889)
    ]

    def plan(_store, _account, _track, source, *_args, **_kwargs):
        return {**_fake_plan(store, source), "candidate_count": len(rows),
                "planned_count": len(rows), "planned": rows}

    monkeypatch.setattr("applypilot.dispatch.queue_plan", plan)

    class ReceiptDriver(FakeDriver):
        def apply(self, row):
            self.applies += 1
            return ApplyOutcome("success", "confirmed", receipt={
                "provider": "@g_jobbot", "vacancy_id": row["id"],
                "confirmation_message_id": int(row["id"]),
            })

    driver = ReceiptDriver()
    result = dispatch_apply(
        store, "default", "ai_core", ["getmatch.ru"], 100,
        allow_live=True, drivers={"getmatch.ru": driver},
        source_limits={"getmatch.ru": {"per_run": 1, "per_day": 10}},
        run_id="run-source-cap",
    )

    assert result["summary"] == {"success": 1, "limit_reached": 2}
    assert driver.applies == 1
    assert len(store.source_attempts("default", "getmatch.ru")) == 1


def test_dispatch_processes_full_hundred_candidate_pool(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "jobs.sqlite3")
    rows = [
        {"id": str(50_000 + index), "source": "getmatch.ru", "name": "AI Engineer",
         "company": "Example", "channel_key": f"getmatch.ru:{50_000 + index}"}
        for index in range(100)
    ]

    def plan(_store, _account, _track, source, *_args, **_kwargs):
        return {**_fake_plan(store, source), "candidate_count": len(rows),
                "planned_count": len(rows), "planned": rows}

    monkeypatch.setattr("applypilot.dispatch.queue_plan", plan)

    class ReceiptDriver(FakeDriver):
        def apply(self, row):
            self.applies += 1
            return ApplyOutcome("success", "confirmed", receipt={
                "provider": "@g_jobbot", "vacancy_id": row["id"],
                "confirmation_message_id": int(row["id"]),
            })

    driver = ReceiptDriver()
    result = dispatch_apply(
        store, "default", "ai_core", ["getmatch.ru"], 100,
        allow_live=True, drivers={"getmatch.ru": driver},
        source_limits={"getmatch.ru": {"per_run": 100, "per_day": 100}},
        run_id="run-hundred",
    )

    assert result["summary"] == {"success": 100}
    assert driver.preflights == 100
    assert driver.applies == 100
    assert len(store.source_attempts("default", "getmatch.ru", 100)) == 100


def test_dispatch_apply_exception_becomes_unknown_and_never_retries(tmp_path: Path, monkeypatch) -> None:
    _config_obj, store = _seed(tmp_path)
    import applypilot.dispatch as dispatch_module

    monkeypatch.setattr(
        dispatch_module, "queue_plan",
        lambda *args, **kwargs: _fake_plan(store, "getmatch.ru"),
    )
    failing = FakeDriver(error=RuntimeError("browser crashed"))
    first = dispatch_apply(
        store, "default", "ai_core", ["getmatch.ru"], 100,
        allow_live=True, drivers={"getmatch.ru": failing}, run_id="run-fail",
    )
    assert first["summary"] == {"unknown": 1}
    assert store.source_attempts("default", "getmatch.ru")[0]["status"] == "unknown"

    retry = FakeDriver()
    second = dispatch_apply(
        store, "default", "ai_core", ["getmatch.ru"], 100,
        allow_live=True, drivers={"getmatch.ru": retry}, run_id="run-retry",
    )
    assert second["results"][0]["status"] == "channel_conflict"
    assert retry.applies == 0
    assert len(store.source_attempts("default", "getmatch.ru")) == 1


def test_one_ambiguous_submit_does_not_pause_source_for_independent_rows(
    tmp_path: Path, monkeypatch,
) -> None:
    store = Store(tmp_path / "jobs.sqlite3")
    rows = [
        {"id": "50001", "source": "getmatch.ru", "name": "AI Engineer",
         "company": "Example", "channel_key": "getmatch.ru:50001"},
        {"id": "50002", "source": "getmatch.ru", "name": "LLM Engineer",
         "company": "Example", "channel_key": "getmatch.ru:50002"},
    ]

    def plan(_store, _account, _track, source, *_args, **_kwargs):
        return {**_fake_plan(store, source), "candidate_count": len(rows),
                "planned_count": len(rows), "planned": rows}

    monkeypatch.setattr("applypilot.dispatch.queue_plan", plan)

    class MixedDriver(FakeDriver):
        def apply(self, row):
            self.applies += 1
            if row["id"] == "50001":
                return ApplyOutcome("unknown", "ambiguous callback")
            return ApplyOutcome("success", "confirmed", receipt={
                "provider": "@g_jobbot", "vacancy_id": row["id"],
                "confirmation_message_id": 50002,
            })

    driver = MixedDriver()
    result = dispatch_apply(
        store, "default", "ai_core", ["getmatch.ru"], 100,
        allow_live=True, drivers={"getmatch.ru": driver}, run_id="run-mixed",
    )

    assert result["summary"] == {"unknown": 1, "success": 1}
    assert driver.applies == 2
    assert sorted(row["status"] for row in store.source_attempts(
        "default", "getmatch.ru", 10,
    )) == ["success", "unknown"]


def test_dispatch_apply_skips_when_preflight_blocks(tmp_path: Path, monkeypatch) -> None:
    _config_obj, store = _seed(tmp_path)
    import applypilot.dispatch as dispatch_module

    monkeypatch.setattr(
        dispatch_module, "queue_plan",
        lambda *args, **kwargs: _fake_plan(store, "yandex.ru/jobs"),
    )

    class BlockedDriver(FakeDriver):
        def preflight(self, row):
            self.preflights += 1
            return ApplyOutcome("needs_manual", "login required")

    driver = BlockedDriver()
    result = dispatch_apply(
        store, "default", "ai_core", ["yandex.ru/jobs"], 100,
        allow_live=True, drivers={"yandex.ru/jobs": driver}, run_id="run-blocked",
    )
    assert result["summary"] == {"needs_manual": 1}
    assert driver.applies == 0
    assert store.source_attempts("default", "yandex.ru/jobs") == []


@pytest.mark.parametrize("receipt", [
    {}, {"id": "anything"},
    {"provider": "@g_jobbot", "vacancy_id": "other", "confirmation_message_id": 42},
    {"provider": "untrusted", "vacancy_id": "36887", "confirmation_message_id": 42},
    {"provider": "@g_jobbot", "vacancy_id": "36887", "confirmation_message_id": True},
    {"provider": "@g_jobbot", "vacancy_id": "36887", "confirmation_message_id": "²"},
])
def test_unverified_success_is_quarantined_and_source_pauses(tmp_path, monkeypatch, receipt):
    store = Store(tmp_path / "jobs.sqlite3")

    def plan(_store, _account, _track, source, *_args, **_kwargs):
        result = _fake_plan(store, source)
        if source == "getmatch.ru":
            result["planned"].append({**result["planned"][0], "id": "36888",
                                      "channel_key": "getmatch.ru:36888"})
        return result

    monkeypatch.setattr("applypilot.dispatch.queue_plan", plan)
    bad = FakeDriver(ApplyOutcome("success", receipt=receipt))
    other = FakeDriver(ApplyOutcome("failed_before_submit", "not sent"))
    result = dispatch_apply(store, "default", "ai_core", ["getmatch.ru", "geekjob.ru"],
                            allow_live=True, drivers={"getmatch.ru": bad, "geekjob.ru": other})
    assert result["summary"] == {"unknown": 1, "needs_manual": 1, "failed_before_submit": 1}
    assert bad.applies == 1 and other.applies == 1
    attempts = store.source_attempts("default", "getmatch.ru")
    assert len(attempts) == 1 and attempts[0]["status"] == "unknown"


@pytest.mark.parametrize("raises", [False, True])
def test_success_persistence_failure_is_not_reported_as_delivered(tmp_path, monkeypatch, raises):
    store = Store(tmp_path / "jobs.sqlite3")
    monkeypatch.setattr("applypilot.dispatch.queue_plan",
                        lambda *args, **kwargs: _fake_plan(store, "getmatch.ru"))
    original = store.record_source_application

    def record(*args, **kwargs):
        if args[5] == "success":
            if raises:
                raise OSError("disk unavailable")
            return False
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "record_source_application", record)
    driver = FakeDriver(ApplyOutcome("success", receipt={
        "provider": "@g_jobbot", "vacancy_id": "36887", "confirmation_message_id": 42,
    }))
    result = dispatch_apply(store, "default", "ai_core", ["getmatch.ru"],
                            allow_live=True, drivers={"getmatch.ru": driver})
    assert result["summary"] == {"unknown": 1}
    assert result["results"][0]["action"] != "applied"
    assert store.source_attempts("default", "getmatch.ru")[0]["status"] == "submitting"


def test_unknown_sources_cannot_claim_success_with_an_arbitrary_receipt():
    assert not verified_success_receipt("new.example", "1", {"vacancy_id": "1"})


def test_geekjob_receipt_requires_successful_post_and_history_for_same_vacancy():
    receipt = {"provider": "geekjob.ru", "vacancy_id": "1", "history_verified": True,
               "post_paths": ["/rest/responses/create"], "post_statuses": [200]}
    assert verified_success_receipt("geekjob.ru", "1", receipt)
    assert not verified_success_receipt("geekjob.ru", "2", receipt)
    assert not verified_success_receipt("geekjob.ru", "1", {**receipt, "post_statuses": [500]})
