"""Yandex Career safe-only milestone: no application POSTs.

The survey iframe is allowed to load through Yandex's read-only getSurvey
endpoint; all actual submission endpoints remain outside the allowlist.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from applypilot.dispatch import verified_success_receipt
from applypilot.yandex_apply import (
    EXPECTED_SURVEY_FIELDS,
    canonical_job_id,
    inspect_yandex_page,
    is_survey_read_request,
)


def test_yandex_receipt_requires_success_page_and_applied_state() -> None:
    base = {
        "provider": "yandex.ru/jobs", "vacancy_id": "48171", "publication_id": "48171",
        "success_url": "https://forms.yandex.ru/surveys/1/success/?publication_id=48171",
        "success_path": "/surveys/1/success/", "success_marker": "success_path",
        "success_verified": True, "applied_state_verified": True,
    }
    assert verified_success_receipt("yandex.ru/jobs", "48171", base) is True
    assert verified_success_receipt(
        "yandex.ru/jobs", "48171", {**base, "applied_state_verified": False},
    ) is False
    assert verified_success_receipt(
        "yandex.ru/jobs", "48171", {**base, "publication_id": "99999"},
    ) is False
    assert verified_success_receipt("yandex.ru/jobs", "99999", base) is False


@pytest.mark.parametrize("url,expected", [
    ("https://yandex.ru/jobs/vacancies/48876", "48876"),
    ("https://yandex.ru/jobs/vacancies/role-name-48876", "48876"),
    ("https://yandex.ru/jobs/vacancies/role-name-48876/", "48876"),
    ("https://yandex.ru/jobs/vacancies/role-no-number", ""),
    ("https://passport.yandex.ru/jobs/vacancies/48876", ""),
    ("http://yandex.ru/jobs/vacancies/48876", ""),
    ("https://yandex.ru/jobs/profile", ""),
])
def test_yandex_exact_job_identity(url: str, expected: str) -> None:
    assert canonical_job_id(url) == expected


@pytest.mark.parametrize("method,url,expected", [
    ("POST", "https://forms.yandex.ru/gateway/root/form/getSurvey", True),
    ("post", "https://forms.yandex.ru/gateway/root/form/getSurvey", True),
    ("POST", "https://forms.yandex.ru/gateway/root/form/submit", False),
    ("POST", "https://forms.yandex.ru/gateway/root/form/sendAnswers", False),
    ("POST", "https://evil.example/gateway/root/form/getSurvey", False),
    ("POST", "http://forms.yandex.ru/gateway/root/form/getSurvey", False),
    ("PUT", "https://forms.yandex.ru/gateway/root/form/getSurvey", False),
])
def test_yandex_only_survey_fetch_post_is_whitelisted(
    method: str, url: str, expected: bool,
) -> None:
    request = SimpleNamespace(method=method, url=url)
    assert is_survey_read_request(request) is expected


def test_yandex_form_schema_has_only_expected_candidate_inputs() -> None:
    assert EXPECTED_SURVEY_FIELDS == {
        "cover_letter", "agree_considiration", "agree_career", "agree_events",
    }


def test_yandex_never_loads_already_applied_or_wrong_url() -> None:
    class NoNavigation:
        def goto(self, *args, **kwargs):
            raise AssertionError("Browser must not navigate for a blocked candidate")

    row = {
        "id": "48876", "url": "https://yandex.ru/jobs/vacancies/48876",
        "name": "LLM Engineer", "application_state": "UNKNOWN",
    }
    result = inspect_yandex_page(NoNavigation(), row)
    assert result["status"] == "already_applied_or_quarantined"
    assert result["submitted"] is False
    result = inspect_yandex_page(
        NoNavigation(), {**row, "application_state": "NOT_STARTED",
                         "url": "https://evil.example/jobs/vacancies/48876"},
    )
    assert result["status"] == "invalid_vacancy_url"
    assert result["submitted"] is False


def test_yandex_archived_job_is_blocked_before_opening_form() -> None:
    class Body:
        def inner_text(self, timeout=0):
            return "Эта вакансия в архиве, но можно посмотреть похожие"

    class FakePage:
        url = "https://yandex.ru/jobs/vacancies/llm-engineer-48555"
        def goto(self, url, **kwargs):
            return SimpleNamespace(status=200)
        def locator(self, selector):
            assert selector == "body", "Archived job must not inspect/submit its form"
            return Body()

    row = {
        "id": "48555", "url": "https://yandex.ru/jobs/vacancies/48555",
        "name": "LLM Engineer", "application_state": "NOT_STARTED",
    }
    result = inspect_yandex_page(FakePage(), row)
    assert result["status"] == "archived"
    assert result["submitted"] is False
