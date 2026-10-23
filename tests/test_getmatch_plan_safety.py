"""Telegram Getmatch dry-run must never mistake unrelated callbacks for an apply."""

from __future__ import annotations

from pathlib import Path

from applypilot.accounts import telegram_queue_plan
from applypilot.storage import Store


def _seed(store: Store, vacancy_id: str, *, score_decision: str) -> None:
    stamp = "2026-10-23T10:00:00+00:00"
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO vacancies
            (account,source,vacancy_id,title,company,url,
             created_at,first_seen_at,last_seen_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("getmatch-primary", "getmatch.ru", vacancy_id,
             "AI Agent Engineer", "Synthetic Inc",
             f"https://getmatch.ru/vacancies/{vacancy_id}",
             stamp, stamp, stamp, stamp),
        )
        conn.execute(
            """INSERT INTO vacancy_track_scores
            (account,source,vacancy_id,track,score,score_decision,filter_status,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            ("getmatch-primary", "getmatch.ru", vacancy_id, "ai_core",
             85, score_decision,
             "pass" if score_decision == "pass" else "reject", stamp),
        )


def _message(message_id: int, vacancy_id: str, buttons: list[tuple[str, str]]) -> dict:
    return {
        "message_id": message_id,
        "chat": "@g_jobbot",
        "sent_at": f"2026-10-23T10:{message_id % 60:02}:00+00:00",
        "text": f"AI Engineer https://getmatch.ru/vacancies/{vacancy_id}",
        "buttons": [{"text": name, "kind": kind} for name, kind in buttons],
        "vacancy_refs": [{"source": "getmatch.ru", "id": vacancy_id}],
        "application_likely": True, "has_callback": True,
    }


def test_getmatch_plan_excludes_skip_and_wrong_callback_and_deduplicates(tmp_path: Path) -> None:
    store = Store(tmp_path / "data.sqlite3")
    _seed(store, "36887", score_decision="pass")
    _seed(store, "36888", score_decision="reject")
    _seed(store, "36889", score_decision="pass")
    store.save_telegram_messages("getmatch-primary", [
        _message(101, "36887", [
            ("🇷🇺 Россия", "callback"), ("👉 Откликнуться в боте", "callback"),
        ]),
        _message(102, "36888", [("👉 Откликнуться в боте", "callback")]),
        _message(103, "36889", [("🇷🇺 Россия", "callback")]),
        _message(99, "36887", [("Откликнуться", "callback")]),
    ])

    plan = telegram_queue_plan(store, "getmatch-primary", "ai_core", 20)
    assert plan["mode"] == "dry_run"
    assert plan["planned_count"] == 1
    assert plan["blocked_count"] == 1
    assert plan["unmatched_count"] == 1
    candidate = plan["planned"][0]
    assert candidate["vacancy_id"] == "36887"
    assert candidate["button_index"] == 1
    assert candidate["button_text"] == "👉 Откликнуться в боте"
    assert candidate["can_send"] is False
    assert plan["blocked"][0]["vacancy_id"] == "36888"
    assert "not_relevant" in plan["blocked"][0]["reason"]
    assert plan["unmatched"][0]["reason"] == "no_positive_apply_callback"
    assert store.source_attempts("getmatch-primary", "getmatch.ru") == []


def test_getmatch_plan_excludes_confirmed_application(tmp_path: Path) -> None:
    store = Store(tmp_path / "data.sqlite3")
    _seed(store, "36887", score_decision="pass")
    store.save_telegram_messages("getmatch-primary", [
        _message(101, "36887", [("Откликнуться", "callback")]),
    ])
    with store.connect() as conn:
        conn.execute(
            """UPDATE vacancies SET application_state='APPLIED_CONFIRMED'
            WHERE account='getmatch-primary' AND source='getmatch.ru' AND vacancy_id='36887'"""
        )
    plan = telegram_queue_plan(store, "getmatch-primary", "ai_core", 20)
    assert plan["planned_count"] == 0
    assert plan["blocked_count"] == 1
    assert all(x["can_send"] is False for x in plan["blocked"])


def test_getmatch_plan_never_accepts_callbacks_from_another_chat(tmp_path: Path) -> None:
    store = Store(tmp_path / "data.sqlite3")
    _seed(store, "36887", score_decision="pass")
    message = _message(104, "36887", [("Откликнуться", "callback")])
    message["chat"] = "@unrelated_bot"
    store.save_telegram_messages("getmatch-primary", [message])
    plan = telegram_queue_plan(store, "getmatch-primary", "ai_core", 10)
    assert plan["planned_count"] == 0
    assert plan["unmatched"][0]["reason"] == "unexpected_telegram_chat"
