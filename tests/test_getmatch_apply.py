"""Single-vacancy Getmatch callback pilot: no live Telegram calls in tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.getmatch_apply import execute_getmatch_pilot, plan_getmatch_pilot
from applypilot.storage import Store
from applypilot.telegram_client import LiveActionDisabled


def seeded(tmp_path: Path, *, approved: bool = True,
           outgoing: bool = False) -> Store:
    store = Store(tmp_path / "db.sqlite3")
    stamp = "2026-10-23T10:00:00+00:00"
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO vacancies
            (account,source,vacancy_id,title,company,url,application_state,
             created_at,first_seen_at,last_seen_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("getmatch-primary", "getmatch.ru", "35881", "LLM Developer",
             "Synthetic", "https://getmatch.ru/vacancies/35881-llm-developer",
             "READY_TO_APPLY" if approved else "NOT_STARTED",
             stamp, stamp, stamp, stamp),
        )
        conn.execute(
            """INSERT INTO vacancy_track_scores
            (account,source,vacancy_id,track,score,score_decision,filter_status,
             review_state,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            ("getmatch-primary", "getmatch.ru", "35881", "ai_core", 69,
             "pass", "unknown", "APPROVED" if approved else "UNREVIEWED", stamp),
        )
    store.save_telegram_messages("getmatch-primary", [{
        "message_id": 100,
        "chat": "@g_jobbot", "outgoing": outgoing,
        "text": "LLM Developer https://getmatch.ru/vacancies/35881",
        "sent_at": stamp,
        "buttons": [
            {"kind": "callback", "text": "🇷🇺 Россия", "callback_sha256": "b" * 16},
            {"kind": "callback", "text": "👉 Откликнуться в боте",
             "callback_sha256": "a" * 16, "callback_present": True},
        ],
        "vacancy_refs": [{"source": "getmatch.ru", "id": "35881"}],
        "application_likely": True, "has_callback": True,
    }])
    return store


def test_getmatch_pilot_plan_is_not_approval(tmp_path: Path) -> None:
    store = seeded(tmp_path, approved=False)
    plan = plan_getmatch_pilot(store, "35881")
    assert plan["status"] == "needs_approval"
    assert plan["button_index"] == 1
    assert plan["can_send"] is False
    assert store.source_attempts("getmatch-primary", "getmatch.ru") == []


def test_getmatch_pilot_preflight_selects_apply_not_country(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    plan = plan_getmatch_pilot(store, "35881")
    assert plan["status"] == "ready_for_single_live_pilot"
    assert plan["button_index"] == 1
    assert plan["callback_sha256"] == "a" * 16
    assert plan["can_send"] is False


def test_getmatch_pilot_writes_nothing_without_live_optin(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    plan = execute_getmatch_pilot(store, tmp_path, "35881")
    assert plan["mode"] == "dry_run"
    assert store.source_attempts("getmatch-primary", "getmatch.ru") == []
    with pytest.raises(LiveActionDisabled):
        execute_getmatch_pilot(store, tmp_path, "35881", allow_live=True)


def test_getmatch_pilot_records_verified_receipt_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPLYPILOT_TELEGRAM_LIVE", "1")
    store = seeded(tmp_path)
    called: list[int] = []

    def transport(_root, _account, _store, plan):
        called.append(plan["button_index"])
        return {
            "status": "success",
            "reason": "new bot confirmation",
            "receipt": {
                "provider": "@g_jobbot",
                "confirmation_message_id": 101,
                "vacancy_id": plan["vacancy_id"],
            },
        }

    result = execute_getmatch_pilot(
        store, tmp_path, "35881", allow_live=True, transport=transport,
    )
    assert result["status"] == "success"
    assert result["recorded"] is True
    assert called == [1]
    assert store.vacancy_track_row(
        "getmatch-primary", "getmatch.ru", "35881", "ai_core",
    )["application_state"] == "APPLIED_CONFIRMED"
    second = execute_getmatch_pilot(
        store, tmp_path, "35881", allow_live=True, transport=transport,
    )
    assert second["status"] != "ready_for_single_live_pilot"
    assert called == [1]
    assert len(store.source_attempts("getmatch-primary", "getmatch.ru")) == 1


def test_getmatch_pilot_unknown_is_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPLYPILOT_TELEGRAM_LIVE", "1")
    store = seeded(tmp_path)
    result = execute_getmatch_pilot(
        store, tmp_path, "35881", allow_live=True,
        transport=lambda *_: {"status": "unknown", "reason": "no receipt"},
    )
    assert result["status"] == "unknown"
    assert store.source_attempts("getmatch-primary", "getmatch.ru")[0]["status"] == "unknown"
    assert plan_getmatch_pilot(store, "35881")["status"] == "not_ready"


def test_getmatch_pilot_refuses_outgoing_forged_message(tmp_path: Path) -> None:
    store = seeded(tmp_path, outgoing=True)
    assert plan_getmatch_pilot(store, "35881")["status"] == "not_ready"


@pytest.mark.parametrize(
    "receipt",
    [
        {"confirmation_message_id": 101, "vacancy_id": "35881"},
        {"provider": "@wrong_bot", "confirmation_message_id": 101, "vacancy_id": "35881"},
        {"provider": "@g_jobbot", "confirmation_message_id": 101, "vacancy_id": "99999"},
        {"provider": "@g_jobbot", "confirmation_message_id": -1, "vacancy_id": "35881"},
    ],
)
def test_getmatch_pilot_rejects_invalid_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt: dict,
) -> None:
    monkeypatch.setenv("APPLYPILOT_TELEGRAM_LIVE", "1")
    store = seeded(tmp_path)
    result = execute_getmatch_pilot(
        store, tmp_path, "35881", allow_live=True,
        transport=lambda *_: {
            "status": "success", "receipt": receipt,
            "reason": "unverified proof",
        },
    )
    assert result["status"] == "unknown"
    assert store.source_attempts("getmatch-primary", "getmatch.ru")[0]["status"] == "unknown"
