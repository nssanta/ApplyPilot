from __future__ import annotations

from pathlib import Path

from applypilot.config import AppConfig, effective_search
from applypilot.dispatch import (
    ApplyOutcome,
    approve_batch,
    dispatch_apply,
    dispatch_plan,
)
from applypilot.multisource import scan_sources
from applypilot.storage import Store

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
    driver = FakeDriver(ApplyOutcome("success", "confirmed by test", receipt={"id": "r1"}))
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
