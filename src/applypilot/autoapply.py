from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

LOGGER = logging.getLogger(__name__)
SUBMIT_SELECTOR = '[data-qa="vacancy-response-submit-popup"]'
RESUME_SELECTOR = '[data-qa="resume-title"]'
LETTER_SELECTOR = (
    '[data-qa="vacancy-response-letter-input"], '
    '[data-qa="vacancy-response-popup-form-letter-input"]'
)


def _contains_captcha(text: str) -> bool:
    lowered = text.casefold()
    return "captcha" in lowered or "капч" in lowered


def _normalize_resume_title(value: str) -> str:
    return " ".join(str(value).split()).casefold()


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
            text = verify_page.locator("body").inner_text(timeout=5000)
            flat = " ".join(text.split()).casefold()
            if (
                " ".join(str(resume_title).split()).casefold() in flat
                and "видимость резюме видно всем работодателям, зарегистрированным на hh.ru" in flat
            ):
                return True
            if check < 4:
                verify_page.wait_for_timeout(700)
        return False
    except Exception:
        LOGGER.debug("Could not independently verify HH resume visibility", exc_info=True)
        return False
    finally:
        if verify_page is not None:
            try:
                verify_page.close()
            except Exception:
                LOGGER.debug("Could not close visibility verification page", exc_info=True)


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


def _select_resume(page, dialog, resume: str) -> str:
    """Принимает единственное видимое резюме HH; иначе требует одно точное совпадение названия."""
    root = dialog or page
    options = root.locator(RESUME_SELECTOR)
    visible = []
    exact = []
    for index in range(options.count()):
        option = options.nth(index)
        try:
            if not option.is_visible(timeout=1000):
                continue
            lines = [" ".join(line.split()) for line in option.inner_text(timeout=1000).splitlines() if line.strip()]
            visible.append(option)
            if lines and _normalize_resume_title(lines[0]) == _normalize_resume_title(resume):
                exact.append(option)
        except Exception:
            LOGGER.debug("Could not inspect a resume option", exc_info=True)
    if len(exact) == 1:
        exact[0].click(timeout=8000)
        return ""
    if len(visible) == 1:
        return ""  # HH уже выбрал единственное резюме, доступное в этом popup отклика.
    return "resume selection is missing or ambiguous"


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


def apply_one(page, item: dict, resume: str, cover_letter: str = "", dry_run: bool = False,
              confirmation_timeout_seconds: float = 15.0,
              visibility_verified: bool = False) -> ApplyResult:
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
        if not allowed_hh_url(str(page.url)):
            return ApplyResult("needs_manual", "initial navigation left HH hostname")
        body = page.locator("body").inner_text().lower()
        if "вы откликнулись" in body or "отклик отправлен" in body:
            return ApplyResult("already_applied", "HH reports an existing application")
        if _contains_captcha(body):
            return ApplyResult("needs_manual", "CAPTCHA detected")
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
        page.wait_for_timeout(1500)
        if not allowed_hh_url(str(page.url)):
            return ApplyResult("needs_manual", "external ATS")
        body = page.locator("body").inner_text().lower()
        if "отклик отправлен" in body:
            return ApplyResult("success", "confirmed after first response action")
        if "вы уже откликались" in body or "уже откликались" in body:
            return ApplyResult("already_applied", "HH reports an existing application")
        if _contains_captcha(body):
            return ApplyResult("unknown", "CAPTCHA detected after first response action")
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
            if _contains_captcha(body):
                return ApplyResult("unknown", "CAPTCHA detected while waiting for HH response")
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
            if _contains_captcha(body):
                return ApplyResult("unknown", "CAPTCHA detected in full-page response")
        dialog = _response_dialog(page, submit)
        if dialog is None and not submit.is_visible(timeout=2000):
            if has_screening_form(page):
                return ApplyResult("needs_manual", "screening questions require manual review")
            return ApplyResult("unknown", "first response action had no follow-up dialog")
        response_root = dialog or page
        response_text = response_root.inner_text(timeout=2000).lower() if dialog is not None else body
        if _contains_captcha(response_text):
            return ApplyResult("unknown", "CAPTCHA detected after first response action")
        if "поменяйте видимость резюме" in response_text:
            if not visibility_verified:
                return ApplyResult("needs_manual", "resume visibility must be changed manually")
            # HH may display a stale warning after the exact resume is confirmed
            # public on its own page. Do not override a disabled submit control.
            if not submit.is_enabled(timeout=2000):
                return ApplyResult("needs_manual", "visibility warning and submit is disabled")
        if "сопроводительное письмо обязательное" in response_text and not cover_letter.strip():
            return ApplyResult("needs_manual", "required cover letter is missing")
        if has_screening_form(response_root):
            return ApplyResult("needs_manual", "screening questions require manual review")
        dialog_controls = response_root.locator(
            f"{RESUME_SELECTOR}, {LETTER_SELECTOR}, {SUBMIT_SELECTOR}"
        )
        if dialog_controls.count() == 0:
            return ApplyResult("unknown", "first response action had no follow-up dialog")
        resume_error = _select_resume(page, dialog, resume)
        if resume_error:
            return ApplyResult("needs_manual", resume_error)
        if cover_letter:
            field = response_root.locator(LETTER_SELECTOR).first
            if field.count() != 1 or not field.is_visible(timeout=2000):
                return ApplyResult("needs_manual", "cover letter field is missing")
            field.fill(cover_letter)
        if has_screening_form(response_root):
            return ApplyResult("needs_manual", "screening questions require manual review")
        if not submit.is_visible(timeout=8000):
            return ApplyResult("needs_manual", "confirmation button not found")
        # A disabled HH submit cannot complete a response. Diagnose it before
        # attempting a click, rather than misclassifying a click timeout as an
        # uncertain submission. Never force-click or retry a submitted action.
        if not submit.is_enabled(timeout=3000):
            return ApplyResult("needs_manual", "HH confirmation button is disabled")
        # HH может ненадолго оставлять кнопку popup неактивной, пока стабилизируется состояние формы.
        # Ожидание здесь остаётся одной попыткой клика: не используем force-click и не повторяем действие.
        submit.click(timeout=15000)
        page.wait_for_timeout(1500)
        if not allowed_hh_url(str(page.url)):
            return ApplyResult("needs_manual", "submission navigated to external ATS")
        confirmed = _wait_for_confirmation(page, confirmation_timeout_seconds)
        return confirmed or ApplyResult("unknown", "submission not confirmed by HH page")
    except Exception as exc:  # noqa: BLE001 — неоднозначный браузерный исход всегда классифицируется как unknown
        return ApplyResult("unknown", f"browser operation ended ambiguously: {str(exc)[:160]}")