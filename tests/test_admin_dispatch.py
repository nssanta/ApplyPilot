from __future__ import annotations

from pathlib import Path

from applypilot.admin import AdminApp
from applypilot.config import AppConfig, effective_search
from applypilot.dispatch import approve_batch
from applypilot.multisource import resolve_source_account, scan_sources
from applypilot.storage import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/public"


def _app(tmp_path: Path) -> tuple[AdminApp, AppConfig]:
    config = AppConfig(
        tmp_path, tmp_path / "data", FIXTURES / "profile.toml", FIXTURES / "search.toml",
    )
    profile = config.load_profile()
    search = effective_search(config.load_search())
    scan_sources(
        config, "default", profile, search, sources=["getmatch.ru"],
        fixture_dir=FIXTURES, limit=100,
    )
    ledger = resolve_source_account(config, profile, "getmatch.ru")
    approve_batch(
        Store(config.db_path), ledger, "default", "getmatch.ru", ["36887"],
        reason="admin public fixture review",
    )
    return AdminApp(config), config


def _message() -> dict:
    return {
        "chat": "@g_jobbot",
        "message_id": 42,
        "sent_at": "2026-10-20T10:00:00+00:00",
        "text": "AI Engineer\nhttps://getmatch.ru/vacancies/36887",
        "buttons": [{
            "text": "Откликнуться", "kind": "webview",
            "url": "https://getmatch.ru/vacancies/36887",
        }],
        "vacancy_refs": [{"source": "getmatch.ru", "id": "36887"}],
        "application_likely": True,
        "has_callback": False,
    }


def test_admin_dispatch_is_dry_run(tmp_path: Path) -> None:
    app, _config = _app(tmp_path)
    result = app.dispatch_overview("default", ["getmatch.ru"], 100)
    assert "error" not in result
    assert result["mode"] == "dry_run"
    assert result["sources"] == ["getmatch.ru"]
    assert result["planned_count"] == 0
    assert result["blocked_count"] >= 1


def test_admin_telegram_import_links_saved_message(tmp_path: Path) -> None:
    app, config = _app(tmp_path)
    ledger = resolve_source_account(config, config.load_profile(), "getmatch.ru")
    Store(config.db_path).save_telegram_messages("getmatch-primary", [_message()])

    result = app.telegram_import("default")

    assert result["linked_count"] == 1
    assert result["ledger_account"] == ledger
    assert result["missing_count"] == 0


def test_admin_clickers_are_explicitly_unverified(tmp_path: Path) -> None:
    app, _config = _app(tmp_path)
    result = app.clickers_overview()
    assert len(result["clickers"]) == 4
    assert all(not spec["submit_verified"] for spec in result["clickers"])
    assert all(spec["apply_status"] == "needs_live_validation" for spec in result["clickers"])


def test_admin_dispatch_rejects_unknown_track(tmp_path: Path) -> None:
    app, _config = _app(tmp_path)
    assert "error" in app.dispatch_overview("missing-track", None, 100)
