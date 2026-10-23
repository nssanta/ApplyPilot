"""Evidence-only Getmatch bot receipt reconciliation regression tests."""

from __future__ import annotations

from pathlib import Path

from applypilot.getmatch_receipts import reconcile_getmatch_receipts
from applypilot.storage import Store


def seed(store: Store, vacancy_id: str = "36887") -> None:
    stamp = "2026-10-20T10:00:00+00:00"
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO vacancies
            (account,source,vacancy_id,title,company,url,created_at,
             first_seen_at,last_seen_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("getmatch-primary", "getmatch.ru", vacancy_id,
             "Synthetic LLM Engineer", "Synthetic", f"https://getmatch.ru/vacancies/{vacancy_id}",
             stamp, stamp, stamp, stamp),
        )
        conn.execute(
            """INSERT INTO vacancy_track_scores
            (account,source,vacancy_id,track,score,score_decision,filter_status,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            ("getmatch-primary", "getmatch.ru", vacancy_id, "ai_core",
             85, "pass", "pass", stamp),
        )


def bot_message(
    message_id: int = 2517293, *, outgoing: bool | None = False,
    text: str = "✅ Отклик отправлен.",
    chat: str = "@g_jobbot", vacancy_ids: tuple[str, ...] = ("36887",),
) -> dict:
    return {
        "message_id": message_id,
        "chat": chat, "sent_at": "2026-10-20T11:00:00+00:00",
        "text": text, "outgoing": outgoing,
        "buttons": [], "application_likely": True, "has_callback": False,
        "vacancy_refs": [{"source": "getmatch.ru", "id": job_id} for job_id in vacancy_ids],
    }


def test_official_bot_receipt_marks_prior_submission_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    seed(store)
    store.save_telegram_messages("getmatch-primary", [bot_message()])
    first = reconcile_getmatch_receipts(store)
    assert first["confirmed_count"] == 1
    assert first["receipts"][0]["status"] == "recorded_already_applied"
    row = store.vacancy_track_row("getmatch-primary", "getmatch.ru", "36887", "ai_core")
    assert row["application_state"] == "ALREADY_APPLIED"
    assert store.source_attempts("getmatch-primary", "getmatch.ru")[0]["status"] == "already_applied"

    second = reconcile_getmatch_receipts(store)
    assert second["confirmed_count"] == 0
    assert second["receipts"][0]["status"] == "already_recorded"
    assert len(store.source_attempts("getmatch-primary", "getmatch.ru")) == 1


def test_receipt_ignores_outgoing_unknown_direction_or_wrong_chat(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    seed(store)
    store.save_telegram_messages("getmatch-primary", [
        bot_message(101, outgoing=True),
        bot_message(102, outgoing=None),
        bot_message(103, chat="@other_bot"),
    ])
    assert reconcile_getmatch_receipts(store)["confirmed_count"] == 0
    assert store.source_attempts("getmatch-primary", "getmatch.ru") == []


def test_receipt_requires_unambiguous_id_and_exact_confirmation(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    seed(store)
    store.save_telegram_messages("getmatch-primary", [
        bot_message(101, vacancy_ids=("36887", "36888")),
        bot_message(102, text="✅ Теперь вы можете отправить отклик!"),
        bot_message(103, text="Отклик не отправлен"),
    ])
    result = reconcile_getmatch_receipts(store)
    assert result["confirmed_count"] == 0
    assert result["skipped"][0]["reason"] == "ambiguous_vacancy"


def test_bot_receipt_can_reconcile_existing_unknown_without_retry(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    seed(store)
    assert store.record_source_application(
        "getmatch-primary", "getmatch.ru", "36887", "ai_core",
        "getmatch.ru:36887", "unknown", note="callback result ambiguous",
    )
    store.save_telegram_messages("getmatch-primary", [bot_message()])
    result = reconcile_getmatch_receipts(store)
    assert result["confirmed_count"] == 1
    assert result["receipts"][0]["status"] == "reconciled_success"
    assert store.source_attempts("getmatch-primary", "getmatch.ru")[0]["status"] == "success"


def test_legacy_messages_without_direction_are_not_trusted(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    seed(store)
    message = bot_message()
    message.pop("outgoing")
    store.save_telegram_messages("getmatch-primary", [message])
    assert reconcile_getmatch_receipts(store)["confirmed_count"] == 0
    assert store.telegram_messages("getmatch-primary")[0]["outgoing"] is None
