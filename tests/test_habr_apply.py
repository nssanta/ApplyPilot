"""Offline tests of the real Habr native response contract (no network or accounts)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from applypilot.habr_apply import HabrPageDriver, apply_habr_page, inspect_habr_page

ROW = {
    "source": "career.habr.com",
    "id": "1000169407",
    "url": "https://career.habr.com/vacancies/1000169407",
    "name": "Python-разработчик AI-сервисов (LLM / RAG)",
    "company": "QSOFT",
    "review_state": "APPROVED",
    "application_state": "READY_TO_APPLY",
    "channel_key": "career.habr.com:1000169407",
    "channel_type": "native_form",
}


class StubLocator:
    def __init__(self, page: StubHabrPage, kind: str) -> None:
        self.page = page
        self.kind = kind

    def count(self) -> int:
        if self.kind == "history_link":
            return int(self.page.applied and self.page.history_available)
        if self.kind == "response_section":
            return 0
        if self.kind in {"submit_section", "submit", "vacancy_header"}:
            return 1
        return 0

    def locator(self, selector: str) -> StubLocator:
        if self.kind == "submit_section" and selector == "button[type='submit']":
            return StubLocator(self.page, "submit")
        return StubLocator(self.page, "missing")

    def filter(self, *, has_text=None) -> StubLocator:
        return self

    @property
    def first(self) -> StubLocator:
        return self

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def inner_text(self, timeout: int = 0) -> str:
        if self.kind == "vacancy_header":
            return "Можно удалённо" if self.page.remote else "Только офис, Москва"
        if self.page.on_history:
            return "Ваши отклики\n" + (
                "Компания QSOFT\nPython-разработчик AI-сервисов (LLM / RAG)"
                if self.page.applied and self.page.history_available else "Нет откликов"
            )
        return (
            "Python-разработчик AI-сервисов (LLM / RAG)\n"
            + ("Можно удалённо\n" if self.page.remote else "Офис в Москве\n")
            + ("Отклик отправлен" if self.page.applied and self.page.notice else "Ваш отклик")
        )

    def click(self, timeout: int = 0) -> None:
        self.page.click_count += 1
        if not self.page.send_request:
            return
        request = SimpleNamespace(
            method="POST",
            url="https://career.habr.com/api/frontend/vacancies/1000169407/responses",
        )
        for callback in list(self.page.listeners["request"]):
            callback(request)
        response = SimpleNamespace(
            request=request, url=request.url, status=self.page.post_status,
        )
        for callback in list(self.page.listeners["response"]):
            callback(response)
        if self.page.post_status == 200:
            self.page.applied = True


class StubHabrPage:
    def __init__(
        self, *, prior=False, remote=True, send_request=True,
        post_status=200, history_available=True, notice=True,
    ) -> None:
        self.applied = prior
        self.remote = remote
        self.send_request = send_request
        self.post_status = post_status
        self.history_available = history_available
        self.notice = notice
        self.on_history = False
        self.url = ""
        self.click_count = 0
        self.listeners: dict[str, list] = {"request": [], "response": []}
        self.routes: list[tuple[str, object]] = []
        self.closed = False
        self.visited: list[str] = []

    def goto(self, url: str, *, wait_until: str, timeout: int):
        self.visited.append(url)
        self.url = url
        self.on_history = url.endswith("/responses")
        return SimpleNamespace(status=200)

    def locator(self, selector: str) -> StubLocator:
        if selector == "body":
            return StubLocator(self, "body")
        if selector.startswith("a[href="):
            return StubLocator(self, "history_link")
        if selector == ".vacancy-header":
            return StubLocator(self, "vacancy_header")
        if selector == ".create-vacancy-response__apply":
            return StubLocator(self, "submit_section")
        if selector == ".create-vacancy-response":
            return StubLocator(self, "response_section")
        return StubLocator(self, "missing")

    def on(self, event: str, callback) -> None:
        self.listeners[event].append(callback)

    def route(self, pattern: str, handler) -> None:
        self.routes.append((pattern, handler))

    def remove_listener(self, event: str, callback) -> None:
        self.listeners[event].remove(callback)

    def wait_for_timeout(self, _ms: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_verified_habr_apply_has_single_post_and_exact_history_receipt() -> None:
    page = StubHabrPage()
    result = apply_habr_page(page, ROW)
    assert result.status == "success"
    assert result.receipt["post_status"] == 200
    assert result.receipt["verified_history"] is True
    assert page.click_count == 1
    assert page.visited[-1] == "https://career.habr.com/responses"
    assert all(not listeners for listeners in page.listeners.values())


def test_existing_application_skips_submit() -> None:
    page = StubHabrPage(prior=True)
    result = apply_habr_page(page, ROW)
    assert result.status == "already_applied"
    assert page.click_count == 0


def test_habr_remote_requirement_is_enforced() -> None:
    page = StubHabrPage(remote=False)
    result = apply_habr_page(page, ROW)
    assert result.status == "needs_manual"
    assert page.click_count == 0


def test_no_post_after_click_is_unknown_and_never_fake_success() -> None:
    page = StubHabrPage(send_request=False)
    result = apply_habr_page(page, ROW)
    assert result.status == "unknown"
    assert page.click_count == 1


def test_post_without_history_is_unknown_not_success() -> None:
    page = StubHabrPage(history_available=False)
    result = apply_habr_page(page, ROW)
    assert result.status == "unknown"
    assert page.click_count == 1


def test_habr_unauthorized_or_unapproved_row_never_clicks() -> None:
    page = StubHabrPage()
    for patch in (
        {"review_state": "UNREVIEWED"},
        {"application_state": "NEEDS_FORM"},
        {"source": "hh.ru"},
        {"url": "https://attacker.example/vacancies/1000169407"},
        {"id": "not_the_same_id"},
    ):
        result = apply_habr_page(page, {**ROW, **patch})
        assert result.status == "needs_manual"
    assert page.click_count == 0


def test_habr_driver_checks_approval_and_closes_browser_page() -> None:
    page = StubHabrPage()
    driver = HabrPageDriver(lambda: page)
    assert driver.submit_verified
    assert driver.preflight(ROW).status == "ready"
    assert page.routes and page.routes[0][0] == "**/*"
    assert driver.preflight({**ROW, "application_state": "NEEDS_FORM"}).status == "needs_manual"
    result = driver.apply(ROW)
    assert result.status == "success"
    assert page.closed is True


def test_habr_verified_driver_routes_real_dispatch_and_prevents_duplicate(tmp_path) -> None:
    from applypilot.dispatch import dispatch_apply, dispatch_plan
    from applypilot.storage import Store

    store = Store(tmp_path / "jobs.sqlite3")
    stamp = "2026-10-22T00:00:00+00:00"
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO vacancies
            (account,source,vacancy_id,title,company,url,application_state,
             created_at,first_seen_at,last_seen_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("habr-primary", "career.habr.com", "1000169407",
             ROW["name"], "QSOFT", ROW["url"], "READY_TO_APPLY",
             stamp, stamp, stamp, stamp),
        )
        conn.execute(
            """INSERT INTO vacancy_track_scores
            (account,source,vacancy_id,track,score,score_decision,filter_status,
             review_state,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("habr-primary", "career.habr.com", "1000169407", "ai_core",
             70, "pass", "unknown", "APPROVED", stamp),
        )
    no_driver = dispatch_plan(
        store, "default", "ai_core", ["career.habr.com"],
        accounts={"career.habr.com": "habr-primary"},
    )
    assert no_driver["planned_count"] == 0
    assert no_driver["blocked_count"] == 1

    page = StubHabrPage()
    driver = HabrPageDriver(lambda: page)
    first = dispatch_apply(
        store, "default", "ai_core", ["career.habr.com"], 10,
        allow_live=True, drivers={"career.habr.com": driver},
        accounts={"career.habr.com": "habr-primary"}, run_id="one-habr",
    )
    assert first["summary"] == {"success": 1}
    assert page.click_count == 1
    assert store.source_attempts("habr-primary", "career.habr.com")[0]["status"] == "success"

    second = dispatch_apply(
        store, "default", "ai_core", ["career.habr.com"], 10,
        allow_live=True, drivers={"career.habr.com": HabrPageDriver(StubHabrPage)},
        accounts={"career.habr.com": "habr-primary"}, run_id="never-retry",
    )
    assert second["planned_count"] == 0
    assert len(store.source_attempts("habr-primary", "career.habr.com")) == 1


