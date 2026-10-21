import csv
from concurrent.futures import ThreadPoolExecutor

import pytest

from applypilot.storage import Store


def test_legacy_import_is_idempotent(tmp_path):
    path = tmp_path / "apply_log.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["vacancy_id", "name", "status", "timestamp"])
        writer.writeheader()
        writer.writerow({"vacancy_id": "1", "name": "A", "status": "timeout", "timestamp": "2026-01-01"})
    store = Store(tmp_path / "state.sqlite3")
    first = store.import_csv(path)
    second = store.import_csv(path)
    assert first.logical_rows == 1
    assert first.events_added == 1
    assert second.already_imported is True
    assert second.events_added == 0
    assert store.statuses()["1"] == "unknown"
    assert store.count()["unknown"] == 1
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_import_timeout_then_success_keeps_final_success(tmp_path):
    path = tmp_path / "apply_log.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["vacancy_id", "status", "timestamp"])
        writer.writeheader()
        writer.writerow({"vacancy_id": "1", "status": "timeout", "timestamp": "2026-01-01T00:00:00+00:00"})
        writer.writerow({"vacancy_id": "1", "status": "success", "timestamp": "2026-01-02T00:00:00+00:00"})
    store = Store(tmp_path / "state.sqlite3")
    store.import_csv(path)
    assert store.statuses()["1"] == "success"
    assert store.count()["success"] == 1


