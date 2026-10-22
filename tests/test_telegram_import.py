from __future__ import annotations

import json
from pathlib import Path

from applypilot.config import AppConfig, effective_search
from applypilot.multisource import resolve_source_account, scan_sources
from applypilot.storage import Store
from applypilot.telegram_import import import_telegram_vacancies

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/public"


def _store(tmp_path: Path) -> tuple[Store, str]:
    config = AppConfig(
        tmp_path, tmp_path / "data", FIXTURES / "profile.toml", FIXTURES / "search.toml",
    )
    profile = config.load_profile()
    search = effective_search(config.load_search())
    scan_sources(
        config, "ai_core", profile, search, sources=["getmatch.ru"],
        fixture_dir=FIXTURES, limit=100,
    )
    ledger = resolve_source_account(config, profile, "getmatch.ru")
    return Store(config.db_path), ledger


def _message(vacancy_id: str = "36887", message_id: int = 42) -> dict:
    return {
        "chat": "@g_jobbot",
        "message_id": message_id,
        "sent_at": "2026-10-20T10:00:00+00:00",
        "text": f"AI Engineer at Gamma AI\nhttps://getmatch.ru/vacancies/{vacancy_id}",
        "buttons": [{
            "text": "Откликнуться",
            "kind": "webview",
            "url": f"https://getmatch.ru/vacancies/{vacancy_id}",
            "callback_present": False,
            "callback_sha256": "should-not-leak",
        }],
        "vacancy_refs": [{"source": "getmatch.ru", "id": vacancy_id}],
        "application_likely": True,
        "has_callback": False,
    }


def test_import_links_existing_scanned_vacancy(tmp_path: Path) -> None:
    store, ledger = _store(tmp_path)
    store.save_telegram_messages("getmatch-primary", [_message()])

    result = import_telegram_vacancies(store, ledger_account=ledger)

    assert result["linked_count"] == 1
    assert result["missing_count"] == 0
    payload = store.vacancy_payload(ledger, "getmatch.ru", "36887")
    assert payload["name"] == "AI Engineer"
    assert payload["telegram"]["message_id"] == 42
    assert payload["telegram"]["buttons"][0]["kind"] == "webview"
    assert "should-not-leak" not in json.dumps(payload, ensure_ascii=False)


def test_import_is_idempotent(tmp_path: Path) -> None:
    store, ledger = _store(tmp_path)
    store.save_telegram_messages("getmatch-primary", [_message()])

    first = import_telegram_vacancies(store, ledger_account=ledger)
    second = import_telegram_vacancies(store, ledger_account=ledger)

    assert first["linked_count"] == 1
    assert second["linked_count"] == 1
    assert store.vacancy_payload(ledger, "getmatch.ru", "36887")["telegram"]["message_id"] == 42


def test_missing_ref_can_be_reported_or_created_review_only(tmp_path: Path) -> None:
    store, ledger = _store(tmp_path)
    store.save_telegram_messages("getmatch-primary", [_message("99999", 77)])

    report = import_telegram_vacancies(store, ledger_account=ledger)
    assert report["linked_count"] == 0
    assert report["missing_count"] == 1
    assert report["missing"][0]["id"] == "99999"

    created = import_telegram_vacancies(store, ledger_account=ledger, create_missing=True)
    assert created["created_count"] == 1
    payload = store.vacancy_payload(ledger, "getmatch.ru", "99999")
    assert payload["description_status"] == "telegram"
    score = store.vacancy_track_row(ledger, "getmatch.ru", "99999", "ai_core")
    assert score["score_decision"] == "maybe"


def test_import_ignores_other_sources(tmp_path: Path) -> None:
    store, ledger = _store(tmp_path)
    message = _message()
    message["vacancy_refs"] = [{"source": "hh.ru", "id": "138413511"}]
    store.save_telegram_messages("getmatch-primary", [message])

    result = import_telegram_vacancies(store, ledger_account=ledger)

    assert result["linked_count"] == 0
    assert result["missing_count"] == 0
