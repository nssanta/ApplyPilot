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
        items, segments = scan_many(
            filters.get("queries", []), filters.get("areas", [113]),
            int(filters.get("pages", 1)), bool(filters.get("remote", False)),
            date_from=str(filters["date_from"]) if filters.get("date_from") else None,
            pause_seconds=float(filters.get("pause_seconds", 1.0)),
            request_budget=(None if filters.get("request_budget") is None
                            else int(filters["request_budget"])),
            order_by=str(filters.get("order_by", "relevance")),
        )
        return [self.normalize(item) for item in items], segments

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
