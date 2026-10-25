"""Offline mock-browser checks for the authenticated GeekJob form.

All HTTP requests are intercepted in Playwright and served from synthetic HTML.
No access to real candidate accounts or live GeekJob is performed here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

from applypilot.geekjob_apply import (
    apply_geekjob_page,
    inspect_geekjob_page,
    vacancy_id,
)

VID = "6ab0dcc140fca7e4f5078639"
URL = f"https://geekjob.ru/vacancy/{VID}"
ROW = {
    "id": VID,
    "name": "Data Scientist / AI Engineer",
    "source": "geekjob.ru",
    "url": URL,
    "score": 77,
    "review_state": "APPROVED",
    "application_state": "READY_TO_APPLY",
    "resume": "Example AI Engineer",
}
LETTER = "Здравствуйте! Разрабатываю AI/LLM-сервисы на Python, интегрирую API и агентные инструменты. Буду рад обсудить задачу."

@dataclass
class BrowserScenario:
    applied: bool = False
    post_count: int = 0
    has_cv: bool = True
    remote: bool = True
    excludes_russia: bool = False
    extra_question: bool = False
    show_form: bool = True
    post_status: int = 200
    reply_in_history: bool = True

    def vacancy_html(self) -> str:
        desc = "Python API, LLM and agent services."
        if self.excludes_russia:
            desc += " Удалённо кроме РФ."
        form = ""
        if self.show_form:
            cv = ('<option value="0">Example AI Engineer</option>'
                  if self.has_cv else "<option>Other resume</option>")
            additional = '<input id="secret-question" required>' if self.extra_question else ""
            form = f"""<form>
                <label for="mycvlist">Выберите резюме</label><select id="mycvlist">{cv}</select>
                <label for="respond-text">Измените сопроводительный текст</label>
                <textarea id="respond-text" required></textarea>{additional}
                <button type="submit">Откликнуться на вакансию</button></form>"""
        return f"""<!doctype html><html lang="ru"><body>
            <article class="vacancy"><h1>Data Scientist / AI Engineer</h1>
            <div class="jobinfo">{"Удаленная работа" if self.remote else "Офис в Москве"}</div>
            <div class="description">{desc}</div></article>
            {form}
            <script>
              const f=document.querySelector('form');
              if(f)f.addEventListener('submit', async e=>{{
                e.preventDefault();
                await fetch('/rest/responses/create',{{method:'POST',body:'test'}})
              }});
            </script>
            </body></html>"""

    def serve(self, route) -> None:
        request = route.request
        url = request.url
        if request.method == "POST":
            self.post_count += 1
            route.fulfill(status=self.post_status, body='{"ok":true}',content_type="application/json")
        elif "/responses" in url and "/rest/" not in url:
            anchor = f'<a href="/vacancy/{VID}">Existing response</a>' if self.applied and self.reply_in_history else ""
            route.fulfill(status=200,body=f"<html><body>{anchor}</body></html>",content_type="text/html; charset=utf-8")
        elif "/vacancy/" in url:
            route.fulfill(status=200,body=self.vacancy_html(),content_type="text/html; charset=utf-8")
        else:
            route.fulfill(status=404,body="unknown",content_type="text/plain")
        if request.method == "POST" and self.post_status == 200:
            self.applied = True


@pytest.fixture()
def page_with_scenario():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome",headless=True)
        except playwright_api.Error as exc:
            if "is not found" in str(exc) or "Executable doesn't exist" in str(exc):
                pytest.skip("optional Google Chrome browser is not installed")
            raise
        ctx = browser.new_context()
        created = []
        def factory(scenario: BrowserScenario):
            page = ctx.new_page()
            page.route("**/*",scenario.serve)
            created.append(page)
            return page
        yield factory
        ctx.close()
        browser.close()


def test_geekjob_readonly_inspector_finds_actual_form(page_with_scenario) -> None:
    scene = BrowserScenario()
    page = page_with_scenario(scene)
    result = inspect_geekjob_page(page, ROW)
    assert result["status"] == "native_form"
    assert result["selected_resume"] == "Example AI Engineer"
    assert result["letter_required"] is True
    assert scene.post_count == 0


@pytest.mark.parametrize("modification,blocked",[
    ({"has_cv": False}, "resume_missing_or_ambiguous"),
    ({"remote": False}, "not_remote"),
    ({"excludes_russia": True}, "remote_excludes_russia"),
    ({"extra_question": True}, "extra_questions"),
    ({"show_form": False}, "native_form_missing"),
])
def test_geekjob_inspector_rejects_unsupported_forms(page_with_scenario, modification, blocked) -> None:
    scene = BrowserScenario(**modification)
    page = page_with_scenario(scene)
    status = inspect_geekjob_page(page, ROW)
    assert status["status"] == blocked
    assert scene.post_count == 0


def test_geekjob_success_needs_post_and_exact_history(page_with_scenario) -> None:
    scene = BrowserScenario()
    page = page_with_scenario(scene)
    outcome = apply_geekjob_page(page, ROW, LETTER)
    assert outcome.status == "success"
    assert outcome.receipt["history_verified"] is True
    assert scene.post_count == 1


def test_geekjob_submitted_without_history_stays_unknown(page_with_scenario) -> None:
    scene = BrowserScenario(reply_in_history=False)
    outcome = apply_geekjob_page(page_with_scenario(scene), ROW, LETTER)
    assert outcome.status == "unknown"
    assert scene.post_count == 1


def test_geekjob_existing_history_never_clicks(page_with_scenario) -> None:
    scene = BrowserScenario(applied=True)
    result = apply_geekjob_page(page_with_scenario(scene), ROW, LETTER)
    assert result.status == "already_applied"
    assert scene.post_count == 0


@pytest.mark.parametrize("row,letter", [
    ({**ROW, "review_state": "UNREVIEWED"}, LETTER),
    ({**ROW, "application_state": "UNKNOWN"}, LETTER),
    ({**ROW, "source": "hh.ru"}, LETTER),
    ({**ROW, "url": "https://example.invalid/vacancy/" + VID}, LETTER),
    (ROW, ""),
])
def test_geekjob_never_submits_unapproved_or_mismatched(
    page_with_scenario, row, letter,
) -> None:
    scene = BrowserScenario()
    result = apply_geekjob_page(page_with_scenario(scene), row, letter)
    assert result.status == "needs_manual"
    assert scene.post_count == 0


def test_only_canonical_24_digit_hex_vacancy_ids_allowed() -> None:
    assert vacancy_id(URL) == VID
    for url in ("http://geekjob.ru/vacancy/" + VID,
                "https://evil.com/vacancy/" + VID,
                "https://geekjob.ru/vacancy/notanid",
                "https://geekjob.ru/vacancy/" + VID + "/apply"):
        assert vacancy_id(url) == ""


def test_geekjob_never_guesses_a_resume_title():
    row = {**ROW, "resume": ""}
    result = inspect_geekjob_page(None, row)
    assert result["status"] == "resume_not_configured"
    assert result["submitted"] is False
