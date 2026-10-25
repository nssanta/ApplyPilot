from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from applypilot.storage import Store


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "applypilot.sqlite")


def test_one_ats_channel_has_one_writer_across_sources(tmp_path):
    store = _store(tmp_path)
    channel = "ats:jobs.example.com/apply/42"
    assert store.reserve_source_application("acc", "hh.ru", "1", "ai_core", channel, "run-1")[0]
    ok, message = store.reserve_source_application(
        "acc", "career.habr.com", "2", "ai_core", channel, "run-2",
    )
    assert ok is False
    assert "reserved" in message or "channel" in message
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM channel_reservations WHERE account=? AND channel_key=?",
            ("acc", channel),
        ).fetchone()[0] == 1


def test_native_channels_remain_independent(tmp_path):
    store = _store(tmp_path)
    assert store.reserve_source_application("acc", "hh.ru", "1", "ai_core", "hh.ru:1", "run-1")[0]
    assert store.reserve_source_application(
        "acc", "career.habr.com", "1", "ai_core", "career.habr.com:1", "run-1",
    )[0]


def test_two_accounts_do_not_share_a_channel_lock(tmp_path):
    store = _store(tmp_path)
    channel = "ats:jobs.example.com/apply/7"
    assert store.reserve_source_application("a", "hh.ru", "7", "ai_core", channel, "run-a")[0]
    assert store.reserve_source_application("b", "hh.ru", "7", "ai_core", channel, "run-b")[0]


def test_submitting_unknown_and_evidence_backed_reconciliation(tmp_path):
    store = _store(tmp_path)
    channel = "career.habr.com:100"
    assert store.reserve_source_application(
        "acc", "career.habr.com", "100", "ai_core", channel, "run-1",
    )[0]
    assert store.record_source_application(
        "acc", "career.habr.com", "100", "ai_core", channel, "submitting", run_id="run-1",
    )
    assert store.record_source_application(
        "acc", "career.habr.com", "100", "ai_core", channel, "unknown",
        note="form result was not observable", run_id="run-1",
    )
    # Unknown is a quarantine: no direct promotion and no automatic retry.
    assert store.record_source_application(
        "acc", "career.habr.com", "100", "ai_core", channel, "success",
    ) is False
    assert store.reconcile_source_application(
        "acc", "career.habr.com", "100", "ai_core", channel, "",
    ) is False
    assert store.reconcile_source_application(
        "acc", "career.habr.com", "100", "ai_core", channel,
        "provider response page shows the application in history",
    )
    attempts = store.source_attempts("acc", "career.habr.com")
    assert attempts[0]["status"] == "success"
    assert attempts[0]["note"].startswith("reconciled:")
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM channel_reservations WHERE account=? AND channel_key=?",
            ("acc", channel),
        ).fetchone()[0] == 0


def test_success_is_immutable(tmp_path):
    store = _store(tmp_path)
    channel = "hh.ru:55"
    assert store.record_source_application("acc", "hh.ru", "55", "ai_core", channel, "submitting")
    assert store.record_source_application("acc", "hh.ru", "55", "ai_core", channel, "success")
    assert store.record_source_application("acc", "hh.ru", "55", "ai_core", channel, "unknown") is False
    assert store.record_source_application(
        "acc", "hh.ru", "55", "ai_core", channel, "failed_before_submit",
    ) is False
    assert store.source_attempts("acc", "hh.ru")[0]["status"] == "success"


def test_stale_submitting_reservation_is_quarantined_not_retried(tmp_path):
    store = _store(tmp_path)
    channel = "ats:jobs.example.com/apply/9"
    assert store.reserve_source_application("acc", "hh.ru", "9", "ai_core", channel, "run-9")[0]
    assert store.record_source_application(
        "acc", "hh.ru", "9", "ai_core", channel, "submitting", run_id="run-9",
    )
    with store.connect() as conn:
        conn.execute("UPDATE source_reservations SET created_at='2000-01-01T00:00:00+00:00'")
        conn.execute("UPDATE source_application_attempts SET updated_at='2000-01-01T00:00:00+00:00'")
    recovered = store.recover_stale_source_reservations(max_age_seconds=1, account="acc")
    assert recovered
    attempts = store.source_attempts("acc", "hh.ru")
    assert attempts[0]["status"] == "unknown"
    assert "quarantined" in attempts[0]["note"]
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM channel_reservations WHERE account=? AND channel_key=?",
            ("acc", channel),
        ).fetchone()[0] == 0


def test_planning_conflict_is_recomputed_not_taken_from_stale_link(tmp_path):
    store = _store(tmp_path)
    item = {
        "id": "10", "name": "AI Engineer", "company": "Example",
        "application_channel": "ats:jobs.example.com/apply/10",
        "application_channel_type": "external_ats",
    }
    store.save_vacancy_links("acc", "career.habr.com", [item])
    link = store.vacancy_link("acc", "career.habr.com", "10")
    assert link["duplicate_of_source"] == ""
    assert store.record_source_application(
        "acc", "hh.ru", "99", "ai_core", "ats:jobs.example.com/apply/10", "submitting",
    )
    conflict = store.channel_conflict(
        "acc", "career.habr.com", "10", "ats:jobs.example.com/apply/10",
    )
    assert conflict is not None
    assert conflict["kind"] == "applied_or_quarantined"
    assert conflict["source"] == "hh.ru"


def test_source_limits_count_unknown_and_are_scoped_to_account_and_source(tmp_path):
    store = _store(tmp_path)
    assert store.reserve_source_application(
        "acc", "career.habr.com", "100", "ai_core", "career.habr.com:100", "run-1",
        per_run=10, per_day=1,
    )[0]
    assert store.record_source_application(
        "acc", "career.habr.com", "100", "ai_core", "career.habr.com:100",
        "submitting", run_id="run-1",
    )
    assert store.record_source_application(
        "acc", "career.habr.com", "100", "ai_core", "career.habr.com:100",
        "unknown", run_id="run-1",
    )

    blocked, reason = store.reserve_source_application(
        "acc", "career.habr.com", "101", "ai_core", "career.habr.com:101", "run-2",
        per_run=10, per_day=1,
    )
    assert not blocked
    assert reason.startswith("source per-day limit reached")

    # The same profile limit is independent for another source and account.
    assert store.reserve_source_application(
        "acc", "getmatch.ru", "100", "ai_core", "getmatch.ru:100", "run-1",
        per_run=10, per_day=1,
    )[0]
    assert store.reserve_source_application(
        "other-account", "career.habr.com", "100", "ai_core", "career.habr.com:100", "run-1",
        per_run=10, per_day=1,
    )[0]


def test_source_run_cap_counts_active_reservations_and_is_atomic(tmp_path):
    store = _store(tmp_path)

    def reserve(index: int) -> bool:
        ok, _reason = store.reserve_source_application(
            "acc", "getmatch.ru", str(index), "ai_core", f"getmatch.ru:{index}", "run-1",
            per_run=5, per_day=100,
        )
        return ok

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(reserve, range(20)))

    assert sum(results) == 5
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM source_reservations WHERE account=? AND source=?",
            ("acc", "getmatch.ru"),
        ).fetchone()[0] == 5
