"""Getmatch native web-form driver with source-history receipts.

The Telegram callback remains the preferred channel when @g_jobbot has a
current card.  This module adds the browser fallback: authenticated web session,
exact vacancy page, native application modal, ``/api/offers/<id>/apply`` POST and
an exact ``/applications`` history entry.  A clicked button alone is never a
success.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from .dispatch import ApplyOutcome

SOURCE = "getmatch.ru"
HOST = "getmatch.ru"
APPLY_PATH_TEMPLATE = "/api/offers/{vacancy_id}/apply"
APPLICATIONS_URL = "https://getmatch.ru/applications"


def canonical_vacancy_id(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname not in {HOST, "www.getmatch.ru"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "vacancies":
        return ""
    token = parts[1].split("-", 1)[0]
    return token if token.isdigit() else ""


def _applications_contains(page: Any, vacancy_id: str, timeout_ms: int) -> bool:
    response = page.goto(APPLICATIONS_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    if response is None or int(response.status) != 200:
        raise RuntimeError("Getmatch applications page is unavailable")
    if urlparse(str(page.url)).path.rstrip("/") != "/applications":
        raise RuntimeError("Getmatch applications page redirected")
    page.wait_for_timeout(1800)
    links = page.locator(f'a[href*="/vacancies/{vacancy_id}"]')
    if links.count() > 0:
        return True
    try:
        body = page.locator("body").inner_text(timeout=5000).casefold()
    except Exception:  # noqa: BLE001
        return False
    return f"/vacancies/{vacancy_id}" in body


def apply_getmatch_web_page(
    page: Any, row: Mapping[str, Any], letter: str, *, timeout_ms: int = 25_000,
) -> ApplyOutcome:
    vacancy_id = str(row.get("id") or "")
    if canonical_vacancy_id(str(row.get("url") or "")) != vacancy_id:
        return ApplyOutcome("needs_manual", "invalid Getmatch vacancy URL/ID")
    if row.get("source") not in (None, "", SOURCE):
        return ApplyOutcome("needs_manual", "wrong source for Getmatch web driver")
    if (row.get("review_state") != "APPROVED"
            or row.get("application_state") != "READY_TO_APPLY"):
        return ApplyOutcome("needs_manual", "Getmatch vacancy is not approved and ready")
    if row.get("channel_type") == "external_ats":
        return ApplyOutcome("needs_manual", "external ATS is not a Getmatch form")
    letter = str(letter or "").strip()
    if len(letter) < 20 or len(letter) > 4500:
        return ApplyOutcome("needs_manual", "Getmatch cover letter is missing or out of bounds")

    clicked = False
    posts: list[tuple[str, int]] = []
    apply_path = APPLY_PATH_TEMPLATE.format(vacancy_id=vacancy_id)

    def on_response(response: Any) -> None:
        request = getattr(response, "request", None)
        if request is None or str(getattr(request, "method", "")).upper() != "POST":
            return
        parsed = urlparse(str(response.url))
        if parsed.hostname == HOST and parsed.path == apply_path:
            posts.append((parsed.path, int(getattr(response, "status", 0) or 0)))

    page.on("response", on_response)
    try:
        response = page.goto(str(row["url"]), wait_until="domcontentloaded", timeout=timeout_ms)
        if response is None or int(response.status) != 200:
            return ApplyOutcome("failed_before_submit", "Getmatch vacancy HTTP response is not 200")
        if canonical_vacancy_id(str(page.url)) != vacancy_id:
            return ApplyOutcome("needs_manual", "Getmatch vacancy redirected")
        page.wait_for_timeout(1200)
        body = page.locator("body").inner_text(timeout=8000).casefold()
        if "вы откликнулись" in body or "отклик отправлен" in body:
            if _applications_contains(page, vacancy_id, timeout_ms):
                return ApplyOutcome(
                    "already_applied",
                    "Getmatch applications history already contains the exact vacancy",
                    evidence=APPLICATIONS_URL,
                    receipt={
                        "provider": SOURCE, "vacancy_id": vacancy_id,
                        "applications_verified": True,
                        "applications_url": APPLICATIONS_URL,
                        "already_applied": True,
                    },
                )
            return ApplyOutcome("needs_manual", "Getmatch page claims an application but history is missing")
        if any(flag in body for flag in (
            "вакансия снята", "вакансия в архиве", "набор закрыт",
            "the vacancy has been archived", "no longer hiring",
        )):
            return ApplyOutcome("needs_manual", "Getmatch vacancy is archived")
        expected = str(row.get("name") or "").strip()
        if expected and expected.casefold() not in body:
            return ApplyOutcome("needs_manual", "Getmatch vacancy title mismatch")
        opener = page.locator("button").filter(has_text="Откликнуться")
        visible = [
            opener.nth(index) for index in range(opener.count())
            if opener.nth(index).is_visible()
        ]
        if not visible:
            return ApplyOutcome("needs_manual", "Getmatch apply button is unavailable")
        visible[0].click(timeout=9000)
        page.wait_for_timeout(900)
        form = page.locator("form.b-apply-modal__form")
        if form.count() != 1:
            return ApplyOutcome("failed_before_submit", "Getmatch application form is unavailable")
        textarea = form.locator("textarea[name='cover_letter']")
        if textarea.count() != 1:
            return ApplyOutcome("needs_manual", "Getmatch cover-letter field is unavailable")
        textarea.fill(letter)
        submit = form.locator("button[type='submit'].apply-button")
        if submit.count() != 1 or not submit.first.is_enabled():
            return ApplyOutcome("needs_manual", "Getmatch submit button is unavailable")
        clicked = True
        submit.first.click(timeout=12_000)
        deadline = time.monotonic() + max(6.0, min(20.0, timeout_ms / 1000))
        confirmed = False
        while time.monotonic() < deadline:
            page.wait_for_timeout(500)
            try:
                after = page.locator("body").inner_text(timeout=3000).casefold()
            except Exception:  # noqa: BLE001 - transient navigation is not success
                after = ""
            if "вы откликнулись" in after or "отклик отправлен" in after:
                confirmed = True
                break
        if not confirmed:
            return ApplyOutcome("unknown", "Getmatch submit clicked; success text not confirmed")
        if not _applications_contains(page, vacancy_id, timeout_ms):
            return ApplyOutcome("unknown", "Getmatch success text shown but exact history entry is missing")
        post_ok = any(200 <= status < 300 for _, status in posts)
        if not post_ok:
            return ApplyOutcome(
                "unknown",
                "Getmatch history confirmed but apply POST was not observed",
            )
        return ApplyOutcome(
            "success",
            "Getmatch apply POST and exact applications-history entry confirmed",
            evidence=f"{APPLICATIONS_URL} vacancy={vacancy_id}",
            receipt={
                "provider": SOURCE, "vacancy_id": vacancy_id,
                "post_path": apply_path,
                "post_status": next(status for _, status in posts if 200 <= status < 300),
                "applications_verified": True,
                "applications_url": APPLICATIONS_URL,
            },
        )
    except Exception as exc:  # noqa: BLE001 - clicked but uncertain must never retry
        return ApplyOutcome(
            "unknown" if clicked else "failed_before_submit",
            f"Getmatch web apply failure: {type(exc).__name__}",
        )
    finally:
        remove = getattr(page, "remove_listener", None)
        if callable(remove):
            remove("response", on_response)


class GetmatchWebDriver:
    """Native browser fallback for Getmatch; receipt = exact history entry."""

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
            raise ValueError("Getmatch web interval must be between 0 and 30 seconds")
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
                "needs_manual", "Getmatch web batch paused after UNKNOWN; reconcile first",
            )
        if canonical_vacancy_id(str(row.get("url") or "")) != str(row.get("id") or ""):
            return ApplyOutcome("needs_manual", "Getmatch vacancy URL is not allowed")
        if (row.get("source") not in (None, "", SOURCE)
                or row.get("review_state") != "APPROVED"
                or row.get("application_state") != "READY_TO_APPLY"):
            return ApplyOutcome("needs_manual", "Getmatch vacancy not approved or ready")
        if len(str(self.letter_factory(row) or "").strip()) < 20:
            return ApplyOutcome("needs_manual", "Getmatch cover letter is not ready")
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
            outcome = apply_getmatch_web_page(
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


class GetmatchHybridDriver:
    """Callback first, native web form as the fallback for the same source."""

    source = SOURCE
    submit_verified = True

    def __init__(
        self,
        root: Any,
        store: Any,
        *,
        account: str = "getmatch-primary",
        telegram_account: str = "getmatch-primary",
        track: str = "ai_core",
        page_factory: Callable[[], Any] | None = None,
        letter_factory: Callable[[Mapping[str, Any]], str] | None = None,
        min_interval_seconds: float = 2.0,
        transport: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        from .getmatch_apply import GetmatchCallbackDriver, click_and_confirm

        self.callback = GetmatchCallbackDriver(
            root, store, account=account, telegram_account=telegram_account,
            track=track, min_interval_seconds=min_interval_seconds,
            transport=transport or click_and_confirm,
        )
        self.web = (
            GetmatchWebDriver(
                page_factory, letter_factory,
                min_interval_seconds=min_interval_seconds,
            )
            if page_factory is not None else None
        )
        self._modes: dict[str, str] = {}

    def preflight(self, row: Mapping[str, Any]) -> ApplyOutcome:
        vacancy_id = str(row.get("id") or "")
        callback = self.callback.preflight(dict(row))
        if callback.status == "ready":
            self._modes[vacancy_id] = "callback"
            return callback
        if self.web is not None:
            web = self.web.preflight(row)
            if web.status == "ready":
                self._modes[vacancy_id] = "web"
                return web
            return web
        return callback

    def apply(self, row: Mapping[str, Any]) -> ApplyOutcome:
        vacancy_id = str(row.get("id") or "")
        mode = self._modes.pop(vacancy_id, "")
        if mode == "callback":
            return self.callback.apply(dict(row))
        if mode == "web" and self.web is not None:
            return self.web.apply(row)
        return ApplyOutcome("needs_manual", "Getmatch hybrid mode was not preflight-verified")