def test_reservation_uses_one_budget_unit_and_deduplicates(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    item = {"id": "1", "name": "A"}
    assert store.reserve(item, "run", per_run=1, per_day=5)[0]
    assert not store.reserve(item, "run", per_run=1, per_day=5)[0]
    assert not store.reserve({"id": "2", "name": "B"}, "run", per_run=1, per_day=5)[0]
    store.record(item, "success", run_id="run")
    assert store.statuses()["1"] == "success"


def test_manual_outcome_releases_transient_reservation_for_a_future_run(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    item = {"id": "1", "name": "A"}

    assert store.reserve(item, "first", per_run=1, per_day=2)[0]
    store.record(item, "needs_manual", run_id="first")

    assert store.reserve(item, "second", per_run=1, per_day=2)[0]


def test_missing_timestamp_import_is_idempotent(tmp_path):
    path = tmp_path / "apply_log.csv"
    path.write_text("vacancy_id,status\n1,timeout\n", encoding="utf-8")
    store = Store(tmp_path / "state.sqlite3")
    store.import_csv(path)
    store.import_csv(path)
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_blocked_ids_exclude_only_terminal_or_ambiguous_outcomes(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    for vacancy_id, status in {
        "success": "success", "already": "already_applied", "unknown": "unknown",
        "skipped": "skipped", "manual": "needs_manual", "failed": "failed_before_submit",
    }.items():
        store.record({"id": vacancy_id}, status)

    assert store.blocked_ids() == {"success", "already", "unknown"}


def test_run_items_keep_the_exact_dry_run_list(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    item = {"id": "1", "name": "A", "resume": "Resume"}
    store.start_run("dry", "account", "dry-run", tmp_path / "input.json", 10, [item])
    store.mark_run_item("dry", "1", "prepared", "would apply")
    store.finish_run("dry", "completed")

    summary = store.run_summary("dry")
    assert summary is not None
    assert summary["run"]["status"] == "completed"
    assert summary["counts"] == {"prepared": 1}


def test_negotiation_reconciles_unknown_without_replaying_the_run(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    items = [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]
    store.start_run("live", "account", "apply", tmp_path / "input.json", 2, items)
    store.record(items[0], "unknown", "submission not confirmed", "live", "account")
    store.finish_run("live", "stopped_unknown", "unknown result for vacancy 1")
    store.replace_negotiation_statuses(
        [{"vacancy_id": "1", "status": "not_viewed"}], "account"
    )

    assert store.reconcile_unknowns_from_negotiations("account") == ["1"]
    assert store.statuses("account")["1"] == "success"
    summary = store.run_summary("live")
    assert summary is not None
    assert summary["run"]["status"] == "stopped_reconciled"
    assert summary["counts"] == {"prepared": 1, "success": 1}
    assert store.reconcile_unknowns_from_negotiations("account") == []


def test_negotiation_snapshot_isolated_by_account_and_complete_empty_clears_only_owner(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.replace_negotiation_statuses(
        [{"vacancy_id": "same", "status": "not_viewed"}], "first", complete=True
    )
    store.replace_negotiation_statuses(
        [{"vacancy_id": "same", "status": "viewed"}], "second", complete=True
    )

    store.replace_negotiation_statuses([], "first", complete=True)

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT account,vacancy_id,status FROM negotiation_statuses ORDER BY account"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("second", "same", "viewed")]


def test_partial_negotiation_snapshot_preserves_rows_outside_fetched_page(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.replace_negotiation_statuses(
        [
            {"vacancy_id": "one", "status": "not_viewed"},
            {"vacancy_id": "two", "status": "viewed"},
        ],
        "account",
        complete=True,
    )

    store.replace_negotiation_statuses(
        [{"vacancy_id": "one", "status": "invitation"}], "account", complete=False
    )

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT vacancy_id,status FROM negotiation_statuses "
            "WHERE account=? ORDER BY vacancy_id",
            ("account",),
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("one", "invitation"),
        ("two", "viewed"),
    ]


def test_reconciliation_requires_known_fresh_negotiation_evidence(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.record({"id": "stale"}, "unknown", run_id="run", account="account")
    store.record({"id": "unrecognized"}, "unknown", run_id="run", account="account")
    store.replace_negotiation_statuses(
        [
            {"vacancy_id": "stale", "status": "not_viewed"},
            {"vacancy_id": "unrecognized", "status": "mystery"},
        ],
        "account",
        complete=True,
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE negotiation_statuses SET fetched_at='2000-01-01T00:00:00+00:00' "
            "WHERE account=? AND vacancy_id=?",
            ("account", "stale"),
        )

    assert store.reconcile_unknowns_from_negotiations("account") == []
    assert store.statuses("account") == {"stale": "unknown", "unrecognized": "unknown"}


def test_recover_interrupted_runs_requires_run_lock_and_is_idempotent(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    item = {"id": "one", "name": "A"}
    store.start_run("abandoned", "account", "apply", tmp_path / "input.json", 1, [item])
    assert store.reserve(item, "abandoned", per_run=1, per_day=1, account="account")[0]

    with pytest.raises(RuntimeError, match="run lock"):
        store.recover_interrupted_runs("account")

    with store.run_lock():
        assert store.recover_interrupted_runs("account") == ["one"]
    summary = store.run_summary("abandoned")
    assert summary is not None
    assert summary["run"]["status"] != "running"
    assert "interrupted" in summary["run"]["stop_reason"]
    assert summary["counts"] == {"unknown": 1}
    assert store.statuses("account")["one"] == "unknown"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE account=? AND vacancy_id=? AND status='unknown'",
            ("account", "one"),
        ).fetchone()[0] == 1

    with store.run_lock():
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(store.recover_interrupted_runs, "account")
            with pytest.raises(RuntimeError, match="run lock"):
                future.result()
        assert store.recover_interrupted_runs("account") == []
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def _campaign_item(vacancy_id, *, title="AI Engineer", description="AI", score=90):
    return {
        "id": vacancy_id, "name": title, "company": "Example", "url": f"https://hh.ru/vacancy/{vacancy_id}",
        "description": description, "description_status": "ok", "score": score,
        "score_decision": "pass", "filter_status": "pass", "resume": "AI Agent Engineer",
        "reasons": ["keyword:ai"], "hard_killers": [], "soft_killers": [], "filter_reasons": [],
    }


def _save_campaign(store, campaign_id, track, items, status="ok"):
    previous = store.start_campaign(campaign_id, "account", track, {"query": "AI"})
    result = store.save_campaign_items(campaign_id, "account", track, items, status=status,
                                       snapshot_path=f"{campaign_id}.json")
    return previous, result


def test_campaign_dedupe_history_and_user_decisions_survive_repeat_scan(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    previous, first = _save_campaign(store, "first", "ai", [
        _campaign_item("one"), _campaign_item("two", title="LLM Engineer"),
    ])
    assert previous == ""
    assert first["first_seen"] == 2
    store.set_review_state("account", "hh.ru", "one", "APPROVED", reason="strong match",
                           notes="confirm salary", track="ai")

    previous, second = _save_campaign(store, "second", "ai", [_campaign_item("one")])
    assert previous == "first"
    assert second["known"] == 1

    previous, third = _save_campaign(store, "third", "ai", [
        _campaign_item("one"), _campaign_item("two", title="LLM Engineer"),
    ])
    assert previous == "second"
    assert third["known"] == 1
    assert third["new_since_previous"] == 1
    rows = {row["id"]: row for row in store.list_campaign_vacancies("account", "ai", limit=10)}
    assert rows["one"]["review_state"] == "APPROVED"
    assert rows["one"]["review_notes"] == "confirm salary"
    assert len({row["id"] for row in rows.values()}) == 2


def test_partial_campaign_does_not_become_comparison_baseline_or_delete_cards(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    _save_campaign(store, "full", "ai", [_campaign_item("one")])
    _save_campaign(store, "partial", "ai", [_campaign_item("two")], status="partial")
    previous, third = _save_campaign(store, "third", "ai", [_campaign_item("one")])

    assert previous == "full"
    assert third["known"] == 1
    assert {row["id"] for row in store.list_campaign_vacancies("account", "ai", limit=10)} == {"one", "two"}


def test_review_decision_is_scoped_to_track(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    item = _campaign_item("one")
    _save_campaign(store, "ai-run", "ai", [item])
    _save_campaign(store, "backend-run", "backend", [item])

    store.set_review_state("account", "hh.ru", "one", "APPROVED", reason="AI match", track="ai")
    store.set_review_state("account", "hh.ru", "one", "SKIP", reason="wrong backend", track="backend")

    ai = store.list_campaign_vacancies("account", "ai", "all", 10)[0]
    backend = store.list_campaign_vacancies("account", "backend", "all", 10)[0]
    assert ai["review_state"] == "APPROVED"
    assert backend["review_state"] == "SKIP"
    assert store.list_campaign_vacancies("account", "ai", "approved", 10)[0]["id"] == "one"
    assert store.list_campaign_vacancies("account", "backend", "approved", 10) == []


def test_application_state_and_review_state_are_separate_and_letter_task_required(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    _save_campaign(store, "first", "ai", [_campaign_item("one")])
    store.set_review_state("account", "hh.ru", "one", "APPROVED", reason="approved", track="ai")
    store.record({**_campaign_item("one"), "track": "ai"}, "needs_manual",
                 "required cover letter is missing", account="account")
    row = store.list_campaign_vacancies("account", "ai", "all", 10)[0]
    task = store.workflow_tasks("account", "letter")[0]

    assert row["review_state"] == "APPROVED"
    assert row["application_state"] == "NEEDS_LETTER"
    assert task["status"] == "NEEDS_MANUAL"
    assert store.approved_campaign_items("account", "ai") == []

    task_id = store.create_workflow_task("account", "hh.ru", "one", "ai", "letter", "DRAFT",
                                         payload={"source": "template"}, draft_text="Здравствуйте")
    store.update_workflow_task(task_id, "account", draft_text="Проверенное письмо", action="approve")
    selected = store.approved_campaign_items("account", "ai")
    assert selected[0]["approved_cover_letter"] == "Проверенное письмо"


def test_form_task_cannot_be_approved_without_question_capture(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    task_id = store.create_workflow_task("account", "hh.ru", "one", "ai", "form", "NEEDS_MANUAL",
                                         payload={"reason": "questions are not captured"})

    with pytest.raises(ValueError, match="question capture"):
        store.update_workflow_task(task_id, "account", action="approve")


def test_letter_preview_does_not_overwrite_saved_or_approved_text(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    task_id = store.create_workflow_task("account", "hh.ru", "one", "ai", "letter", "DRAFT",
                                         payload={"source": "template"}, draft_text="Черновик владельца")
    store.create_workflow_task("account", "hh.ru", "one", "ai", "letter", "DRAFT",
                               payload={"source": "generated"}, draft_text="Новый автоматический текст")
    assert store.workflow_task(task_id, "account")["draft_text"] == "Черновик владельца"

    store.update_workflow_task(task_id, "account", action="approve")
    store.create_workflow_task("account", "hh.ru", "one", "ai", "letter", "DRAFT",
                               payload={"source": "generated"}, draft_text="Заменяющий текст")
    task = store.workflow_task(task_id, "account")
    assert task["status"] == "APPROVED"
    assert task["draft_text"] == "Черновик владельца"


def test_confirmed_apply_marks_approved_letter_task_sent(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    _save_campaign(store, "first", "ai", [_campaign_item("one")])
    task_id = store.create_workflow_task("account", "hh.ru", "one", "ai", "letter", "DRAFT",
                                         draft_text="Одобренный текст")
    store.update_workflow_task(task_id, "account", action="approve")

    store.record({**_campaign_item("one"), "track": "ai", "cover_letter": "Одобренный текст"},
                 "success", "HH confirmed", account="account")

    assert store.workflow_task(task_id, "account")["status"] == "SENT_CONFIRMED"


def test_approved_optional_letter_is_attached_to_ready_campaign_item(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    _save_campaign(store, "first", "ai", [_campaign_item("one")])
    store.set_review_state("account", "hh.ru", "one", "APPROVED", reason="approved", track="ai")
    task_id = store.create_workflow_task("account", "hh.ru", "one", "ai", "letter", "DRAFT",
                                         draft_text="Проверенное письмо")
    store.update_workflow_task(task_id, "account", action="approve")

    selected = store.approved_campaign_items("account", "ai")
    assert selected[0]["approved_cover_letter"] == "Проверенное письмо"


def test_new_tables_are_added_to_older_store_without_dropping_legacy_columns(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sync_snapshots(id INTEGER PRIMARY KEY,source TEXT,status TEXT,item_count INTEGER,error TEXT,created_at TEXT)")
    conn.execute("CREATE TABLE negotiation_statuses(account TEXT,vacancy_id TEXT,status TEXT,name TEXT,company TEXT,updated_at TEXT,fetched_at TEXT,PRIMARY KEY(account,vacancy_id))")
    conn.commit()
    conn.close()

    store = Store(path)
    with store.connect() as migrated:
        assert "account" in {row["name"] for row in migrated.execute("PRAGMA table_info(sync_snapshots)")}
        assert "resume" in {row["name"] for row in migrated.execute("PRAGMA table_info(negotiation_statuses)")}
        tables = {row["name"] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"campaign_runs", "vacancies", "workflow_tasks", "inbox_messages"} <= tables


def test_legacy_global_review_state_migrates_to_existing_track_rows(tmp_path):
    import sqlite3

    path = tmp_path / "legacy-reviews.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE vacancies (
            account TEXT, source TEXT, vacancy_id TEXT, review_state TEXT,
            review_reason TEXT, review_notes TEXT, application_state TEXT, updated_at TEXT
        );
        CREATE TABLE vacancy_track_scores (
            account TEXT, source TEXT, vacancy_id TEXT, track TEXT, score INTEGER,
            score_decision TEXT, filter_status TEXT, reasons_json TEXT, problems_json TEXT,
            filter_reasons_json TEXT, resume TEXT, campaign_id TEXT, latest_change TEXT,
            last_seen_at TEXT, PRIMARY KEY(account,source,vacancy_id,track)
        );
        INSERT INTO vacancies VALUES('account','hh.ru','one','APPROVED','reviewed','notes','READY_TO_APPLY','now');
        INSERT INTO vacancy_track_scores VALUES(
            'account','hh.ru','one','ai',40,'pass','pass','[]','[]','[]','AI','run','first_seen','now'
        );
    """)
    conn.commit()
    conn.close()

    store = Store(path)
    with store.connect() as migrated:
        score = migrated.execute(
            "SELECT review_state,review_reason,review_notes FROM vacancy_track_scores WHERE track='ai'"
        ).fetchone()
        vacancy = migrated.execute("SELECT review_state FROM vacancies WHERE vacancy_id='one'").fetchone()
    assert tuple(score) == ("APPROVED", "reviewed", "notes")
    assert vacancy[0] == "UNREVIEWED"


def test_first_campaign_distinguishes_previously_seen_registry_items_from_new_campaign_hits(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.start_campaign("first", "account", "ai", {"query": "AI"})
    older = {
        "id": "legacy", "name": "AI Engineer", "first_seen": "2026-09-01",
        "is_new": False, "score_decision": "reject", "filter_status": "unknown",
        "description": "AI work", "problems": ["salary:unpublished"],
    }
    recent = {
        "id": "recent", "name": "LLM Engineer", "first_seen": "2026-10-21",
        "is_new": True, "score_decision": "pass", "filter_status": "pass",
        "description": "RAG and agents", "problems": ["salary:lower_bound_meets_threshold"],
    }
    summary = store.save_campaign_items(
        "first", "account", "ai", [older, recent], status="ok", snapshot_path="first.json",
    )
    assert summary["items"] == 2
    assert summary["known"] == 1
    assert summary["first_seen"] == 1
    assert summary["new_since_previous"] == 0
    assert summary["matched"] == 1
    assert summary["maybe"] == 0
    assert summary["rejected"] == 1
    rows = {x["id"]: x for x in store.list_campaign_vacancies("account", "ai")}
    assert "salary:unpublished" in rows["legacy"]["problems"]


def test_campaign_fit_buckets_are_exclusive_even_when_filter_is_unknown():
    from applypilot.storage import campaign_fit_bucket

    assert campaign_fit_bucket("pass", "pass") == "fit"
    assert campaign_fit_bucket("unknown", "pass") == "maybe"
    assert campaign_fit_bucket("pass", "unknown") == "maybe"
    assert campaign_fit_bucket("unknown", "reject") == "skip"
    assert campaign_fit_bucket("reject", "unknown") == "skip"


def test_top_review_queue_excludes_confirmed_or_unknown_application_states(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    account = "account"
    store.start_campaign("batch", account, "ai", {"query": "AI"})
    rows = [
        {"id": label, "name": f"{label} AI Engineer", "score": 80,
         "score_decision": "pass", "filter_status": "pass",
         "url": f"https://hh.ru/vacancy/{label}"}
        for label in ("available", "sent", "unknown")
    ]
    store.save_campaign_items("batch", account, "ai", rows, status="ok", snapshot_path="one.json")
    store.record(rows[1], "success", account=account)
    store.record(rows[2], "unknown", account=account)
    top = store.list_campaign_vacancies(account, "ai", view="top")
    assert [row["id"] for row in top] == ["available"]
    # Их история не теряется, меняется только рабочий вид для выбора откликов.
    assert len(store.list_campaign_vacancies(account, "ai", view="all")) == 3


def test_batch_review_is_atomic_and_blocks_already_applied(tmp_path):
    from applypilot.storage import Store

    store = Store(tmp_path / "state.sqlite3")
    store.start_campaign("bulk1", "account", "ai", {"query": "AI"})
    items = [
        {"id": str(i), "name": "AI Agent Engineer", "score": 75,
         "score_decision": "pass", "filter_status": "pass", "resume": "AI Engineer",
         "url": f"https://hh.ru/vacancy/{i}"}
        for i in (101, 102, 103)
    ]
    store.save_campaign_items("bulk1", "account", "ai", items, status="ok", snapshot_path="x.json")
    store.record(items[-1], "success", account="account")
    import pytest

    with pytest.raises(ValueError, match="cannot batch approve"):
        store.set_review_state_batch(
            "account", "hh.ru", ["101", "103"], "APPROVED", reason="operator batch", track="ai",
        )
    rows = {x["id"]: x for x in store.list_campaign_vacancies("account", "ai")}
    assert rows["101"]["review_state"] == "UNREVIEWED"
    assert rows["102"]["review_state"] == "UNREVIEWED"
    assert rows["103"]["application_state"] == "APPLIED_CONFIRMED"

    assert store.set_review_state_batch(
        "account", "hh.ru", ["101", "102", "101"], "APPROVED",
        reason="operator batch", track="ai",
    ) == 2
    rows = {x["id"]: x for x in store.list_campaign_vacancies("account", "ai")}
    assert rows["101"]["review_state"] == "APPROVED"
    assert rows["102"]["application_state"] == "READY_TO_APPLY"
    assert [x["id"] for x in store.approved_campaign_items("account", "ai")] == ["101", "102"]


def test_batch_review_rejects_bad_fit_and_wrong_track(tmp_path):
    from applypilot.storage import Store

    store = Store(tmp_path / "state.sqlite3")
    store.start_campaign("bulk2", "account", "ai", {"query": "LLM"})
    store.save_campaign_items(
        "bulk2", "account", "ai",
        [{"id": "bad", "name": "Java Backend", "score": 91, "score_decision": "reject",
          "filter_status": "pass", "resume": "AI Engineer"}],
        status="ok", snapshot_path="x.json",
    )
    import pytest

    with pytest.raises(ValueError, match="screening"):
        store.set_review_state_batch(
            "account", "hh.ru", ["bad"], "APPROVED", reason="mistaken", track="ai",
        )
    with pytest.raises(ValueError, match="not present"):
        store.set_review_state_batch(
            "account", "hh.ru", ["bad"], "APPROVED", reason="mistaken", track="backend",
        )


def test_hh_reconciliation_updates_persistent_campaign_card(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    item = {"id": "hh-confirmed", "name": "AI Engineer", "resume": "AI", "score": 80,
            "score_decision": "pass", "filter_status": "pass"}
    store.start_campaign("case", "account", "ai", {"query": "AI"})
    store.save_campaign_items("case", "account", "ai", [item], status="ok",
                              snapshot_path="case.json")
    store.record(item, "unknown", "HH response not proven", account="account")
    before = store.list_campaign_vacancies("account", "ai", "unknown", 10)
    assert [x["id"] for x in before] == ["hh-confirmed"]
    store.replace_negotiation_statuses(
        [{"vacancy_id": "hh-confirmed", "status": "not_viewed"}], "account"
    )
    assert store.reconcile_unknowns_from_negotiations("account") == ["hh-confirmed"]
    after = store.list_campaign_vacancies("account", "ai", "applied", 10)
    assert [x["id"] for x in after] == ["hh-confirmed"]
    assert after[0]["application_state"] == "APPLIED_CONFIRMED"
    assert store.list_campaign_vacancies("account", "ai", "unknown", 10) == []
