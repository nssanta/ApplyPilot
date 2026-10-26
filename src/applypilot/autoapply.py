from __future__ import annotations

import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

LOGGER = logging.getLogger(__name__)
SUBMIT_SELECTOR = '[data-qa="vacancy-response-submit-popup"]'
RESUME_SELECTOR = '[data-qa="resume-title"]'
LETTER_SELECTOR = (
    '[data-qa="vacancy-response-letter-input"], '
    '[data-qa="vacancy-response-popup-form-letter-input"]'
)
CAPTCHA_NOTE_PREFIX = "CAPTCHA"


def _contains_captcha(text: str) -> bool:
    lowered = text.casefold()
    return "captcha" in lowered or "капч" in lowered or "подтвердите, что вы не робот" in lowered


def _normalize_resume_title(value: str) -> str:
    return " ".join(str(value).split()).casefold()


def _captcha_page(page) -> bool:
    """Detects an actual HH challenge, not a FAQ mention of the word CAPTCHA."""
    try:
        url = str(page.url).casefold()
        if "/captcha" in url or "captcha" in url:
            return True
        body = page.locator("body").inner_text(timeout=5000)
        return _contains_captcha(body)
    except Exception:
        LOGGER.debug("Could not inspect page for CAPTCHA", exc_info=True)
        return False


def _manual_captcha(page, timeout_seconds: float, interactive: bool) -> bool:
    """Keeps the real Chrome window open for the owner to pass HH's challenge.

    Returns True only when the challenge page has actually gone away. The
    caller must never retry a final submit just because this timed out.
    """
    if not _captcha_page(page):
        return True
    if not interactive or timeout_seconds <= 0:
        return False
    print(
        "HH CAPTCHA: решите проверку в открытом окне Chrome. "
        f"Ожидание до {int(timeout_seconds)} с; финальная кнопка не нажимается повторно.",
        file=sys.stderr,
        flush=True,
    )
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        if not _captcha_page(page):
            print("HH CAPTCHA passed; продолжаю текущую вакансию.", file=sys.stderr, flush=True)
            return True
    print("HH CAPTCHA not passed within timeout.", file=sys.stderr, flush=True)
    return False


def _letter_required(text: str) -> bool:
    lowered = " ".join(str(text).split()).casefold()
    return (
        "сопроводительное письмо обязательное" in lowered
        or "сопроводительное письмо обязательно" in lowered
        or ("письмо" in lowered and "обязательн" in lowered)
    )


@dataclass(frozen=True)
class ApplyResult:
    status: str
    note: str


def allowed_hh_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and (host == "hh.ru" or host.endswith(".hh.ru"))


def verify_resume_visibility(context, resume_id: str, resume_title: str) -> bool:
    """Read the exact HH resume page before permitting a stale visibility banner.

    This never changes resume settings. An unrecognized title, ID, URL, missing
    visibility indicator or network error blocks the override.
    """
    if not re.fullmatch(r"[a-f0-9]{32,64}", resume_id, flags=re.IGNORECASE):
        return False
    if not str(resume_title).strip():
        return False
    # HH sometimes returns a half-loaded body in the first Chrome tab.
    # Only a positive read from the exact public resume can authorize sending.
    for attempt in range(3):
        verify_page = None
        try:
            verify_page = context.new_page()
            verify_page.goto(
                f"https://hh.ru/resume/{resume_id}", wait_until="domcontentloaded", timeout=30000
            )
            page_path = urlparse(str(verify_page.url)).path.rstrip("/")
            if not allowed_hh_url(str(verify_page.url)) or not page_path.endswith(f"/resume/{resume_id}"):
                return False
            for check in range(5):
                try:
                    text = verify_page.locator("body").inner_text(timeout=5000)
                except Exception:
                    LOGGER.debug("HH resume body still loading", exc_info=True)
                    text = ""
                flat = " ".join(text.split()).casefold()
                if (
                    " ".join(str(resume_title).split()).casefold() in flat
                    and "видимость резюме видно всем работодателям, зарегистрированным на hh.ru" in flat
                ):
                    return True
                if check < 4:
                    verify_page.wait_for_timeout(800)
        except Exception:
            LOGGER.debug("HH visibility check attempt %s failed", attempt + 1, exc_info=True)
        finally:
            if verify_page is not None:
                try:
                    verify_page.close()
                except Exception:
                    LOGGER.debug("Could not close HH visibility page", exc_info=True)
    return False


