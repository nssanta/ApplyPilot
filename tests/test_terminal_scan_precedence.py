"""Historical HH negotiation receipts must survive later read-only scans."""
from __future__ import annotations

from pathlib import Path

from applypilot.storage import Store


def test_scan_refresh_cannot_downgrade_hh_verified_application_to_needs_form(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "receipts.sqlite3")
    item = {
        "id": "12345", "name": "Python AI backend engineer", "company": "Example",
        "description": "Production Python LLM agent RAG and FastAPI services.",
        "description_status": "ok", "score": 85,
        "score_decision": "pass", "filter_status": "pass", "problems": [],
    }
    store.start_campaign("first", "acct", "ai", {}, source="hh.ru")
    store.save_campaign_items(
        "first", "acct", "ai", [item], status="ok",
        snapshot_path="first.json", source="hh.ru",
    )
    store.record(item, "needs_manual", "screening form required", account="acct")
    assert store.list_campaign_vacancies("acct", "ai", "all", None)[0][
        "application_state"
    ] == "NEEDS_FORM"

    store.replace_negotiation_statuses(
        [{"vacancy_id": "12345", "status": "not_viewed"}],
        account="acct", complete=True,
    )
    assert store.list_campaign_vacancies("acct", "ai", "all", None)[0][
        "application_state"
    ] == "APPLIED_CONFIRMED"

    store.start_campaign("second", "acct", "ai", {}, source="hh.ru")
    store.save_campaign_items(
        "second", "acct", "ai", [item], status="ok",
        snapshot_path="second.json", source="hh.ru",
    )
    assert store.list_campaign_vacancies("acct", "ai", "all", None)[0][
        "application_state"
    ] == "APPLIED_CONFIRMED"
