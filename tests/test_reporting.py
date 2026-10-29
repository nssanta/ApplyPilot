"""Management report separates new receipts from history and unchecked inbox."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from applypilot.reporting import management_report
from applypilot.storage import Store


def test_management_report_does_not_count_history_or_unchecked_inbox_as_zero(tmp_path: Path):
    store = Store(tmp_path / "state.sqlite3")
    store.create_execution("exec-1", {"args": {}, "tracks": ["ai"], "sources": ["hh.ru"]})
    store.execution_run("exec-1", "run-1", "hh.ru", "ai")
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO runs(run_id,account,mode,status,started_at) "
            "VALUES('run-1','hh-primary','apply','completed',?)",
            (datetime.now(UTC).isoformat(),),
        )
        conn.execute(
            "INSERT INTO run_items(run_id,vacancy_id,ordinal,name,status,created_at,updated_at) "
            "VALUES('run-1','new-1',1,'AI Engineer','success',?,?)",
            (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
    now = datetime.now(UTC)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO negotiation_statuses(account,vacancy_id,status,updated_at,fetched_at) "
            "VALUES('hh-primary','old','discard',?,?)",
            ((now - timedelta(days=2)).isoformat(), (now - timedelta(days=2)).isoformat()),
        )
        conn.execute(
            "INSERT INTO negotiation_statuses(account,vacancy_id,status,updated_at,fetched_at) "
            "VALUES('hh-primary','fresh','discard',?,?)",
            ((now - timedelta(minutes=10)).isoformat(), now.isoformat()),
        )
    report = management_report(
        store, track="ai", sources=["hh.ru"], accounts={"hh.ru": "hh-primary"},
        execution_id="exec-1",
        campaign_started_at=(now - timedelta(hours=1)).isoformat(),
        states={"hh.ru": "pending_search"},
        scan={"sources": [{"source": "hh.ru", "status": "partial", "items": 4, "errors": []}]},
        live={}, stops={}, queue={"hh.ru": {"ready": 2}},
        rows_by_source={"hh.ru": []},
    )
    assert report["new_confirmed"] == 1
    assert report["refusals"]["new"] == 1
    assert report["refusals"]["items"][0]["vacancy_id"] == "fresh"
    assert report["inbox"]["checked"] is False
    assert report["inbox"]["status"] == "not_checked"
    assert report["ready_queue"] == 2


def test_repeated_status_sync_preserves_detection_time(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    monkeypatch.setattr("applypilot.storage.now", lambda: "2025-01-01T00:00:00+00:00")
    store.replace_negotiation_statuses([{"id": "old", "status": "discard"}])
    monkeypatch.setattr("applypilot.storage.now", lambda: "2025-01-03T00:00:00+00:00")
    store.replace_negotiation_statuses([{"id": "old", "status": "discard"}])
    row = store.negotiation_details()["old"]
    assert row["status_detected_at"] == "2025-01-01T00:00:00+00:00"
    assert row["fetched_at"] == "2025-01-03T00:00:00+00:00"
    store.replace_negotiation_statuses([{"id": "old", "status": "interview"}], complete=False)
    assert store.negotiation_details()["old"]["status_detected_at"] == "2025-01-03T00:00:00+00:00"


def test_old_status_and_stale_inbox_are_not_current_campaign_activity(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    monkeypatch.setattr("applypilot.storage.now", lambda: "2025-01-01T00:00:00+00:00")
    store.replace_negotiation_statuses([{"id": "old", "status": "discard"}])
    store.save_sync_snapshot("hh.ru/messages", "ok", account="default")
    monkeypatch.setattr("applypilot.storage.now", lambda: "2025-01-03T00:00:00+00:00")
    store.replace_negotiation_statuses([{"id": "old", "status": "discard"}])
    report = management_report(
        store, track="example", sources=["hh.ru"], accounts={"hh.ru": "default"},
        execution_id="", campaign_started_at="2025-01-02T00:00:00+00:00",
        states={}, scan={}, live={}, stops={}, queue={}, rows_by_source={},
    )
    assert report["refusals"]["new"] == 0
    assert report["inbox"]["checked"] is False
    assert report["inbox"]["status"] == "not_checked"
