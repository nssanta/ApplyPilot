"""Source-native GeekJob form inspection and controlled single-application engine.

Only GET/HEAD/OPTIONS are permitted during the inspector. The writer is
deliberately disabled for dispatch until a real vacancy's server receipt has
been independently confirmed. No form is clicked without APPROVED, a ready
vacancy, exact resume selection and a supplied per-vacancy cover letter.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .dispatch import ApplyOutcome

SOURCE = "geekjob.ru"
ACCOUNT_HOSTS = {"geekjob.ru", "my.geekjob.ru"}
HISTORY_URL = "https://my.geekjob.ru/responses"
ID_PATTERN = re.compile(r"^/vacancy/([0-9a-fA-F]{24})/?$")
RESUME_NAME = "AI/LLM Engineer / AI Agent Engineer"

def vacancy_id(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname not in {"geekjob.ru", "www.geekjob.ru"}:
        return ""
    match = ID_PATTERN.fullmatch(parsed.path)
    return match.group(1).lower() if match else ""


def _history_contains(page: Any, vid: str, timeout_ms: int) -> bool:
    response = page.goto(HISTORY_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    if response is None or response.status != 200:
        raise RuntimeError("GeekJob applicant-history HTTP response is not 200")
    if urlparse(page.url).path != "/responses":
        raise RuntimeError("GeekJob applicant history redirected")
    # Never match by employer name: different vacancies can share an employer.
    return page.locator(f'a[href*="/vacancy/{vid}"]').count() > 0


def _load_form(page: Any, row: Mapping[str, Any], timeout_ms: int) -> dict[str, Any]:
    vid = str(row.get("id") or "")
    url = str(row.get("url") or "")
    if not vid or vacancy_id(url) != vid.lower():
        return {"status": "invalid_id_or_url"}
    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if response is None or response.status != 200:
        return {"status": "http_unavailable"}
    if urlparse(page.url).path != f"/vacancy/{vid}":
        return {"status": "redirect"}
    try:
        page.wait_for_selector("article.vacancy h1", state="visible", timeout=8000)
    except Exception:  # noqa: BLE001 - unsupported page cannot be submitted
        return {"status": "vacancy_details_missing"}
    title = page.locator("article.vacancy h1").first.inner_text(timeout=3500).strip()
    expected = str(row.get("name") or "").strip()
    if not expected or expected.casefold() != title.casefold():
        return {"status": "title_mismatch", "title": title[:110]}
    article = page.locator("article.vacancy")
    header = article.locator(".jobinfo").first
    if header.count() != 1:
        return {"status": "no_jobinfo"}
    summary = header.inner_text(timeout=3500).casefold()
    remote = "удаленная работа" in summary or "удалённая работа" in summary
    if not remote:
        return {"status": "not_remote"}
    desc = article.locator(".description")
    text = desc.inner_text(timeout=5000).casefold() if desc.count() else ""
    if any(flag in text for flag in ("кроме рф", "кроме россии", "за исключением рф", "remote except russia")):
        return {"status": "remote_excludes_russia"}
    try:
        page.wait_for_selector("#mycvlist", state="visible", timeout=4500)
    except Exception:  # noqa: BLE001 - form absent is manual
        return {"status": "native_form_missing"}
    form = page.locator("form").filter(has=page.locator("#respond-text"))
    if form.count() != 1:
        return {"status": "form_ambiguous"}
    selected = form.locator("#mycvlist")
    options = selected.locator("option").all_inner_texts()
    matches = [i for i, name in enumerate(options) if name.strip() == RESUME_NAME]
    if len(matches) != 1:
        return {"status": "resume_missing_or_ambiguous", "resume_count": len(options)}
    extra = form.locator("input:visible,textarea:visible,select:visible")
    known = {"mycvlist", "respond-text"}
    unknown = [extra.nth(i).evaluate("(e)=>e.id||e.name||e.type")
               for i in range(min(extra.count(), 30))
               if extra.nth(i).evaluate("(e)=>e.id||e.name||e.type") not in known]
    if unknown:
        return {"status": "extra_questions", "count": len(unknown)}
    submit = form.locator('button[type="submit"]')
    if submit.count() != 1 or not submit.first.is_enabled() or not submit.first.is_visible():
        return {"status": "submit_unavailable"}
    if not re.search("откликнуться", submit.first.inner_text(), re.IGNORECASE):
        return {"status": "unexpected_submit"}
    return {"status": "native_form", "selected_resume": RESUME_NAME,
            "resume_option_index": matches[0], "letter_required": True,
            "remote_verified": True, "fields": ["mycvlist", "respond-text"]}


def inspect_geekjob_page(page: Any, row: Mapping[str, Any], *,
                         timeout_ms: int = 25_000) -> dict[str, Any]:
    """Read-only form audit; caller MUST block mutating HTTP methods."""
    result = {"source": SOURCE, "id": str(row.get("id") or ""),
              "score": row.get("score", 0), "review_state": row.get("review_state", ""),
              "submitted": False, "status": "unverified"}
    if row.get("application_state") in {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN"}:
        return {**result, "status": "already_applied_or_quarantined"}
    try:
        found = _load_form(page, row, timeout_ms)
        return {**result, **found}
    except Exception as exc:  # noqa: BLE001 - a bad card cannot interrupt a batch
        return {**result, "status": "inspection_failed", "error_type": type(exc).__name__}


def apply_geekjob_page(page: Any, row: Mapping[str, Any], letter: str, *,
                       timeout_ms: int = 25_000) -> ApplyOutcome:
    vid = str(row.get("id") or "")
    if (vacancy_id(str(row.get("url") or "")) != vid.lower()
            or row.get("source") != SOURCE):
        return ApplyOutcome("needs_manual", "invalid GeekJob vacancy ID / source")
    if row.get("review_state") != "APPROVED" or row.get("application_state") != "READY_TO_APPLY":
        return ApplyOutcome("needs_manual", "GeekJob vacancy is not approved and ready")
    if not letter or len(letter.strip()) < 50 or len(letter) > 2500:
        return ApplyOutcome("needs_manual", "an approved vacancy-specific cover letter is required")
    clicked = False
    posts: list[tuple[str, int]] = []
    def on_response(response: Any) -> None:
        u = urlparse(response.url)
        if (response.request.method.upper() == "POST"
                and u.hostname in ACCOUNT_HOSTS
                and ("respond" in u.path or "response" in u.path)):
            posts.append((u.path, int(response.status)))
    page.on("response", on_response)
    try:
        if _history_contains(page, vid, timeout_ms):
            return ApplyOutcome("already_applied", "exact GeekJob vacancy already in applicant history")
        form_info = _load_form(page, row, timeout_ms)
        if form_info.get("status") != "native_form":
            return ApplyOutcome("needs_manual", "GeekJob " + str(form_info.get("status")))
        form = page.locator("form").filter(has=page.locator("#respond-text"))
        form.locator("#mycvlist").select_option(index=form_info["resume_option_index"])
        form.locator("#respond-text").fill(letter.strip())
        if form.locator("#respond-text").input_value().strip() != letter.strip():
            return ApplyOutcome("needs_manual", "GeekJob letter did not populate")
        submit = form.locator('button[type="submit"]')
        clicked = True
        submit.click(timeout=12_000)
        page.wait_for_timeout(1200)
        # A callback click is not success; verify source-specific history.
        try:
            proof = _history_contains(page, vid, timeout_ms)
        except Exception:  # noqa: BLE001 - missing history never means success
            proof = False
        if proof and any(200 <= status < 300 for _, status in posts):
            return ApplyOutcome("success", "GeekJob POST and exact applicant history confirmed",
                                evidence=f"{HISTORY_URL} vacancy={vid}",
                                receipt={"provider": SOURCE, "vacancy_id": vid,
                                         "post_paths": [x[0] for x in posts],
                                         "history_verified": True})
        return ApplyOutcome("unknown", "GeekJob submit clicked; exact history + POST receipt not confirmed")
    except Exception as exc:  # noqa: BLE001 - clicked but uncertain must never retry
        return ApplyOutcome(
            "unknown" if clicked else "failed_before_submit",
            f"GeekJob apply failure: {type(exc).__name__}",
        )
    finally:
        if callable(getattr(page, "remove_listener", None)):
            page.remove_listener("response", on_response)


class GeekJobPageDriver:
    """Conservative opt-in driver; unverified until first confirmed live receipt."""

    source = SOURCE
    submit_verified = False

    def __init__(self, page_factory: Callable[[], Any], letter_dir: Path, *,
                 min_interval_seconds: float = 2.0):
        if not 0 <= min_interval_seconds <= 30:
            raise ValueError("invalid GeekJob interval")
        self.page_factory = page_factory
        self.letter_dir = Path(letter_dir)
        self.min_interval_seconds = float(min_interval_seconds)
        self._paused = False

    def preflight(self, row: Mapping[str, Any]) -> ApplyOutcome:
        vid = str(row.get("id") or "")
        if self._paused:
            return ApplyOutcome("needs_manual", "GeekJob batch paused after UNKNOWN")
        if (row.get("source") != SOURCE or vacancy_id(str(row.get("url") or "")) != vid.lower()
                or row.get("review_state") != "APPROVED"
                or row.get("application_state") != "READY_TO_APPLY"):
            return ApplyOutcome("needs_manual", "GeekJob vacancy not approved/ready")
        letter = self.letter_dir / f"{vid}.txt"
        if not letter.is_file() or letter.is_symlink():
            return ApplyOutcome("needs_manual", "vacancy-specific GeekJob letter is not ready")
        return ApplyOutcome("ready")

    def apply(self, row: Mapping[str, Any]) -> ApplyOutcome:
        vid = str(row.get("id") or "")
        check = self.preflight(row)
        if check.status != "ready":
            return check
        text = (self.letter_dir / f"{vid}.txt").read_text(encoding="utf-8")
        page = self.page_factory()
        try:
            result = apply_geekjob_page(page, row, text)
            if result.status == "unknown":
                self._paused = True
            return result
        finally:
            page.close()
