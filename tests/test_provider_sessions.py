from __future__ import annotations

import fcntl
import json
import stat
from contextlib import nullcontext
from pathlib import Path

import pytest

from applypilot import provider_sessions
from applypilot.provider_sessions import (
    PlaywrightUnavailable,
    ProviderSessionManager,
    SessionStatus,
    classify_provider_page,
)


class FakeLocator:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self, timeout: int | None = None) -> str:
        return self.text


class FakePage:
    def __init__(self, url: str, body: str = "") -> None:
        self.url = url
        self.body = body
        self.goto_calls: list[tuple[str, dict]] = []
        self.locator_calls: list[str] = []

    def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append((url, kwargs))

    def locator(self, selector: str) -> FakeLocator:
        self.locator_calls.append(selector)
        return FakeLocator(self.body)


class FakeContext:
    def __init__(self, page: FakePage, state: dict | None = None) -> None:
        self.page = page
        self.state = state if state is not None else {"cookies": [], "origins": []}
        self.closed = False
        self.new_page_calls = 0
        self.storage_state_calls = 0

    def new_page(self) -> FakePage:
        self.new_page_calls += 1
        return self.page

    def storage_state(self) -> dict:
        self.storage_state_calls += 1
        return self.state

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, context: FakeContext | None = None, error: Exception | None = None) -> None:
        self.context = context
        self.error = error
        self.calls: list[dict] = []

    def launch_persistent_context(self, user_data_dir: str, **kwargs) -> FakeContext:
        self.calls.append({"user_data_dir": str(user_data_dir), **kwargs})
        if self.error is not None:
            raise self.error
        assert self.context is not None
        return self.context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


def fake_playwright(
    context: FakeContext | None = None, error: Exception | None = None
) -> tuple[FakePlaywright, FakeChromium]:
    chromium = FakeChromium(context=context, error=error)
    return FakePlaywright(chromium), chromium


