"""Yandex Career read-only application-form inspector.

Yandex Career is a first-party employer careers site, not an aggregator.
The apply UI opens a cross-origin iframe at forms.yandex.ru and loads a survey
by POST /gateway/root/form/getSurvey. That POST is read-only discovery,
NOT an application. The inspector requires the caller to deny every other
mutating HTTP request. No Yandex live submit path is claimed here.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

from .dispatch import ApplyOutcome

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


SUCCESS_PATH = "/success"
SUCCESS_MARKERS = (
    "отклик отправлен", "отклик успешно отправлен", "спасибо за отклик",
    "ваш отклик", "application sent", "thanks for applying",
)


def _survey_frame(page: Any) -> Any | None:
    frames = [
        frame for frame in getattr(page, "frames", ())
        if urlparse(str(frame.url)).hostname == FORM_HOST
    ]
    return frames[0] if len(frames) == 1 else None


def _survey_publication(frame: Any) -> str:
    parameters = parse_qs(urlparse(str(frame.url)).query)
    values = parameters.get("publication_id") or []
    return str(values[0]) if len(values) == 1 else ""


def _applied_state(page: Any, vacancy_url: str, timeout_ms: int) -> tuple[bool, str]:
    """Re-read the source vacancy after submit; a success page alone is not enough."""
    try:
        response = page.goto(vacancy_url, wait_until="domcontentloaded", timeout=timeout_ms)
        if response is None or int(response.status) != 200:
            return False, ""
        page.wait_for_timeout(1800)
        body = page.locator("body").inner_text(timeout=8000)
        folded = body.casefold()
        marker = ""
        for value in ("вы откликнулись на вакансию", "отклик отправлен", "вы уже откликнулись"):
            if value in folded:
                marker = value
                break
        buttons = page.locator("button").filter(has_text="Вы откликнулись")
        disabled = buttons.count() > 0 and not buttons.first.is_enabled()
        if marker and disabled:
            return True, marker
        return False, marker
    except Exception:  # noqa: BLE001 - missing post-check keeps the result ambiguous
        return False, ""


def _success_url(page: Any) -> tuple[str, str]:
    """Return ``(url, body)`` for a real source success page, if present."""
    candidates = [str(getattr(page, "url", ""))]
    candidates.extend(str(frame.url) for frame in getattr(page, "frames", ()))
    for url in candidates:
        parsed = urlparse(url)
        if parsed.hostname == FORM_HOST and parsed.path.rstrip("/").endswith(SUCCESS_PATH):
            return url, ""
    body = ""
    try:
        body = page.locator("body").inner_text(timeout=2500).casefold()
    except Exception:  # noqa: BLE001 - a missing body is not a success signal
        return "", ""
    if any(marker in body for marker in SUCCESS_MARKERS):
        return str(getattr(page, "url", "")), body
    return "", ""


def apply_yandex_page(
    page: Any, row: Mapping[str, Any], letter: str, *, timeout_ms: int = 25_000,
) -> ApplyOutcome:
    """Submit the exact Yandex survey after source-side preflight.

    A success page is necessary but not sufficient: the receipt records the
    survey publication id, success URL and every mutating form POST observed.
    """
    vacancy_id = str(row.get("id") or "")
    if canonical_job_id(str(row.get("url") or "")) != vacancy_id:
        return ApplyOutcome("needs_manual", "invalid Yandex vacancy URL/ID")
    if row.get("source") not in (None, "", SOURCE):
        return ApplyOutcome("needs_manual", "wrong source for Yandex driver")
    if (row.get("review_state") != "APPROVED"
            or row.get("application_state") != "READY_TO_APPLY"):
        return ApplyOutcome("needs_manual", "Yandex vacancy is not approved and ready")
    if row.get("channel_type") == "external_ats":
        return ApplyOutcome("needs_manual", "external ATS is not a Yandex survey")
    letter = str(letter or "").strip()
    if len(letter) < 20 or len(letter) > 5000:
        return ApplyOutcome("needs_manual", "Yandex cover letter is missing or out of bounds")

    clicked = False
    submit_posts: list[tuple[str, int]] = []

    def on_response(response: Any) -> None:
        request = getattr(response, "request", None)
        if request is None or str(getattr(request, "method", "")).upper() != "POST":
            return
        parsed = urlparse(str(response.url))
        if parsed.hostname == FORM_HOST and parsed.path != SURVEY_GET_PATH:
            submit_posts.append((parsed.path, int(getattr(response, "status", 0) or 0)))

    page.on("response", on_response)
    try:
        response = page.goto(str(row["url"]), wait_until="domcontentloaded", timeout=timeout_ms)
        if response is None or int(response.status) != 200:
            return ApplyOutcome("failed_before_submit", "Yandex vacancy HTTP response is not 200")
        if canonical_job_id(str(page.url)) != vacancy_id:
            return ApplyOutcome("needs_manual", "Yandex vacancy redirected")
        body = page.locator("body").inner_text(timeout=8000).casefold()
        if any(flag in body for flag in (
            "эта вакансия в архиве", "вакансия в архиве", "набор закрыт",
        )):
            return ApplyOutcome("needs_manual", "Yandex vacancy is archived")
        headings = page.locator("h1")
        if headings.count() != 1:
            return ApplyOutcome("needs_manual", "Yandex heading is ambiguous")
        title = headings.first.inner_text(timeout=3000).strip()
        if not str(row.get("name") or "").strip() or (
            title.casefold() != str(row["name"]).strip().casefold()
        ):
            return ApplyOutcome("needs_manual", "Yandex vacancy title mismatch")
        if "удалённая работа" not in body and "удаленная работа" not in body:
            return ApplyOutcome("needs_manual", "Yandex remote format is not verified")
        opener = page.locator("button").filter(has_text="Откликнуться")
        if opener.count() != 1 or not opener.first.is_enabled():
            return ApplyOutcome("needs_manual", "Yandex apply button is unavailable or ambiguous")
        opener.first.click(timeout=9000)
        page.wait_for_timeout(1200)
        frame = _survey_frame(page)
        if frame is None:
            return ApplyOutcome("failed_before_submit", "Yandex survey iframe is unavailable")
        if _survey_publication(frame) != vacancy_id:
            return ApplyOutcome("needs_manual", "Yandex survey publication id mismatch")
        try:
            frame.wait_for_selector(
                "textarea[name='cover_letter']", state="visible", timeout=9500,
            )
        except Exception:  # noqa: BLE001 - unloaded form is never submitted
            return ApplyOutcome("failed_before_submit", "Yandex survey did not load")
        names = frame.locator("input[name],textarea[name],select[name]").evaluate_all(
            "(els)=>els.map(e=>e.getAttribute('name')).filter(Boolean)"
        )
        unique = {str(name) for name in names}
        if unique != EXPECTED_SURVEY_FIELDS:
            return ApplyOutcome(
                "needs_manual",
                f"Yandex survey fields changed: {sorted(unique)}",
            )
        frame.locator("textarea[name='cover_letter']").fill(letter)
        consent = frame.locator(f"input[name='{REQUIRED_CONSENT}']")
        if consent.count() != 1:
            return ApplyOutcome("needs_manual", "Yandex required consent is ambiguous")
        if not consent.is_checked():
            consent.check(timeout=4000)
        if not consent.is_checked():
            return ApplyOutcome("needs_manual", "Yandex required consent is not checked")
        try:
            frame.wait_for_selector("button[type='submit']", state="visible", timeout=8000)
        except Exception:  # noqa: BLE001 - form hydration can lag behind the textarea
            return ApplyOutcome("failed_before_submit", "Yandex submit button did not load")
        submit = frame.locator("button[type='submit']")
        if submit.count() != 1 or not submit.first.is_enabled():
            return ApplyOutcome("needs_manual", "Yandex submit button is unavailable")
        if "отправить отклик" not in submit.first.inner_text(timeout=2000).casefold():
            return ApplyOutcome("needs_manual", "unexpected Yandex submit button")
        clicked = True
        submit.first.click(timeout=12_000)
        deadline = time.monotonic() + max(5.0, min(20.0, timeout_ms / 1000))
        success_url = success_body = ""
        while time.monotonic() < deadline:
            success_url, success_body = _success_url(page)
            if success_url:
                break
            page.wait_for_timeout(400)
        if not success_url:
            return ApplyOutcome(
                "unknown",
                "Yandex submit clicked; source success page not confirmed",
            )
        marker = next((value for value in SUCCESS_MARKERS if value in success_body), "")
        if not marker and success_body:
            return ApplyOutcome("unknown", "Yandex success page text was not recognized")
        applied, applied_marker = _applied_state(page, str(row["url"]), timeout_ms)
        if not applied:
            return ApplyOutcome(
                "unknown",
                "Yandex success page shown, but applied state is not confirmed",
            )
        return ApplyOutcome(
            "success",
            "Yandex success page and applied state confirmed",
            evidence=f"yandex.ru publication={vacancy_id}",
            receipt={
                "provider": SOURCE,
                "vacancy_id": vacancy_id,
                "publication_id": vacancy_id,
                "success_url": success_url,
                "success_path": urlparse(success_url).path,
                "success_marker": marker or applied_marker or "success_path",
                "success_verified": True,
                "applied_state_verified": True,
                "applied_state_marker": applied_marker,
                "submit_posts": [path for path, _ in submit_posts],
                "submit_statuses": [status for _, status in submit_posts],
            },
        )
    except Exception as exc:  # noqa: BLE001 - clicked but uncertain must never retry
        return ApplyOutcome(
            "unknown" if clicked else "failed_before_submit",
            f"Yandex apply failure: {type(exc).__name__}",
        )
    finally:
        remove = getattr(page, "remove_listener", None)
        if callable(remove):
            remove("response", on_response)


class YandexPageDriver:
    """Verified native Yandex survey driver for an authenticated context."""

    source = SOURCE
    submit_verified = True

    def __init__(
        self,
        page_factory: Callable[[], Any],
        letter_factory: Callable[[Mapping[str, Any]], str] | None = None,
        *,
        timeout_ms: int = 25_000,
        min_interval_seconds: float = 2.0,
    ) -> None:
        if not 0 <= min_interval_seconds <= 30:
            raise ValueError("Yandex live interval must be between 0 and 30 seconds")
        self.page_factory = page_factory
        self.letter_factory = letter_factory or (
            lambda row: str(row.get("approved_cover_letter") or "")
        )
        self.timeout_ms = timeout_ms
        self.min_interval_seconds = float(min_interval_seconds)
        self._last_started: float | None = None
        self._paused_after_unknown = False
        self._quarantined_ids: set[str] = set()

    def preflight(self, row: Mapping[str, Any]) -> ApplyOutcome:
        if str(row.get("id") or "") in self._quarantined_ids:
            return ApplyOutcome("needs_manual", "vacancy quarantined after UNKNOWN")
        if self._paused_after_unknown:
            return ApplyOutcome(
                "needs_manual", "Yandex batch paused after UNKNOWN; reconcile first",
            )
        if canonical_job_id(str(row.get("url") or "")) != str(row.get("id") or ""):
            return ApplyOutcome("needs_manual", "Yandex vacancy URL is not allowed")
        if (row.get("source") not in (None, "", SOURCE)
                or row.get("review_state") != "APPROVED"
                or row.get("application_state") != "READY_TO_APPLY"):
            return ApplyOutcome("needs_manual", "Yandex vacancy not approved or ready")
        if row.get("channel_type") == "external_ats":
            return ApplyOutcome("needs_manual", "external ATS requires a separate driver")
        if len(str(self.letter_factory(row) or "").strip()) < 20:
            return ApplyOutcome("needs_manual", "Yandex cover letter is not ready")
        return ApplyOutcome("ready")

    def apply(self, row: Mapping[str, Any]) -> ApplyOutcome:
        check = self.preflight(row)
        if check.status != "ready":
            return check
        if self._last_started is not None:
            wait = self.min_interval_seconds - (time.monotonic() - self._last_started)
            if wait > 0:
                time.sleep(wait)
        self._last_started = time.monotonic()
        page = self.page_factory()
        try:
            outcome = apply_yandex_page(
                page, row, self.letter_factory(row), timeout_ms=self.timeout_ms,
            )
            if outcome.status == "unknown":
                self._quarantined_ids.add(str(row.get("id") or ""))
                self._paused_after_unknown = outcome.blocks_source
            return outcome
        finally:
            close = getattr(page, "close", None)
            if callable(close):
                close()
