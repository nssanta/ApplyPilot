"""Manual/archived outcomes cannot silently become READY again."""

from pathlib import Path

from applypilot.storage import Store, campaign_application_state


def test_generic_manual_result_enters_non_dispatchable_workflow(tmp_path: Path) -> None:
    assert campaign_application_state("needs_manual", "application button not found") == "NEEDS_MANUAL"
    assert campaign_application_state("needs_manual", "Getmatch vacancy is archived") == "NEEDS_MANUAL"
    store = Store(tmp_path / "state.sqlite3")
    item = {
        "id": "3113", "name": "Backend Developer Python", "company": "X",
        "description": "Python API integrations", "description_status": "ok",
        "score": 85, "score_decision": "pass", "filter_status": "pass",
        "problems": [], "resume": "AI Engineer",
    }
    store.start_campaign("run1", "account", "ai", {}, source="getmatch.ru")
    store.save_campaign_items(
        "run1", "account", "ai", [item], status="ok",
        snapshot_path="snapshot.json", source="getmatch.ru",
    )
    store.set_review_state(
        "account", "getmatch.ru", "3113", "APPROVED", reason="approved", track="ai",
    )
    assert store.record_source_application(
        "account", "getmatch.ru", "3113", "ai", "getmatch.ru:3113", "submitting",
    )
    assert store.record_source_application(
        "account", "getmatch.ru", "3113", "ai", "getmatch.ru:3113", "needs_manual",
        note="Getmatch vacancy is archived",
    )
    with store.connect() as conn:
        raw = conn.execute(
            "SELECT application_state FROM vacancies WHERE account=? AND source=? AND vacancy_id=?",
            ("account", "getmatch.ru", "3113"),
        ).fetchone()
    assert raw["application_state"] == "NEEDS_MANUAL"
    assert store.list_campaign_vacancies("account", "ai", "ready", 10, "getmatch.ru") == []

    # A later scan must not turn a previously blocked manual outcome back into READY.
    store.start_campaign("run2", "account", "ai", {}, source="getmatch.ru")
    store.save_campaign_items(
        "run2", "account", "ai", [item], status="ok",
        snapshot_path="snapshot2.json", source="getmatch.ru",
    )
    with store.connect() as conn:
        raw = conn.execute(
            "SELECT application_state FROM vacancies WHERE account=? AND source=? AND vacancy_id=?",
            ("account", "getmatch.ru", "3113"),
        ).fetchone()
    assert raw["application_state"] == "NEEDS_MANUAL"
    assert store.list_campaign_vacancies("account", "ai", "ready", 10, "getmatch.ru") == []