def write_state(
    manager: ProviderSessionManager,
    source: str = "habr",
    account: str = "default",
) -> Path:
    path = manager.storage_state_path(source, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    return path


def manager_with_context(
    tmp_path: Path, page: FakePage, *, input_fn=None
) -> tuple[ProviderSessionManager, FakeContext, list[tuple]]:
    context = FakeContext(page)
    launches: list[tuple] = []

    def context_factory(playwright, profile_dir, headless):
        launches.append((playwright, profile_dir, headless))
        return context

    manager = ProviderSessionManager(
        tmp_path,
        playwright_factory=lambda: nullcontext(object()),
        context_factory=context_factory,
        page_factory=lambda ctx: ctx.new_page(),
        input_fn=input_fn,
    )
    return manager, context, launches


def test_profiles_are_isolated_by_source_and_account(tmp_path):
    manager = ProviderSessionManager(tmp_path)

    default = manager.profile_dir("habr")
    second = manager.profile_dir("habr", "second")
    other = manager.profile_dir("geekjob")

    assert default == tmp_path / "habr" / "default" / "profile"
    assert len({default, second, other}) == 3
    assert all(path.is_relative_to(tmp_path) for path in (default, second, other))
    assert "Default" not in default.parts
    assert manager.session_paths("hh")["profile_dir"] == str(
        tmp_path / "hh.ru" / "default" / "profile"
    )
    assert json.loads(json.dumps(manager.session_paths("habr")))["account"] == "default"


def test_login_opens_visible_chrome_and_saves_state_atomically(tmp_path, caplog):
    secret = "synthetic-secret-cookie-value"
    state = {"cookies": [{"name": "sid", "value": secret}], "origins": []}
    page = FakePage("https://career.habr.com/login")
    context = FakeContext(page, state)
    playwright, chromium = fake_playwright(context)
    prompts: list[str] = []
    manager = ProviderSessionManager(
        tmp_path,
        playwright_factory=lambda: nullcontext(playwright),
        input_fn=lambda prompt: prompts.append(prompt) or "",
    )

    with caplog.at_level("DEBUG"):
        result = manager.login("habr")

    state_path = manager.storage_state_path("habr")
    call = chromium.calls[0]
    assert result["status"] == SessionStatus.SAVED.value
    assert result["ok"] is True
    assert result["account_label"] == "Основной"
    assert prompts and "Habr Career" in prompts[0]
    assert page.goto_calls[0][0] == "https://career.habr.com/login"
    assert call["user_data_dir"] == str(manager.profile_dir("habr"))
    assert call["headless"] is False
    assert call["no_viewport"] is True
    assert call["channel"] == "chrome"
    assert context.closed is True
    assert state_path.exists()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert json.loads(state_path.read_text(encoding="utf-8")) == state
    assert secret not in json.dumps(result)
    assert secret not in caplog.text
    assert not list(state_path.parent.glob(".storage_state.json.*"))


def test_check_launches_headless_persistent_chrome_and_confirms(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_sessions, "real_user_agent", lambda: "test-agent")
    page = FakePage("https://career.habr.com/", "Мой профиль Выйти")
    context = FakeContext(page)
    playwright, chromium = fake_playwright(context)
    manager = ProviderSessionManager(tmp_path, playwright_factory=lambda: nullcontext(playwright))
    write_state(manager)

    result = manager.check("habr")

    call = chromium.calls[0]
    assert result["status"] == SessionStatus.CONFIRMED.value
    assert result["ok"] is True
    assert result["url"] == "https://career.habr.com/"
    assert call["headless"] is True
    assert call["user_data_dir"] == str(manager.profile_dir("habr"))
    assert call["channel"] == "chrome"
    assert call["user_agent"] == "test-agent"
    assert page.goto_calls[0][0] == "https://career.habr.com/"
    assert page.locator_calls == ["body"]
    assert context.closed is True
    assert result["storage_state_present"] is True


def test_check_expired_on_login_redirect(tmp_path):
    page = FakePage("https://career.habr.com/login", "Войти через Хабр Аккаунт")
    manager, context, launches = manager_with_context(tmp_path, page)
    write_state(manager)

    result = manager.check("habr")

    assert result["status"] == SessionStatus.EXPIRED.value
    assert "login" in result["detail"]
    assert launches[0][2] is True
    assert context.closed is True


def test_check_blocked_on_captcha(tmp_path):
    page = FakePage("https://career.habr.com/", "Подтвердите, что вы не робот")
    manager, context, _launches = manager_with_context(tmp_path, page)
    write_state(manager)

    result = manager.check("habr")

    assert result["status"] == SessionStatus.BLOCKED.value
    assert context.closed is True


def test_check_missing_does_not_launch(tmp_path):
    page = FakePage("https://career.habr.com/", "Выйти")
    manager, context, launches = manager_with_context(tmp_path, page)

    result = manager.check("habr")

    assert result["status"] == SessionStatus.MISSING.value
    assert launches == []
    assert context.closed is False
    assert not manager.profile_dir("habr").exists()


def test_check_profile_lock_is_explicit(tmp_path):
    page = FakePage("https://career.habr.com/", "Выйти")
    manager, context, launches = manager_with_context(tmp_path, page)
    write_state(manager)
    lock_path = Path(manager.session_paths("habr")["lock"])
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = manager.check("habr")
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    assert result["status"] == SessionStatus.PROFILE_LOCKED.value
    assert launches == []
    assert context.closed is False


def test_missing_playwright_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_sessions, "_default_playwright_available", lambda: False)
    manager = ProviderSessionManager(tmp_path)

    result = manager.check("habr")

    assert result["status"] == SessionStatus.PLAYWRIGHT_MISSING.value
    assert "pip install" in result["detail"]
    assert not manager.profile_dir("habr").exists()


def test_unavailable_playwright_factory_is_explicit(tmp_path):
    def factory():
        raise PlaywrightUnavailable("synthetic missing browser")

    manager = ProviderSessionManager(tmp_path, playwright_factory=factory)

    result = manager.login("habr")

    assert result["status"] == SessionStatus.PLAYWRIGHT_MISSING.value


