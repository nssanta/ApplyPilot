"""Verified Habr Career native applicant response driver.

The signed-in one-click response contract was observed on a real Habr vacancy:
POST /api/frontend/vacancies/{id}/responses -> HTTP 200, then an exact
/vacancies/{id} link appears in the authenticated /responses history.

Only an explicitly approved and ready vacancy can be submitted. Browser
contexts and credentials are owned by the caller; this module never opens
a Telegram session, accesses a password or bypasses a challenge.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from .dispatch import ApplyOutcome

HABR_HOST = "career.habr.com"
HABR_HISTORY = "https://career.habr.com/responses"
RESPONSE_PATTERN = re.compile(r"^/vacancies/([0-9]+)/?$")


def _vacancy_id(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname != HABR_HOST:
        return ""
    match = RESPONSE_PATTERN.fullmatch(parsed.path)
    return match.group(1) if match else ""


def _history_contains(page: Any, vacancy_id: str, *, timeout_ms: int = 25_000) -> bool:
    response = page.goto(HABR_HISTORY, wait_until="domcontentloaded", timeout=timeout_ms)
    if response is None or response.status != 200:
        raise RuntimeError("Habr applicant response history is unavailable")
    if urlparse(str(page.url)).path != "/responses":
        raise RuntimeError("Habr redirected away from applicant response history")
    # Exact native vacancy ID, not a company name that may have other roles.
    return page.locator(f'a[href="/vacancies/{vacancy_id}"]').count() > 0


def _remote_allowed(text: str) -> bool:
    content = text.casefold()
    return any(phrase in content for phrase in (
        "можно удалённо", "можно удаленно",
        "удалённый формат", "удаленный формат",
        "работа удаленно", "работа удалённо",
    ))


def apply_habr_page(
    page: Any,
    row: Mapping[str, Any],
    *,
    require_remote: bool = True,
    timeout_ms: int = 25_000,
) -> ApplyOutcome:
    """Submit one Habr vacancy only when its on-site receipt can be checked."""
    url = str(row.get("url") or "")
    vacancy_id = str(row.get("id") or "")
    if not vacancy_id or _vacancy_id(url) != vacancy_id:
        return ApplyOutcome("needs_manual", "Habr vacancy URL and canonical ID do not match")
    if row.get("source") not in ("", None, HABR_HOST):
        return ApplyOutcome("needs_manual", "wrong source for Habr native driver")
    if row.get("review_state") != "APPROVED":
        return ApplyOutcome("needs_manual", "vacancy is not explicitly approved")
    if row.get("application_state") != "READY_TO_APPLY":
        return ApplyOutcome("needs_manual", "vacancy is not ready for a real submission")

    clicked = False
    writes: list[str] = []
    responses: list[int] = []
    application_post = f"/api/frontend/vacancies/{vacancy_id}/responses"

    def on_request(request: Any) -> None:
        if (request.method.upper() == "POST"
                and urlparse(request.url).hostname == HABR_HOST
                and urlparse(request.url).path == application_post):
            writes.append("POST")

    def on_response(response: Any) -> None:
        request = response.request
        if (request.method.upper() == "POST"
                and urlparse(response.url).hostname == HABR_HOST
                and urlparse(response.url).path == application_post):
            responses.append(int(response.status))

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        if _history_contains(page, vacancy_id, timeout_ms=timeout_ms):
            return ApplyOutcome(
                "already_applied", "Habr applicant history already contains this exact vacancy",
                evidence=f"{HABR_HISTORY} vacancy={vacancy_id}",
                receipt={"history": "/responses", "vacancy_id": vacancy_id},
            )

        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if response is None or response.status != 200:
            return ApplyOutcome("failed_before_submit", "Habr vacancy detail did not load")
        if urlparse(str(page.url)).path != f"/vacancies/{vacancy_id}":
            return ApplyOutcome("needs_manual", "Habr vacancy redirected to another page")

        visible_body = page.locator("body").inner_text(timeout=7000)
        expected_title = str(row.get("name") or "").strip()
        if not expected_title or expected_title.casefold() not in visible_body.casefold():
            return ApplyOutcome("needs_manual", "vacancy title cannot be verified on live Habr page")
        if require_remote and not _remote_allowed(visible_body):
            return ApplyOutcome("needs_manual", "remote eligibility is not verified on Habr page")

        section = page.locator(".create-vacancy-response__apply")
        submit = section.locator("button[type='submit']").filter(has_text=re.compile(r"^Откликнуться$"))
        if (section.count() != 1 or submit.count() != 1
                or not submit.first.is_visible() or not submit.first.is_enabled()):
            return ApplyOutcome("needs_manual", "native Habr one-click form is unavailable")

        # Additional visible applicant questions must be handled explicitly:
        # don't fill in unknown personal facts or click through an extra form.
        response_section = page.locator(".create-vacancy-response")
        if response_section.count():
            fields = response_section.locator("textarea,select,input:not([type='hidden'])")
            if any(fields.nth(i).is_visible() for i in range(min(fields.count(), 50))):
                return ApplyOutcome("needs_manual", "Habr response requires additional applicant fields")

        clicked = True
        submit.first.click(timeout=12_000)
        page.wait_for_timeout(1000)
        after_text = page.locator("body").inner_text(timeout=7000)
        confirmed_notice = "отклик отправлен" in after_text.casefold()

        if not (200 in responses and confirmed_notice):
            if not writes and not responses:
                return ApplyOutcome("needs_manual", "no native Habr submission request was observed")
            return ApplyOutcome(
                "unknown", "Habr POST occurred but submission response or on-page notice is unverified",
            )
        try:
            verified_history = _history_contains(page, vacancy_id, timeout_ms=timeout_ms)
        except Exception:  # noqa: BLE001 - a missing receipt must not be reported as success
            verified_history = False
        if not verified_history:
            return ApplyOutcome("unknown", "Habr POST accepted but applicant history has no exact receipt")
        return ApplyOutcome(
            "success",
            "Habr POST 200, site notice and exact applicant-history entry confirmed",
            evidence=f"{HABR_HISTORY} vacancy={vacancy_id}",
            receipt={
                "provider": HABR_HOST,
                "vacancy_id": vacancy_id,
                "post_status": 200,
                "post_path": application_post,
                "verified_history": True,
            },
        )
    except Exception as exc:  # noqa: BLE001 - ambiguous network outcomes are quarantined
        # After a click, even a timeout might mean the POST reached Habr.
        return ApplyOutcome(
            "unknown" if clicked or writes else "failed_before_submit",
            f"Habr apply {'ambiguous' if clicked or writes else 'not started'}: "
            f"{type(exc).__name__}: {str(exc)[:120]}",
        )
    finally:
        # No network/application side effects from detaching local listeners.
        if callable(getattr(page, "remove_listener", None)):
            page.remove_listener("request", on_request)
            page.remove_listener("response", on_response)


class HabrPageDriver:
    """Reusable source-aware Habr driver for an authenticated Playwright context."""

    source = HABR_HOST
    submit_verified = True

    def __init__(
        self,
        page_factory: Callable[[], Any],
        *,
        require_remote: bool = True,
        timeout_ms: int = 25_000,
    ) -> None:
        self.page_factory = page_factory
        self.require_remote = require_remote
        self.timeout_ms = timeout_ms

    def preflight(self, row: Mapping[str, Any]) -> ApplyOutcome:
        if _vacancy_id(str(row.get("url") or "")) != str(row.get("id") or ""):
            return ApplyOutcome("needs_manual", "Habr vacancy URL is not allowed")
        if (row.get("source") not in (None, "", HABR_HOST)
                or row.get("review_state") != "APPROVED"
                or row.get("application_state") != "READY_TO_APPLY"):
            return ApplyOutcome("needs_manual", "Habr vacancy not approved or ready")
        if row.get("channel_type") == "external_ats":
            return ApplyOutcome("needs_manual", "external ATS requires a separate verified driver")
        return ApplyOutcome("ready")

    def apply(self, row: Mapping[str, Any]) -> ApplyOutcome:
        page = self.page_factory()
        try:
            return apply_habr_page(
                page, row, require_remote=self.require_remote,
                timeout_ms=self.timeout_ms,
            )
        finally:
            page.close()