def test_readonly_habr_form_audit_classifies_native_remote_form() -> None:
    page = StubHabrPage()
    result = inspect_habr_page(page, ROW)
    assert result["status"] == "native_ready_for_approval"
    assert result["native_submit"] is True
    assert result["remote_verified"] is True
    assert result["visible_fields"] == 0
    assert result["submitted"] is False
    assert page.click_count == 0


def test_readonly_habr_audit_never_calls_apply_on_nonremote_page() -> None:
    page = StubHabrPage(remote=False)
    result = inspect_habr_page(page, ROW)
    assert result["status"] == "remote_not_verified"
    assert not result["remote_verified"]
    assert page.click_count == 0


def test_readonly_habr_audit_rejects_noncanonical_host_without_loading() -> None:
    page = StubHabrPage()
    result = inspect_habr_page(page, {**ROW, "url": "https://evil.example/vacancies/1000169407"})
    assert result["status"] == "invalid_url"
    assert page.visited == []


def test_habr_driver_stops_batch_after_unverified_click() -> None:
    pages: list[StubHabrPage] = []

    def factory() -> StubHabrPage:
        page = StubHabrPage(send_request=False)
        pages.append(page)
        return page

    driver = HabrPageDriver(factory, min_interval_seconds=0)
    first = driver.apply(ROW)
    assert first.status == "unknown"
    assert driver.preflight(ROW).status == "needs_manual"
    second = driver.apply(ROW)
    assert second.status == "needs_manual"
    assert len(pages) == 1
    assert pages[0].click_count == 1


