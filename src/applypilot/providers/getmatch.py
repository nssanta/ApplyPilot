"""Read-only Getmatch provider with bounded public sitemap discovery.

Getmatch renders its catalogue client-side, but publishes vacancy detail URLs in
``/sitemap.xml``.  This provider deliberately never calls the private ``/api/``
surface: it reads the sitemap, pre-filters slugs, fetches a bounded number of
detail pages and parses schema.org ``JobPosting`` JSON-LD (with a visible-HTML
fallback for saved fixtures).  Vacancy IDs are *not* treated as publication
dates; only a real ``datePosted`` value is stored.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import tempfile
import time
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

_BASE = "https://getmatch.ru"
_SITEMAP_URL = f"{_BASE}/sitemap.xml"
_USER_AGENT = "ApplyPilot/0.1 (read-only source discovery)"
_VACANCY_PATH_RE = re.compile(r"/vacancies/(\d+)(?:-([^/]+))?/?$")
_VACANCY_ANY_RE = re.compile(r"/vacancies/(\d+)")
_SITEMAP_LOC_RE = re.compile(r"<loc\b[^>]*>(.*?)</loc>", re.IGNORECASE | re.DOTALL)

# Slug pre-filter: a broad AI/backend vocabulary, not a hidden quality verdict.
_SLUG_WEIGHTS = {
    "llm": 6,
    "ai": 5,
    "agent": 5,
    "genai": 5,
    "python": 4,
    "ml": 4,
    "data": 3,
    "backend": 3,
    "golang": 2,
    "fullstack": 2,
}


class _Budget:
    """Request counter; ``None`` means no configured limit."""

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


def _native_and_slug(url: str) -> tuple[str, str]:
    path = urlparse(clean_text(url)).path
    match = _VACANCY_PATH_RE.search(path)
    if not match:
        return "", ""
    return match.group(1), (match.group(2) or "").strip("-").casefold()


def _slug_score(slug: str) -> int:
    low = clean_text(slug).casefold()
    tokens = {token for token in re.split(r"[^a-z0-9]+", low) if token}
    score = 0
    for term, weight in _SLUG_WEIGHTS.items():
        if term in tokens:
            score += weight
        elif len(term) >= 3 and term in low:
            score += max(1, weight - 2)
    if tokens & {"senior", "lead", "principal", "staff", "head"}:
        score += 1
    return score


def _slug_role_score(slug: str, terms: Any) -> int:
    """Scores explicit role terms from the active search profile against a URL slug."""
    slug_tokens = [
        token for token in re.split(r"[^a-z0-9а-яё]+", clean_text(slug).casefold()) if token
    ]
    tokens = set(slug_tokens)
    compact_slug = "".join(slug_tokens)
    score = 0
    for raw_term in terms or ():
        words = [
            token for token in re.split(r"[^a-z0-9а-яё]+", clean_text(str(raw_term)).casefold())
            if token
        ]
        if not words:
            continue
        if set(words).issubset(tokens) or "".join(words) in compact_slug:
            score += 1
    return score


def _sitemap_rows(xml_text: str) -> list[dict[str, Any]]:
    """Parses public sitemap ``<loc>`` values and deduplicates by vacancy ID."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _SITEMAP_LOC_RE.findall(xml_text or ""):
        value = html_lib.unescape(clean_text(raw))
        value = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", value)
        parsed = urlparse(value)
        if parsed.netloc and parsed.netloc.casefold() not in {"getmatch.ru", "www.getmatch.ru"}:
            continue
        native_id, slug = _native_and_slug(value)
        if not native_id or native_id in seen:
            continue
        seen.add(native_id)
        rows.append({
            "native_id": native_id,
            "slug": slug,
            "url": canonical_url(_BASE, value),
            "score": _slug_score(slug),
            "sitemap_position": len(rows),
        })
    return rows


