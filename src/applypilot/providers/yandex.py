"""Read-only Yandex Careers provider using confirmed public endpoints.

Only the two listing URLs confirmed during the source audit are used:
``/jobs/vacancies?work_modes=remote`` and
``/jobs/vacancies?services=generativetech&work_modes=remote``.  The provider
does not invent text queries, pagination or sorting; every HTTP request is
charged to the caller's ``request_budget``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ..parser import ScanSegment
from .base import (
    SourceCapabilities,
    canonical_url,
    clean_text,
    json_ld,
    make_item,
    parse_iso_datetime,
    parse_salary,
    segment,
)

_BASE = "https://yandex.ru/jobs"
_USER_AGENT = "ApplyPilot/0.1 (read-only source discovery)"
_ENDPOINTS = (
    ("remote", f"{_BASE}/vacancies?work_modes=remote"),
    ("generativetech-remote", f"{_BASE}/vacancies?services=generativetech&work_modes=remote"),
)
_ARCHIVE_MARKERS = (
    "вакансия в архиве",
    "вакансия закрыта",
    "набор закрыт",
    "неактуальна",
    "vacancy is closed",
    "position closed",
)


class _Budget:
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


def _fetch(
    filters: Mapping[str, Any], url: str, timeout: float
) -> tuple[str | None, str, str, int]:
    client = filters.get("session") or requests.Session()
    try:
        response = client.get(url, timeout=timeout, headers={"User-Agent": _USER_AGENT})
    except requests.RequestException:
        return None, "failed", "network error", 1
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 403:
        return None, "blocked", "HTTP 403 access limited", 1
    if status == 429:
        return None, "blocked", "HTTP 429 rate limited", 1
    if status >= 400:
        return None, "failed", f"HTTP {status}", 1
    text = getattr(response, "text", "") or ""
    if not clean_text(text):
        return None, "failed", "empty response", 1
    return text, "ok", "", 1


def _is_archived_text(text: str) -> bool:
    low = clean_text(text).casefold()
    return any(marker in low for marker in _ARCHIVE_MARKERS)


def _known_listing(soup: BeautifulSoup, html: str) -> bool:
    if "VacancySnippet_" in html:
        return True
    if soup.select("a[href*='/jobs/vacancies/']"):
        return True
    heading = soup.select_one("h1")
    return bool(heading and "вакансии" in heading.get_text(" ", strip=True).casefold())


def _job_posting(soup: BeautifulSoup) -> dict[str, Any] | None:
    for value in json_ld(soup):
        types = value.get("@type")
        if isinstance(types, str):
            types = [types]
        if any(str(item).casefold() == "jobposting" for item in types or []):
            return value
    return None


def _json_location(job: Mapping[str, Any]) -> str:
    location = job.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else {}
    if not isinstance(location, Mapping):
        return ""
    address = location.get("address")
    if isinstance(address, str):
        return clean_text(address)
    if not isinstance(address, Mapping):
        return ""
    parts = []
    for key in ("addressLocality", "addressRegion", "addressCountry"):
        value = address.get(key)
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("addressCountry")
        if value:
            parts.append(clean_text(value))
    return ", ".join(dict.fromkeys(parts))


def _json_description(job: Mapping[str, Any]) -> str:
    raw = job.get("description") or ""
    if not raw:
        return ""
    return clean_text(BeautifulSoup(str(raw), "html.parser").get_text(" ", strip=True))


class YandexProvider:
    source = "yandex.ru/jobs"
    label = "Карьера Яндекса"
    base_url = _BASE
    login_url = "https://yandex.ru/jobs/profile"
    capabilities = SourceCapabilities(
        discover=True,
        inspect=True,
        login=False,
        check=False,
        apply=False,
        apply_status="needs_live_validation",
    )

    def normalize(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(raw or {})
        url = clean_text(data.get("url") or data.get("href") or "")
        native_id = clean_text(data.get("native_id") or data.get("id") or "")
        if not native_id:
            native_id = self._native_id(url)
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
        for key in ("skills", "detail_page", "published_display", "apply_status", "list_page"):
            if key in data:
                extra[key] = data[key]
        final_url = self._url(native_id)
        return make_item(
            source=self.source,
            native_id=native_id,
            name=name,
            company=clean_text(data.get("company") or ""),
            url=final_url,
            description=description,
            salary=salary,
            experience=clean_text(data.get("experience") or ""),
            area=clean_text(data.get("area") or ""),
            remote_text=clean_text(data.get("remote_text") or ""),
            published=clean_text(data.get("published") or ""),
            application_url=final_url,
            source_status=clean_text(data.get("source_status") or "ok"),
            description_status=clean_text(
                data.get("description_status") or ("snippet" if description else "missing")
            ),
            extra=extra,
        )

    def _native_id(self, url: str) -> str:
        path = urlparse(clean_text(url)).path.rstrip("/")
        match = re.search(r"-(\d+)$", path)
        return match.group(1) if match else ""

    def _url(self, native_id: str) -> str:
        return f"{_BASE}/vacancies/{native_id}"

    def _parse_cards_detailed(
        self, html: str, page_url: str = ""
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("article")
        stats = {
            "cards": len(cards),
            "parsed": 0,
            "archived": 0,
            "external": 0,
            "invalid": 0,
            "duplicates": 0,
        }
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for card in cards:
            text = clean_text(card.get_text(" ", strip=True))
            if _is_archived_text(text):
                stats["archived"] += 1
                continue
            heading = card.select_one("h2,h3")
            link = heading.select_one("a[href]") if heading is not None else None
            if link is None:
                link = card.select_one("a[href*='/jobs/vacancies/']")
            if link is None:
                stats["invalid"] += 1
                continue
            href = canonical_url(_BASE, str(link.get("href") or ""))
            parsed = urlparse(href)
            if parsed.netloc.casefold() not in {"yandex.ru", "www.yandex.ru"}:
                stats["external"] += 1
                continue
            native_id = self._native_id(href)
            if not native_id:
                stats["invalid"] += 1
                continue
            if native_id in seen:
                stats["duplicates"] += 1
                continue
            title = clean_text(link.get_text(" ", strip=True))
            if not title and heading is not None:
                title = clean_text(heading.get_text(" ", strip=True))
            if not title:
                stats["invalid"] += 1
                continue
            company = ""
            brand = card.select_one("[class*=headerTitle]")
            if brand is not None:
                company = clean_text(brand.get_text(" ", strip=True))
            if not company:
                service = card.select_one("a[href*='/jobs/services/']")
                if service is not None:
                    company = clean_text(service.get_text(" ", strip=True))
            if not company and "яндекс" in text.casefold():
                company = "Яндекс"
            body = card.select_one("[class*=VacancySnippet_body]")
            description = " ".join(
                clean_text(node.get_text(" ", strip=True))
                for node in (body.select("p") if body is not None else [])
                if clean_text(node.get_text(" ", strip=True))
            )
            salary = None
            salary_match = re.search(
                r"\d[\d\s\u00a0]*(?:[—–-]\s*\d[\d\s\u00a0]*)?\s*(?:₽|руб|\$|€)", text
            )
            if salary_match:
                salary = parse_salary(salary_match.group(0))
            area_parts: list[str] = []
            for node in card.select(
                "a[href*='/jobs/vacancies/city_'], a[href*='city=']"
            ):
                value = clean_text(node.get_text(" ", strip=True))
                value = re.sub(r",?\s*и ещё\s+\d+.*$", "", value).strip(" ,")
                if value and value not in area_parts:
                    area_parts.append(value)
            area = ", ".join(area_parts)
            skills = []
            for node in card.select("a[href*='skills=']"):
                value = clean_text(node.get_text(" ", strip=True))
                value = re.sub(r",?\s*и ещё\s+\d+.*$", "", value).strip(" ,")
                if value and not value.casefold().startswith("и ещё"):
                    skills.append(value)
            published = ""
            published_display = ""
            time_node = card.select_one("time")
            if time_node is not None:
                published_display = clean_text(time_node.get_text(" ", strip=True))
                published = parse_iso_datetime(time_node.get("datetime") or "")
            items.append(make_item(
                source=self.source,
                native_id=native_id,
                name=title,
                company=company,
                url=href,
                description=description,
                salary=salary,
                area=area,
                remote_text=text,
                published=published,
                application_url=href,
                description_status="snippet" if description else "missing",
                extra={
                    "skills": list(dict.fromkeys(skills)),
                    "detail_page": False,
                    "published_display": published_display,
                    "list_page": page_url,
                    "apply_status": "needs_live_validation",
                },
            ))
            seen.add(native_id)
            stats["parsed"] += 1
        return items, stats

    def _parse_cards(self, html: str, page_url: str = "") -> list[dict[str, Any]]:
        return self._parse_cards_detailed(html, page_url)[0]

    def _parse_detail(self, html: str, url: str, native_id: str = "") -> dict[str, Any] | None:
        soup = BeautifulSoup(html, "html.parser")
        if _is_archived_text(soup.get_text(" ", strip=True)):
            return None
        native_id = native_id or self._native_id(url)
        if not native_id:
            return None
        job = _job_posting(soup)
        title = ""
        company = ""
        description = ""
        area = ""
        salary = None
        published = ""
        if job is not None:
            title = clean_text(job.get("title") or job.get("name"))
            organization = job.get("hiringOrganization")
            if isinstance(organization, Mapping):
                company = clean_text(organization.get("name") or organization.get("legalName"))
            else:
                company = clean_text(organization)
            description = _json_description(job)
            area = _json_location(job)
            base_salary = job.get("baseSalary")
            if base_salary:
                salary = parse_salary(base_salary)
            published = parse_iso_datetime(job.get("datePosted"))
        if not title:
            heading = soup.select_one("h1")
            title = clean_text(heading.get_text(" ", strip=True)) if heading else ""
        if not title:
            return None
        if not company:
            brand = soup.select_one("[class*=headerTitle]")
            company = clean_text(brand.get_text(" ", strip=True)) if brand is not None else ""
        if not description:
            main = soup.select_one("main") or soup
            paragraphs = [
                clean_text(node.get_text(" ", strip=True))
                for node in main.select("p")
                if clean_text(node.get_text(" ", strip=True))
            ]
            description = " ".join(paragraphs[:30])
        if not area:
            location = soup.select_one("[class*=location], [class*=Location]")
            if location is not None:
                area = clean_text(location.get_text(" ", strip=True))
        text = clean_text(soup.get_text(" ", strip=True))
        if not salary:
            salary_match = re.search(
                r"\d[\d\s\u00a0]*(?:[—–-]\s*\d[\d\s\u00a0]*)?\s*(?:₽|руб|\$|€)", text
            )
            if salary_match:
                salary = parse_salary(salary_match.group(0))
        if not published:
            time_node = soup.select_one("time[datetime]")
            if time_node is not None:
                published = parse_iso_datetime(time_node.get("datetime") or "")
        final_url = self._url(native_id)
        return make_item(
            source=self.source,
            native_id=native_id,
            name=title,
            company=company,
            url=final_url,
            description=description,
            salary=salary,
            area=area,
            remote_text=text,
            published=published,
            application_url=final_url,
            description_status="ok" if description else "missing",
            extra={"detail_page": True, "apply_status": "needs_live_validation"},
        )

    def discover(
        self, filters: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        filters = dict(filters or {})
        direct = filters.get("fixture_path") or filters.get("fixture")
        if direct and Path(str(direct)).is_file():
            return self._discover_fixture(Path(str(direct)))
        directory = filters.get("fixture_dir")
        if directory:
            root = Path(str(directory))
            for name in ("yandex_genai_remote_list.html", "yandex_jobs.html"):
                candidate = root / name
                if candidate.is_file():
                    return self._discover_fixture(candidate)
            return [], [segment("fixture", status="failed", items=0, requests=0,
                                error="fixture not found")]
        return self._discover_live(filters)

    def _discover_fixture(
        self, path: Path
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        html = path.read_text(encoding="utf-8")
        items, stats = self._parse_cards_detailed(html, str(path))
        known = _known_listing(BeautifulSoup(html, "html.parser"), html)
        if items:
            status = "ok"
        elif known:
            status = "empty_verified"
        else:
            status = "failed"
        error = ""
        if not items:
            error = "no active Yandex vacancy cards" if known else "unrecognized listing HTML"
        elif stats["archived"] or stats["external"] or stats["invalid"]:
            error = (
                f"cards={stats['cards']} parsed={stats['parsed']} "
                f"archived={stats['archived']} external={stats['external']} "
                f"invalid={stats['invalid']}"
            )
        return items, [segment("fixture", status=status, items=len(items), requests=0,
                               error=error)]

    def _discover_live(
        self, filters: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        budget = _Budget(_request_budget(filters))
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        segments: list[ScanSegment] = []
        for label, url in _ENDPOINTS:
            if not budget.take():
                segments.append(segment(label, status="truncated", items=0, requests=0,
                                        error="request budget reached",
                                        order_by="confirmed_endpoint"))
                continue
            html, status, error, used = _fetch(filters, url, _timeout(filters))
            if html is None:
                segments.append(segment(label, status=status, items=0, requests=used,
                                        error=error, order_by="confirmed_endpoint"))
                continue
            page_items, stats = self._parse_cards_detailed(html, url)
            known = _known_listing(BeautifulSoup(html, "html.parser"), html)
            added = 0
            for item in page_items:
                if item["native_id"] in seen:
                    continue
                seen.add(item["native_id"])
                items.append(item)
                added += 1
            if added or page_items:
                seg_status = "ok"
            elif known:
                seg_status = "empty_verified"
            else:
                seg_status = "failed"
            detail = (
                f"cards={stats['cards']} parsed={stats['parsed']} added={added} "
                f"duplicates={len(page_items) - added} archived={stats['archived']} "
                f"external={stats['external']} invalid={stats['invalid']}"
            )
            if not added and not known:
                detail = f"{detail}; unrecognized listing HTML"
            segments.append(segment(label, status=seg_status, items=added, requests=used,
                                    error=detail, order_by="confirmed_endpoint"))
        return items, segments

    def inspect(
        self, native_id: str, filters: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        filters = dict(filters or {})
        path = filters.get("detail_fixture_path")
        if path and Path(str(path)).is_file():
            html = Path(str(path)).read_text(encoding="utf-8")
            return self._parse_detail(
                html, str(filters.get("detail_fixture_url") or self._url(str(native_id))),
                str(native_id),
            ) or {}
        url = self._url(str(native_id))
        html, _status, _error, _used = _fetch(filters, url, _timeout(filters))
        if html is None:
            return {}
        return self._parse_detail(html, url, str(native_id)) or {}
