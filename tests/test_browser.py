from __future__ import annotations

from pathlib import Path

from applypilot.browser import browser_settings, launch_context, real_user_agent
from applypilot.cli import build_parser


class _Context:
    def close(self):
        pass


class _Chromium:
    def __init__(self):
        self.calls = []

    def launch_persistent_context(self, user_data_dir, **kwargs):
        self.calls.append((user_data_dir, kwargs))
        return _Context()


class _Playwright:
    def __init__(self):
        self.chromium = _Chromium()


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
