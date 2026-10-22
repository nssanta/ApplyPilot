"""Shared, read-only contracts and normalization helpers for source providers.

Provider modules only turn public HTML/JSON into ApplyPilot's canonical vacancy
shape.  They never authenticate, click an application button or mutate SQLite.
Browser/application concerns live in the source-aware queue and session layers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from ..parser import ScanSegment

SOURCE_STATUSES = {"ok", "partial", "blocked", "failed", "empty_verified", "truncated"}
APPLICATION_STATUSES = {
    "ready", "needs_live_validation", "legacy_runner", "manual_only", "mirror_only", "unavailable",
}


@dataclass(frozen=True)
class SourceCapabilities:
    """What a source can actually do in this build.

    ``apply`` is deliberately conservative.  A parser fixture proves discovery,
    not an authenticated submit form.
    """

    discover: bool = True
    inspect: bool = False
    login: bool = False
    check: bool = False
    apply: bool = False
    sync: bool = False
    inbox: bool = False
    external: bool = False
    mirror: str = ""
    apply_status: str = "unavailable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "discover": self.discover,
            "inspect": self.inspect,
            "login": self.login,
            "check": self.check,
            "apply": self.apply,
            "sync": self.sync,
            "inbox": self.inbox,
            "external": self.external,
            "mirror": self.mirror,
            "apply_status": self.apply_status,
        }


@dataclass
class ProviderScan:
    """Normalized discovery result plus diagnostics for one provider."""

    source: str
    items: list[dict[str, Any]] = field(default_factory=list)
    segments: list[ScanSegment] = field(default_factory=list)
    status: str = "empty_verified"
    error: str = ""
    fixture: bool = False
    requests: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "items": self.items,
            "status": self.status,
            "error": self.error,
            "fixture": self.fixture,
            "requests": self.requests,
            "segments": [
                {
                    "query": segment.query, "area": segment.area, "pages": segment.pages,
                    "status": segment.status, "items": segment.items, "error": segment.error,
                    "requests": segment.requests, "order_by": segment.order_by,
                }
                for segment in self.segments
            ],
        }


def clean_text(value: Any) -> str:
    """Collapses whitespace without trusting or interpreting page text."""
    return " ".join(str(value or "").replace("\u00a0", " ").replace("\u202f", " ").split())


def canonical_url(base_url: str, href: str) -> str:
    """Resolves a public URL and removes fragments/credentials for stable identity."""
    absolute = urljoin(base_url, clean_text(href))
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    netloc = parsed.netloc.lower()
    if parsed.username or parsed.password:
        return ""
    path = parsed.path or "/"
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def channel_for_url(source: str, native_id: str, url: str, *, external: bool = False,
                    mirror_source: str = "", mirror_id: str = "") -> tuple[str, str, str]:
    """Returns ``(channel_key, channel_type, application_url)``.

    A native form is a distinct channel per source.  An external ATS URL is a
    shared channel even when it is discovered on two boards.  DreamJob aliases
    HH's native channel instead of creating a second HH application.
    """
    if mirror_source and mirror_id:
        return f"{mirror_source}:{mirror_id}", "mirror", f"https://hh.ru/vacancy/{mirror_id}"
    if external and url:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
        key = f"ats:{host}{path}"
        return key, "external_ats", url
    return f"{source}:{native_id}", "native_form", url


def parse_iso_datetime(value: Any) -> str:
    """Returns an ISO timestamp only when the page supplied a real timestamp."""
    text = clean_text(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _number(value: str, multiplier: int = 1) -> int | float | None:
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        return None
    number *= multiplier
    return int(number) if number.is_integer() else number


def parse_salary(value: Any, *, default_currency: str = "RUR") -> dict[str, Any] | None:
    """Parses a *published* salary range, never a site's predicted salary.

    Handles ``от 180 000 до 220 000 ₽``, ``2K — 2.5K $`` and
    ``300–400k RUR net``.  A string containing a prediction disclaimer returns
    ``None``: a forecast is not an advertised salary.
    """
    if isinstance(value, dict):
        return _salary_from_json(value)
    text = clean_text(value)
    if not text:
        return None
    low = text.casefold()
    if any(marker in low for marker in (
        "похожие специалисты", "зарплата не указана", "salary not specified",
        "predict", "примерный уровень", "оценка зарплаты",
    )):
        return None
    currency = ""
    if re.search(r"₽|руб\b|руб\.|rur\b|rub\b", low):
        currency = "RUR"
    elif re.search(r"\$|usd\b|доллар", low):
        currency = "USD"
    elif re.search(r"€|eur\b|евро", low):
        currency = "EUR"
    currency = currency or default_currency.upper()

    # Remove thousands separators only when they sit between digit groups.
    normalized = re.sub(r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?:\D|$))", "", text)
    tokens = re.findall(r"(?<![\d.])(\d+(?:[.,]\d+)?)\s*([kKкК])?", normalized)
    if not tokens:
        return None
    values: list[tuple[float, bool]] = []
    for raw, suffix in tokens[:4]:
        parsed = _number(raw)
        if parsed is None:
            continue
        values.append((float(parsed), bool(suffix)))
    if not values:
        return None
    # "300-400k" applies the suffix to the whole range.
    if any(has_k for _, has_k in values) and max(raw for raw, _ in values) < 1000:
        values = [(raw * 1000, True) for raw, _ in values]
    else:
        values = [(raw * (1000 if has_k else 1), has_k) for raw, has_k in values]
    numbers = [int(raw) if float(raw).is_integer() else raw for raw, _ in values]
    has_from = bool(re.search(r"\bот\b|from", low))
    has_to = bool(re.search(r"\bдо\b|up to", low))
    if len(numbers) == 1:
        lower = numbers[0] if has_from or not has_to else None
        upper = numbers[0] if has_to and not has_from else None
        if lower is None and upper is None:
            lower = numbers[0]
    else:
        lower, upper = numbers[0], numbers[1]
        if lower > upper:
            lower, upper = upper, lower
    gross: bool | None = None
    if re.search(r"\bnet\b|на руки|после вычета", low):
        gross = False
    elif re.search(r"\bgross\b|до вычета", low):
        gross = True
    return {
        "from": lower,
        "to": upper,
        "currency": currency,
        "gross": gross,
        "provenance": "published",
    }


def _salary_from_json(value: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = value.get("value") if isinstance(value.get("value"), dict) else value
    lower = raw.get("minValue") or raw.get("from") or raw.get("minimum")
    upper = raw.get("maxValue") or raw.get("to") or raw.get("maximum")
    currency = str(raw.get("currency") or value.get("currency") or "RUR").upper()
    if not lower and not upper:
        return None
    try:
        lower = int(float(lower)) if lower not in (None, "") else None
        upper = int(float(upper)) if upper not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return {
        "from": lower,
        "to": upper,
        "currency": {"RUB": "RUR", "RUR": "RUR", "USD": "USD", "EUR": "EUR"}.get(currency, currency),
        "gross": raw.get("gross") if isinstance(raw.get("gross"), bool) else None,
        "provenance": "published",
    }


def remote_info(*values: Any) -> dict[str, Any]:
    """Classifies remote eligibility, preserving conflicts instead of guessing.

    A listing saying "remote except Russia" is explicitly ineligible for the
    owner's Russia-based search.  Mixed office/remote listings remain visible,
    but the conflict is recorded for review.
    """
    text = clean_text(" ".join(str(value or "") for value in values))
    low = text.casefold()
    if not low:
        return {"is_remote": False, "remote_confidence": "unknown",
                "remote_reason": "not_specified", "work_format": []}
    ineligible = bool(re.search(
        r"(?:кроме|за исключением|исключая|except|excluding)\s*(?:рф|росси|russia)|"
        r"(?:рф|росси|russia).{0,40}(?:не рассматрива|кроме|except)|"
        r"(?:только|only)\s*(?:ес|eu|европ|europe|сша|usa|великобритан|uk)\b",
        low,
    ))
    remote = bool(re.search(r"удал[её]н|дистанцион|remote|home\s*office|work\s*from\s*home", low))
    hybrid = "гибрид" in low or "hybrid" in low
    office = bool(re.search(r"\bофис\b|\boffice\b|на месте|on-site|onsite", low))
    formats: list[str] = []
    if remote:
        formats.append("remote")
    if hybrid:
        formats.append("hybrid")
    if office:
        formats.append("office")
    if ineligible:
        return {"is_remote": False, "remote_confidence": "ineligible",
                "remote_reason": "remote_except_russia", "work_format": formats}
    if remote or hybrid:
        confidence = "listed_mixed" if office and not hybrid else "listed"
        return {"is_remote": True, "remote_confidence": confidence,
                "remote_reason": "listed", "work_format": formats}
    if office:
        return {"is_remote": False, "remote_confidence": "office",
                "remote_reason": "office_only", "work_format": formats}
    return {"is_remote": False, "remote_confidence": "unknown",
            "remote_reason": "not_specified", "work_format": formats}


def make_item(*, source: str, native_id: str, name: str, company: str, url: str,
              description: str = "", salary: dict[str, Any] | None = None,
              experience: str = "", area: str = "", remote_text: str = "",
              published: str = "", application_url: str = "",
              external: bool = False, mirror_source: str = "", mirror_id: str = "",
              source_status: str = "ok", description_status: str = "ok",
              extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Builds the canonical item consumed by scoring, snapshots and SQLite."""
    source = str(source).strip()
    native_id = str(native_id).strip()
    channel, channel_type, channel_url = channel_for_url(
        source, native_id, application_url or url, external=external,
        mirror_source=mirror_source, mirror_id=mirror_id,
    )
    remote = remote_info(remote_text or " ".join((area, description[:600])))
    item: dict[str, Any] = {
        "id": native_id,
        "native_id": native_id,
        "source": source,
        "name": clean_text(name),
        "company": clean_text(company),
        "url": url,
        "description": clean_text(description),
        "description_status": description_status,
        "salary": salary,
        "salary_provenance": "published" if salary else "unknown",
        "experience": clean_text(experience),
        "area": clean_text(area),
        "is_remote": bool(remote["is_remote"]),
        "remote_confidence": remote["remote_confidence"],
        "remote_reason": remote["remote_reason"],
        "work_format": remote["work_format"],
        "published": published if parse_iso_datetime(published) else "",
        "published_verified": bool(parse_iso_datetime(published)),
        "application_channel": channel,
        "application_channel_type": channel_type,
        "application_url": channel_url,
        "mirror_source": mirror_source,
        "mirror_native_id": mirror_id,
        "source_status": source_status,
        "job_group_key": job_group_key(company, name),
    }
    if extra:
        item.update(dict(extra))
    return item


