from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.providers import getmatch as getmatch_module
from applypilot.providers.getmatch import GetMatchProvider, is_archived_text
from applypilot.storage import Store

_ARCHIVED_EN = (
    "<html><body><h1>Python Developer</h1><p>The vacancy has been archived</p>"
    "<p>The company is no longer hiring for this position.</p></body></html>"
)


def test_archive_banner_is_recognised_in_both_languages() -> None:
    assert is_archived_text("Вакансия в архиве, посмотрите похожие")
    assert is_archived_text("The vacancy has been archived. The company is no longer hiring")
    assert not is_archived_text("Python Developer, remote, откликнуться")


def test_scan_reports_english_archived_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sitemap = (
        '<?xml version="1.0"?><urlset>'
        "<url><loc>https://getmatch.ru/vacancies/100-python-developer</loc></url>"
        "<url><loc>https://getmatch.ru/vacancies/200-python-developer</loc></url>"
        "</urlset>"
    )

    def fake_fetch(_filters: object, url: str, _timeout: float):
        if url.endswith("/sitemap.xml"):
            return sitemap, "ok", "", 1
        if "/100-" in url:
            return _ARCHIVED_EN, "ok", "", 1
        active = (
            "<html><body><h1>Python Developer</h1>"
            "<div class='vacancy-description'>Python backend</div></body></html>"
        )
        return active, "ok", "", 1

    monkeypatch.setattr(getmatch_module, "_fetch", fake_fetch)
    sink: list[str] = []
    items, segments = GetMatchProvider().discover({
        "sitemap_url": "https://getmatch.ru/sitemap.xml",
        "state_path": str(tmp_path / "seen.json"),
        "primary_role_terms": ["python"],
        "details_limit": 5, "request_budget": 5,
        "archived_sink": sink,
    })

    assert [item["native_id"] for item in items] == ["200"]
    assert sink == ["100"]
    assert "archived=1" in segments[-1].error


def test_archived_rows_leave_queues_but_applied_rows_stay(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    stamp = "2026-11-01T10:00:00+00:00"
    with store.connect() as conn:
        for vacancy_id, state in (("1", "NOT_STARTED"), ("2", "READY_TO_APPLY"),
                                  ("3", "APPLIED_CONFIRMED"), ("4", "UNKNOWN")):
            conn.execute(
                """INSERT INTO vacancies
                (account,source,vacancy_id,title,company,url,application_state,
                 created_at,first_seen_at,last_seen_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("acc", "getmatch.ru", vacancy_id, "Role", "Co",
                 f"https://getmatch.ru/vacancies/{vacancy_id}", state,
                 stamp, stamp, stamp, stamp),
            )

    changed = store.mark_source_archived("acc", "getmatch.ru", ["1", "2", "3", "4", "9"],
                                         "getmatch.ru: vacancy is archived on the site")

    assert changed == 2
    with store.connect() as conn:
        states = dict(conn.execute(
            "SELECT vacancy_id, application_state FROM vacancies ORDER BY vacancy_id"
        ).fetchall())
    assert states == {"1": "NEEDS_MANUAL", "2": "NEEDS_MANUAL",
                      "3": "APPLIED_CONFIRMED", "4": "UNKNOWN"}
    assert store.mark_source_archived("acc", "getmatch.ru", [], "x") == 0
