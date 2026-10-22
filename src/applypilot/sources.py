"""Минимальные контракты источников вакансий и адаптер существующего HH scanner."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .parser import ScanSegment, scan_many


@runtime_checkable
class SourceAdapter(Protocol):
    """Обязательная часть адаптера: поиск и приведение данных к общему словарю."""

    source: str

    def discover(self, filters: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[ScanSegment]]: ...

    def normalize(self, raw: Mapping[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class InspectionAdapter(Protocol):
    """Необязательная возможность источника читать страницу вакансии без действий."""

    def inspect(self, source_job_id: str) -> dict[str, Any]: ...


@runtime_checkable
class ApplicationAdapter(Protocol):
    """Необязательная возможность готовить и отправлять отклик с явным approval."""

    def apply(self, application: Mapping[str, Any], approval: Mapping[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class ApplicationSyncAdapter(Protocol):
    def sync_applications(self) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class MessageSyncAdapter(Protocol):
    def sync_messages(self) -> Sequence[Mapping[str, Any]]: ...


class HhSearchAdapter:
    """Тонкий фасад над текущим HH scanner; сеть вызывается только из discover()."""

    source = "hh.ru"

    def discover(self, filters: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        """Сначала первая страница КАЖДОЙ фразы, затем глубина при остатке бюджета."""
        queries = list(filters.get("queries", []))
        areas = list(filters.get("areas", [113]))
        pages = max(1, int(filters.get("pages", 1)))
        remote = bool(filters.get("remote", False))
        date_from = str(filters["date_from"]) if filters.get("date_from") else None
        pause = float(filters.get("pause_seconds", 1.0))
        budget = None if filters.get("request_budget") is None else int(filters["request_budget"])
        order = str(filters.get("order_by", "relevance"))
        base = {
            "only_remote": remote, "date_from": date_from,
            "pause_seconds": pause, "order_by": order,
        }
        items, segments = scan_many(queries, areas, 1, request_budget=budget, **base)
        total_used = sum(segment.requests for segment in segments)
        result = {str(item["id"]): self.normalize(item) for item in items if item.get("id")}
        if pages > 1 and not any(segment.status in {"failed", "captcha"} for segment in segments):
            # HH commonly uses two HTTP requests (redirect) for one search page.
            # Never spend a whole limited budget on just the first two phrases.
            for segment in list(segments):
                if budget is not None and total_used >= budget:
                    break
                if segment.pages != 1 or segment.status != "truncated" or segment.error:
                    continue
                extra, extra_segments = scan_many(
                    [segment.query], [segment.area], pages - 1, start_page=1,
                    request_budget=None if budget is None else budget - total_used, **base,
                )
                total_used += sum(part.requests for part in extra_segments)
                segments.extend(extra_segments)
                for raw in extra:
                    key = str(raw.get("id") or "")
                    if not key:
                        continue
                    if key not in result:
                        result[key] = self.normalize(raw)
                        continue
                    current = result[key]
                    for source in ("query_sources", "area_sources", "sort_sources"):
                        current[source] = sorted(set(current.get(source, [])) | set(raw.get(source, [])))
                if any(part.status in {"failed", "captcha"} for part in extra_segments):
                    break
        return list(result.values()), segments

    def normalize(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(raw)
        vacancy_id = str(item.get("id") or item.get("vacancyId") or item.get("vacancy_id") or "").strip()
        if vacancy_id:
            item["id"] = vacancy_id
        item["source"] = self.source
        if not item.get("url") and vacancy_id:
            item["url"] = f"https://hh.ru/vacancy/{vacancy_id}"
        if "name" not in item and item.get("title") is not None:
            item["name"] = item["title"]
        if not isinstance(item.get("salary"), (dict, type(None))):
            item["salary"] = None
        return item