def job_group_key(company: str, title: str) -> str:
    """Stable cross-source *review* grouping; never an application channel."""
    normalized = f"{clean_text(company).casefold()}|{clean_text(title).casefold()}"
    normalized = re.sub(r"[^0-9a-zа-яё]+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24] if normalized != "|" else ""


def json_ld(soup: Any) -> list[dict[str, Any]]:
    """Returns all JSON-LD objects from a BeautifulSoup document."""
    result: list[dict[str, Any]] = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or node.get_text() or "")
        except (TypeError, json.JSONDecodeError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if isinstance(value, dict):
                result.append(value)
                graph = value.get("@graph")
                if isinstance(graph, list):
                    result.extend(part for part in graph if isinstance(part, dict))
    return result


def fixture_path(filters: Mapping[str, Any], *names: str) -> Path | None:
    """Resolves a fixture without ever requiring tests to touch the network."""
    direct = filters.get("fixture_path") or filters.get("fixture")
    if direct:
        path = Path(str(direct))
        return path if path.is_file() else None
    directory = filters.get("fixture_dir")
    if directory:
        root = Path(str(directory))
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return candidate
    return None


def fixture_html(filters: Mapping[str, Any], *names: str) -> tuple[str, str, bool]:
    """Returns ``(html, base_url, is_fixture)`` for a fixture or empty values."""
    path = fixture_path(filters, *names)
    if path is None:
        return "", "", False
    return path.read_text(encoding="utf-8"), str(filters.get("fixture_url") or ""), True


def segment(query: str, *, status: str, items: int = 0, error: str = "",
            requests: int = 0, pages: int = 1, area: int = 0,
            order_by: str = "relevance") -> ScanSegment:
    return ScanSegment(query, area, pages, status, items, error, requests, order_by)


def status_from_segments(segments: list[ScanSegment], items: int) -> str:
    statuses = {segment.status for segment in segments}
    if "blocked" in statuses or "captcha" in statuses:
        return "blocked"
    if "failed" in statuses:
        return "failed" if not items else "partial"
    if "partial" in statuses:
        return "partial"
    if "truncated" in statuses:
        return "truncated"
    return "ok" if items else "empty_verified"
