"""Read-only GeekJob provider built from the saved public HTML fixtures.

GeekJob exposes public vacancy cards at ``/vacancies`` and ``/vacancies/N`` and
detail pages at ``/vacancy/<id>``.  The provider never calls the site's
``/json/`` or ``/rest/`` endpoints and never submits an application.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..parser import ScanSegment
from .base import (
    SourceCapabilities,
    canonical_url,
    clean_text,
    job_group_key,
    make_item,
    parse_iso_datetime,
    parse_salary,
    segment,
)

_BASE = "https://geekjob.ru"
_LOGIN = "https://geekjob.ru/login"
_USER_AGENT = "ApplyPilot/0.1 (read-only source discovery)"
_MAX_PAGES = 50
_MAX_DETAILS = 500


class _Budget:
    """Small request counter; ``None`` means no configured limit."""

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.used = 0

    def take(self) -> bool:
        if self.limit is not None and self.used >= self.limit:
            return False
        self.used += 1
        return True


def _bounded_int(
    filters: Mapping[str, Any],
    keys: tuple[str, ...],
    default: int,
    maximum: int,
    minimum: int = 0,
) -> int:
    for key in keys:
        value = filters.get(key)
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        return max(minimum, min(number, maximum))
    return default


def _timeout(filters: Mapping[str, Any]) -> float:
    try:
        value = float(filters.get("timeout", 20.0))
    except (TypeError, ValueError):
        return 20.0
    return min(60.0, max(1.0, value))


def _request_budget(filters: Mapping[str, Any]) -> int | None:
    value = filters.get("request_budget")
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _fixture_comment_url(html: str) -> str:
    match = re.search(r"URL:\s*(https://[^\s;]+)", html[:1500])
    return match.group(1) if match else ""


def _list_fixture(filters: Mapping[str, Any], page: int) -> Path | None:
    directory = filters.get("fixture_dir")
    if directory:
        root = Path(str(directory))
        if page == 1:
            names = ["geekjob_list.html"]
        else:
            names = [
                f"geekjob_list_page{page}.html",
                f"geekjob_list_{page}.html",
            ]
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return candidate
    direct = filters.get("fixture_path") or filters.get("fixture")
    if direct and page == 1:
        path = Path(str(direct))
        return path if path.is_file() else None
    return None


def _detail_fixture(filters: Mapping[str, Any], native_id: str = "") -> Path | None:
    direct = filters.get("detail_fixture_path")
    if direct:
        path = Path(str(direct))
        return path if path.is_file() else None
    directory = filters.get("fixture_dir")
    if not directory:
        return None
    root = Path(str(directory))
    names = ["geekjob_detail.html"]
    if native_id:
        names.insert(0, f"geekjob_detail_{native_id}.html")
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _fetch(
    session: Any, url: str, timeout: float
) -> tuple[str | None, str, str, int]:
    client = session or requests.Session()
    try:
        response = client.get(
            url, timeout=timeout, headers={"User-Agent": _USER_AGENT}
        )
    except requests.RequestException:
        return None, "failed", "network error", 1
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 403:
        return None, "blocked", "HTTP 403 access limited", 1
    if status == 429:
        return None, "blocked", "HTTP 429 rate limited", 1
    if status >= 500:
        return None, "failed", f"HTTP {status}", 1
    if status != 200:
        return None, "failed", f"HTTP {status}", 1
    return str(getattr(response, "text", "") or ""), "ok", "", 1


def _challenge(html: str) -> bool:
    low = html.casefold()
    return any(
        marker in low
        for marker in (
            "captcha",
            "капч",
            "не робот",
            "access denied",
            "доступ ограничен",
            "too many requests",
        )
    )


def _explicit_empty(soup: BeautifulSoup) -> bool:
    text = clean_text(soup.get_text(" ", strip=True)).casefold()
    return any(
        marker in text
        for marker in (
            "ничего не найдено",
            "вакансий не найдено",
            "нет вакансий",
            "no vacancies",
            "по вашему запросу",
        )
    )


def _geekjob_id(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]+", value):
        return value
    parsed = urlparse(urljoin(_BASE, value))
    if (parsed.hostname or "").lower() not in {"geekjob.ru", "www.geekjob.ru"}:
        return ""
    match = re.search(r"/vacancy/([0-9a-fA-F]+)(?:/|$)", parsed.path)
    return match.group(1) if match else ""


def _geekjob_url(native_id: str, value: str = "") -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host in {"geekjob.ru", "www.geekjob.ru"} and re.search(
        rf"/vacancy/{re.escape(native_id)}(?:/|$)", parsed.path
    ):
        return canonical_url(_BASE, parsed.path)
    return f"{_BASE}/vacancy/{native_id}"


def _parse_salary_text(value: Any) -> dict[str, Any] | None:
    """Parses published salary text, tolerating the site's uppercase ``K``."""
    if hasattr(value, "get_text"):
        value = value.get_text(" ", strip=True)
    if isinstance(value, str):
        value = re.sub(r"(?<=\d)K\b", "k", value)
    return parse_salary(value)


