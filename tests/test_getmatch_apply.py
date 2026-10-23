"""Single-vacancy Getmatch callback pilot: no live Telegram calls in tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from applypilot.getmatch_apply import (
    GetmatchCallbackDriver,
    execute_getmatch_pilot,
    getmatch_profile_acceptance,
    plan_getmatch_pilot,
)
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
        "message_id": 101,
        "chat": "@g_jobbot", "outgoing": False,
        "text": "**Ваш профиль принят**🤘\nС ним можно откликаться на вакансии в один клик",
        "sent_at": datetime.now(UTC).isoformat(),
        "buttons": [], "vacancy_refs": [], "application_likely": False,
        "has_callback": False,
    }, {
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


def test_pilot_blocks_when_official_profile_acceptance_missing(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    with store.connect() as conn:
        conn.execute("DELETE FROM telegram_messages WHERE message_id=101")
    assert getmatch_profile_acceptance(store)["status"] == "unverified"
    assert plan_getmatch_pilot(store, "35881")["status"] == "needs_profile_acceptance"


def test_pilot_rejects_stale_and_incoming_untrusted_acceptance(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    with store.connect() as conn:
        conn.execute(
            "UPDATE telegram_messages SET sent_at=? WHERE message_id=101",
            ((datetime.now(UTC) - timedelta(days=8)).isoformat(),),
        )
    assert getmatch_profile_acceptance(store)["status"] == "stale"
    assert plan_getmatch_pilot(store, "35881")["status"] == "needs_profile_acceptance"
    with store.connect() as conn:
        conn.execute("UPDATE telegram_messages SET sent_at=?,outgoing=1 WHERE message_id=101",
                     (datetime.now(UTC).isoformat(),))
    assert getmatch_profile_acceptance(store)["status"] == "unverified"



def test_getmatch_verified_driver_dispatches_once_and_blocks_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from applypilot.dispatch import dispatch_apply, dispatch_plan

    monkeypatch.setenv("APPLYPILOT_TELEGRAM_LIVE", "1")
    store = seeded(tmp_path)
    calls = []

    def confirmed_transport(_root, _account, _store, plan):
        calls.append(plan["vacancy_id"])
        return {
            "status": "success",
            "reason": "new official bot proof for exact vacancy",
            "receipt": {
                "provider": "@g_jobbot",
                "vacancy_id": plan["vacancy_id"],
                "confirmation_message_id": 10111,
            },
        }

    source_accounts = {"getmatch.ru": "getmatch-primary"}
    no_driver = dispatch_plan(
        store, "default", "ai_core", ["getmatch.ru"], 100,
        accounts=source_accounts,
    )
    assert no_driver["planned_count"] == 0
    assert no_driver["blocked_count"] == 1
    driver = GetmatchCallbackDriver(
        tmp_path, store, min_interval_seconds=0,
        transport=confirmed_transport,
    )
    result = dispatch_apply(
        store, "default", "ai_core", ["getmatch.ru"], 100,
        allow_live=True, drivers={"getmatch.ru": driver},
        accounts=source_accounts, run_id="getmatch-test-batch",
    )
    assert driver.submit_verified is True
    assert result["summary"] == {"success": 1}
    assert calls == ["35881"]
    assert result["results"][0]["recorded"] is True
    assert store.vacancy_track_row(
        "getmatch-primary", "getmatch.ru", "35881", "ai_core",
    )["application_state"] == "APPLIED_CONFIRMED"
    result2 = dispatch_apply(
        store, "default", "ai_core", ["getmatch.ru"], 100,
        allow_live=True, drivers={"getmatch.ru": GetmatchCallbackDriver(
            tmp_path, store, min_interval_seconds=0,
            transport=confirmed_transport,
        )},
        accounts=source_accounts, run_id="getmatch-batch-2",
    )
    assert result2["planned_count"] == 0
    assert calls == ["35881"]
    assert len(store.source_attempts("getmatch-primary", "getmatch.ru")) == 1


def test_getmatch_dispatch_pauses_after_unknown_without_resending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from applypilot.dispatch import dispatch_apply

    monkeypatch.setenv("APPLYPILOT_TELEGRAM_LIVE", "1")
    store = seeded(tmp_path)
    stamp = datetime.now(UTC).isoformat()
    with store.connect() as conn:
        for vid, score in (("35882", 80), ("35883", 75)):
            conn.execute(
                """INSERT INTO vacancies
                (account,source,vacancy_id,title,company,url,application_state,
                 created_at,first_seen_at,last_seen_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("getmatch-primary", "getmatch.ru", vid, "LLM Developer",
                 "Synthetic Inc", f"https://getmatch.ru/vacancies/{vid}",
                 "READY_TO_APPLY", stamp, stamp, stamp, stamp),
            )
            conn.execute(
                """INSERT INTO vacancy_track_scores
                (account,source,vacancy_id,track,score,score_decision,filter_status,
                 review_state,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("getmatch-primary", "getmatch.ru", vid, "ai_core", score,
                 "pass", "pass", "APPROVED", stamp),
            )
    for vid in ("35882", "35883"):
        store.save_telegram_messages("getmatch-primary", [{
            "message_id": int(vid), "chat": "@g_jobbot", "outgoing": False,
            "text": f"LLM Developer https://getmatch.ru/vacancies/{vid}",
            "sent_at": stamp,
            "buttons": [{"kind": "callback", "text": "👉 Откликнуться",
                         "callback_sha256": "a" * 16, "callback_present": True}],
            "vacancy_refs": [{"source": "getmatch.ru", "id": vid}],
            "application_likely": True, "has_callback": True,
        }])
    calls = []

    def simulated_transport(_root, _account, _store, plan):
        calls.append(plan["vacancy_id"])
        return {"status": "unknown", "reason": "bot confirmation delayed"}

    driver = GetmatchCallbackDriver(
        tmp_path, store, min_interval_seconds=0, transport=simulated_transport,
    )
    result = dispatch_apply(
        store, "default", "ai_core", ["getmatch.ru"], 100,
        allow_live=True, drivers={"getmatch.ru": driver},
        accounts={"getmatch.ru": "getmatch-primary"}, run_id="getmatch-unknown",
    )
    assert result["summary"]["unknown"] == 1
    assert result["summary"]["needs_manual"] == 2
    assert len(calls) == 1
    blocked_id = calls[0]
    assert store.vacancy_track_row(
        "getmatch-primary", "getmatch.ru", blocked_id, "ai_core",
    )["application_state"] == "UNKNOWN"
    assert len(store.source_attempts("getmatch-primary", "getmatch.ru")) == 1


def test_getmatch_verified_driver_blocks_live_without_second_optin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APPLYPILOT_TELEGRAM_LIVE", raising=False)
    store = seeded(tmp_path)
    driver = GetmatchCallbackDriver(tmp_path, store)
    assert driver.preflight({
        "id": "35881", "source": "getmatch.ru",
        "review_state": "APPROVED", "application_state": "READY_TO_APPLY",
    }).status == "needs_manual"
    assert store.source_attempts("getmatch-primary", "getmatch.ru") == []
