"""Fixture-backed regression tests for Getmatch, Yandex Careers and DreamJob.

All tests are offline.  Network-shaped failures use a tiny fake session; the
final test monkeypatches ``requests.Session.get`` to prove fixture paths never
touch the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from applypilot.providers.dreamjob import DreamJobProvider
from applypilot.providers.getmatch import GetMatchProvider, _sitemap_rows, _slug_score
from applypilot.providers.yandex import YandexProvider

FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "private"
    / "research"
    / "sources_2026-10-22"
)
GETMATCH_SITEMAP = FIXTURES / "getmatch_sitemap_sample.xml"
GETMATCH_ACTIVE = FIXTURES / "getmatch_active_llm.html"
GETMATCH_ARCHIVED = FIXTURES / "getmatch_archived.html"
GETMATCH_DETAIL = FIXTURES / "getmatch_detail.html"
YANDEX_GENAI = FIXTURES / "yandex_genai_remote_list.html"
YANDEX_JOBS = FIXTURES / "yandex_jobs.html"
DREAMJOB_LIST = FIXTURES / "dreamjob_company_list.html"
DREAMJOB_DETAIL = FIXTURES / "dreamjob_hh_mirror.html"

pytestmark = pytest.mark.skipif(
    not all(path.is_file() for path in (
        GETMATCH_SITEMAP, GETMATCH_ACTIVE, GETMATCH_ARCHIVED, GETMATCH_DETAIL,
        YANDEX_GENAI, YANDEX_JOBS, DREAMJOB_LIST, DREAMJOB_DETAIL,
    )),
    reason="saved public source fixtures are not present",
)


class _Response:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.headers: dict[str, str] = {}


class _Session:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _Response:
        self.urls.append(url)
        return _Response(self.text, self.status_code)


def _sitemap(*urls: str) -> str:
    locations = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return (
        "<?xml version='1.0' encoding='utf8'?>"
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locations}</urlset>"
    )


# ---------------------------------------------------------------------------
# Getmatch
# ---------------------------------------------------------------------------


def test_getmatch_sitemap_fixture_discovers_without_manual_detail_urls() -> None:
    items, segments = GetMatchProvider().discover({"fixture_dir": str(FIXTURES)})

    ids = {item["native_id"] for item in items}
    assert ids == {"36887", "36789"}
    assert "25000" not in ids
    assert all(item["source"] == "getmatch.ru" for item in items)
    assert all(item["url"].startswith("https://getmatch.ru/vacancies/") for item in items)
    assert all("hh.ru" not in str(item["application_url"]) for item in items)

    llm = next(item for item in items if item["native_id"] == "36887")
    assert llm["name"] == "LLM Engineer"
    assert llm["company"] == "БЮРО 1440"
    assert llm["salary"] == {
        "from": 300000,
        "to": 400000,
        "currency": "RUR",
        "gross": False,
        "provenance": "published",
    }
    # The saved visible fixture has no JSON-LD date; an ID must never become a date.
    assert llm["published"] == ""
    assert llm["published_verified"] is False
    assert llm["is_remote"] is True
    assert llm["description_status"] == "ok"
    assert llm["application_channel"] == "getmatch.ru:36887"
    assert llm["application_channel_type"] == "native_form"

    assert segments[0].query == "sitemap"
    assert segments[0].items == 56
    assert segments[0].requests == 0
    assert segments[1].query == "details"
    assert segments[1].items == 2
    assert segments[1].requests == 0
    assert "sitemap_urls=56" in segments[1].error


def test_getmatch_sitemap_fixture_path_finds_sibling_details() -> None:
    items, segments = GetMatchProvider().discover({
        "sitemap_fixture_path": str(GETMATCH_SITEMAP),
    })

    assert {item["native_id"] for item in items} == {"36887", "36789"}
    assert segments[0].items == 56
    assert segments[1].items == 2


def test_getmatch_seen_ids_filter_is_honoured_and_extended() -> None:
    seen = ["36887"]
    items, _ = GetMatchProvider().discover({
        "sitemap_text": _sitemap(
            "https://getmatch.ru/vacancies/36887-llm-engineer",
            "https://getmatch.ru/vacancies/36789-product-engineer-python-v-komandu-analitiki",
        ),
        "fixture_dir": str(FIXTURES),
        "seen_ids": seen,
    })

    assert [item["native_id"] for item in items] == ["36789"]
    assert set(seen) == {"36887", "36789"}


def test_getmatch_sitemap_excludes_archived_and_deduplicates() -> None:
    text = _sitemap(
        "https://getmatch.ru/vacancies/36887-llm-engineer",
        "https://getmatch.ru/vacancies/36887-llm-engineer",
        "https://getmatch.ru/vacancies/25000-team-lead-data-science",
    )
    items, segments = GetMatchProvider().discover({
        "sitemap_text": text,
        "fixture_dir": str(FIXTURES),
        "details_limit": 10,
    })

    assert [item["native_id"] for item in items] == ["36887"]
    assert "archived=1" in segments[1].error
    assert "sitemap_urls=2" in segments[0].error
    assert "matched=2" in segments[0].error


def test_getmatch_sitemap_delta_uses_atomic_seen_state(tmp_path: Path) -> None:
    state_path = tmp_path / "getmatch-seen.json"
    text = _sitemap(
        "https://getmatch.ru/vacancies/36887-llm-engineer",
        "https://getmatch.ru/vacancies/36789-product-engineer-python-v-komandu-analitiki",
    )
    filters = {
        "sitemap_text": text,
        "fixture_dir": str(FIXTURES),
        "state_path": str(state_path),
        "details_limit": 10,
    }
    provider = GetMatchProvider()
    first_items, _ = provider.discover(filters)
    assert {item["native_id"] for item in first_items} == {"36887", "36789"}

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(payload["seen_ids"]) == {"36887", "36789"}
    assert not list(tmp_path.glob(".getmatch-seen.json.*"))

    second_items, second_segments = provider.discover(filters)
    assert second_items == []
    assert second_segments[0].items == 2
    assert second_segments[1].items == 0
    assert second_segments[1].requests == 0
    assert "unseen=0" in second_segments[1].error


def test_getmatch_sitemap_seen_state_accumulates_across_bounded_scans(tmp_path: Path) -> None:
    path = tmp_path / "sitemap-seen.json"
    filters = {
        "sitemap_text": _sitemap(
            "https://getmatch.ru/vacancies/36887-llm-engineer",
            "https://getmatch.ru/vacancies/36789-product-engineer-python-v-komandu-analitiki",
        ),
        "fixture_dir": str(FIXTURES),
        "state_path": str(path),
        "details_limit": 1,
    }
    provider = GetMatchProvider()
    first, _ = provider.discover(filters)
    assert len(first) == 1
    second, _ = provider.discover(filters)
    assert len(second) == 1
    assert first[0]["id"] != second[0]["id"]
    state = json.loads(path.read_text(encoding="utf-8"))
    assert set(state["seen_ids"]) == {"36887", "36789"}
    third, segments = provider.discover(filters)
    assert third == []
    assert "unseen=0" in segments[-1].error


def test_getmatch_direct_telegram_hints_advance_between_bounded_runs(
    tmp_path: Path,
) -> None:
    # Use only the tracked synthetic public fixture; do not access the web.
    html = GETMATCH_ACTIVE.read_text(encoding="utf-8")
    session = _Session(html)
    cursor = tmp_path / "telegram_seen.json"
    urls = [
        "https://getmatch.ru/vacancies/36887",
        "https://getmatch.ru/vacancies/36789",
    ]
    filters = {
        "detail_urls": urls,
        "session": session,
        "state_path": str(cursor),
        "details_limit": 1,
        "request_budget": 2,
    }
    provider = GetMatchProvider()
    first, _ = provider.discover(filters)
    second, _ = provider.discover(filters)
    third, segments = provider.discover(filters)
    assert [x["native_id"] for x in first] == ["36887"]
    assert [x["native_id"] for x in second] == ["36789"]
    assert third == []
    assert "previously_seen=2" in segments[0].error
    assert len(session.urls) == 2
    assert set(json.loads(cursor.read_text())["seen_ids"]) == {"36887", "36789"}


def test_getmatch_detail_json_ld_supplies_real_date_salary_and_location(
    tmp_path: Path,
) -> None:
    html = """
    <html><body><main>
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Senior LLM Engineer",
        "hiringOrganization": {"@type": "Organization", "name": "Acme AI"},
        "datePosted": "2026-10-20T11:45:14",
        "jobLocationType": "TELECOMMUTE",
        "jobLocation": {"@type": "Place", "address": {
          "@type": "PostalAddress", "addressLocality": "Москва",
          "addressCountry": "Россия"
        }},
        "baseSalary": {"@type": "MonetaryAmount", "currency": "RUB",
          "value": {"@type": "QuantitativeValue", "minValue": 300000,
                    "maxValue": 400000}},
        "description": "<p>Build <b>agents</b> with Python.</p>"
      }
      </script>
    </main></body></html>
    """
    path = tmp_path / "detail.html"
    path.write_text(html, encoding="utf-8")
    items, _ = GetMatchProvider().discover({
        "fixture_path": str(path),
        "fixture_url": "https://getmatch.ru/vacancies/99999-senior-llm-engineer",
    })

    assert len(items) == 1
    item = items[0]
    assert item["native_id"] == "99999"
    assert item["name"] == "Senior LLM Engineer"
    assert item["company"] == "Acme AI"
    assert item["published"] == "2026-10-20T11:45:14+00:00"
    assert item["published_verified"] is True
    assert item["salary"]["from"] == 300000
    assert item["salary"]["to"] == 400000
    assert item["salary"]["currency"] == "RUR"
    assert item["area"] == "Москва, Россия"
    assert item["is_remote"] is True
    assert "Build agents with Python." in item["description"]
    assert item["json_ld"] is True


def test_getmatch_slug_prefilter_and_detail_limit() -> None:
    assert _slug_score("llm-engineer") > _slug_score("python-developer")
    assert _slug_score("hr-manager") == 0
    rows = _sitemap_rows(_sitemap(
        "https://getmatch.ru/vacancies/11111-hr-manager",
        "https://getmatch.ru/vacancies/36887-llm-engineer",
    ))
    assert [row["native_id"] for row in rows] == ["11111", "36887"]

    items, segments = GetMatchProvider().discover({
        "sitemap_text": _sitemap(
            "https://getmatch.ru/vacancies/11111-hr-manager",
            "https://getmatch.ru/vacancies/36887-llm-engineer",
        ),
        "fixture_dir": str(FIXTURES),
        "details_limit": 1,
    })
    assert [item["native_id"] for item in items] == ["36887"]
    assert "matched=1" in segments[0].error


def test_getmatch_live_403_is_blocked_not_empty_verified() -> None:
    items, segments = GetMatchProvider().discover({
        "session": _Session("Forbidden", 403),
    })

    assert items == []
    assert segments[0].status == "blocked"
    assert segments[0].status != "empty_verified"


def test_getmatch_unknown_html_is_failed_not_empty_verified() -> None:
    items, segments = GetMatchProvider().discover({"fixture_path": str(YANDEX_GENAI)})

    assert items == []
    assert segments[0].status == "failed"
    assert segments[0].status != "empty_verified"


# ---------------------------------------------------------------------------
# Yandex Careers
# ---------------------------------------------------------------------------


def test_yandex_fixture_cards_have_semantic_fields() -> None:
    items, segments = YandexProvider().discover({"fixture_path": str(YANDEX_GENAI)})

    assert len(items) == 3
    first = items[0]
    assert first["native_id"] == "48171"
    assert first["name"] == "Аналитик-разработчик для оценки качества моделей в Alice AI"
    assert first["company"] == "Yandex R&D"
    assert first["source"] == "yandex.ru/jobs"
    assert first["url"].startswith("https://yandex.ru/jobs/vacancies/")
    assert first["application_channel"] == "yandex.ru/jobs:48171"
    assert first["is_remote"] is True
    assert first["description_status"] == "snippet"
    assert segments[0].status == "ok"
    assert segments[0].items == 3


def test_yandex_large_fixture_skips_external_cards() -> None:
    items, segments = YandexProvider().discover({"fixture_path": str(YANDEX_JOBS)})

    assert len(items) == 9
    assert all(item["url"].startswith("https://yandex.ru/jobs/vacancies/") for item in items)
    assert all("ai-trainers.ya.ru" not in item["url"] for item in items)
    assert "external=11" in segments[0].error


def test_yandex_archived_card_is_excluded(tmp_path: Path) -> None:
    html = """
    <html><body>
      <article>
        <h2><a href="/jobs/vacancies/active-llm-111">Active LLM engineer</a></h2>
        <p>Build agents with Python.</p>
        <ul><li><a href="/jobs/vacancies?work_modes=remote">Удалённая работа</a></li></ul>
      </article>
      <article>
        <h2><a href="/jobs/vacancies/closed-llm-222">Closed LLM engineer</a></h2>
        <p>Вакансия закрыта.</p>
      </article>
    </body></html>
    """
    path = tmp_path / "yandex.html"
    path.write_text(html, encoding="utf-8")
    items, segments = YandexProvider().discover({"fixture_path": str(path)})

    assert [item["native_id"] for item in items] == ["111"]
    assert "archived=1" in segments[0].error


def test_yandex_live_uses_only_confirmed_endpoints_and_request_budget() -> None:
    session = _Session(YANDEX_GENAI.read_text(encoding="utf-8"))
    items, segments = YandexProvider().discover({"session": session})

    assert session.urls == [
        "https://yandex.ru/jobs/vacancies?work_modes=remote",
        "https://yandex.ru/jobs/vacancies?services=generativetech&work_modes=remote",
    ]
    assert all("text=" not in url and "page=" not in url for url in session.urls)
    assert len(items) == 3
    assert [segment.status for segment in segments] == ["ok", "ok"]
    assert [segment.requests for segment in segments] == [1, 1]
    assert all(segment.order_by == "confirmed_endpoint" for segment in segments)

    limited = _Session(YANDEX_GENAI.read_text(encoding="utf-8"))
    items, segments = YandexProvider().discover({
        "session": limited,
        "request_budget": 1,
    })
    assert limited.urls == ["https://yandex.ru/jobs/vacancies?work_modes=remote"]
    assert len(items) == 3
    assert segments[1].status == "truncated"
    assert segments[1].requests == 0


def test_yandex_429_is_blocked_not_empty_verified() -> None:
    items, segments = YandexProvider().discover({"session": _Session("rate", 429)})

    assert items == []
    assert all(segment.status == "blocked" for segment in segments)


def test_yandex_generic_article_page_is_not_a_listing(tmp_path: Path) -> None:
    path = tmp_path / "news.html"
    path.write_text(
        "<html><body><article><p>Company news, not a vacancy.</p></article></body></html>",
        encoding="utf-8",
    )
    items, segments = YandexProvider().discover({"fixture_path": str(path)})

    assert items == []
    assert segments[0].status == "failed"
    assert segments[0].status != "empty_verified"


def test_yandex_unknown_html_is_failed_not_empty_verified(tmp_path: Path) -> None:
    path = tmp_path / "unknown.html"
    path.write_text("<html><body><div>not a vacancy list</div></body></html>",
                    encoding="utf-8")
    items, segments = YandexProvider().discover({"fixture_path": str(path)})

    assert items == []
    assert segments[0].status == "failed"
    assert segments[0].status != "empty_verified"


def test_yandex_detail_inspection_reads_date_and_archived_marker(tmp_path: Path) -> None:
    detail = tmp_path / "detail.html"
    detail.write_text(
        """
        <html><body><main>
          <h1>Senior LLM Engineer</h1>
          <p>Build agents with Python and RAG.</p>
          <time datetime="2026-10-20T11:45:14">20 October</time>
        </main></body></html>
        """,
        encoding="utf-8",
    )
    item = YandexProvider().inspect("99999", {
        "detail_fixture_path": str(detail),
        "detail_fixture_url": "https://yandex.ru/jobs/vacancies/senior-llm-99999",
    })
    assert item["native_id"] == "99999"
    assert item["published"] == "2026-10-20T11:45:14+00:00"
    assert item["description_status"] == "ok"
    assert item["application_channel"] == "yandex.ru/jobs:99999"

    archived = tmp_path / "archived.html"
    archived.write_text(
        "<html><body><h1>Senior LLM Engineer</h1><p>Вакансия закрыта.</p></body></html>",
        encoding="utf-8",
    )
    assert YandexProvider().inspect("99999", {
        "detail_fixture_path": str(archived),
    }) == {}


# ---------------------------------------------------------------------------
# DreamJob (HH mirror)
# ---------------------------------------------------------------------------


def test_dreamjob_company_listing_enriches_and_aliases_hh() -> None:
    items, segments = DreamJobProvider().discover({"fixture_dir": str(FIXTURES)})

    assert len(items) == 1
    item = items[0]
    assert item["source"] == "dreamjob.ru"
    assert item["native_id"] == "138413511"
    assert item["mirror_source"] == "hh.ru"
    assert item["mirror_native_id"] == "138413511"
    assert item["application_channel"] == "hh.ru:138413511"
    assert item["application_channel_type"] == "mirror"
    assert item["application_url"] == "https://hh.ru/vacancy/138413511"
    assert item["apply_status"] == "mirror_only"
    assert item["name"] == "AI-first разработчик"
    assert item["company"] == "Progressive Mind"
    assert item["salary"] == {
        "from": 5000,
        "to": 6000,
        "currency": "USD",
        "gross": None,
        "provenance": "published",
    }
    assert item["is_remote"] is True
    assert item["description_status"] == "ok"
    assert item["url"] == "https://dreamjob.ru/employers/108825/vakansii/138413511"
    assert segments[0].query == "company_listing"
    assert segments[0].status == "ok"
    assert "enriched=1" in segments[0].error


def test_dreamjob_listing_without_detail_keeps_snippet_and_mirror_alias() -> None:
    items, segments = DreamJobProvider().discover({"fixture_path": str(DREAMJOB_LIST)})

    assert len(items) == 1
    item = items[0]
    assert item["description_status"] == "snippet"
    assert item["application_channel"] == "hh.ru:138413511"
    assert item["application_channel_type"] == "mirror"
    assert item["apply_status"] == "mirror_only"
    assert "enriched=0" in segments[0].error


def test_dreamjob_details_limit_zero_keeps_listing_snippets() -> None:
    items, segments = DreamJobProvider().discover({
        "fixture_dir": str(FIXTURES),
        "details_limit": 0,
    })

    assert len(items) == 1
    assert items[0]["description_status"] == "snippet"
    assert "enriched=0" in segments[0].error


def test_dreamjob_empty_listing_is_failed_not_fake_ok(tmp_path: Path) -> None:
    path = tmp_path / "empty-listing.html"
    path.write_text(
        """
        <html><body><main class="vacancies">
          <h1 class="vacancies__title-jobboard">Вакансии компании Empty</h1>
        </main></body></html>
        """,
        encoding="utf-8",
    )
    items, segments = DreamJobProvider().discover({"fixture_path": str(path)})

    assert items == []
    assert segments[0].status == "failed"
    assert "no vacancies" in segments[0].error


def test_dreamjob_403_is_blocked_not_empty_verified() -> None:
    items, segments = DreamJobProvider().discover({"session": _Session("Forbidden", 403)})

    assert items == []
    assert segments[0].status == "blocked"
    assert segments[0].status != "empty_verified"


def test_dreamjob_archived_detail_is_excluded(tmp_path: Path) -> None:
    archived = tmp_path / "archived-detail.html"
    archived.write_text(
        "<html><body><h1 class='job-vacancy__head'>AI-first разработчик</h1>"
        "<p>Вакансия закрыта.</p></body></html>",
        encoding="utf-8",
    )
    items, segments = DreamJobProvider().discover({
        "fixture_path": str(DREAMJOB_LIST),
        "detail_fixtures": {"138413511": str(archived)},
    })

    assert items == []
    assert segments[0].status == "failed"
    assert "archived=1" in segments[0].error


def test_providers_do_not_touch_network_when_fixtures_are_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider attempted a network request")

    monkeypatch.setattr(requests.Session, "get", fail)

    getmatch_items, _ = GetMatchProvider().discover({"fixture_dir": str(FIXTURES)})
    yandex_items, _ = YandexProvider().discover({"fixture_path": str(YANDEX_GENAI)})
    dreamjob_items, _ = DreamJobProvider().discover({"fixture_dir": str(FIXTURES)})

    assert len(getmatch_items) == 2
    assert len(yandex_items) == 3
    assert len(dreamjob_items) == 1