def _load_seen(filters: Mapping[str, Any]) -> tuple[set[str], Path | None]:
    seen: set[str] = set()
    state_path = filters.get("state_path")
    path = Path(str(state_path)) if state_path else None
    now = time.time()
    active_ttl = max(86400, int(filters.get("active_refresh_days", 7)) * 86400)
    archived_ttl = max(active_ttl, int(filters.get("archived_refresh_days", 30)) * 86400)
    if path is not None and path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            active = payload.get("active_seen")
            archived = payload.get("archived_seen")
            if isinstance(active, Mapping) or isinstance(archived, Mapping):
                raw = []
                for values, ttl in ((active, active_ttl), (archived, archived_ttl)):
                    if not isinstance(values, Mapping):
                        continue
                    for value, stamp in values.items():
                        try:
                            fresh = 0 <= now - float(stamp) < ttl
                        except (TypeError, ValueError):
                            fresh = False
                        if fresh:
                            seen.add(str(value))
            else:
                raw = payload.get("seen_ids") or payload.get("ids") or payload.get("seen_urls") or []
                try:
                    legacy_age = now - path.stat().st_mtime
                except OSError:
                    legacy_age = 0
                # Legacy state had no per-ID timestamps. Recheck it once if the
                # old cursor is stale so active listings can be refreshed.
                if legacy_age >= max(active_ttl, archived_ttl):
                    raw = []
        else:
            raw = payload
        if isinstance(raw, (str, bytes)):
            raw = [raw]
        if isinstance(raw, Mapping):
            raw = list(raw.keys())
        for value in raw or []:
            match = _VACANCY_ANY_RE.search(str(value))
            if match:
                seen.add(match.group(1))
            elif str(value).strip():
                seen.add(str(value).strip())
    provided = filters.get("seen_ids")
    if isinstance(provided, (str, bytes)):
        provided = [provided]
    if isinstance(provided, Mapping):
        provided = list(provided.keys())
    for value in provided or []:
        match = _VACANCY_ANY_RE.search(str(value))
        if match:
            seen.add(match.group(1))
        elif str(value).strip():
            seen.add(str(value).strip())
    return seen, path


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _persist_seen(
    path: Path | None,
    seen: set[str],
    urls: set[str],
    filters: Mapping[str, Any],
    *,
    refreshed_ids: set[str] | None = None,
    archived_ids: set[str] | None = None,
) -> None:
    if path is not None:
        previous: dict[str, Any] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, json.JSONDecodeError):
            previous = {}
        timestamp = time.time()
        active_seen = dict(previous.get("active_seen") or {})
        archived_seen = dict(previous.get("archived_seen") or {})
        if not active_seen and not archived_seen:
            try:
                legacy_stamp = path.stat().st_mtime
            except OSError:
                legacy_stamp = timestamp
            legacy = previous.get("seen_ids") or previous.get("ids") or []
            if isinstance(legacy, (str, bytes)):
                legacy = [legacy]
            if isinstance(legacy, Mapping):
                legacy = list(legacy.keys())
            for value in legacy:
                match = _VACANCY_ANY_RE.search(str(value))
                if match:
                    active_seen.setdefault(match.group(1), legacy_stamp)
        current = {str(value) for value in seen}
        refreshed = {str(value) for value in (refreshed_ids or set())}
        archived = {str(value) for value in (archived_ids or set())}
        active_seen = {
            key: value for key, value in active_seen.items()
            if key in current and key not in archived
        }
        archived_seen = {
            key: value for key, value in archived_seen.items()
            if key in current or key in archived
        }
        for value in current - archived:
            if value not in active_seen or value in refreshed:
                active_seen[value] = timestamp
        for value in archived:
            active_seen.pop(value, None)
            archived_seen[value] = timestamp
        _atomic_write_json(path, {
            "version": 2,
            "seen_ids": sorted(current),  # compatibility with older readers
            "active_seen": active_seen,
            "archived_seen": archived_seen,
            "seen_urls": sorted(urls),
        })
    provided = filters.get("seen_ids")
    if isinstance(provided, set):
        provided.update(seen)
    elif isinstance(provided, list):
        known = {str(value) for value in provided}
        provided.extend(value for value in sorted(seen) if value not in known)


def _fixture_url(path: Path) -> str:
    try:
        head = path.read_text(encoding="utf-8")[:1600]
    except OSError:
        return ""
    match = re.search(r"https://getmatch\.ru/vacancies/[0-9a-z-]+", head)
    return match.group(0) if match else ""