def _response_dialog(page, submit):
    """Возвращает видимый popup отклика с финальным элементом отправки."""
    for selector in (
        "xpath=ancestor::*[@role='dialog'][1]",
        "xpath=ancestor::*[contains(@class,'vacancy-response-popup')][1]",
    ):
        try:
            dialog = submit.locator(selector).first
            if dialog.count() == 1 and dialog.is_visible(timeout=1000):
                return dialog
        except Exception:
            LOGGER.debug("Could not inspect response-dialog root %s", selector, exc_info=True)
    try:
        dialogs = page.locator("[role='dialog']")
        for index in range(dialogs.count()):
            dialog = dialogs.nth(index)
            if dialog.is_visible(timeout=1000) and dialog.locator(SUBMIT_SELECTOR).count() == 1:
                return dialog
    except Exception:
        LOGGER.debug("Could not locate the response dialog from page roots", exc_info=True)
    return None


def has_screening_form(page_or_dialog) -> bool:
    """Определяет форму диалога отклика, не путая её с публичным FAQ вакансии."""
    for selector in ("[data-qa='task-body']", ".vacancy-response-popup__screening"):
        try:
            if page_or_dialog.locator(selector).first.is_visible(timeout=2000):
                return True
        except Exception:
            LOGGER.debug("Could not inspect response-dialog selector %s", selector, exc_info=True)
            continue
    return False


def screening_answer_for(question: str, answers: dict | None) -> str:
    """Return only an explicitly configured private answer for a known prompt."""
    text = " ".join(str(question or "").casefold().split())
    answers = answers or {}
    if re.search(r"salary|compensation|зарплат|доход|оклад", text):
        key = "salary"
    elif re.search(r"english|английск|уровень владения языком", text):
        key = "english"
    elif re.search(r"why|мотивац|почему|заинтерес|интерес к вакансии", text):
        key = "motivation"
    else:
        return ""
    return str(answers.get(key) or "").strip()


def _fill_known_screening_answers(root, answers: dict | None,
                                  facts: dict | None = None) -> tuple[bool, list[str]]:
    """Fill fields from private facts; leave unknown fields for review.

    When structured facts are configured, the generic form engine handles
    text, textarea, number, select, radio and checkbox controls.  The legacy
    text-only path remains as a compatibility fallback for callers that have
    no facts block.
    """
    if facts is not None:
        try:
            from .forms import fill_screening_form

            result = fill_screening_form(root, answers or {}, facts or {})
            if result.fields:
                return result.resolved, list(result.unresolved)
        except Exception:
            LOGGER.debug("Structured screening form failed; trying text fallback", exc_info=True)
    try:
        tasks = root.locator("[data-qa='task-body']")
        count = tasks.count()
    except Exception:
        LOGGER.debug("Could not inspect HH screening tasks", exc_info=True)
        return False, ["screening fields could not be inspected"]
    if count == 0:
        return False, ["screening form has no readable questions"]
    unresolved: list[str] = []
    for index in range(count):
        task = tasks.nth(index)
        try:
            question = " ".join(task.inner_text(timeout=1000).split())
            answer = screening_answer_for(question, answers)
            fields = task.locator("textarea, input[type='text'], input:not([type]), input[type='number']")
            if not answer or fields.count() != 1:
                unresolved.append(question[:240] or f"question {index + 1}")
                continue
            field = fields.first
            if not field.is_visible(timeout=1000) or not field.is_enabled(timeout=1000):
                unresolved.append(question[:240] or f"question {index + 1}")
                continue
            field.fill(answer)
        except Exception:
            LOGGER.debug("Could not fill HH screening task %s", index, exc_info=True)
            unresolved.append(f"question {index + 1}")
    return not unresolved, unresolved


def _screening_manual_note(unresolved: list[str], answers: dict | None) -> str:
    if not answers:
        return "screening questions require manual review"
    return "unanswered screening questions: " + "; ".join(unresolved)


