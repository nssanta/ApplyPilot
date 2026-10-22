"""Provider registry and capability catalogue."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .base import SourceCapabilities


class ProviderRegistry:
    """Small explicit registry; no dynamic imports from untrusted configuration."""

    def __init__(self, providers: Iterable[Any] = ()) -> None:
        self._providers: dict[str, Any] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: Any) -> None:
        source = str(getattr(provider, "source", "")).strip()
        if not source:
            raise ValueError("provider has no source")
        self._providers[source] = provider

    def get(self, source: str) -> Any:
        try:
            return self._providers[source]
        except KeyError as exc:
            raise KeyError(f"unknown source: {source}") from exc

    def sources(self) -> list[str]:
        return list(self._providers)

    def catalogue(self) -> list[dict[str, Any]]:
        rows = []
        for source, provider in self._providers.items():
            capabilities = getattr(provider, "capabilities", SourceCapabilities())
            rows.append({
                "source": source,
                "label": str(getattr(provider, "label", source)),
                "base_url": str(getattr(provider, "base_url", "")),
                "login_url": str(getattr(provider, "login_url", "")),
                "capabilities": capabilities.as_dict(),
            })
        return rows


def default_registry() -> ProviderRegistry:
    """Builds the built-in registry without importing network code eagerly."""
    from .dreamjob import DreamJobProvider
    from .geekjob import GeekJobProvider
    from .getmatch import GetMatchProvider
    from .habr import HabrProvider
    from .hh import HhProvider
    from .yandex import YandexProvider

    return ProviderRegistry((
        HhProvider(), HabrProvider(), GetMatchProvider(), GeekJobProvider(),
        YandexProvider(), DreamJobProvider(),
    ))
