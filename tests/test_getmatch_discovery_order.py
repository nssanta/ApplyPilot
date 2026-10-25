"""Public synthetic regressions for bounded Getmatch sitemap discovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from applypilot.providers import getmatch as getmatch_module
from applypilot.providers.getmatch import GetMatchProvider


def _sitemap(*urls: str) -> str:
    locations = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return (
        "<?xml version='1.0' encoding='utf8'?>"
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locations}</urlset>"
    )


def _detail(path: Path, title: str) -> Path:
    path.write_text(
        f"<html><body><h1>{title}</h1><div class='vacancy-description'>Python and agents</div></body></html>",
        encoding="utf-8",
    )
    return path


def test_ai_role_terms_and_sitemap_order_advance_bounded_scans(tmp_path: Path) -> None:
    state_path = tmp_path / "seen.json"
    sitemap = _sitemap(
        "https://getmatch.ru/vacancies/100-ai-agent-engineer",
        "https://getmatch.ru/vacancies/900-ai-agent-developer",
        "https://getmatch.ru/vacancies/901-python-developer",
    )
    fixtures = {
        "https://getmatch.ru/vacancies/100-ai-agent-engineer": _detail(
            tmp_path / "100.html", "AI Agent Engineer",
        ),
        "https://getmatch.ru/vacancies/900-ai-agent-developer": _detail(
            tmp_path / "900.html", "AI Agent Developer",
        ),
        "https://getmatch.ru/vacancies/901-python-developer": _detail(
            tmp_path / "901.html", "Python Developer",
        ),
    }
    filters = {
        "sitemap_text": sitemap,
        "detail_fixtures": fixtures,
        "primary_role_terms": ["agent"],
        "state_path": str(state_path),
        "details_limit": 1,
    }
    provider = GetMatchProvider()

    first, first_segments = provider.discover(filters)
    second, _ = provider.discover(filters)
    third, _ = provider.discover(filters)

    assert [item["native_id"] for item in first] == ["100"]
    assert "details limit reached" in first_segments[-1].error
    assert [item["native_id"] for item in second] == ["900"]
    assert [item["native_id"] for item in third] == ["901"]


def test_failed_detail_remains_unseen_without_starving_later_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sitemap = _sitemap(
        "https://getmatch.ru/vacancies/100-ai-agent-engineer",
        "https://getmatch.ru/vacancies/901-python-developer",
    )
    html = "<html><body><h1>Python Developer</h1></body></html>"
    requested: list[str] = []

    def fake_fetch(_filters: object, url: str, _timeout: float):
        requested.append(url)
        if url.endswith("/sitemap.xml"):
            return sitemap, "ok", "", 1
        if "/vacancies/100-" in url:
            return None, "failed", "HTTP 500", 1
        return html, "ok", "", 1

    monkeypatch.setattr(getmatch_module, "_fetch", fake_fetch)
    state_path = tmp_path / "seen.json"
    filters = {
        "sitemap_url": "https://getmatch.ru/sitemap.xml",
        "state_path": str(state_path),
        "primary_role_terms": ["agent"],
        "details_limit": 1,
        "request_budget": 4,
    }
    provider = GetMatchProvider()

    first, first_segments = provider.discover(filters)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    second, _ = provider.discover(filters)

    assert [item["native_id"] for item in first] == ["901"]
    assert first_segments[-1].status == "partial"
    assert set(state["seen_ids"]) == {"901"}
    assert second == []
    assert requested.count("https://getmatch.ru/vacancies/100-ai-agent-engineer") == 2
    assert "https://getmatch.ru/vacancies/901-python-developer" in requested


def test_fresh_cached_getmatch_details_avoid_reloading_seen_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sitemap = _sitemap(
        "https://getmatch.ru/vacancies/100-ai-agent-engineer",
        "https://getmatch.ru/vacancies/200-ai-agent-developer",
    )
    requested: list[str] = []

    def fake_fetch(_filters: object, url: str, _timeout: float):
        requested.append(url)
        if url.endswith("/sitemap.xml"):
            return sitemap, "ok", "", 1
        return (
            (
                "<html><body><h1>AI Agent Developer</h1>"
                "<div class='vacancy-description'>Build AI agents</div></body></html>"
            ),
            "ok", "", 1,
        )

    monkeypatch.setattr(getmatch_module, "_fetch", fake_fetch)
    filters = {
        "sitemap_url": "https://getmatch.ru/sitemap.xml",
        "state_path": str(tmp_path / "seen.json"),
        "primary_role_terms": ["agent"],
        "details_limit": 2,
        "request_budget": 2,
        "known_descriptions": {
            "100": {
                "id": "100", "native_id": "100", "source": "getmatch.ru",
                "name": "AI Agent Engineer", "description": "Cached verified description",
                "description_status": "ok", "url": "https://getmatch.ru/vacancies/100",
            },
        },
    }

    first, segments = GetMatchProvider().discover(filters)

    assert [item["native_id"] for item in first] == ["100", "200"]
    assert first[0]["description"] == "Cached verified description"
    assert "https://getmatch.ru/vacancies/100-ai-agent-engineer" not in requested
    assert "https://getmatch.ru/vacancies/200-ai-agent-developer" in requested
    assert "cached=1" in segments[-1].error


def test_telegram_priority_links_do_not_replace_sitemap_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sitemap = _sitemap(
        "https://getmatch.ru/vacancies/100-ai-agent-engineer",
        "https://getmatch.ru/vacancies/200-python-developer",
    )
    requested: list[str] = []

    def fake_fetch(_filters: object, url: str, _timeout: float):
        requested.append(url)
        if url.endswith("/sitemap.xml"):
            return sitemap, "ok", "", 1
        native_id = "100" if "/100-" in url else "200"
        html = (
            f"<html><body><h1>Role {native_id}</h1>"
            "<div class='vacancy-description'>Python and agents</div></body></html>"
        )
        return html, "ok", "", 1

    monkeypatch.setattr(getmatch_module, "_fetch", fake_fetch)
    filters = {
        "detail_urls": ["https://getmatch.ru/vacancies/100-ai-agent-engineer"],
        "include_sitemap": True,
        "sitemap_url": "https://getmatch.ru/sitemap.xml",
        "state_path": str(tmp_path / "sitemap.json"),
        "telegram_state_path": str(tmp_path / "telegram.json"),
        "primary_role_terms": ["agent"],
        "details_limit": 10,
        "request_budget": 10,
    }

    items, segments = GetMatchProvider().discover(filters)

    assert {item["native_id"] for item in items} == {"100", "200"}
    assert requested.count("https://getmatch.ru/vacancies/100-ai-agent-engineer") == 1
    assert "https://getmatch.ru/vacancies/200-python-developer" in requested
    assert [segment.query for segment in segments] == ["detail_urls", "sitemap", "details"]