def test_process_singleton_error_maps_to_profile_locked(tmp_path):
    playwright, _chromium = fake_playwright(
        error=RuntimeError("ProcessSingleton: profile is already in use")
    )
    manager = ProviderSessionManager(tmp_path, playwright_factory=lambda: nullcontext(playwright))
    write_state(manager)

    result = manager.check("habr")

    assert result["status"] == SessionStatus.PROFILE_LOCKED.value


def test_unknown_host_is_never_confirmed(tmp_path):
    manager = ProviderSessionManager(tmp_path)
    config = manager.source_config("habr")

    status, detail = classify_provider_page(config, "https://evil.example/", "Выйти")

    assert status is SessionStatus.UNKNOWN
    assert "unexpected host" in detail


def test_hh_mapping_delegates_to_existing_classifier(tmp_path):
    manager = ProviderSessionManager(tmp_path)
    config = manager.source_config("hh")

    assert config.source == "hh.ru"
    assert config.delegate_hh is True
    status, _detail = classify_provider_page(
        config, "https://hh.ru/applicant/resumes", "Мои резюме Выйти"
    )
    assert status is SessionStatus.CONFIRMED
    status, _detail = classify_provider_page(config, "https://hh.ru/account/login", "")
    assert status is SessionStatus.EXPIRED
    status, _detail = classify_provider_page(
        config, "https://hh.ru/applicant/resumes", "Подтвердите, что вы не робот"
    )
    assert status is SessionStatus.BLOCKED


def test_getmatch_anonymous_profile_is_expired(tmp_path):
    manager = ProviderSessionManager(tmp_path)
    config = manager.source_config("getmatch")

    status, _detail = classify_provider_page(
        config, "https://getmatch.ru/profile", "Для просмотра профиля нужно войти"
    )

    assert status is SessionStatus.EXPIRED


def test_dreamjob_cabinet_denial_is_expired(tmp_path):
    manager = ProviderSessionManager(tmp_path)
    config = manager.source_config("dreamjob")

    status, _detail = classify_provider_page(config, "https://dreamjob.ru/i/", "403 Forbidden")

    assert status is SessionStatus.EXPIRED


def test_context_and_page_factories_are_injectable(tmp_path):
    page = FakePage("https://career.habr.com/", "Выйти")
    context = FakeContext(page)
    seen: dict = {}

    def context_factory(playwright, profile_dir, headless):
        seen["profile_dir"] = profile_dir
        seen["headless"] = headless
        return context

    def page_factory(ctx):
        seen["context"] = ctx
        return page

    manager = ProviderSessionManager(
        tmp_path,
        playwright_factory=lambda: nullcontext(object()),
        context_factory=context_factory,
        page_factory=page_factory,
    )
    write_state(manager)

    result = manager.check("habr")

    assert result["status"] == SessionStatus.CONFIRMED.value
    assert seen["profile_dir"] == manager.profile_dir("habr")
    assert seen["headless"] is True
    assert seen["context"] is context


def test_login_cancel_does_not_save_state(tmp_path):
    page = FakePage("https://career.habr.com/login")
    context = FakeContext(page)
    playwright, _chromium = fake_playwright(context)

    def cancel(_prompt: str) -> str:
        raise EOFError

    manager = ProviderSessionManager(
        tmp_path,
        playwright_factory=lambda: nullcontext(playwright),
        input_fn=cancel,
    )

    result = manager.login("habr")

    assert result["status"] == SessionStatus.UNKNOWN.value
    assert not manager.storage_state_path("habr").exists()
    assert context.closed is True


def test_catalogue_is_json_safe_and_complete(tmp_path):
    manager = ProviderSessionManager(tmp_path)

    rows = json.loads(json.dumps(manager.catalogue()))
    sources = {row["source"] for row in rows}

    assert {"hh.ru", "habr", "getmatch", "geekjob", "yandex", "dreamjob"} <= sources
    assert all(row["login_url"].startswith("https://") for row in rows)
    assert all(row["check_url"].startswith("https://") for row in rows)


def test_unsafe_account_is_rejected(tmp_path):
    manager = ProviderSessionManager(tmp_path)

    with pytest.raises(ValueError):
        manager.profile_dir("habr", "../escape")
