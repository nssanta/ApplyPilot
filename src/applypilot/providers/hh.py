"""HH provider facade preserving the existing, verified HH scanner and runner."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..parser import ScanSegment
from ..sources import HhSearchAdapter
from .base import SourceCapabilities


class HhProvider:
    source = "hh.ru"
    label = "HH.ru"
    base_url = "https://hh.ru"
    login_url = "https://hh.ru/account/login"
    capabilities = SourceCapabilities(
        discover=True, inspect=True, login=True, check=True, apply=True,
        sync=True, inbox=True, apply_status="legacy_runner",
    )

    def __init__(self) -> None:
        self._adapter = HhSearchAdapter()

    def discover(self, filters: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
        return self._adapter.discover(filters)

    def normalize(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        return self._adapter.normalize(raw)
