"""Focused fixture tests for the read-only Habr Career and GeekJob providers."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from applypilot.providers.base import parse_salary
from applypilot.providers.geekjob import GeekJobProvider
from applypilot.providers.habr import HabrProvider

FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "private"
    / "research"
    / "sources_2026-10-22"
)
HABR_LIST = FIXTURES / "habr_list.html"
HABR_LLM_LIST = FIXTURES / "habr_llm_list.html"
HABR_DETAIL = FIXTURES / "habr_detail.html"
GEEKJOB_LIST = FIXTURES / "geekjob_list.html"
GEEKJOB_DETAIL = FIXTURES / "geekjob_detail.html"
HABR_PAGE2 = FIXTURES / "habr_python_list_page2.html"
GEEKJOB_PAGE2 = FIXTURES / "geekjob_list_page2.html"

pytestmark = pytest.mark.skipif(
    not all(path.is_file() for path in (HABR_LIST, HABR_DETAIL, GEEKJOB_LIST, GEEKJOB_DETAIL)),
    reason="saved public source fixtures are not present",
)


class _Response:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.headers: dict[str, str] = {}


class _Session:
    def __init__(self, text: str) -> None:
        self.text = text
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _Response:
        self.urls.append(url)
        return _Response(self.text)


class _MappedSession:
    def __init__(self, responses: dict[str, _Response | str]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _Response:
        self.urls.append(url)
        value = self.responses.get(url)
        if value is None:
            return _Response("", 404)
        if isinstance(value, _Response):
            return value
        return _Response(value)


class _NoBudget:
    def take(self) -> bool:
        return True


def _habr_card(native_id: str, title: str, *, skills: tuple[str, ...] = ()) -> str:
    skill_html = "".join(
        f'<a class="vacancy-card__skills-chip" href="/vacancies/skills/x">{skill}</a>'
        for skill in skills
    )
    return (
        '<div class="vacancy-card">'
        f'<a class="vacancy-card__title-link" href="/vacancies/{native_id}">{title}</a>'
        '<div class="vacancy-card__company"><a href="/companies/example">Example</a></div>'
        '<div class="vacancy-card__date"><time datetime="2026-10-22T10:00:00+03:00">'
        "22 октября</time></div>"
        f'<div class="vacancy-card__skills">{skill_html}</div>'
        "</div>"
    )


def _habr_detail(native_id: str, title: str, description: str = "Full description") -> str:
    return (
        "<html><body>"
        f'<h1 class="page-title__title">{title}</h1>'
        f'<div class="vacancy-description__text">{description}</div>'
        f'<div data-vacancy-favorite-for="{native_id}"></div>'
        "</body></html>"
    )


def _geekjob_card(
    native_id: str,
    title: str,
    *,
    location: str = "Москва, Россия",
    remote: str = "remote",
) -> str:
    remote_html = f'<span class="remote-label">{remote}</span>' if remote else ""
    return (
        '<li class="collection-item">'
        f'<div class="info"><a href="/vacancy/{native_id}">{location}<br/>'
        '<span class="salary"></span></a></div>'
        f'<p class="vacancy-name"><a class="title" href="/vacancy/{native_id}">{title}</a></p>'
        f'<p class="company-name"><a href="/vacancy/{native_id}">Example</a></p>'
        f'<div class="info">{remote_html}</div>'
        f'<time class="truncate datetime-info">'
        f'<a href="/vacancy/{native_id}">22 октября</a></time>'
        "</li>"
    )


def _geekjob_detail(title: str, description: str = "Full description") -> str:
    return (
        '<html><body><article class="vacancy">'
        f"<h1>{title}</h1>"
        '<h5 class="company-name">Example</h5>'
        f'<div class="description"><div id="vacancy-description">{description}</div></div>'
        '<a class="respondbtn" href="#scroll2respondbox">Откликнуться</a>'
        "</article></body></html>"
    )


def _habr_items(filters: dict[str, object] | None = None) -> list[dict[str, object]]:
    provider = HabrProvider()
    items, _segments = provider.discover(
        {"fixture_dir": str(FIXTURES), "queries": ["python"], **(filters or {})}
    )
    return items


def _geek_items(filters: dict[str, object] | None = None) -> list[dict[str, object]]:
    provider = GeekJobProvider()
    items, _segments = provider.discover(
        {"fixture_dir": str(FIXTURES), "max_pages": 1, **(filters or {})}
    )
    return items


def test_habr_fixture_discovery_and_canonical_fields() -> None:
    provider = HabrProvider()
    items, segments = provider.discover(
        {"fixture_dir": str(FIXTURES), "queries": ["python"], "max_pages": 1}
    )

    assert len(items) == 8
    assert len(segments) == 1
    assert segments[0].query == "python"
    assert segments[0].status == "ok"
    assert segments[0].items == 8
    assert segments[0].requests == 0

    first = items[0]
    assert first["native_id"] == "1000169462"
    assert first["name"] == "Старший Backend-разработчик в VK"
    assert first["company"] == "VK"
    assert first["source"] == "career.habr.com"
    assert first["url"] == "https://career.habr.com/vacancies/1000169462"
    assert first["application_channel"] == "career.habr.com:1000169462"
    assert first["application_channel_type"] == "native_form"
    assert first["experience"] == "Lead"
    assert first["area"] == "Москва"
    assert first["is_remote"] is True
    assert first["remote_confidence"] == "listed"
    assert first["published"] == "2026-10-22T14:02:54+00:00"
    assert first["published_verified"] is True
    assert first["description_status"] == "missing"
    assert first["salary"] is None
    assert first["salary_provenance"] == "unknown"
    assert "Похожие специалисты" not in str(first["salary"])

    assert all(item["source"] == "career.habr.com" for item in items)
    assert all(item["url"].startswith("https://career.habr.com/vacancies/") for item in items)
    assert all("hh.ru" not in str(item["url"]) for item in items)
    assert all("hh.ru" not in str(item["application_url"]) for item in items)


def test_habr_query_selects_matching_saved_fixture() -> None:
    provider = HabrProvider()
    items, segments = provider.discover(
        {"fixture_dir": str(FIXTURES), "skills": ["llm"], "max_pages": 1}
    )

    assert len(items) == 8
    assert segments[0].status == "ok"
    assert items[0]["native_id"] == "1000167169"
    assert items[0]["company"] == "Cloud.ru"


def test_habr_detail_fixture_parses_published_salary_and_company() -> None:
    provider = HabrProvider()
    detail = provider.inspect(
        "1000168944", {"detail_fixture_path": str(HABR_DETAIL)}
    )

    assert detail["native_id"] == "1000168944"
    assert detail["name"] == "Full-Stack разработчик - Most AI"
    assert detail["company"] == "Most AI"
    assert detail["source"] == "career.habr.com"
    assert detail["url"] == "https://career.habr.com/vacancies/1000168944"
    assert detail["salary"] == {
        "from": 180000,
        "to": 220000,
        "currency": "RUR",
        "gross": None,
        "provenance": "published",
    }
    assert detail["salary_provenance"] == "published"
    assert detail["description_status"] == "ok"
    assert "Most AI" in detail["description"]
    assert detail["published"] == "2026-10-06T17:51:04+00:00"
    assert detail["is_remote"] is True
    assert detail["apply_status"] == "needs_live_validation"
    assert "hh.ru" not in detail["url"]


def test_habr_detail_fixture_is_added_when_not_in_list() -> None:
    provider = HabrProvider()
    items, segments = provider.discover(
        {
            "fixture_dir": str(FIXTURES),
            "queries": ["python"],
            "detail_fixture_path": str(HABR_DETAIL),
        }
    )

    assert len(items) == 9
    detail = next(item for item in items if item["native_id"] == "1000168944")
    assert detail["company"] == "Most AI"
    assert detail["description_status"] == "ok"
    assert len(segments) == 2
    assert segments[-1].status == "ok"
    assert segments[-1].items == 1


def test_habr_prediction_is_not_a_published_salary() -> None:
    assert parse_salary("Похожие специалисты получают 367 000 - 456 000 ₽") is None
    first = _habr_items()[0]
    assert first["salary"] is None
    assert first["salary_provenance"] == "unknown"


def test_habr_pagination_urls_and_request_budget() -> None:
    provider = HabrProvider()
    session = _Session(HABR_LIST.read_text(encoding="utf-8"))
    items, segments = provider.discover(
        {"queries": ["python"], "max_pages": 2, "session": session}
    )

    assert session.urls == [
        "https://career.habr.com/vacancies/skills/python",
        "https://career.habr.com/vacancies/skills/python?page=2",
    ]
    assert len(items) == 8
    assert [segment.pages for segment in segments] == [1, 2]
    assert [segment.requests for segment in segments] == [1, 1]
    assert all(segment.status == "ok" for segment in segments)

    limited = _Session(HABR_LIST.read_text(encoding="utf-8"))
    items, segments = provider.discover(
        {"queries": ["python"], "max_pages": 3, "request_budget": 1, "session": limited}
    )
    assert len(limited.urls) == 1
    assert len(items) == 8
    assert segments[0].status == "ok"
    assert segments[1].status == "truncated"
    assert segments[1].error == "request budget reached"


def test_habr_unknown_html_is_failed_not_empty_verified() -> None:
    provider = HabrProvider()
    items, segments = provider.discover({"fixture_path": str(GEEKJOB_LIST)})

    assert items == []
    assert len(segments) == 1
    assert segments[0].status == "failed"
    assert segments[0].status != "empty_verified"


def test_habr_missing_fixture_is_failed_without_network() -> None:
    provider = HabrProvider()
    items, segments = provider.discover(
        {"fixture_dir": str(FIXTURES / "does-not-exist"), "queries": ["python"]}
    )

    assert items == []
    assert segments[0].status == "failed"
    assert segments[0].error == "fixture not found"


def test_geekjob_fixture_discovery_and_canonical_fields() -> None:
    provider = GeekJobProvider()
    items, segments = provider.discover(
        {"fixture_dir": str(FIXTURES), "max_pages": 1}
    )

    assert len(items) == 8
    assert len(segments) == 1
    assert segments[0].status == "ok"
    assert segments[0].items == 8
    assert segments[0].requests == 0

    first = items[0]
    assert first["native_id"] == "6ada197365a5c9108200e4b2"
    assert first["name"] == "Sales manager B2B (Латам, Igmaing)"
    assert first["company"] == "Betting Software"
    assert first["source"] == "geekjob.ru"
    assert first["url"] == "https://geekjob.ru/vacancy/6ada197365a5c9108200e4b2"
    assert first["application_channel"] == "geekjob.ru:6ada197365a5c9108200e4b2"
    assert first["salary"] == {
        "from": 2000,
        "to": 2500,
        "currency": "USD",
        "gross": None,
        "provenance": "published",
    }
    assert first["salary_provenance"] == "published"
    assert first["area"] == "Буэнос - Айрис , Аргентина"
    assert first["is_remote"] is True
    assert first["remote_confidence"] == "listed"
    assert first["published"] == ""
    assert first["published_verified"] is False
    assert first["published_display"] == "22 октября"
    assert first["description_status"] == "missing"

    assert all(item["source"] == "geekjob.ru" for item in items)
    assert all(item["url"].startswith("https://geekjob.ru/vacancy/") for item in items)
    assert all("hh.ru" not in str(item["url"]) for item in items)
    assert all("hh.ru" not in str(item["application_url"]) for item in items)


def test_geekjob_detail_enriches_remote_except_russia_as_ineligible() -> None:
    items = _geek_items({"detail_fixture_path": str(GEEKJOB_DETAIL)})
    detail = next(
        item for item in items if item["native_id"] == "6ad9f5371c45e275c308e5c8"
    )

    assert detail["description_status"] == "ok"
    assert "кроме РФ" in detail["description"]
    assert detail["is_remote"] is False
    assert detail["remote_confidence"] == "ineligible"
    assert detail["remote_reason"] == "remote_except_russia"
    assert detail["published"] == ""
    assert detail["published_verified"] is False
    assert detail["respond_button"] is True
    assert detail["apply_status"] == "needs_live_validation"


def test_geekjob_detail_fixture_fields() -> None:
    provider = GeekJobProvider()
    detail = provider.inspect(
        "6ad9f5371c45e275c308e5c8",
        {"detail_fixture_path": str(GEEKJOB_DETAIL)},
    )

    assert detail["native_id"] == "6ad9f5371c45e275c308e5c8"
    assert detail["name"] == "DevOps engineer"
    assert detail["company"] == "Агентство / HR ресурс Mission Hire"
    assert detail["experience"] == "Джуниор • Миддл"
    assert detail["salary"] is None
    assert detail["description_status"] == "ok"
    assert detail["is_remote"] is False
    assert detail["remote_confidence"] == "ineligible"
    assert detail["remote_reason"] == "remote_except_russia"
    assert detail["published"] == ""
    assert detail["url"] == "https://geekjob.ru/vacancy/6ad9f5371c45e275c308e5c8"
    assert "hh.ru" not in detail["application_url"]


def test_geekjob_pagination_urls_and_request_budget() -> None:
    provider = GeekJobProvider()
    session = _Session(GEEKJOB_LIST.read_text(encoding="utf-8"))
    items, segments = provider.discover(
        {"max_pages": 3, "session": session}
    )

    assert session.urls == [
        "https://geekjob.ru/vacancies",
        "https://geekjob.ru/vacancies/2",
        "https://geekjob.ru/vacancies/3",
    ]
    assert len(items) == 8
    assert len(segments) == 3
    assert [segment.pages for segment in segments] == [1, 2, 3]
    assert [segment.requests for segment in segments] == [1, 1, 1]
    assert all(segment.status == "ok" for segment in segments)

    limited = _Session(GEEKJOB_LIST.read_text(encoding="utf-8"))
    items, segments = provider.discover(
        {"max_pages": 3, "request_budget": 1, "session": limited}
    )
    assert len(limited.urls) == 1
    assert len(items) == 8
    assert segments[0].status == "ok"
    assert segments[1].status == "truncated"


def test_geekjob_unknown_html_is_failed_not_empty_verified() -> None:
    provider = GeekJobProvider()
    items, segments = provider.discover({"fixture_path": str(HABR_LIST)})

    assert items == []
    assert len(segments) == 1
    assert segments[0].status == "failed"
    assert segments[0].status != "empty_verified"


def test_providers_do_not_touch_network_when_fixtures_are_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider attempted a network request")

    monkeypatch.setattr(requests.Session, "get", fail)
    habr_items = _habr_items()
    geek_items = _geek_items()
    assert len(habr_items) == 8
    assert len(geek_items) == 8


def test_normalize_rebuilds_non_hh_urls_and_rejects_invalid_rows() -> None:
    habr = HabrProvider()
    geek = GeekJobProvider()

    habr_item = habr.normalize(
        {
            "native_id": "1000169462",
            "name": "Backend",
            "url": "https://hh.ru/vacancy/1000169462",
        }
    )
    assert habr_item["url"] == "https://career.habr.com/vacancies/1000169462"
    assert "hh.ru" not in habr_item["application_url"]

    geek_item = geek.normalize(
        {
            "id": "6ada197365a5c9108200e4b2",
            "title": "Sales manager",
            "url": "https://hh.ru/vacancy/6ada197365a5c9108200e4b2",
        }
    )
    assert geek_item["url"] == "https://geekjob.ru/vacancy/6ada197365a5c9108200e4b2"
    assert "hh.ru" not in geek_item["application_url"]

    assert habr.normalize({}) == {}
    assert geek.normalize({"name": "no id"}) == {}

def test_habr_ai_core_phrases_are_deduplicated_to_confirmed_catalogues() -> None:
    llm_session = _Session(HABR_LIST.read_text(encoding="utf-8"))
    llm_items, llm_segments = HabrProvider().discover(
        {
            "queries": [
                "AI Agent Engineer",
                "LLM Engineer",
                "AI Engineer",
                "Разработчик ИИ агентов",
            ],
            "max_pages": 1,
            "session": llm_session,
        }
    )
    assert llm_session.urls == ["https://career.habr.com/vacancies/skills/llm"]
    assert len(llm_segments) == 1
    assert llm_segments[0].query == "llm"
    assert len(llm_items) == 8

    python_session = _Session(HABR_LIST.read_text(encoding="utf-8"))
    python_items, python_segments = HabrProvider().discover(
        {
            "queries": ["Python LLM", "AI Agents Python", "Python backend AI"],
            "max_pages": 1,
            "session": python_session,
        }
    )
    assert python_session.urls == ["https://career.habr.com/vacancies/skills/python"]
    assert len(python_segments) == 1
    assert python_segments[0].query == "python"
    assert len(python_items) == 8


def test_habr_unknown_query_uses_public_vacancies_list() -> None:
    url = "https://career.habr.com/vacancies"
    session = _MappedSession({url: _habr_card("1000000009", "AI Engineer")})
    items, segments = HabrProvider().discover(
        {"queries": ["Java developer"], "max_pages": 1, "session": session}
    )

    assert session.urls == [url]
    assert "/skills/" not in session.urls[0]
    assert segments[0].query == "Java developer"
    assert segments[0].status == "ok"
    assert [item["native_id"] for item in items] == ["1000000009"]


def test_habr_unknown_query_with_fixture_dir_fails_instead_of_fake_empty() -> None:
    items, segments = HabrProvider().discover(
        {"fixture_dir": str(FIXTURES), "queries": ["Java developer"]}
    )

    assert items == []
    assert segments[0].status == "failed"
    assert segments[0].error == "fixture not found"


@pytest.mark.skipif(not HABR_PAGE2.is_file(), reason="Habr page 2 fixture is not present")
def test_habr_page2_fixture_is_parsed() -> None:
    items, segments = HabrProvider().discover(
        {"fixture_dir": str(FIXTURES), "queries": ["python"], "max_pages": 2}
    )

    assert len(items) == 16
    assert [segment.pages for segment in segments] == [1, 2]
    assert [segment.items for segment in segments] == [8, 8]
    assert all(segment.status == "ok" for segment in segments)
    assert any(item["native_id"] == "1000169000" for item in items)


def test_habr_detail_enrichment_prioritizes_potential() -> None:
    list_url = "https://career.habr.com/vacancies/skills/llm"
    detail_url = "https://career.habr.com/vacancies/1000000002"
    session = _MappedSession(
        {
            list_url: _habr_card("1000000001", "Sales manager B2B")
            + _habr_card("1000000002", "AI/LLM Engineer", skills=("Python", "LLM")),
            detail_url: _habr_detail("1000000002", "AI/LLM Engineer"),
        }
    )

    items, _segments = HabrProvider().discover(
        {"queries": ["LLM Engineer"], "max_details": 1, "session": session}
    )
    by_id = {item["native_id"]: item for item in items}

    assert by_id["1000000002"]["description_status"] == "ok"
    assert by_id["1000000001"]["description_status"] == "missing"
    assert session.urls == [list_url, detail_url]


def test_habr_detail_blocked_is_reported_as_partial() -> None:
    list_url = "https://career.habr.com/vacancies/skills/llm"
    detail_url = "https://career.habr.com/vacancies/1000000002"
    session = _MappedSession(
        {
            list_url: _habr_card("1000000002", "AI/LLM Engineer"),
            detail_url: _Response("", 403),
        }
    )

    items, segments = HabrProvider().discover(
        {"queries": ["LLM Engineer"], "max_details": 1, "session": session}
    )

    assert items[0]["description_status"] == "missing"
    assert any(
        item.query == "detail" and item.status == "partial" and "blocked" in item.error
        for item in segments
    )


@pytest.mark.skipif(not GEEKJOB_PAGE2.is_file(), reason="GeekJob page 2 fixture is not present")
def test_geekjob_page2_fixture_is_parsed() -> None:
    items, segments = GeekJobProvider().discover(
        {"fixture_dir": str(FIXTURES), "max_pages": 2}
    )

    assert len(items) == 16
    assert [segment.pages for segment in segments] == [1, 2]
    assert [segment.items for segment in segments] == [8, 8]
    assert all(segment.status == "ok" for segment in segments)
    assert any(item["native_id"] == "6ad72a96a6c579bec107462b" for item in items)


def test_geekjob_detail_enrichment_prioritizes_potential() -> None:
    list_url = "https://geekjob.ru/vacancies"
    detail_url = "https://geekjob.ru/vacancy/a2"
    session = _MappedSession(
        {
            list_url: _geekjob_card("a1", "Sales manager")
            + _geekjob_card("a2", "AI/LLM Engineer"),
            detail_url: _geekjob_detail("AI/LLM Engineer"),
        }
    )

    items, _segments = GeekJobProvider().discover(
        {"max_pages": 1, "max_details": 1, "session": session}
    )
    by_id = {item["native_id"]: item for item in items}

    assert by_id["a2"]["description_status"] == "ok"
    assert by_id["a1"]["description_status"] == "missing"
    assert session.urls == [list_url, detail_url]


def test_geekjob_remote_badge_is_not_enough_when_except_russia() -> None:
    session = _MappedSession(
        {
            "https://geekjob.ru/vacancies": _geekjob_card(
                "b1", "AI Engineer", location="Удалённо, кроме РФ"
            )
        }
    )
    items, _segments = GeekJobProvider().discover({"max_pages": 1, "session": session})

    assert len(items) == 1
    assert items[0]["is_remote"] is False
    assert items[0]["remote_confidence"] == "ineligible"
    assert items[0]["remote_reason"] == "remote_except_russia"


def test_geekjob_detail_cache_is_reused_within_run() -> None:
    list_url = "https://geekjob.ru/vacancies"
    detail_url = "https://geekjob.ru/vacancy/a2"
    session = _MappedSession(
        {
            list_url: _geekjob_card("a2", "AI/LLM Engineer"),
            detail_url: _geekjob_detail("AI/LLM Engineer"),
        }
    )
    provider = GeekJobProvider()
    items, _segments = provider.discover(
        {"max_pages": 1, "max_details": 1, "session": session}
    )

    assert "a2" in provider.description_cache
    first_urls = list(session.urls)
    cached = dict(items[0])
    cached["description"] = ""
    cached["description_status"] = "missing"
    provider._add_detail(
        {},
        {"a2": cached},
        [],
        fixture_mode=False,
        max_details=1,
        budget=_NoBudget(),
        session=session,
        timeout=1.0,
    )

    assert session.urls == first_urls
    assert provider.description_cache["a2"]["description_status"] == "ok"


def test_geekjob_detail_blocked_is_reported_as_partial() -> None:
    list_url = "https://geekjob.ru/vacancies"
    detail_url = "https://geekjob.ru/vacancy/a2"
    session = _MappedSession(
        {
            list_url: _geekjob_card("a2", "AI/LLM Engineer"),
            detail_url: _Response("", 429),
        }
    )

    items, segments = GeekJobProvider().discover(
        {"max_pages": 1, "max_details": 1, "session": session}
    )

    assert items[0]["description_status"] == "missing"
    assert any(
        item.query == "detail" and item.status == "partial" and "blocked" in item.error
        for item in segments
    )


@pytest.mark.parametrize("status", [403, 429])
def test_providers_report_http_block_on_list(status: int) -> None:
    habr_session = _MappedSession(
        {"https://career.habr.com/vacancies/skills/llm": _Response("", status)}
    )
    habr_items, habr_segments = HabrProvider().discover(
        {"queries": ["LLM"], "max_pages": 1, "session": habr_session}
    )
    assert habr_items == []
    assert habr_segments[0].status == "blocked"
    assert str(status) in habr_segments[0].error

    geek_session = _MappedSession({"https://geekjob.ru/vacancies": _Response("", status)})
    geek_items, geek_segments = GeekJobProvider().discover(
        {"max_pages": 1, "session": geek_session}
    )
    assert geek_items == []
    assert geek_segments[0].status == "blocked"
    assert str(status) in geek_segments[0].error


@pytest.mark.parametrize(
    ("provider", "url", "filters"),
    [
        (
            HabrProvider(),
            "https://career.habr.com/vacancies/skills/llm",
            {"queries": ["LLM"]},
        ),
        (GeekJobProvider(), "https://geekjob.ru/vacancies", {}),
    ],
)
def test_plain_unknown_html_is_failed_not_empty_verified(
    provider: object, url: str, filters: dict[str, object]
) -> None:
    session = _MappedSession(
        {url: "<html><body><p>maintenance</p></body></html>"}
    )
    items, segments = provider.discover(  # type: ignore[attr-defined]
        {**filters, "max_pages": 1, "session": session}
    )

    assert items == []
    assert segments[0].status == "failed"
    assert segments[0].status != "empty_verified"

def test_geekjob_detail_does_not_upgrade_except_russia_from_list() -> None:
    list_url = "https://geekjob.ru/vacancies"
    detail_url = "https://geekjob.ru/vacancy/b2"
    session = _MappedSession(
        {
            list_url: _geekjob_card(
                "b2", "AI Engineer", location="Удалённо, кроме РФ"
            ),
            detail_url: _geekjob_detail("AI Engineer", description="Remote team"),
        }
    )

    items, _segments = GeekJobProvider().discover(
        {"max_pages": 1, "max_details": 1, "session": session}
    )

    assert items[0]["is_remote"] is False
    assert items[0]["remote_confidence"] == "ineligible"
    assert items[0]["remote_reason"] == "remote_except_russia"

@pytest.mark.skipif(
    not (HABR_PAGE2.is_file() and GEEKJOB_PAGE2.is_file()),
    reason="page 2 fixtures are not present",
)
def test_page2_fixtures_do_not_touch_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider attempted a network request")

    monkeypatch.setattr(requests.Session, "get", fail)
    habr_items, _habr_segments = HabrProvider().discover(
        {"fixture_dir": str(FIXTURES), "queries": ["python"], "max_pages": 2}
    )
    geek_items, _geek_segments = GeekJobProvider().discover(
        {"fixture_dir": str(FIXTURES), "max_pages": 2}
    )

    assert len(habr_items) == 16
    assert len(geek_items) == 16

def test_habr_fixture_dir_without_queries_uses_generic_fixture() -> None:
    items, segments = HabrProvider().discover({"fixture_dir": str(FIXTURES)})

    assert len(items) == 8
    assert segments[0].status == "ok"
