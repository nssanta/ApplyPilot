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
import time
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


def inspect_habr_page(
    page: Any, row: Mapping[str, Any], *, timeout_ms: int = 25_000,
) -> dict[str, Any]:
    """Read-only classification of a real Habr vacancy's applicant form.

    The caller must enforce GET/HEAD/OPTIONS-only routing. Inspecting never
    invokes a button, sends an application or grants review approval.
    """
    vacancy_id = str(row.get("id") or "")
    url = str(row.get("url") or "")
    result: dict[str, Any] = {
        "id": vacancy_id, "source": HABR_HOST,
        "score": row.get("score", 0), "review_state": row.get("review_state", ""),
        "status": "unverified", "native_submit": False,
        "visible_fields": 0, "remote_verified": False, "submitted": False,
    }
    if not vacancy_id or _vacancy_id(url) != vacancy_id:
        return {**result, "status": "invalid_url"}
    if row.get("application_state") in {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN"}:
        return {**result, "status": "already_applied_or_quarantined"}
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if response is None or response.status != 200:
            return {**result, "status": "http_unavailable"}
        if urlparse(str(page.url)).path != f"/vacancies/{vacancy_id}":
            return {**result, "status": "redirect"}
        body = page.locator("body").inner_text(timeout=7000)
        if "отклик отправлен" in body.casefold():
            return {**result, "status": "already_applied_on_site"}
        expected_title = str(row.get("name") or "").strip()
        if not expected_title or expected_title.casefold() not in body.casefold():
            return {**result, "status": "title_mismatch"}
        # Similar vacancies can mention remote work even if the target role
        # is office-only; trust only the current vacancy header's chips.
        header = page.locator(".vacancy-header")
        remote = (header.count() == 1
                  and _remote_allowed(header.inner_text(timeout=4000)))
        section = page.locator(".create-vacancy-response__apply")
        native = False
        if section.count() == 1:
            buttons = section.locator("button[type='submit']").filter(
                has_text=re.compile(r"Откликнуться", re.IGNORECASE)
            )
            native = (buttons.count() == 1 and buttons.first.is_visible()
                      and buttons.first.is_enabled())
        form = page.locator(".create-vacancy-response")
        field_count = 0
        if form.count():
            fields = form.locator("textarea,select,input:not([type='hidden'])")
            field_count = sum(
                int(fields.nth(index).is_visible())
                for index in range(min(fields.count(), 50))
            )
        external = page.locator(
            f'a[href*="/vacancies/{vacancy_id}/external_response"]'
        ).count() > 0
        if external and not native:
            status = "external_ats"
        elif not native:
            status = "no_native_form"
        elif field_count:
            status = "form_requires_answers"
        elif not remote:
            status = "remote_not_verified"
        else:
            status = "native_ready_for_approval"
        return {
            **result, "status": status, "native_submit": native,
            "visible_fields": field_count, "remote_verified": remote,
            "external_ats": external,
        }
    except Exception as exc:  # noqa: BLE001 - a bad provider page cannot block a batch
        return {**result, "status": "inspection_failed",
                "error_type": type(exc).__name__}


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
        header = page.locator(".vacancy-header")
        remote_in_header = (header.count() == 1
                            and _remote_allowed(header.inner_text(timeout=4000)))
        if require_remote and not remote_in_header:
            return ApplyOutcome("needs_manual", "remote eligibility is not verified in vacancy header")

        section = page.locator(".create-vacancy-response__apply")
        submit = section.locator("button[type='submit']").filter(has_text=re.compile(r"Откликнуться", re.IGNORECASE))
        if "отклик отправлен" in visible_body.casefold():
            return ApplyOutcome("already_applied", "Habr vacancy page already shows an application")
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
                # An actual button was clicked; network-event loss or delayed
                # submission makes the outcome ambiguous. Never retry blindly.
                return ApplyOutcome(
                    "unknown", "Habr button was clicked but no submission receipt was observed",
                )
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
        min_interval_seconds: float = 2.0,
    ) -> None:
        if not 0 <= min_interval_seconds <= 30:
            raise ValueError("Habr live interval must be between 0 and 30 seconds")
        self.page_factory = page_factory
        self.require_remote = require_remote
        self.timeout_ms = timeout_ms
        self.min_interval_seconds = float(min_interval_seconds)
        self._last_apply_started: float | None = None
        self._paused_after_unknown = False
        self._quarantined_ids: set[str] = set()

    def preflight(self, row: Mapping[str, Any]) -> ApplyOutcome:
        if str(row.get("id") or "") in self._quarantined_ids:
            return ApplyOutcome("needs_manual", "vacancy quarantined after UNKNOWN")
        if self._paused_after_unknown:
            return ApplyOutcome(
                "needs_manual",
                "Habr run paused after an ambiguous submission; reconcile the UNKNOWN first",
            )
        if _vacancy_id(str(row.get("url") or "")) != str(row.get("id") or ""):
            return ApplyOutcome("needs_manual", "Habr vacancy URL is not allowed")
        if (row.get("source") not in (None, "", HABR_HOST)
                or row.get("review_state") != "APPROVED"
                or row.get("application_state") != "READY_TO_APPLY"):
            return ApplyOutcome("needs_manual", "Habr vacancy not approved or ready")
        if row.get("channel_type") == "external_ats":
            return ApplyOutcome("needs_manual", "external ATS requires a separate verified driver")
        page = self.page_factory()
        try:
            route = getattr(page, "route", None)
            if not callable(route):
                return ApplyOutcome("needs_manual", "read-only Habr preflight cannot block writes")

            def read_only(route_handler: Any) -> None:
                if route_handler.request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
                    route_handler.continue_()
                else:
                    route_handler.abort()

            route("**/*", read_only)
            inspection = inspect_habr_page(page, row, timeout_ms=self.timeout_ms)
        finally:
            close = getattr(page, "close", None)
            if callable(close):
                close()
        if inspection.get("status") != "native_ready_for_approval":
            return ApplyOutcome(
                "needs_manual",
                f"Habr source preflight: {inspection.get('status') or 'unknown'!s}",
            )
        return ApplyOutcome("ready")

    def apply(self, row: Mapping[str, Any]) -> ApplyOutcome:
        if str(row.get("id") or "") in self._quarantined_ids:
            return ApplyOutcome("needs_manual", "vacancy quarantined after UNKNOWN")
        if self._paused_after_unknown:
            return ApplyOutcome("needs_manual", "Habr run is paused after UNKNOWN")
        if self._last_apply_started is not None:
            wait = self.min_interval_seconds - (time.monotonic() - self._last_apply_started)
            if wait > 0:
                time.sleep(wait)
        self._last_apply_started = time.monotonic()
        page: Any | None = None
        try:
            page = self.page_factory()
            outcome = apply_habr_page(
                page, row, require_remote=self.require_remote,
                timeout_ms=self.timeout_ms,
            )
            if outcome.status == "unknown":
                self._quarantined_ids.add(str(row.get("id") or ""))
                self._paused_after_unknown = outcome.blocks_source
            return outcome
        except Exception:
            self._paused_after_unknown = True
            raise
        finally:
            if page is not None:
                page.close()