def _scan_detail_fixtures(mapping: dict[str, Path], root: Path) -> None:
    if not root.is_dir():
        return
    for candidate in sorted(root.glob("*.html")):
        url = _fixture_url(candidate)
        native_id, _slug = _native_and_slug(url)
        if native_id:
            mapping.setdefault(native_id, candidate)


def _detail_fixture_map(filters: Mapping[str, Any]) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    explicit = filters.get("detail_fixtures")
    if isinstance(explicit, Mapping):
        for key, value in explicit.items():
            path = Path(str(value))
            if not path.is_file():
                continue
            match = _VACANCY_ANY_RE.search(str(key))
            if match:
                mapping[match.group(1)] = path
    directory = filters.get("fixture_dir")
    if directory:
        _scan_detail_fixtures(mapping, Path(str(directory)))
    for key in ("sitemap_fixture_path", "sitemap_path", "fixture_path"):
        value = filters.get(key)
        if not value:
            continue
        path = Path(str(value))
        if not path.is_file():
            continue
        if path.suffix.casefold() == ".xml":
            _scan_detail_fixtures(mapping, path.parent)
            continue
        try:
            head = path.read_text(encoding="utf-8")[:800].casefold()
        except OSError:
            continue
        if "<urlset" in head:
            _scan_detail_fixtures(mapping, path.parent)
    direct = filters.get("detail_fixture_path")
    if direct:
        path = Path(str(direct))
        if path.is_file():
            url = _fixture_url(path) or str(filters.get("detail_fixture_url") or "")
            native_id, _slug = _native_and_slug(url)
            if native_id:
                mapping[native_id] = path
    return mapping


def _sitemap_fixture_text(filters: Mapping[str, Any]) -> str | None:
    if filters.get("sitemap_text") is not None:
        return str(filters["sitemap_text"])
    for key in ("sitemap_fixture_path", "sitemap_path"):
        value = filters.get(key)
        if not value:
            continue
        path = Path(str(value))
        if path.is_file():
            return path.read_text(encoding="utf-8")
    direct = filters.get("fixture_path") or filters.get("fixture")
    if direct:
        path = Path(str(direct))
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if "<urlset" in text.casefold() or "<sitemapindex" in text.casefold():
                return text
    directory = filters.get("fixture_dir")
    if directory:
        candidate = Path(str(directory)) / "getmatch_sitemap_sample.xml"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
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


def _is_archived(soup: BeautifulSoup) -> bool:
    text = soup.get_text(" ", strip=True).casefold()
    return "вакансия в архиве" in text


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


def _json_company(job: Mapping[str, Any]) -> str:
    organization = job.get("hiringOrganization")
    if isinstance(organization, Mapping):
        return clean_text(organization.get("name") or organization.get("legalName"))
    return clean_text(organization)


def _json_description(job: Mapping[str, Any]) -> str:
    raw = job.get("description") or ""
    if not raw:
        return ""
    return clean_text(BeautifulSoup(str(raw), "html.parser").get_text(" ", strip=True))