def _select_resume(page, dialog, resume: str) -> str:
    """Выбирает точное резюме; единственное видимое резюме принимает только при совпадении."""
    root = dialog or page
    options = root.locator(RESUME_SELECTOR)
    visible = []
    exact = []
    titles = []
    for index in range(options.count()):
        option = options.nth(index)
        try:
            if not option.is_visible(timeout=1000):
                continue
            lines = [" ".join(line.split()) for line in option.inner_text(timeout=1000).splitlines() if line.strip()]
            visible.append(option)
            titles.append(lines[0] if lines else "")
            if lines and _normalize_resume_title(lines[0]) == _normalize_resume_title(resume):
                exact.append(option)
        except Exception:
            LOGGER.debug("Could not inspect a resume option", exc_info=True)
    if len(exact) == 1:
        exact[0].click(timeout=8000)
        return ""
    if len(exact) > 1:
        return "resume selection is ambiguous: duplicate exact titles"
    if len(visible) == 1:
        # HH may preselect the only applicable resume. Accept it only when the
        # title is the configured one; never silently send with another resume.
        return "" if _normalize_resume_title(titles[0]) == _normalize_resume_title(resume) else (
            f"HH preselected a different resume: {titles[0]!r}"
        )
    if not visible:
        return "resume selection is missing"
    return f"configured resume is not available in HH popup: {titles!r}"


def _wait_for_confirmation(page, timeout_seconds: float) -> ApplyResult | None:
    checks = max(1, round(max(0.0, timeout_seconds) * 2) + 1)
    for check in range(checks):
        body = page.locator("body").inner_text().lower()
        if "отклик отправлен" in body or "ваш отклик отправлен" in body or "вы откликнулись" in body:
            return ApplyResult("success", "confirmed by HH page after submission")
        if "вы уже откликались" in body or "уже откликались" in body:
            return ApplyResult("already_applied", "HH reports an existing application")
        if _contains_captcha(body):
            return ApplyResult("unknown", "CAPTCHA detected after submission")
        if check + 1 < checks:
            page.wait_for_timeout(500)
    return None


def _classify_after_submit(page, item: dict, cover_letter: str, confirmation_timeout_seconds: float,
                           manual_captcha_timeout: float, interactive: bool) -> ApplyResult:
    page.wait_for_timeout(1500)
    if not allowed_hh_url(str(page.url)):
        return ApplyResult("needs_manual", "submission navigated to external ATS")
    body = page.locator("body").inner_text(timeout=5000).lower()
    if _contains_captcha(body) or "/captcha" in str(page.url).casefold():
        if _manual_captcha(page, manual_captcha_timeout, interactive):
            confirmed = _wait_for_confirmation(page, confirmation_timeout_seconds)
            if confirmed is not None:
                return confirmed
        return ApplyResult("unknown", "CAPTCHA detected after submission")
    confirmed = _wait_for_confirmation(page, confirmation_timeout_seconds)
    if confirmed is not None:
        return confirmed
    return ApplyResult("unknown", "submission not confirmed by HH page")