def test_habr_driver_paces_two_confirmed_jobs(monkeypatch) -> None:
    from applypilot import habr_apply

    times = iter((100.0, 100.7, 100.7))
    calls: list[float] = []
    monkeypatch.setattr(habr_apply.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(habr_apply.time, "sleep", calls.append)
    driver = HabrPageDriver(StubHabrPage, min_interval_seconds=2)
    assert driver.apply(ROW).status == "success"
    assert driver.apply(ROW).status == "success"
    assert len(calls) == 1
    assert round(calls[0], 2) == 1.3


def test_habr_multi_job_dispatch_quarantines_unknown_and_continues(tmp_path, monkeypatch) -> None:
    from applypilot.dispatch import ApplyOutcome, dispatch_apply
    from applypilot.storage import Store

    store = Store(tmp_path / "jobs.sqlite3")
    ids = ("1000169408", "1000169409", "1000169410")
    stamp = "2026-10-22T00:00:00+00:00"
    with store.connect() as conn:
        for vacancy_id in ids:
            conn.execute(
                """INSERT INTO vacancies
                (account,source,vacancy_id,title,company,url,application_state,
                 created_at,first_seen_at,last_seen_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("habr-primary", "career.habr.com", vacancy_id,
                 ROW["name"], "Example", f"https://career.habr.com/vacancies/{vacancy_id}",
                 "READY_TO_APPLY", stamp, stamp, stamp, stamp),
            )
            conn.execute(
                """INSERT INTO vacancy_track_scores
                (account,source,vacancy_id,track,score,score_decision,filter_status,
                 review_state,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("habr-primary", "career.habr.com", vacancy_id, "ai_core",
                 75, "pass", "pass", "APPROVED", stamp),
            )

    def simulated_apply(page, row, **kwargs):
        if row["id"] == ids[0]:
            return ApplyOutcome("success", "confirmed", receipt={"provider": "career.habr.com", "vacancy_id": row["id"],
                    "post_status": 200, "verified_history": True,
                    "post_path": f"/api/frontend/vacancies/{row['id']}/responses"})
        return ApplyOutcome("unknown", "after click, confirmation missing")

    monkeypatch.setattr("applypilot.habr_apply.apply_habr_page", simulated_apply)
    pages: list[StubHabrPage] = []

    def factory():
        page = StubHabrPage()
        pages.append(page)
        return page

    driver = HabrPageDriver(factory, min_interval_seconds=0)
    result = dispatch_apply(
        store, "default", "ai_core", ["career.habr.com"], 100,
        allow_live=True, drivers={"career.habr.com": driver},
        accounts={"career.habr.com": "habr-primary"}, run_id="multi-batch",
    )
    assert result["summary"] == {
        "success": 1, "unknown": 2,
    }
    # All candidates receive read-only preflight and an independent attempt.
    assert len(pages) == 6
    assert all(page.routes for page in pages[:3])
    assert all(page.closed for page in pages)
    attempts = {item["vacancy_id"]: item["status"] for item in store.source_attempts(
        "habr-primary", "career.habr.com",
    )}
    assert attempts == {ids[0]: "success", ids[1]: "unknown", ids[2]: "unknown"}


def test_remote_signal_must_be_in_target_header_not_recommended_jobs() -> None:
    page = StubHabrPage(remote=False)
    # The overall page includes a similar job that allows remote work,
    # but the target vacancy's own header is office-only.
    original_locator = page.locator

    def locator(selector: str):
        item = original_locator(selector)
        if selector == "body":
            inner = item.inner_text
            item.inner_text = lambda timeout=0: inner(timeout) + "Похожие вакансии: Можно удалённо"
        return item

    page.locator = locator
    result = inspect_habr_page(page, ROW)
    assert result["status"] == "remote_not_verified"
    outcome = apply_habr_page(page, ROW)
    assert outcome.status == "needs_manual"
    assert page.click_count == 0


@pytest.mark.parametrize("patch", [
    {"approved_cover_letter": "Индивидуальное согласованное письмо"},
    {"cover_letter": "Письмо, которое нельзя молча потерять"},
    {"cover_letter_required": True},
])
def test_habr_letter_request_never_falls_back_to_profile_only(patch) -> None:
    page = StubHabrPage()
    result = apply_habr_page(page, {**ROW, **patch})
    assert result.status == "needs_manual"
    assert "profile-only" in result.note
    assert page.click_count == 0
    assert page.visited == []


def test_habr_driver_letter_policy_blocks_preflight_and_apply_before_browser() -> None:
    def unexpected_page():
        raise AssertionError("A letter-required response must stop before browser actions")

    driver = HabrPageDriver(unexpected_page, require_cover_letter=True)
    assert driver.preflight(ROW).status == "needs_manual"
    assert driver.apply(ROW).status == "needs_manual"


def test_habr_direct_letter_policy_cannot_be_bypassed_by_empty_row_letter() -> None:
    page = StubHabrPage()
    result = apply_habr_page(page, ROW, require_cover_letter=True)
    assert result.status == "needs_manual"
    assert page.click_count == 0
    assert page.visited == []
