"""Regression tests for the opt-in, non-invasive PR #4 macOS helpers."""
from __future__ import annotations

import requests

from applypilot import browser, sources
from applypilot.parser import ScanSegment


def test_mac_user_agent_matches_platform(monkeypatch):
    monkeypatch.setattr(browser.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser, "chrome_version", lambda: "157.0.0.0")
    assert "Macintosh; Intel Mac OS X" in browser.real_user_agent()
    assert "Chrome/157.0.0.0" in browser.real_user_agent()


def test_hh_adapter_passes_existing_http_session_to_scanner(monkeypatch):
    supplied = requests.Session()
    observed = []

    def fake_scan_many(queries, areas, pages, **kwargs):
        observed.append(kwargs.get("session"))
        return [], [ScanSegment(queries[0], areas[0], 1, "ok", 0, requests=1)]

    monkeypatch.setattr(sources, "scan_many", fake_scan_many)
    try:
        sources.HhSearchAdapter().discover({
            "queries": ["Python"],
            "areas": [113],
            "pages": 1,
            "remote": True,
            "request_budget": 1,
            "session": supplied,
        })
    finally:
        supplied.close()
    assert observed and all(session is supplied for session in observed)


def test_browser_proxy_only_when_explicit(tmp_path):
    calls = []

    class Context:
        def close(self):
            pass

    class Chromium:
        def launch_persistent_context(self, profile_dir, **kwargs):
            calls.append(kwargs)
            return Context()

    class Playwright:
        chromium = Chromium()

    browser.launch_context(
        Playwright(), data_dir=tmp_path,
        profile={"browser": {"headless": False}},
        proxy_url="http://127.0.0.1:4567",
    ).close()
    browser.launch_context(
        Playwright(), data_dir=tmp_path,
        profile={"browser": {"headless": False}},
    ).close()
    assert calls[0]["proxy"] == {"server": "http://127.0.0.1:4567"}
    assert "proxy" not in calls[1]


def test_macos_chrome_app_binary_fallback(monkeypatch):
    monkeypatch.setattr(browser.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser, "_CHROME_CANDIDATES", ())
    monkeypatch.setattr(
        browser.Path, "is_file",
        lambda path: str(path) == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    assert browser._chrome_binary() == (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
