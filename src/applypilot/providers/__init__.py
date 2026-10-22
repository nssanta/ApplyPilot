"""Read-only vacancy source providers for ApplyPilot Multi-Source."""

from .base import (
    ProviderScan,
    SourceCapabilities,
    canonical_url,
    job_group_key,
    make_item,
    parse_salary,
    remote_info,
)
from .registry import ProviderRegistry, default_registry

__all__ = [
    "ProviderRegistry",
    "ProviderScan",
    "SourceCapabilities",
    "canonical_url",
    "default_registry",
    "job_group_key",
    "make_item",
    "parse_salary",
    "remote_info",
]
