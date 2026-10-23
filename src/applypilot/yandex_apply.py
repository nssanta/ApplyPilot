"""Yandex Career read-only application-form inspector.

Yandex Career is a first-party employer careers site, not an aggregator.
The apply UI opens a cross-origin iframe at forms.yandex.ru and loads a survey
by POST /gateway/root/form/getSurvey. That POST is read-only discovery,
NOT an application. The inspector requires the caller to deny every other
mutating HTTP request. No Yandex live submit path is claimed here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

SOURCE = "yandex.ru/jobs"
FORM_HOST = "forms.yandex.ru"
SURVEY_GET_PATH = "/gateway/root/form/getSurvey"
REQUIRED_CONSENT = "agree_considiration"
OPTIONAL_CONSENTS = {"agree_career", "agree_events"}
EXPECTED_SURVEY_FIELDS = {"cover_letter", REQUIRED_CONSENT, *OPTIONAL_CONSENTS}


def canonical_job_id(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if (parsed.scheme != "https" or parsed.hostname not in {"yandex.ru", "www.yandex.ru"}
            or not parsed.path.startswith("/jobs/vacancies/")):
        return ""
    last = parsed.path.rstrip("/").split("/")[-1]
    if last.isdecimal():
        return last
    # Yandex redirects /48876 to /role-name-48876.
    token = last.rsplit("-", 1)[-1]
    return token if token.isdecimal() else ""


def is_survey_read_request(request: Any) -> bool:
    u = urlparse(str(request.url))
    return (request.method.upper() == "POST"
            and u.scheme == "https" and u.hostname == FORM_HOST
            and u.path == SURVEY_GET_PATH)


def inspect_yandex_page(
    page: Any, row: Mapping[str, Any], *, timeout_ms: int = 25_000,
) -> dict[str, Any]:
    """Inspect an authorized Yandex vacancy without submitting an application."""
    vacancy_id = str(row.get("id") or "")
    result = {
        "id": vacancy_id, "source": SOURCE,
        "score": row.get("score", 0), "review_state": row.get("review_state", ""),
        "status": "unverified", "submitted": False,
        "resume_source": "Yandex Career profile", "submit_verified": False,
    }
    if not vacancy_id or canonical_job_id(str(row.get("url") or "")) != vacancy_id:
        return {**result, "status": "invalid_vacancy_url"}
    if row.get("application_state") in {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN"}:
        return {**result, "status": "already_applied_or_quarantined"}
    try:
        response = page.goto(
            str(row["url"]), wait_until="domcontentloaded", timeout=timeout_ms,
        )
        if response is None or response.status != 200:
            return {**result, "status": "http_unavailable"}
        if canonical_job_id(str(page.url)) != vacancy_id:
            return {**result, "status": "redirect_mismatch"}
        body = page.locator("body").inner_text(timeout=8000).casefold()
        if any(flag in body for flag in (
            "эта вакансия в архиве", "вакансия в архиве", "набор закрыт",
        )):
            return {**result, "status": "archived"}
        headings = page.locator("h1")
        if headings.count() != 1:
            return {**result, "status": "heading_ambiguous"}
        title = headings.first.inner_text(timeout=3000).strip()
        expected = str(row.get("name") or "").strip()
        if not expected or title.casefold() != expected.casefold():
            return {**result, "status": "title_mismatch"}
        # The job page can indicate both remote AND hybrid, but only remote
        # availability is a positive signal; geo exclusions remain manual.
        if "удалённая работа" not in body and "удаленная работа" not in body:
            return {**result, "status": "remote_not_verified"}
        opener = page.locator("button").filter(has_text="Откликнуться")
        if opener.count() != 1 or not opener.first.is_enabled():
            return {**result, "status": "authorized_apply_button_unavailable"}
        opener.first.click(timeout=9000)  # opens iframe, does NOT submit
        page.wait_for_timeout(500)
        frames = [
            f for f in page.frames
            if urlparse(str(f.url)).hostname == FORM_HOST
        ]
        if len(frames) != 1:
            return {**result, "status": "survey_iframe_unavailable"}
        frame = frames[0]
        parameters = parse_qs(urlparse(str(frame.url)).query)
        if parameters.get("publication_id") != [vacancy_id]:
            return {**result, "status": "survey_publication_id_mismatch"}
        try:
            frame.wait_for_selector(
                "textarea[name='cover_letter']", state="visible", timeout=9500,
            )
        except Exception:  # noqa: BLE001 - transient questionnaire stays unverified
            return {**result, "status": "survey_not_loaded"}
        names = frame.locator("input[name],textarea[name],select[name]").evaluate_all(
            "(els)=>els.map(e=>e.getAttribute('name')).filter(Boolean)"
        )
        unique = {str(x) for x in names}
        if not EXPECTED_SURVEY_FIELDS.issubset(unique):
            return {**result, "status": "survey_questions_changed", "field_count": len(names)}
        extra = unique - EXPECTED_SURVEY_FIELDS
        if extra:
            return {**result, "status": "survey_has_extra_fields", "extra_count": len(extra)}
        if len(names) != len(unique):
            return {**result, "status": "survey_fields_ambiguous"}
        checked = frame.locator(f"input[name='{REQUIRED_CONSENT}']").is_checked()
        optional_checked = {
            opt: frame.locator(f"input[name='{opt}']").is_checked()
            for opt in sorted(OPTIONAL_CONSENTS)
        }
        return {
            **result, "status": "survey_loaded_submission_unverified",
            "survey_host": FORM_HOST,
            "fields": sorted(unique),
            "required_purpose": "candidate_consideration",
            "consent_checked": checked,
            "optional_consent_states": optional_checked,
            "survey_publication_id": vacancy_id,
        }
    except Exception as exc:  # noqa: BLE001 - inspection errors are never receipts
        return {**result, "status": "inspection_failed", "error_type": type(exc).__name__}
