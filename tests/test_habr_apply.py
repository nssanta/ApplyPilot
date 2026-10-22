"""Offline tests of the real Habr native response contract (no network or accounts)."""

from __future__ import annotations

from types import SimpleNamespace

from applypilot.habr_apply import HabrPageDriver, apply_habr_page

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
        if self.kind in {"submit_section", "submit"}:
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
        if selector == ".create-vacancy-response__apply":
            return StubLocator(self, "submit_section")
        if selector == ".create-vacancy-response":
            return StubLocator(self, "response_section")
        return StubLocator(self, "missing")

    def on(self, event: str, callback) -> None:
        self.listeners[event].append(callback)

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


def test_no_post_is_manual_and_never_fake_success() -> None:
    page = StubHabrPage(send_request=False)
    result = apply_habr_page(page, ROW)
    assert result.status == "needs_manual"
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
