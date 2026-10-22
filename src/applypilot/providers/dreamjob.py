"""Read-only DreamJob provider: public company listings plus HH mirror details.

DreamJob mirrors HH vacancies.  Its vacancy ID is the HH vacancy ID, so the
canonical application channel is always ``hh.ru:<id>`` and never a second
DreamJob channel.  The provider discovers public company-listing cards and
optionally enriches them from the public detail page; it never clicks the
HH response modal or authenticates.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from ..parser import ScanSegment
from .base import (
    SourceCapabilities,
    canonical_url,
    clean_text,
    make_item,
    parse_salary,
    segment,
)

_BASE = "https://dreamjob.ru"
_DEFAULT_LISTING = f"{_BASE}/employers/108825/vakansii"
_USER_AGENT = "ApplyPilot/0.1 (read-only source discovery)"
_VACANCY_LINK_RE = re.compile(r"/employers/(\d+)/vakansii/(\d+)")
_CLOSED_MARKERS = (
    "вакансия в архиве",
    "вакансия закрыта",
    "набор закрыт",
    "неактуальна",
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


def _is_closed(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for block in soup.select(".job-vacancy__block"):
        if block.select_one("h1.job-vacancy__head") is not None:
            low = clean_text(block.get_text(" ", strip=True)).casefold()
            return any(marker in low for marker in _CLOSED_MARKERS)
    low = clean_text(soup.get_text(" ", strip=True)).casefold()
    return any(marker in low for marker in _CLOSED_MARKERS)


def _looks_like_detail(soup: BeautifulSoup) -> bool:
    return bool(soup.select_one("main.job-vacancy") or soup.select_one("h1.job-vacancy__head"))


def _looks_like_listing(soup: BeautifulSoup) -> bool:
    if soup.select_one("main.vacancies") or soup.select_one(".vacancies__title-jobboard"):
        return True
    heading = soup.select_one("h1")
    if heading is not None:
        return "вакансии компании" in heading.get_text(" ", strip=True).casefold()
    return False


def _listing_company(soup: BeautifulSoup) -> str:
    heading = soup.select_one("h1.vacancies__title-jobboard, h1")
    if heading is not None:
        text = clean_text(heading.get_text(" ", strip=True))
        company = re.sub(r"^Вакансии компании\s+", "", text, flags=re.IGNORECASE).strip()
        if company and company.casefold() != "вакансии компании":
            return company
    info = soup.select_one(".company-info__title")
    if info is not None:
        text = clean_text(info.get_text(" ", strip=True))
        company = re.sub(r"^Информация о\s+", "", text, flags=re.IGNORECASE).strip()
        if company:
            return company
    return ""


def _fixture_url(path: Path) -> str:
    try:
        head = path.read_text(encoding="utf-8")[:1600]
    except OSError:
        return ""
    match = re.search(r"https://dreamjob\.ru/employers/\d+/vakansii/\d+", head)
    return match.group(0) if match else ""


def _detail_fixture_map(filters: Mapping[str, Any]) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    explicit = filters.get("detail_fixtures")
    if isinstance(explicit, Mapping):
        for key, value in explicit.items():
            path = Path(str(value))
            if not path.is_file():
                continue
            match = re.search(r"\d+", str(key))
            if match:
                mapping[match.group(0)] = path
    directory = filters.get("fixture_dir")
    if directory:
        root = Path(str(directory))
        if root.is_dir():
            for candidate in sorted(root.glob("dreamjob_*.html")):
                url = _fixture_url(candidate)
                match = _VACANCY_LINK_RE.search(url)
                if match:
                    mapping.setdefault(match.group(2), candidate)
    direct = filters.get("detail_fixture_path")
    if direct:
        path = Path(str(direct))
        if path.is_file():
            url = _fixture_url(path) or str(filters.get("detail_fixture_url") or "")
            match = _VACANCY_LINK_RE.search(url)
            if match:
                mapping[match.group(2)] = path
    return mapping


def _listing_fixture_path(filters: Mapping[str, Any]) -> Path | None:
    direct = filters.get("listing_fixture_path")
    if direct:
        path = Path(str(direct))
        if path.is_file():
            return path
    directory = filters.get("fixture_dir")
    if directory:
        candidate = Path(str(directory)) / "dreamjob_company_list.html"
        if candidate.is_file():
            return candidate
    value = filters.get("fixture_path") or filters.get("fixture")
    if value:
        path = Path(str(value))
        if path.is_file():
            soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
            if _looks_like_listing(soup) and not _looks_like_detail(soup):
                return path
    return None


class DreamJobProvider:
    source = "dreamjob.ru"
    label = "DreamJob (HH mirror)"
    base_url = _BASE
    login_url = ""
    capabilities = SourceCapabilities(
        discover=True,
        inspect=True,
        login=False,
        check=False,
        apply=False,
        external=False,
        mirror="hh.ru",
        apply_status="mirror_only",
    )

    def normalize(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(raw or {})
        url = clean_text(data.get("url") or data.get("href") or "")
        native_id = clean_text(data.get("native_id") or data.get("id") or "")
        match = _VACANCY_LINK_RE.search(url)
        if not native_id and match:
            native_id = match.group(2)
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
            "mirror_only",
            "detail_page",
            "published_display",
            "employer_id",
            "remote_badge",
            "apply_status",
            "detail_status",
        ):
            if key in data:
                extra[key] = data[key]
        employer_id = clean_text(data.get("employer_id") or "")
        final_url = self._url(native_id, url, employer_id)
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
            mirror_source="hh.ru",
            mirror_id=native_id,
            source_status=clean_text(data.get("source_status") or "ok"),
            description_status=clean_text(
                data.get("description_status") or ("ok" if description else "missing")
            ),
            extra={**extra, "mirror_only": True, "apply_status": "mirror_only"},
        )

    def _url(self, native_id: str, url: str = "", employer_id: str = "") -> str:
        match = _VACANCY_LINK_RE.search(url)
        if match and match.group(2) == native_id:
            return canonical_url(_BASE, url)
        if employer_id:
            return f"{_BASE}/employers/{employer_id}/vakansii/{native_id}"
        return ""

    def _detail_url(self, item: Mapping[str, Any]) -> str:
        return str(item.get("url") or "")

    def _parse_listing(
        self, html: str, page_url: str
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        soup = BeautifulSoup(html, "html.parser")
        company = _listing_company(soup)
        cards = soup.select(".vacancy")
        stats = {
            "cards": len(cards),
            "parsed": 0,
            "invalid": 0,
            "duplicates": 0,
        }
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for card in cards:
            head = card.select_one(".vacancy__head")
            if head is None:
                stats["invalid"] += 1
                continue
            href = str(head.get("data-link") or head.get("href") or "")
            match = _VACANCY_LINK_RE.search(href)
            if not match:
                stats["invalid"] += 1
                continue
            employer_id, hh_id = match.group(1), match.group(2)
            if hh_id in seen:
                stats["duplicates"] += 1
                continue
            title = clean_text(head.get_text(" ", strip=True))
            if not title:
                stats["invalid"] += 1
                continue
            salary_node = card.select_one(".vacancy__salary")
            salary = parse_salary(salary_node.get_text(" ", strip=True)) if salary_node else None
            description_node = card.select_one(".line-clamp-3") or card.select_one("p")
            description = (
                clean_text(description_node.get_text(" ", strip=True))
                if description_node is not None else ""
            )
            area_node = card.select_one(".vacancy__city")
            area = clean_text(area_node.get_text(" ", strip=True)) if area_node else ""
            date_node = card.select_one(".vacancy__date")
            published_display = (
                clean_text(date_node.get_text(" ", strip=True)) if date_node is not None else ""
            )
            tags = [
                clean_text(node.get_text(" ", strip=True))
                for node in card.select(".tags__item")
                if clean_text(node.get_text(" ", strip=True))
            ]
            experience = " • ".join(
                value for value in tags
                if value.casefold() not in {"новая", "можно удаленно", "можно удалённо"}
            )
            remote_badge = card.select_one(".tags__item_remotely") is not None
            final_url = canonical_url(
                _BASE, f"/employers/{employer_id}/vakansii/{hh_id}"
            )
            item = make_item(
                source=self.source,
                native_id=hh_id,
                name=title,
                company=company,
                url=final_url,
                description=description,
                salary=salary,
                experience=experience,
                area=area,
                remote_text=" ".join([*tags, area, description]),
                application_url=final_url,
                mirror_source="hh.ru",
                mirror_id=hh_id,
                description_status="snippet" if description else "missing",
                extra={
                    "mirror_only": True,
                    "detail_page": False,
                    "published_display": published_display,
                    "employer_id": employer_id,
                    "remote_badge": remote_badge,
                    "apply_status": "mirror_only",
                    "list_page": page_url,
                },
            )
            items.append(item)
            seen.add(hh_id)
            stats["parsed"] += 1
        return items, stats

    def _parse_detail(
        self, html: str, page_url: str, company_hint: str = ""
    ) -> dict[str, Any] | None:
        soup = BeautifulSoup(html, "html.parser")
        if _is_closed(html):
            return None
        title_node = soup.select_one("h1.job-vacancy__head, h1")
        if title_node is None:
            return None
        title = clean_text(title_node.get_text(" ", strip=True))
        if not title:
            return None
        match = _VACANCY_LINK_RE.search(page_url + " " + html)
        if not match:
            return None
        employer_id, hh_id = match.group(1), match.group(2)
        company = ""
        company_node = soup.select_one(".job-vacancy__name-company")
        if company_node is not None:
            company = clean_text(company_node.get_text(" ", strip=True))
        if not company:
            for anchor in soup.select('a[href$="/vakansii"]'):
                text = clean_text(anchor.get_text(" ", strip=True))
                found = re.match(r"Назад к списку вакансий\s+(.+)", text, flags=re.IGNORECASE)
                if found:
                    company = found.group(1).strip()
                    break
        company = company or company_hint
        salary_node = soup.select_one(".job-vacancy__salary")
        salary = parse_salary(salary_node.get_text(" ", strip=True)) if salary_node else None
        description_node = soup.select_one(".job-vacancy__description, .vacancy-description")
        description = (
            clean_text(description_node.get_text(" ", strip=True))
            if description_node is not None else ""
        )
        area_node = soup.select_one(".job-vacancy__location, .vacancy__city")
        area = clean_text(area_node.get_text(" ", strip=True)) if area_node else ""
        date_node = soup.select_one(".vacancy__date")
        published_display = (
            clean_text(date_node.get_text(" ", strip=True)) if date_node is not None else ""
        )
        tags = [
            clean_text(node.get_text(" ", strip=True))
            for node in soup.select(".tags__item, .job-vacancy__info")
            if clean_text(node.get_text(" ", strip=True))
        ]
        final_url = canonical_url(_BASE, page_url) if page_url else (
            f"{_BASE}/employers/{employer_id}/vakansii/{hh_id}"
        )
        return make_item(
            source=self.source,
            native_id=hh_id,
            name=title,
            company=company,
            url=final_url,
            description=description,
            salary=salary,
            area=area,
            remote_text=" ".join([*tags, area, description[:800]]),
            application_url=final_url,
            mirror_source="hh.ru",
            mirror_id=hh_id,
            description_status="ok" if description else "missing",
            extra={
                "mirror_only": True,
                "detail_page": True,
                "published_display": published_display,
                "employer_id": employer_id,
                "apply_status": "mirror_only",
            },
        )

    def discover(
        self, filters: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        filters = dict(filters or {})
        listing_path = _listing_fixture_path(filters)
        if listing_path is not None:
            return self._discover_fixture_listing(listing_path, filters)
        direct = filters.get("fixture_path") or filters.get("fixture")
        if direct and Path(str(direct)).is_file():
            return self._discover_fixture_detail(Path(str(direct)), filters)
        directory = filters.get("fixture_dir")
        if directory:
            path = Path(str(directory)) / "dreamjob_hh_mirror.html"
            if path.is_file():
                return self._discover_fixture_detail(path, filters)
            return [], [segment("fixture", status="failed", items=0, requests=0,
                                error="fixture not found")]
        return self._discover_live(filters)

    def _discover_fixture_detail(
        self, path: Path, filters: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        html = path.read_text(encoding="utf-8")
        item = self._parse_detail(html, _fixture_url(path))
        items = [item] if item is not None else []
        status = "ok" if items else "failed"
        error = "" if items else "no active mirror vacancy"
        return items, [segment("fixture", status=status, items=len(items), requests=0,
                               error=error)]

    def _merge_listing_detail(
        self, listing: Mapping[str, Any], detail: Mapping[str, Any]
    ) -> dict[str, Any]:
        merged = dict(listing)
        for key, value in detail.items():
            if value in (None, "", [], {}):
                continue
            if (
                key == "description_status"
                and value == "missing"
                and listing.get("description")
            ):
                continue
            merged[key] = value
        if str(detail.get("remote_confidence") or "") in {"", "unknown"}:
            for key in (
                "is_remote", "remote_confidence", "remote_reason", "work_format",
                "remote_badge",
            ):
                if key in listing:
                    merged[key] = listing[key]
        merged["mirror_only"] = True
        merged["detail_page"] = True
        merged["apply_status"] = "mirror_only"
        return merged

    def _discover_fixture_listing(
        self, path: Path, filters: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        html = path.read_text(encoding="utf-8")
        page_url = _fixture_url(path) or str(filters.get("fixture_url") or "")
        items, stats = self._parse_listing(html, page_url)
        if not items:
            return [], [segment("company_listing", status="failed", items=0, requests=0,
                                error="company listing returned no vacancies")]
        details = _detail_fixture_map(filters)
        limit = _bounded_int(filters, ("details_limit", "max_details"), 20, 500, 0)
        enriched = 0
        archived = 0
        errors: list[str] = []
        result: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if index >= limit:
                result.append(item)
                continue
            detail_path = details.get(item["native_id"])
            if detail_path is None:
                result.append(item)
                continue
            detail_html = detail_path.read_text(encoding="utf-8")
            detail = self._parse_detail(
                detail_html, _fixture_url(detail_path) or str(item.get("url") or ""),
                str(item.get("company") or ""),
            )
            if detail is None:
                archived += 1
                continue
            result.append(self._merge_listing_detail(item, detail))
            enriched += 1
        if not result:
            return [], [segment(
                "company_listing", status="failed", items=0, requests=0,
                error=f"cards={stats['cards']} all details archived={archived}",
            )]
        status = "partial" if errors else "ok"
        error = (
            f"cards={stats['cards']} parsed={stats['parsed']} enriched={enriched} "
            f"archived={archived} invalid={stats['invalid']}"
        )
        if errors:
            error = f"{error}; " + "; ".join(errors[:3])
        return result, [segment("company_listing", status=status, items=len(result),
                               requests=0, error=error)]

    def _listing_urls(self, filters: Mapping[str, Any]) -> list[str]:
        raw = (
            filters.get("listing_urls")
            or filters.get("company_listing_urls")
            or filters.get("company_listing_url")
            or filters.get("listing_url")
        )
        if isinstance(raw, str):
            values = [raw]
        elif raw:
            values = [str(value) for value in raw]
        else:
            values = []
        employer_ids = filters.get("employer_ids") or []
        if isinstance(employer_ids, (str, int)):
            employer_ids = [employer_ids]
        values.extend(f"{_BASE}/employers/{employer_id}/vakansii" for employer_id in employer_ids)
        if not values:
            values = [_DEFAULT_LISTING]
        result: list[str] = []
        for value in values:
            url = canonical_url(_BASE, str(value))
            if url and url not in result:
                result.append(url)
        return result

    def _discover_live(
        self, filters: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        budget = _Budget(_request_budget(filters))
        limit = _bounded_int(filters, ("details_limit", "max_details"), 20, 500, 0)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        segments: list[ScanSegment] = []
        for listing_url in self._listing_urls(filters):
            if not budget.take():
                segments.append(segment("company_listing", status="truncated", items=0,
                                        requests=0, error="request budget reached"))
                continue
            html, status, error, used = _fetch(filters, listing_url, _timeout(filters))
            if html is None:
                segments.append(segment("company_listing", status=status, items=0,
                                        requests=used, error=f"{listing_url}: {error}"))
                continue
            soup = BeautifulSoup(html, "html.parser")
            if not _looks_like_listing(soup):
                segments.append(segment(
                    "company_listing", status="failed", items=0, requests=used,
                    error=f"{listing_url}: unrecognized company listing HTML",
                ))
                continue
            page_items, stats = self._parse_listing(html, listing_url)
            if not page_items:
                segments.append(segment(
                    "company_listing", status="failed", items=0, requests=used,
                    error=f"{listing_url}: company listing returned no vacancies",
                ))
                continue
            enriched = 0
            details_fetched = 0
            archived = 0
            errors: list[str] = []
            added = 0
            for item in page_items:
                if item["native_id"] in seen:
                    continue
                detail = None
                detail_archived = False
                if details_fetched < limit:
                    details_fetched += 1
                    detail_url = self._detail_url(item)
                    if not budget.take():
                        errors.append("request budget reached during detail enrichment")
                    else:
                        detail_html, _fetch_status, detail_error, detail_used = _fetch(
                            filters, detail_url, _timeout(filters)
                        )
                        used += detail_used
                        if detail_html is None:
                            errors.append(f"{detail_url}: {detail_error}")
                        else:
                            detail = self._parse_detail(
                                detail_html, detail_url, str(item.get("company") or "")
                            )
                            if detail is None:
                                detail_archived = True
                                archived += 1
                if detail_archived:
                    # A closed detail page is a closed vacancy, not a live card.
                    continue
                final = self._merge_listing_detail(item, detail) if detail else item
                if detail is not None:
                    enriched += 1
                seen.add(item["native_id"])
                items.append(final)
                added += 1
            seg_status = "ok" if added else "failed"
            if errors:
                seg_status = "partial" if added else "failed"
            detail = (
                f"cards={stats['cards']} parsed={stats['parsed']} added={added} "
                f"enriched={enriched} archived={archived} invalid={stats['invalid']}"
            )
            if errors:
                detail = f"{detail}; " + "; ".join(errors[:3])
            segments.append(segment("company_listing", status=seg_status, items=added,
                                    requests=used, error=detail))
        return items, segments

    def inspect(
        self, native_id: str, filters: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        filters = dict(filters or {})
        path = _detail_fixture_map(filters).get(str(native_id))
        if path is not None:
            html = path.read_text(encoding="utf-8")
            return self._parse_detail(html, _fixture_url(path), str(native_id)) or {}
        employer_id = clean_text(filters.get("employer_id") or "")
        if not employer_id:
            return {}
        url = f"{_BASE}/employers/{employer_id}/vakansii/{native_id}"
        html, _status, _error, _used = _fetch(filters, url, _timeout(filters))
        if html is None:
            return {}
        return self._parse_detail(html, url, str(native_id)) or {}
