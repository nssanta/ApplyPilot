from __future__ import annotations

import json
from pathlib import Path

from applypilot.browser import (
    _merge_missing_cookies,
    browser_settings,
    launch_context,
    real_user_agent,
)
from applypilot.cli import build_parser


class _Context:
    def __init__(self, cookies=None):
        self.added = []
        self._cookies = list(cookies or [])

    def close(self):
        pass

    def cookies(self):
        return self._cookies

    def add_cookies(self, cookies):
        self.added.extend(cookies)


class _Chromium:
    def __init__(self, existing_cookies=None):
        self.calls = []
        self.contexts = []
        self.existing_cookies = existing_cookies

    def launch_persistent_context(self, user_data_dir, **kwargs):
        self.calls.append((user_data_dir, kwargs))
        context = _Context(self.existing_cookies)
        self.contexts.append(context)
        return context


class _Playwright:
    def __init__(self, existing_cookies=None):
        self.chromium = _Chromium(existing_cookies)


def _launch(tmp_path: Path, headless: bool):
    playwright = _Playwright()
    session = launch_context(
        playwright,
        data_dir=tmp_path,
        profile={"browser": {"headless": headless}},
    )
    session.close()
    return playwright.chromium.calls[0][1]


def test_browser_defaults_keep_visible_real_chrome_and_hh_timezone():
    settings = browser_settings({})

    assert settings["headless"] is False
    assert settings["locale"] == "ru-RU"
    assert settings["timezone"] == "Asia/Krasnoyarsk"
    assert settings["manual_captcha_timeout_seconds"] == 300


def test_visible_mode_uses_the_real_window_without_ua_override(tmp_path):
    kwargs = _launch(tmp_path, headless=False)

    assert kwargs["headless"] is False
    assert kwargs["no_viewport"] is True
    assert "user_agent" not in kwargs
    assert kwargs["ignore_default_args"] == ["--enable-automation"]
    assert "--disable-blink-features=AutomationControlled" in kwargs["args"]


def test_headless_mode_keeps_chrome_identity_and_desktop_dimensions(tmp_path):
    kwargs = _launch(tmp_path, headless=True)

    assert kwargs["headless"] is True
    assert "HeadlessChrome" not in kwargs["user_agent"]
    assert kwargs["user_agent"] == real_user_agent()
    assert kwargs["viewport"] == {"width": 1366, "height": 768}
    assert kwargs["screen"] == {"width": 1366, "height": 768}
    assert kwargs["timezone_id"] == "Asia/Krasnoyarsk"
    assert "no_viewport" not in kwargs


def test_cli_exposes_explicit_visible_and_headless_modes():
    parser = build_parser()

    apply_args = parser.parse_args(["apply", "--input", "in.json", "--run", "--headless"])
    queue_args = parser.parse_args(["queue", "apply", "--track", "ai", "--run", "--visible"])

    assert apply_args.headless is True and apply_args.visible is False
    assert queue_args.headless is False and queue_args.visible is True


def _state(tmp_path: Path, cookies: list[dict[str, str]]) -> Path:
    path = tmp_path / "hh_session.json"
    path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    return path


def test_merge_missing_cookies_restores_only_absent_cookies(tmp_path):
    state = _state(tmp_path, [
        {"name": "_xsrf", "domain": ".hh.ru", "path": "/", "value": "old"},
        {"name": "crypted_id", "domain": ".hh.ru", "path": "/", "value": "restored"},
    ])
    context = _Context()
    context.cookies = lambda: [
        {"name": "hhtoken", "domain": ".hh.ru", "path": "/", "value": "live"},
        {"name": "_xsrf", "domain": ".hh.ru", "path": "/", "value": "fresh"},
    ]

    assert _merge_missing_cookies(context, state) is True
    assert [cookie["name"] for cookie in context.added] == ["crypted_id"]
    assert context.added[0]["value"] == "restored"


def test_seeded_profile_restores_missing_cookies_without_overwriting(tmp_path):
    profile_dir = tmp_path / "hh_chrome_profile"
    profile_dir.mkdir()
    (profile_dir / ".seeded").write_text("1", encoding="utf-8")
    state = _state(tmp_path, [
        {"name": "_xsrf", "domain": ".hh.ru", "path": "/", "value": "old"},
        {"name": "crypted_id", "domain": ".hh.ru", "path": "/", "value": "restored"},
    ])
    playwright = _Playwright(existing_cookies=[
        {"name": "hhtoken", "domain": ".hh.ru", "path": "/", "value": "live"},
        {"name": "_xsrf", "domain": ".hh.ru", "path": "/", "value": "fresh"},
    ])

    session = launch_context(
        playwright, data_dir=tmp_path, state_path=state,
        profile={"browser": {"headless": True}},
    )
    session.close()

    added = playwright.chromium.contexts[-1].added
    assert [cookie["name"] for cookie in added] == ["crypted_id"]


def test_unseeded_profile_imports_full_state_and_marks_seeded(tmp_path):
    state = _state(tmp_path, [
        {"name": "_xsrf", "domain": ".hh.ru", "path": "/", "value": "one"},
        {"name": "crypted_id", "domain": ".hh.ru", "path": "/", "value": "two"},
    ])
    playwright = _Playwright()

    session = launch_context(
        playwright, data_dir=tmp_path, state_path=state,
        profile={"browser": {"headless": True}},
    )
    session.close()

    assert [cookie["name"] for cookie in playwright.chromium.contexts[0].added] == [
        "_xsrf", "crypted_id",
    ]
    assert (tmp_path / "hh_chrome_profile" / ".applypilot_seeded").exists()


def test_merge_skips_logged_out_profile_and_expired_cookies(tmp_path):
    state = _state(tmp_path, [
        {"name": "crypted_id", "domain": ".hh.ru", "path": "/", "value": "x"},
        {"name": "old", "domain": ".hh.ru", "path": "/", "value": "y", "expires": 1},
    ])
    logged_out = _Context()
    assert _merge_missing_cookies(logged_out, state) is False
    assert logged_out.added == []

    logged_in = _Context([{"name": "hhtoken", "domain": ".hh.ru", "path": "/"}])
    assert _merge_missing_cookies(logged_in, state) is True
    assert [cookie["name"] for cookie in logged_in.added] == ["crypted_id"]
