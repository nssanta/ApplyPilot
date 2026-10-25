"""Минимальные контракты источников вакансий и адаптер существующего HH scanner."""

from __future__ import annotations

import json
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

    @staticmethod
    def page_key(query: str, area: int, order: str) -> str:
        """Stable cursor key for one HH query, region and sort order."""
        return json.dumps([str(query), int(area), str(order)], ensure_ascii=False,
                          separators=(",", ":"))

    def discover(self, filters: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        """Scan saved page offsets, covering each query before adding depth."""
        queries = list(filters.get("queries", []))
        areas = list(filters.get("areas", [113]))
        pages = max(1, int(filters.get("pages", 1)))
        remote = bool(filters.get("remote", False))
        date_from = str(filters["date_from"]) if filters.get("date_from") else None
        pause = float(filters.get("pause_seconds", 1.0))
        budget = None if filters.get("request_budget") is None else int(filters["request_budget"])
        order = str(filters.get("order_by", "relevance"))
        start_pages = filters.get("start_pages", {}) or {}
        base = {
            "only_remote": remote, "date_from": date_from,
            "pause_seconds": pause, "order_by": order,
        }
        remaining = budget
        unique: dict[str, dict[str, Any]] = {}
        segments: list[ScanSegment] = []
        continuations: list[tuple[str, int, int]] = []

        def collect(rows: list[dict[str, Any]]) -> None:
            for raw in rows:
                vacancy_id = str(raw.get("id") or "")
                if not vacancy_id:
                    continue
                if vacancy_id not in unique:
                    unique[vacancy_id] = self.normalize(raw)
                    continue
                current = unique[vacancy_id]
                for field in ("query_sources", "area_sources", "sort_sources"):
                    current[field] = sorted(set(current.get(field, [])) | set(raw.get(field, [])))

        # Cover the saved page for every query/area before spending budget on
        # deeper pages. Null cursors mean that segment was already completed.
        for query in queries:
            for area in areas:
                key = self.page_key(str(query), int(area), order)
                configured_start = start_pages.get(key, 0)
                if configured_start is None:
                    continue
                start = max(0, int(configured_start))
                rows, parts = scan_many(
                    [query], [area], 1, start_page=start,
                    request_budget=remaining, **base,
                )
                collect(rows)
                segments.extend(parts)
                used = sum(part.requests for part in parts)
                if remaining is not None:
                    remaining = max(0, remaining - used)
                if parts and parts[0].status == "truncated" and not parts[0].error and parts[0].pages:
                    continuations.append((str(query), int(area), start + 1))

        # Continue after first-page coverage. A later invocation can resume at
        # the next page without replaying pages that already yielded results.
        if pages > 1:
            for query, area, next_page in continuations:
                if remaining is not None and remaining <= 0:
                    break
                rows, parts = scan_many(
                    [query], [area], pages - 1, start_page=next_page,
                    request_budget=remaining, **base,
                )
                collect(rows)
                segments.extend(parts)
                used = sum(part.requests for part in parts)
                if remaining is not None:
                    remaining = max(0, remaining - used)
        return list(unique.values()), segments

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