def apply_one(page, item: dict, resume: str, cover_letter: str = "", dry_run: bool = False,
              confirmation_timeout_seconds: float = 15.0,
              visibility_verified: bool = False,
              manual_captcha_timeout: float = 0.0,
              interactive: bool = False,
              screening_answers: dict | None = None,
              screening_facts: dict | None = None) -> ApplyResult:
    item["cover_letter_submitted"] = False
    if not allowed_hh_url(str(item.get("url", ""))):
        return ApplyResult("needs_manual", "URL is not an allowed HH hostname")
    if not resume.strip():
        return ApplyResult("needs_manual", "resume is not selected")
    if ((item.get("cover_letter_required") or item.get("requires_cover_letter") or item.get("letter_required"))
            and not cover_letter.strip()):
        return ApplyResult("needs_manual", "required cover letter is missing")
    if dry_run:
        return ApplyResult("prepared", f"would apply with resume={resume}")
    try:
        page.goto(item["url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(random.randint(700, 1600))
        if not allowed_hh_url(str(page.url)):
            return ApplyResult("needs_manual", "initial navigation left HH hostname")
        body = page.locator("body").inner_text().lower()
        if "вы откликнулись" in body or "отклик отправлен" in body:
            return ApplyResult("already_applied", "HH reports an existing application")
        if _contains_captcha(body) or "/captcha" in str(page.url).casefold():
            if not _manual_captcha(page, manual_captcha_timeout, interactive):
                return ApplyResult("needs_manual",
                                   f"{CAPTCHA_NOTE_PREFIX} before submission; manual pass required")
            body = page.locator("body").inner_text().lower()
            if "вы откликнулись" in body or "отклик отправлен" in body:
                return ApplyResult("already_applied", "HH reports an existing application")
        links = page.locator('[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link"]')
        if links.count() == 0:
            return ApplyResult("needs_manual", "application button not found")
        try:
            href = str(links.first.get_attribute("href") or "")
        except Exception:  # noqa: BLE001 — недоступный атрибут не является результатом отправки
            href = ""
        if href and not allowed_hh_url(urljoin(str(page.url), href)):
            return ApplyResult("needs_manual", "application action points to an external ATS")
        links.first.click(timeout=15000)
        page.wait_for_timeout(random.randint(1200, 2600))
        if not allowed_hh_url(str(page.url)):
            return ApplyResult("needs_manual", "external ATS")
        body = page.locator("body").inner_text().lower()
        if "отклик отправлен" in body:
            return ApplyResult("success", "confirmed after first response action")
        if "вы уже откликались" in body or "уже откликались" in body:
            return ApplyResult("already_applied", "HH reports an existing application")
        if _contains_captcha(body) or "/captcha" in str(page.url).casefold():
            # No final submit has happened yet. Solving the challenge can
            # continue this vacancy; timing out is manual, never UNKNOWN.
            if not _manual_captcha(page, manual_captcha_timeout, interactive):
                return ApplyResult("needs_manual",
                                   f"{CAPTCHA_NOTE_PREFIX} before final submission; no submit attempted")
            body = page.locator("body").inner_text().lower()
            if "отклик отправлен" in body or "вы откликнулись" in body:
                return ApplyResult("success", "confirmed after manual CAPTCHA pass")
        submit = page.locator(SUBMIT_SELECTOR).first
        # The first click may start a slow navigation while the URL still
        # appears to be /vacancy/..., or an AJAX popup may render late.
        # Wait for the one legitimate HH submit control before classifying
        # absence of a follow-up form as ambiguous. Never click during wait.
        if not submit.is_visible(timeout=1000):
            try:
                submit.wait_for(state="visible", timeout=8500)
            except Exception:
                LOGGER.debug("HH response control did not appear within the grace period", exc_info=True)
            body = page.locator("body").inner_text(timeout=5000).lower()
            if "отклик отправлен" in body or "вы откликнулись" in body:
                return ApplyResult("success", "confirmed after delayed HH response")
            if "вы уже откликались" in body or "уже откликались" in body:
                return ApplyResult("already_applied", "HH reports an existing application")
            if (_contains_captcha(body) or "/captcha" in str(page.url).casefold()) and not _manual_captcha(
                page, manual_captcha_timeout, interactive
            ):
                return ApplyResult("needs_manual",
                                   f"{CAPTCHA_NOTE_PREFIX} while waiting for HH response; no submit attempted")
        # HH has two legitimate response interfaces: a popup and a separate
        # /applicant/vacancy_response page. Playwright's is_visible() does not
        # wait for loading, so the latter could previously become "unknown"
        # before its submit control and screening questions had rendered.
        if urlparse(str(page.url)).path.rstrip("/") == "/applicant/vacancy_response":
            try:
                submit.wait_for(state="visible", timeout=10000)
            except Exception:
                LOGGER.debug("HH full-page response form did not become ready", exc_info=True)
            page.wait_for_timeout(600)
            body = page.locator("body").inner_text(timeout=5000).lower()
            if "отклик отправлен" in body or "вы откликнулись" in body:
                return ApplyResult("success", "confirmed on HH full-page response")
            if "вы уже откликались" in body or "уже откликались" in body:
                return ApplyResult("already_applied", "HH reports an existing application")
            if (_contains_captcha(body) or "/captcha" in str(page.url).casefold()) and not _manual_captcha(
                page, manual_captcha_timeout, interactive
            ):
                return ApplyResult("needs_manual",
                                   f"{CAPTCHA_NOTE_PREFIX} in full-page response; no submit attempted")
        dialog = _response_dialog(page, submit)
        screening_resolved = False
        if dialog is None and not submit.is_visible(timeout=2000):
            if has_screening_form(page):
                screening_resolved, unresolved = _fill_known_screening_answers(
                    page, screening_answers, screening_facts,
                )
                if not screening_resolved:
                    return ApplyResult(
                        "needs_manual", _screening_manual_note(unresolved, screening_answers),
                    )
                try:
                    submit.wait_for(state="visible", timeout=5000)
                except Exception:
                    LOGGER.debug("HH screening submit did not appear after known answers", exc_info=True)
                if not submit.is_visible(timeout=1000):
                    return ApplyResult("needs_manual", "screening answers filled; submit control is unavailable")
                dialog = _response_dialog(page, submit)
            else:
                return ApplyResult("unknown", "first response action had no follow-up dialog")
        response_root = dialog or page
        response_text = response_root.inner_text(timeout=2000).lower() if dialog is not None else body
        if _contains_captcha(response_text):
            if not _manual_captcha(page, manual_captcha_timeout, interactive):
                return ApplyResult("needs_manual",
                                   f"{CAPTCHA_NOTE_PREFIX} in response form; no submit attempted")
            response_text = response_root.inner_text(timeout=2000).lower()
        letter_required = _letter_required(response_text)
        if letter_required and not cover_letter.strip():
            # The visibility banner is frequently stale on HH. The actual
            # blocker in this state is the mandatory empty cover letter.
            return ApplyResult("needs_manual", "required cover letter is missing")
        resume_error = _select_resume(page, dialog, resume)
        if resume_error:
            return ApplyResult("needs_manual", resume_error)
        if has_screening_form(response_root):
            screening_resolved, unresolved = _fill_known_screening_answers(
                response_root, screening_answers, screening_facts,
            )
            if not screening_resolved:
                return ApplyResult(
                    "needs_manual", _screening_manual_note(unresolved, screening_answers),
                )
        if cover_letter:
            field = response_root.locator(LETTER_SELECTOR).first
            has_letter_field = field.count() > 0 and field.is_visible(timeout=2000)
            if not has_letter_field and letter_required:
                return ApplyResult(
                    "needs_manual", "required cover letter field is missing",
                )
            if has_letter_field:
                field.fill(cover_letter)
                item["cover_letter_submitted"] = True
                page.wait_for_timeout(random.randint(500, 1200))
        if has_screening_form(response_root) and not screening_resolved:
            screening_resolved, unresolved = _fill_known_screening_answers(
                response_root, screening_answers, screening_facts,
            )
            if not screening_resolved:
                return ApplyResult(
                    "needs_manual", _screening_manual_note(unresolved, screening_answers),
                )
        if not submit.is_visible(timeout=8000):
            return ApplyResult("needs_manual", "confirmation button not found")
        # A disabled HH submit cannot complete a response. Diagnose it before
        # attempting a click, rather than misclassifying a click timeout as an
        # uncertain submission. Never force-click or retry a submitted action.
        if not submit.is_enabled(timeout=3000):
            if "поменяйте видимость резюме" in response_text:
                return ApplyResult(
                    "needs_manual",
                    "HH visibility warning and submit is disabled (resume is public on HH page)",
                )
            return ApplyResult("needs_manual", "HH confirmation button is disabled")
        if "поменяйте видимость резюме" in response_text:
            # Live HH evidence: the banner can remain after the exact resume
            # page confirms public visibility, while submit is already enabled.
            # An enabled button is the authoritative UI state; log the stale
            # warning but never block a valid response on it.
            LOGGER.info("HH stale visibility banner ignored because submit is enabled")
        # HH может ненадолго оставлять кнопку popup неактивной, пока стабилизируется состояние формы.
        # Ожидание здесь остаётся одной попыткой клика: не используем force-click и не повторяем действие.
        page.wait_for_timeout(random.randint(1200, 2800))
        if not submit.is_enabled(timeout=2000):
            return ApplyResult("needs_manual", "HH confirmation button became disabled")
        try:
            submit.click(timeout=15000)
        except Exception as exc:  # noqa: BLE001 — клик мог уйти; повторять нельзя
            page.wait_for_timeout(1000)
            body = page.locator("body").inner_text(timeout=5000).lower()
            if "отклик отправлен" in body or "вы откликнулись" in body:
                return ApplyResult("success", "confirmed after ambiguous submit click")
            if "вы уже откликались" in body or "уже откликались" in body:
                return ApplyResult("already_applied", "HH reports an existing application")
            if _contains_captcha(body) or "/captcha" in str(page.url).casefold():
                if _manual_captcha(page, manual_captcha_timeout, interactive):
                    confirmed = _wait_for_confirmation(page, confirmation_timeout_seconds)
                    if confirmed is not None:
                        return confirmed
                return ApplyResult("unknown", "CAPTCHA detected after ambiguous submit click")
            return ApplyResult("unknown",
                               f"final submit click was ambiguous ({str(exc)[:120]}); not retried")
        return _classify_after_submit(
            page, item, cover_letter, confirmation_timeout_seconds,
            manual_captcha_timeout, interactive,
        )
    except Exception as exc:  # noqa: BLE001 — неоднозначный браузерный исход всегда классифицируется как unknown
        return ApplyResult("unknown", f"browser operation ended ambiguously: {str(exc)[:160]}")