def _company_name(article: Any) -> str:
    node = article.select_one("h5.company-name")
    if node is None:
        return ""
    text = clean_text(node.get_text(" ", strip=True))
    return re.sub(r"\s*\(\s*[^()]*\.[a-z]{2,}\s*\)\s*$", "", text, flags=re.IGNORECASE).strip()


_TITLE_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("ai", 10), ("ии", 10), ("llm", 10), ("rag", 9), ("agent", 9),
    ("агент", 9), ("genai", 9), ("mcp", 9), ("langgraph", 9),
    ("langchain", 8), ("python", 6), ("golang", 6), ("go", 6),
    ("typescript", 6), ("ml", 6), ("machine learning", 6), ("нейросет", 6),
    ("backend", 3), ("fullstack", 3), ("full-stack", 3),
    ("engineer", 2), ("developer", 2), ("разработчик", 2), ("инженер", 2),
)
_SKILL_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("ai", 6), ("ии", 6), ("llm", 6), ("rag", 5), ("agent", 5),
    ("агент", 5), ("genai", 5), ("mcp", 5), ("langgraph", 5),
    ("langchain", 4), ("python", 4), ("golang", 4), ("go", 4),
    ("typescript", 4), ("ml", 4), ("machine learning", 4), ("нейросет", 4),
)


def _term_present(text: str, term: str) -> bool:
    if term.isascii() and term.replace("-", "").replace(" ", "").isalnum():
        return re.search(rf"(?<![0-9a-z]){re.escape(term)}(?![0-9a-z])", text) is not None
    return term in text


def _potential_score(item: Mapping[str, Any]) -> int:
    title = clean_text(item.get("name") or "").casefold()
    skills = " ".join(str(value) for value in (item.get("skills") or [])).casefold()
    queries = " ".join(
        str(value) for value in (item.get("query_sources") or [])
    ).casefold()
    score = 0
    for term, weight in _TITLE_WEIGHTS:
        if _term_present(title, term):
            score += weight
    for term, weight in _SKILL_WEIGHTS:
        if _term_present(skills, term):
            score += weight
    if queries:
        score += 1
    if item.get("description_status") != "ok":
        score += 2
    if not item.get("salary"):
        score += 1
    if item.get("published_verified"):
        score += 1
    if item.get("is_remote"):
        score += 1
    return score


