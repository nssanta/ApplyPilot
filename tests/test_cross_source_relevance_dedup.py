"""Cross-platform duplicate preflight: confirmed HH/Habr mirror with employer alias."""
from __future__ import annotations

from pathlib import Path

from applypilot.multisource import queue_plan
from applypilot.storage import Store

TITLE = "AI-инженер / Fullstack-разработчик AI-решений"
AI_DESCRIPTION = " ".join(
    f"Создаем AI агентов с Python FastAPI RAG MCP LangGraph, production API {i}."
    for i in range(25)
)
OTHER_DESCRIPTION = " ".join(
    f"Разрабатываем Oracle Java ERP банковские платежи Kafka BPMN Java {i}."
    for i in range(25)
)


def _save(store: Store, source: str, account: str, vacancy_id: str,
          description: str, company: str) -> None:
    run = f"run-{vacancy_id}"
    store.start_campaign(run, account, "ai", {}, source=source)
    store.save_campaign_items(
        run, account, "ai",
        [{
            "id": vacancy_id, "name": TITLE, "company": company,
            "description": description, "description_status": "ok",
            "score": 90, "score_decision": "pass", "filter_status": "pass",
            "problems": [], "resume": "AI Developer",
        }],
        status="ok", snapshot_path="fixture.json", source=source,
    )


def test_habr_conflict_with_confirmed_hh_under_different_company_alias(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    _save(store, "hh.ru", "hh-account", "hh-1", AI_DESCRIPTION, "Example Labs")
    assert store.record_manual_application("hh-account", "hh.ru", "hh-1")

    _save(store, "career.habr.com", "habr-account", "habr-1",
          AI_DESCRIPTION + " Дополнительные обязанности.", "Example Labs LLC")
    store.set_review_state(
        "habr-account", "career.habr.com", "habr-1", "APPROVED",
        reason="approved", track="ai",
    )
    match = store.cross_source_attempt_conflict(
        "career.habr.com", "habr-1", TITLE, AI_DESCRIPTION,
    )
    assert match and match["source"] == "hh.ru" and match["vacancy_id"] == "hh-1"
    assert match["description_overlap"] >= 0.88

    plan = queue_plan(store, "habr-account", "ai", "career.habr.com", verified_driver=True)
    assert plan["planned_count"] == 0
    assert any(
        item["reason"] == "cross_source_duplicate:hh.ru:hh-1"
        for item in plan["blocked"]
    )


def test_hh_clicker_snapshot_omits_confirmed_habr_duplicate(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    _save(store, "career.habr.com", "habr-account", "habr-1", AI_DESCRIPTION, "Example Labs LLC")
    assert store.record_manual_application("habr-account", "career.habr.com", "habr-1")
    _save(store, "hh.ru", "hh-account", "hh-1", AI_DESCRIPTION, "Example Labs")
    store.set_review_state(
        "hh-account", "hh.ru", "hh-1", "APPROVED", reason="approved", track="ai",
    )
    assert store.approved_campaign_items("hh-account", "ai") == []


def test_same_title_with_different_description_is_not_a_duplicate(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    _save(store, "hh.ru", "hh-account", "hh-1", AI_DESCRIPTION, "Компания А")
    assert store.record_manual_application("hh-account", "hh.ru", "hh-1")
    assert store.cross_source_attempt_conflict(
        "career.habr.com", "habr-1", TITLE, OTHER_DESCRIPTION,
    ) is None



def test_hh_reposted_near_duplicate_with_new_id_is_not_sent_twice(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    _save(store, "hh.ru", "hh-account", "hh-original", AI_DESCRIPTION, "Example Studio")
    assert store.record_manual_application("hh-account", "hh.ru", "hh-original")
    _save(store, "hh.ru", "hh-account", "hh-repost", AI_DESCRIPTION, "EXAMPLE STUDIO")
    store.set_review_state(
        "hh-account", "hh.ru", "hh-repost", "APPROVED", reason="approved", track="ai",
    )
    match = store.cross_source_attempt_conflict(
        "hh.ru", "hh-repost", TITLE, AI_DESCRIPTION,
    )
    assert match and match["source"] == "hh.ru"
    assert match["vacancy_id"] == "hh-original"
    assert store.approved_campaign_items("hh-account", "ai") == []


def test_same_description_with_different_role_is_not_a_duplicate(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    _save(store, "hh.ru", "hh-account", "hh-1", AI_DESCRIPTION, "Example Labs")
    assert store.record_manual_application("hh-account", "hh.ru", "hh-1")
    assert store.cross_source_attempt_conflict(
        "career.habr.com", "habr-1", "Java backend engineer", AI_DESCRIPTION,
    ) is None


def test_unknown_attempt_also_blocks_a_reposted_near_duplicate(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    _save(store, "hh.ru", "hh-account", "hh-original", AI_DESCRIPTION, "Example Studio")
    store.record(
        {"id": "hh-original", "name": TITLE, "company": "Example Studio"},
        "unknown", note="ambiguous submit", account="hh-account",
    )
    _save(store, "hh.ru", "hh-account", "hh-repost", AI_DESCRIPTION, "EXAMPLE STUDIO")
    store.set_review_state(
        "hh-account", "hh.ru", "hh-repost", "APPROVED", reason="approved", track="ai",
    )
    match = store.cross_source_attempt_conflict(
        "hh.ru", "hh-repost", TITLE, AI_DESCRIPTION,
    )
    assert match and match["vacancy_id"] == "hh-original"
    assert match["status"] == "UNKNOWN"
    assert store.approved_campaign_items("hh-account", "ai") == []