class GetMatchProvider:
    source = "getmatch.ru"
    label = "Getmatch"
    base_url = _BASE
    login_url = "https://getmatch.ru/login"
    capabilities = SourceCapabilities(
        discover=True,
        inspect=True,
        login=True,
        check=True,
        apply=False,
        apply_status="verified_telegram_callback",
    )

    def normalize(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(raw or {})
        url = clean_text(data.get("url") or data.get("href") or "")
        native_id = clean_text(data.get("native_id") or data.get("id") or "")
        if not native_id:
            native_id, slug = _native_and_slug(url)
        else:
            _url_id, slug = _native_and_slug(url)
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
        for key in ("skills", "slug", "detail_page", "json_ld", "apply_status"):
            if key in data:
                extra[key] = data[key]
        final_url = self._url(native_id, slug or url)
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
                data.get("description_status") or ("ok" if description else "missing")
            ),
            extra=extra,
        )

    def _url(self, native_id: str, url_or_slug: str = "") -> str:
        if url_or_slug.startswith("http"):
            parsed = urlparse(url_or_slug)
            if parsed.netloc.casefold() in {"getmatch.ru", "www.getmatch.ru"}:
                candidate_id, _slug = _native_and_slug(url_or_slug)
                if candidate_id == native_id:
                    return canonical_url(_BASE, url_or_slug)
        slug = clean_text(url_or_slug).casefold()
        if slug.startswith("http"):
            _candidate_id, slug = _native_and_slug(url_or_slug)
        slug = re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")
        suffix = f"-{slug}" if slug and not slug.startswith(str(native_id)) else ""
        return f"{_BASE}/vacancies/{native_id}{suffix}"

    def _parse_detail(
        self, html: str, url: str, native_id: str = ""
    ) -> dict[str, Any] | None:
        soup = BeautifulSoup(html, "html.parser")
        if _is_archived(soup):
            return None
        url_id, slug = _native_and_slug(url)
        native_id = native_id or url_id
        if not native_id:
            return None
        job = _job_posting(soup)
        title = ""
        company = ""
        published = ""
        salary: dict[str, Any] | None = None
        description = ""
        area = ""
        remote_text = ""
        experience = ""
        if job is not None:
            title = clean_text(job.get("title") or job.get("name"))
            company = _json_company(job)
            published = parse_iso_datetime(job.get("datePosted"))
            base_salary = job.get("baseSalary")
            if isinstance(base_salary, list):
                base_salary = base_salary[0] if base_salary else None
            if base_salary:
                salary = parse_salary(base_salary)
            description = _json_description(job)
            area = _json_location(job)
            if str(job.get("jobLocationType") or "").casefold() == "telecommute":
                remote_text = "Удалённо"
            requirements = job.get("experienceRequirements")
            if isinstance(requirements, Mapping):
                experience = clean_text(requirements.get("name") or requirements.get("description"))
            elif requirements:
                experience = clean_text(requirements)
        if not title:
            heading = soup.select_one("h1")
            title = clean_text(heading.get_text(" ", strip=True)) if heading else ""
        if not title:
            return None
        if not company:
            for heading in soup.select("h2"):
                text = clean_text(heading.get_text(" ", strip=True))
                if text.casefold().startswith("в "):
                    company = text[2:].strip()
                    break
        if not salary:
            salary_node = soup.select_one(".b-vacancy-header h3, .vacancy-header h3, h3")
            if salary_node is not None:
                salary = parse_salary(salary_node.get_text(" ", strip=True))
        if not area:
            location = soup.select_one(".b-location, .b-vacancy-locations, .vacancy-locations")
            if location is not None:
                area = clean_text(location.get_text(" | ", strip=True))
        if not description:
            parts: list[str] = []
            for selector in (".b-vacancy-description", ".vacancy-description",
                             ".b-vacancy-short-description"):
                for node in soup.select(selector):
                    text = clean_text(node.get_text(" ", strip=True))
                    if text and not any(text in part or part in text for part in parts):
                        parts.append(text)
            description = " ".join(parts)
        skills: list[str] = []
        stack = soup.select_one(".b-vacancy-stack")
        if stack is not None:
            for node in stack.select(".b-vacancy-stack-container span"):
                text = clean_text(node.get_text(" ", strip=True))
                if text and text not in skills:
                    skills.append(text)
        final_url = self._url(native_id, url)
        remote = " ".join(value for value in (remote_text, area, description[:800]) if value)
        return make_item(
            source=self.source,
            native_id=native_id,
            name=title,
            company=company,
            url=final_url,
            description=description,
            salary=salary,
            experience=experience,
            area=area,
            remote_text=remote,
            published=published,
            application_url=final_url,
            description_status="ok" if description else "missing",
            extra={
                "skills": skills,
                "slug": slug,
                "detail_page": True,
                "json_ld": job is not None,
                "apply_status": "needs_live_validation",
            },
        )

    def discover(
        self, filters: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        filters = dict(filters or {})
        sitemap_text = _sitemap_fixture_text(filters)
        if sitemap_text is not None:
            return self._discover_sitemap(sitemap_text, filters, offline=True)
        direct = filters.get("fixture_path") or filters.get("fixture")
        if direct and Path(str(direct)).is_file():
            return self._discover_fixture_details(filters)
        urls = [str(value) for value in filters.get("detail_urls", []) if str(value).startswith("http")]
        if urls:
            if not filters.get("include_sitemap"):
                return self._discover_urls(urls, filters)
            # Bot links are a priority source, not a replacement for the
            # public catalogue. Share one request budget and then continue
            # through the sitemap, suppressing IDs already fetched above.
            telegram_filters = dict(filters)
            if filters.get("telegram_state_path"):
                telegram_filters["state_path"] = filters["telegram_state_path"]
            budget = _Budget(_request_budget(filters))
            linked_items, linked_segments = self._discover_urls(
                urls, telegram_filters, budget=budget,
            )
            if any(str(value.status) == "blocked" for value in linked_segments):
                return linked_items, linked_segments
            sitemap_filters = dict(filters)
            sitemap_filters["detail_urls"] = []
            configured_details = _bounded_int(
                filters, ("details_limit", "max_details"), 20, 500, 0,
            )
            linked_requests = sum(int(value.requests or 0) for value in linked_segments)
            sitemap_filters["details_limit"] = max(0, configured_details - linked_requests)
            sitemap_filters["seen_ids"] = {
                str(item.get("native_id") or item.get("id") or "")
                for item in linked_items
                if str(item.get("native_id") or item.get("id") or "").isdigit()
            }
            sitemap_items, sitemap_segments = self._discover_live_sitemap(
                sitemap_filters, budget=budget,
            )
            merged: dict[str, dict[str, Any]] = {}
            for item in [*linked_items, *sitemap_items]:
                key = str(item.get("native_id") or item.get("id") or "")
                if key:
                    merged.setdefault(key, item)
            return list(merged.values()), [*linked_segments, *sitemap_segments]
        return self._discover_live_sitemap(filters)

    def _discover_fixture_details(
        self, filters: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        paths: list[Path] = []
        for key in ("fixture_path", "fixture", "detail_fixture_path"):
            value = filters.get(key)
            if value:
                path = Path(str(value))
                if path.is_file() and path not in paths:
                    paths.append(path)
        items: list[dict[str, Any]] = []
        archived = 0
        invalid = 0
        seen: set[str] = set()
        for path in paths:
            html = path.read_text(encoding="utf-8")
            if _is_archived(BeautifulSoup(html, "html.parser")):
                archived += 1
                continue
            url = _fixture_url(path) or str(filters.get("fixture_url") or "")
            item = self._parse_detail(html, url)
            if item is None:
                invalid += 1
                continue
            if item["native_id"] in seen:
                continue
            seen.add(item["native_id"])
            items.append(item)
        status = "ok" if items else "failed"
        error = ""
        if not items:
            error = "saved fixture is archived" if archived else "no parseable active vacancy"
        elif archived or invalid:
            error = f"archived={archived} invalid={invalid}"
        return items, [segment("fixture", status=status, items=len(items), requests=0,
                               error=error)]

    def _discover_sitemap(
        self,
        sitemap_text: str,
        filters: Mapping[str, Any],
        *,
        offline: bool,
        sitemap_requests: int = 0,
        budget: _Budget | None = None,
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        rows = _sitemap_rows(sitemap_text)
        if not rows:
            return [], [
                segment("sitemap", status="failed", items=0, requests=sitemap_requests,
                        error="no /vacancies/ URLs in sitemap")
            ]
        seen, state_path = _load_seen(filters)
        matched = [row for row in rows if row["score"] > 0]
        role_terms = filters.get("primary_role_terms") or ()
        if isinstance(role_terms, (str, bytes)):
            role_terms = [role_terms]
        for row in matched:
            row["role_score"] = _slug_role_score(row["slug"], role_terms)
        unseen = [row for row in matched if row["native_id"] not in seen]
        if role_terms:
            # The live sitemap contains no <lastmod> fields. Preserve its order
            # only as a stable traversal order after role-specific priority;
            # neither sitemap position nor vacancy ID is a freshness claim.
            unseen.sort(key=lambda row: (
                -int(row["role_score"]), -int(row["score"]), int(row["sitemap_position"]),
            ))
        else:
            unseen.sort(key=lambda row: (
                -int(row["score"]), int(row["sitemap_position"]),
            ))
        fixtures = _detail_fixture_map(filters)
        known_descriptions = filters.get("known_descriptions") or {}
        limit = _bounded_int(filters, ("details_limit", "max_details"), 20, 500, 0)
        if offline:
            candidates = [
                (row, fixtures[row["native_id"]], None) for row in unseen
                if row["native_id"] in fixtures
            ]
        else:
            candidates = [
                (row, None, known_descriptions.get(row["native_id"])) for row in unseen
            ]
        items: list[dict[str, Any]] = []
        archived = 0
        invalid = 0
        cached = 0
        processed = 0
        errors: list[str] = []
        blocked = False
        truncated = False
        truncation_reason = ""
        new_seen: set[str] = set()
        archived_seen: set[str] = set()
        seen_urls: set[str] = set()
        requests_used = 0
        budget = budget if budget is not None else _Budget(_request_budget(filters))
        for row, path, cached_item in candidates:
            if processed >= limit:
                truncated = True
                truncation_reason = "details limit reached"
                break
            if path is not None:
                html = path.read_text(encoding="utf-8")
            elif (isinstance(cached_item, Mapping)
                  and str(cached_item.get("description") or "").strip()):
                item = dict(cached_item)
                item.update({
                    "id": row["native_id"], "native_id": row["native_id"],
                    "source": self.source, "url": row["url"],
                    "application_url": row["url"], "slug": row["slug"],
                })
                items.append(self.normalize(item))
                processed += 1
                cached += 1
                new_seen.add(row["native_id"])
                seen_urls.add(row["url"])
                continue
            else:
                if not budget.take():
                    truncated = True
                    truncation_reason = "request budget reached"
                    break
                html, fetch_status, error, used = _fetch(filters, row["url"], _timeout(filters))
                requests_used += used
                if html is None:
                    errors.append(f"{row['url']}: {error}")
                    blocked = blocked or fetch_status == "blocked"
                    continue
            soup = BeautifulSoup(html, "html.parser")
            if _is_archived(soup):
                archived += 1
                processed += 1
                new_seen.add(row["native_id"])
                archived_seen.add(row["native_id"])
                seen_urls.add(row["url"])
                continue
            item = self._parse_detail(html, row["url"], row["native_id"])
            if item is None:
                invalid += 1
                processed += 1
                continue
            if item["native_id"] in {existing["native_id"] for existing in items}:
                processed += 1
                continue
            items.append(item)
            processed += 1
            new_seen.add(row["native_id"])
            seen_urls.add(row["url"])
        # Incremental sitemap scan: never erase IDs covered in earlier runs.
        # Otherwise a small details_limit loops over the same pages forever.
        _persist_seen(
            state_path, seen | new_seen, seen_urls, filters,
            refreshed_ids=new_seen - archived_seen, archived_ids=archived_seen,
        )

        coverage = (
            f"sitemap_urls={len(rows)} matched={len(matched)} unseen={len(unseen)} "
            f"fixtures={len(fixtures)} cached={cached} archived={archived} invalid={invalid}"
        )
        if errors:
            coverage = f"{coverage} errors={len(errors)}"
        sitemap_status = "ok"
        if not matched:
            sitemap_status = "ok"
        detail_status = "ok"
        if items:
            detail_status = "partial" if errors or truncated else "ok"
        elif blocked:
            detail_status = "blocked"
        elif errors:
            detail_status = "failed"
        elif not candidates:
            if limit == 0:
                detail_status = "truncated"
            elif not unseen:
                detail_status = "ok"
            elif offline:
                detail_status = "failed"
            else:
                detail_status = "ok"
        elif truncated:
            detail_status = "truncated"
        detail_error = coverage
        if errors:
            detail_error = f"{detail_error}; " + "; ".join(errors[:3])
        if truncated:
            detail_error = f"{detail_error}; {truncation_reason or 'scan truncated'}"
        if limit == 0:
            detail_error = f"{detail_error}; details limit is zero"
        elif offline and not items and unseen:
            detail_error = f"{detail_error}; no saved detail fixtures for candidates"
        return items, [
            segment("sitemap", status=sitemap_status, items=len(rows),
                    requests=sitemap_requests, error=coverage),
            segment("details", status=detail_status, items=len(items),
                    requests=requests_used, error=detail_error),
        ]

    def _discover_live_sitemap(
        self, filters: Mapping[str, Any], *, budget: _Budget | None = None
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        budget = budget if budget is not None else _Budget(_request_budget(filters))
        if not budget.take():
            return [], [segment("sitemap", status="truncated", items=0, requests=0,
                                error="request budget reached")]
        url = str(filters.get("sitemap_url") or _SITEMAP_URL)
        text, status, error, used = _fetch(filters, url, _timeout(filters))
        if text is None:
            return [], [segment("sitemap", status=status, items=0, requests=used, error=error)]
        return self._discover_sitemap(
            text, filters, offline=False, sitemap_requests=used, budget=budget
        )

    def _discover_urls(
        self, urls: list[str], filters: Mapping[str, Any], *, budget: _Budget | None = None
    ) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        """Incrementally fetch linked jobs, preserving successful bounded coverage."""
        limit = _bounded_int(filters, ("details_limit", "max_details"), 20, 500, 0)
        budget = budget if budget is not None else _Budget(_request_budget(filters))
        seen, state_path = _load_seen(filters)
        selected: list[str] = []
        skipped_seen = 0
        ids_this_pass: set[str] = set()
        for url in urls:
            native_id, _ = _native_and_slug(url)
            if not native_id or native_id in ids_this_pass:
                continue
            ids_this_pass.add(native_id)
            if native_id in seen:
                skipped_seen += 1
                continue
            selected.append(url)
        items: list[dict[str, Any]] = []
        new_seen: set[str] = set()
        archived_seen: set[str] = set()
        seen_urls: set[str] = set()
        errors: list[str] = []
        blocked = False
        archived = 0
        requests_used = 0
        truncated = False
        for url in selected[:limit]:
            if not budget.take():
                truncated = True
                break
            html, status, error, used = _fetch(filters, url, _timeout(filters))
            requests_used += used
            if html is None:
                errors.append(f"{url}: {error}")
                blocked = blocked or status == "blocked"
                continue
            native_id, _ = _native_and_slug(url)
            if _is_archived(BeautifulSoup(html, "html.parser")):
                archived += 1
                new_seen.add(native_id)
                archived_seen.add(native_id)
                seen_urls.add(url)
                continue
            item = self._parse_detail(html, url)
            if item is not None:
                if item["native_id"] not in {value["native_id"] for value in items}:
                    items.append(item)
                new_seen.add(native_id)
                seen_urls.add(url)
        if state_path is not None:
            _persist_seen(
                state_path, seen | new_seen, seen_urls, filters,
                refreshed_ids=new_seen - archived_seen, archived_ids=archived_seen,
            )
        if items:
            status = "partial" if errors or truncated else "ok"
        elif blocked:
            status = "blocked"
        elif errors:
            status = "failed"
        elif not selected or archived:
            status = "ok"
        else:
            status = "truncated" if truncated else "failed"
        diagnostics = (
            f"linked_urls={len(urls)} unique={len(ids_this_pass)} "
            f"previously_seen={skipped_seen} remaining={len(selected)} "
            f"archived={archived} fetched={requests_used}"
        )
        if errors:
            diagnostics += "; " + "; ".join(errors[:3])
        if truncated:
            diagnostics += "; request budget reached"
        if not items and not errors and not archived and selected and not truncated:
            diagnostics += "; no parseable active vacancy"
        return items, [segment(
            "detail_urls", status=status, items=len(items),
            requests=requests_used, error=diagnostics,
        )]

    def inspect(
        self, native_id: str, filters: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        filters = dict(filters or {})
        path = _detail_fixture_map(filters).get(str(native_id))
        if path is not None:
            html = path.read_text(encoding="utf-8")
            return self._parse_detail(html, _fixture_url(path), str(native_id)) or {}
        url = self._url(str(native_id))
        html, _status, _error, _used = _fetch(filters, url, _timeout(filters))
        if html is None:
            return {}
        return self._parse_detail(html, url, str(native_id)) or {}