def _prioritized(collected: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = list(enumerate(collected.values()))
    ordered.sort(key=lambda pair: (-_potential_score(pair[1]), pair[0]))
    return [item for _, item in ordered]


def _merge_item(target: dict[str, dict[str, Any]], item: dict[str, Any]) -> None:
    key = str(item.get("native_id") or "")
    if not key:
        return
    current = target.get(key)
    if current is None:
        target[key] = item
        return
    for field in ("query_sources", "skills"):
        values = set(current.get(field) or []) | set(item.get(field) or [])
        if values:
            current[field] = sorted(values)
    pages = [value for value in (current.get("list_page"), item.get("list_page")) if value]
    if pages:
        current["list_page"] = min(pages)


def _merge_detail(base: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    detail_remote = detail.get("remote_confidence")
    keep_ineligible = (
        base.get("remote_confidence") == "ineligible"
        and detail_remote != "ineligible"
    )
    # A list card saying "кроме РФ" must not be upgraded to remote merely
    # because the detail page omits that restriction.
    if detail_remote not in (None, "", "unknown") and not keep_ineligible:
        for key in ("is_remote", "remote_confidence", "remote_reason", "work_format"):
            merged[key] = detail.get(key)
    for key in (
        "name",
        "company",
        "description",
        "description_status",
        "experience",
        "area",
        "source_status",
    ):
        value = detail.get(key)
        if value not in (None, "", [], {}):
            merged[key] = value
    if detail.get("published_verified"):
        merged["published"] = detail.get("published", "")
        merged["published_verified"] = True
    if detail.get("salary"):
        merged["salary"] = detail["salary"]
        merged["salary_provenance"] = detail.get("salary_provenance", "published")
    for key in ("respond_button", "detail_page", "skills", "published_display"):
        value = detail.get(key)
        if value not in (None, "", [], {}):
            merged[key] = value
    merged["job_group_key"] = job_group_key(
        str(merged.get("company") or ""), str(merged.get("name") or "")
    )
    return merged


class GeekJobProvider:
    """Read-only GeekJob adapter with offline fixture support."""

    source = "geekjob.ru"
    label = "GeekJob"
    base_url = _BASE
    login_url = _LOGIN
    capabilities = SourceCapabilities(
        discover=True,
        inspect=True,
        login=True,
        check=True,
        apply=False,
        apply_status="needs_live_validation",
    )

    def __init__(self) -> None:
        self.description_cache: dict[str, dict[str, Any]] = {}

    def discover(
        self, filters: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        filters = dict(filters or {})
        self.description_cache = {}
        max_pages = _bounded_int(filters, ("max_pages", "pages"), 1, _MAX_PAGES, 1)
        max_details = _bounded_int(
            filters, ("max_details", "details_limit"), 0, _MAX_DETAILS, 0
        )
        budget = _Budget(_request_budget(filters))
        fixture_mode = bool(
            filters.get("fixture_path")
            or filters.get("fixture")
            or filters.get("fixture_dir")
        )
        session = filters.get("session")
        timeout = _timeout(filters)
        collected: dict[str, dict[str, Any]] = {}
        segments: list[ScanSegment] = []

        for page in range(1, max_pages + 1):
            if fixture_mode:
                path = _list_fixture(filters, page)
                if path is None:
                    if page == 1:
                        segments.append(
                            segment(
                                "geekjob",
                                status="failed",
                                error="fixture not found",
                                pages=page,
                            )
                        )
                    else:
                        segments.append(
                            segment(
                                "geekjob",
                                status="truncated",
                                error=f"page {page} fixture not found",
                                pages=page,
                            )
                        )
                    break
                html = path.read_text(encoding="utf-8")
                used = 0
            else:
                if not budget.take():
                    segments.append(
                        segment(
                            "geekjob",
                            status="truncated",
                            error="request budget reached",
                            pages=page,
                        )
                    )
                    break
                page_url = self._list_url(page)
                html, status, error, used = _fetch(session, page_url, timeout)
                if html is None:
                    segments.append(
                        segment(
                            "geekjob",
                            status=status,
                            error=error,
                            pages=page,
                            requests=used,
                        )
                    )
                    break

            parsed, failures, parse_status = self._parse_list(html)
            for item in parsed:
                item["list_page"] = page
                _merge_item(collected, item)
            if failures and parsed:
                parse_status = "partial"
            error = f"{failures} card(s) could not be parsed" if failures else ""
            segments.append(
                segment(
                    "geekjob",
                    status=parse_status,
                    items=len(parsed),
                    error=error,
                    requests=used,
                    pages=page,
                )
            )
            if parse_status in {"failed", "blocked", "empty_verified"}:
                break

        self._add_detail(
            filters,
            collected,
            segments,
            fixture_mode=fixture_mode,
            max_details=max_details,
            budget=budget,
            session=session,
            timeout=timeout,
        )
        return list(collected.values()), segments

    def _list_url(self, page: int) -> str:
        return self.base_url + "/vacancies" if page <= 1 else f"{self.base_url}/vacancies/{page}"

    def _parse_list(
        self, html: str
    ) -> tuple[list[dict[str, Any]], int, str]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("li.collection-item")
        if not cards:
            if _challenge(html):
                return [], 0, "blocked"
            if _explicit_empty(soup):
                return [], 0, "empty_verified"
            return [], 0, "failed"
        items: list[dict[str, Any]] = []
        failures = 0
        for card in cards:
            item = self._parse_card(card)
            if item:
                items.append(item)
            else:
                failures += 1
        if not items:
            return [], failures, "failed"
        return items, failures, "partial" if failures else "ok"

    def _parse_card(self, card: Any) -> dict[str, Any]:
        link = card.select_one("p.vacancy-name a.title")
        if link is None:
            link = card.select_one("a.title")
        if link is None:
            return {}
        href = str(link.get("href") or "")
        native_id = _geekjob_id(href)
        name = clean_text(link.get_text(" ", strip=True))
        if not native_id or not name:
            return {}
        salary_text = clean_text(
            card.select_one("span.salary").get_text(" ", strip=True)
            if card.select_one("span.salary") is not None
            else ""
        )
        salary = _parse_salary_text(salary_text)
        remote_label = clean_text(
            card.select_one("span.remote-label").get_text(" ", strip=True)
            if card.select_one("span.remote-label") is not None
            else ""
        )
        location = _location(card, salary_text)
        time_node = card.select_one("time")
        published = ""
        datetime_node = card.select_one("time[datetime]")
        if datetime_node is not None:
            published = parse_iso_datetime(datetime_node.get("datetime"))
        return self.normalize(
            {
                "native_id": native_id,
                "name": name,
                "company": clean_text(
                    card.select_one("p.company-name").get_text(" ", strip=True)
                    if card.select_one("p.company-name") is not None
                    else ""
                ),
                "url": _geekjob_url(native_id),
                "salary": salary,
                "area": location,
                "remote_text": " ".join(
                    value for value in (remote_label, location) if value
                ),
                "published": published,
                "published_display": clean_text(
                    time_node.get_text(" ", strip=True) if time_node is not None else ""
                ),
                "description_status": "missing",
                "remote_evidence": "site_badge" if remote_label else "area_only",
                "apply_status": "needs_live_validation",
            }
        )

    def _add_detail(
        self,
        filters: Mapping[str, Any],
        collected: dict[str, dict[str, Any]],
        segments: list[ScanSegment],
        *,
        fixture_mode: bool,
        max_details: int,
        budget: _Budget,
        session: Any,
        timeout: float,
    ) -> None:
        path = _detail_fixture(filters) if fixture_mode else None
        direct = bool(filters.get("detail_fixture_path"))
        if filters.get("detail_fixture_path") and path is None:
            segments.append(
                segment("detail_fixture", status="failed", error="detail fixture not found")
            )
            return
        if path is not None:
            html = path.read_text(encoding="utf-8")
            detail = self.parse_detail(
                html,
                url=_fixture_comment_url(html) or str(filters.get("detail_fixture_url") or ""),
            )
            if not detail:
                segments.append(
                    segment(
                        "detail_fixture",
                        status="failed",
                        error="detail fixture unrecognized",
                    )
                )
                return
            target = str(detail.get("native_id") or "")
            self.description_cache[target] = detail
            if target in collected:
                collected[target] = _merge_detail(collected[target], detail)
            elif direct:
                collected[target] = detail
                segments.append(
                    segment(str(detail.get("name") or "detail_fixture"), status="ok", items=1)
                )
            return
        if fixture_mode or max_details <= 0:
            return

        attempted = 0
        blocked = 0
        failed = 0
        budget_hit = False
        for item in _prioritized(collected):
            if attempted >= max_details:
                break
            native_id = str(item.get("native_id") or "")
            if not native_id:
                continue
            if item.get("description_status") == "ok" and item.get("description"):
                continue
            cached = self.description_cache.get(native_id)
            if cached is not None:
                collected[native_id] = _merge_detail(item, cached)
                continue
            if not budget.take():
                budget_hit = True
                break
            attempted += 1
            url = str(item.get("url") or "")
            html, status, _error, _used = _fetch(session, url, timeout)
            if html is None:
                if status == "blocked":
                    blocked += 1
                else:
                    failed += 1
                continue
            detail = self.parse_detail(html, url=url, native_id=native_id)
            if detail:
                self.description_cache[native_id] = detail
                collected[native_id] = _merge_detail(item, detail)
            else:
                failed += 1
        if budget_hit:
            segments.append(
                segment(
                    "detail",
                    status="truncated",
                    error="request budget reached",
                    requests=attempted,
                )
            )
        if blocked or failed:
            reasons: list[str] = []
            if blocked:
                reasons.append(f"{blocked} detail request(s) blocked")
            if failed:
                reasons.append(f"{failed} detail page(s) could not be parsed")
            segments.append(
                segment(
                    "detail",
                    status="partial",
                    error="; ".join(reasons),
                    requests=attempted,
                )
            )

    def parse_detail(
        self, html: str, *, url: str = "", native_id: str = ""
    ) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        article = soup.select_one("article.vacancy")
        if article is None:
            return {}
        title_node = article.select_one("h1")
        if title_node is None:
            return {}
        name = clean_text(title_node.get_text(" ", strip=True))
        if not name:
            return {}
        native_id = clean_text(native_id)
        if not native_id:
            native_id = _geekjob_id(url) or _geekjob_id(_fixture_comment_url(html))
        if not native_id:
            return {}
        salary = _parse_salary_text(article.select_one(".jobinfo .salary"))
        desc_node = article.select_one("div.description")
        description = (
            clean_text(desc_node.get_text(" ", strip=True)) if desc_node is not None else ""
        )
        location = clean_text(
            article.select_one(".location").get_text(" ", strip=True)
            if article.select_one(".location") is not None
            else ""
        )
        experience = clean_text(
            article.select_one(".category").get_text(" ", strip=True)
            if article.select_one(".category") is not None
            else ""
        )
        jobformat = clean_text(
            article.select_one(".jobinfo .jobformat").get_text(" ", strip=True)
            if article.select_one(".jobinfo .jobformat") is not None
            else ""
        )
        time_node = article.select_one(".time")
        published_display = clean_text(
            time_node.get_text(" ", strip=True) if time_node is not None else ""
        )
        skills = [
            clean_text(link.get_text(" ", strip=True))
            for link in article.select(".tags-list a.chip")
        ]
        return self.normalize(
            {
                "native_id": native_id,
                "name": name,
                "company": _company_name(article),
                "url": _geekjob_url(native_id, url),
                "description": description,
                "description_status": "ok" if description else "missing",
                "salary": salary,
                "experience": experience,
                "area": location,
                "remote_text": " ".join(
                    value for value in (jobformat, location, description[:600]) if value
                ),
                "published_display": published_display,
                "skills": skills,
                "respond_button": article.select_one("a.respondbtn") is not None,
                "detail_page": True,
                "apply_status": "needs_live_validation",
            }
        )

    def inspect(
        self, native_id: str, filters: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        filters = dict(filters or {})
        path = _detail_fixture(filters, native_id)
        if path is not None:
            html = path.read_text(encoding="utf-8")
            return self.parse_detail(
                html,
                url=_fixture_comment_url(html) or str(filters.get("detail_fixture_url") or ""),
                native_id=native_id,
            )
        url = _geekjob_url(native_id)
        html, _status, _error, _used = _fetch(
            filters.get("session"), url, _timeout(filters)
        )
        if html is None:
            return {}
        return self.parse_detail(html, url=url, native_id=native_id)

    def normalize(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(raw or {})
        url = clean_text(data.get("url") or data.get("href") or "")
        native_id = clean_text(data.get("native_id") or data.get("id") or "")
        if not native_id:
            native_id = _geekjob_id(url)
        if not native_id:
            return {}
        name = clean_text(data.get("name") or data.get("title") or "")
        if not name:
            return {}
        salary = data.get("salary")
        if isinstance(salary, str):
            salary = parse_salary(salary)
        elif isinstance(salary, dict) and salary.get("provenance") not in (None, "published"):
            salary = None
        description = clean_text(data.get("description") or "")
        extra = dict(data.get("extra") or {})
        for key in (
            "skills",
            "query_sources",
            "list_page",
            "remote_evidence",
            "respond_button",
            "detail_page",
            "published_display",
            "apply_status",
        ):
            if key in data:
                extra[key] = data[key]
        return make_item(
            source=self.source,
            native_id=native_id,
            name=name,
            company=clean_text(data.get("company") or ""),
            url=_geekjob_url(native_id, url),
            description=description,
            salary=salary,
            experience=clean_text(data.get("experience") or ""),
            area=clean_text(data.get("area") or ""),
            remote_text=clean_text(data.get("remote_text") or ""),
            published=clean_text(data.get("published") or ""),
            application_url=_geekjob_url(native_id, url),
            source_status=clean_text(data.get("source_status") or "ok"),
            description_status=clean_text(
                data.get("description_status") or ("ok" if description else "missing")
            ),
            extra=extra,
        )


def _location(card: Any, salary_text: str) -> str:
    anchor = card.select_one("div.info a")
    if anchor is None:
        return ""
    text = clean_text(anchor.get_text(" ", strip=True))
    if salary_text:
        text = clean_text(text.replace(salary_text, ""))
    return text
