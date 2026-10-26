from __future__ import annotations

from applypilot.dispatch import verified_success_receipt
from applypilot.getmatch_web import canonical_vacancy_id


def test_getmatch_web_vacancy_identity() -> None:
    assert canonical_vacancy_id(
        "https://getmatch.ru/vacancies/31434-llm-inzhener"
    ) == "31434"
    assert canonical_vacancy_id("https://getmatch.ru/vacancies/31434") == "31434"
    assert canonical_vacancy_id("https://evil.example/vacancies/31434") == ""


def test_getmatch_web_receipt_requires_post_and_history() -> None:
    receipt = {
        "provider": "getmatch.ru", "vacancy_id": "31434",
        "post_path": "/api/offers/31434/apply", "post_status": 200,
        "applications_verified": True,
    }
    assert verified_success_receipt("getmatch.ru", "31434", receipt) is True
    assert verified_success_receipt(
        "getmatch.ru", "31434", {**receipt, "applications_verified": False},
    ) is False
    assert verified_success_receipt(
        "getmatch.ru", "31434", {**receipt, "post_status": 500},
    ) is False
    assert verified_success_receipt(
        "getmatch.ru", "31434", {**receipt, "post_path": "/api/offers/999/apply"},
    ) is False
