"""Unified multi-source application dispatcher.

Dry-run is the default and never writes an attempt.  Live mode requires
``allow_live=True`` and an explicit driver for each source.  A source without a
verified driver is skipped with ``needs_live_validation``; it is never replaced
by an HH submission.  Channel reservations are atomic per canonical channel, so
DreamJob mirrors and external ATS destinations cannot receive a second apply.

The module owns orchestration only.  Source-specific browser automation stays in
the provider/clicker layer and HH keeps its verified ``autoapply`` runner.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .multisource import queue_plan
from .providers import ProviderRegistry, default_registry
from .storage import Store

LIVE_STATUS_MAP = {
    "success": "success",
    "already_applied": "already_applied",
    "unknown": "unknown",
    "needs_manual": "needs_manual",
    "failed": "failed_before_submit",
    "failed_before_submit": "failed_before_submit",
    "needs_live_validation": "skipped",
    "skipped": "skipped",
}


@dataclass(frozen=True)
class ApplyOutcome:
    """Result returned by a source driver.

    ``status`` uses the driver vocabulary (success, already_applied, unknown,
    needs_manual, failed, needs_live_validation, skipped); ``receipt`` is stored
    only as evidence in the attempt note, never as a fabricated success.
    """

    status: str
    note: str = ""
    evidence: str = ""
    receipt: Mapping[str, Any] = field(default_factory=dict)


class ApplyDriver(Protocol):
    def preflight(self, row: Mapping[str, Any]) -> ApplyOutcome: ...

    def apply(self, row: Mapping[str, Any]) -> ApplyOutcome: ...


def _capabilities(registry: ProviderRegistry, source: str) -> dict[str, Any]:
    try:
        capabilities = registry.get(source).capabilities
    except KeyError:
        return {"discover": False, "apply": False, "apply_status": "unavailable"}
    if hasattr(capabilities, "as_dict"):
        return dict(capabilities.as_dict())
    return dict(capabilities)


def dispatch_plan(
    store: Store,
    account: str,
    track: str,
    sources: Iterable[str],
    limit: int = 100,
    *,
    accounts: Mapping[str, str] | None = None,
    registry: ProviderRegistry | None = None,
) -> dict[str, Any]:
    """Aggregates per-source dry-run plans without reserving or writing."""
    registry = registry or default_registry()
    account_for = dict(accounts or {})
    selected = list(dict.fromkeys(str(source) for source in sources if str(source).strip()))
    plans: dict[str, dict[str, Any]] = {}
    planned: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for source in selected:
        source_account = str(account_for.get(source, account))
        plan = queue_plan(store, source_account, track, source, limit, registry=registry)
        plan["account"] = source_account
        plans[source] = plan
        for row in plan.get("planned", []):
            planned.append({**row, "source": source, "account": source_account})
        for row in plan.get("blocked", []):
            blocked.append({**row, "source": source, "account": source_account})
    return {
        "mode": "dry_run",
        "account": account,
        "accounts": {source: str(account_for.get(source, account)) for source in selected},
        "track": track,
        "sources": selected,
        "capabilities": {source: _capabilities(registry, source) for source in selected},
        "plans": plans,
        "candidate_count": sum(int(plan.get("candidate_count", 0)) for plan in plans.values()),
        "planned_count": len(planned),
        "blocked_count": len(blocked),
        "planned": planned,
        "blocked": blocked,
    }


def approve_batch(
    store: Store,
    account: str,
    track: str,
    source: str,
    vacancy_ids: Iterable[str],
    *,
    state: str = "APPROVED",
    reason: str = "",
) -> dict[str, Any]:
    """Applies one explicit review decision to a bounded batch of IDs."""
    ids = list(dict.fromkeys(str(value).strip() for value in vacancy_ids if str(value).strip()))
    if not ids:
        raise ValueError("at least one vacancy id is required")
    updated = store.set_review_state_batch(
        account, source, ids, state, reason=reason, track=track,
    )
    return {
        "source": source, "state": state, "requested": len(ids), "updated": int(updated),
        "reason": reason,
    }


def dispatch_apply(
    store: Store,
    account: str,
    track: str,
    sources: Iterable[str],
    limit: int = 100,
    *,
    allow_live: bool = False,
    drivers: Mapping[str, ApplyDriver] | None = None,
    accounts: Mapping[str, str] | None = None,
    registry: ProviderRegistry | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Executes a plan; dry-run unless ``allow_live`` and a driver are present.

    Every attempted source is reserved atomically before the driver is called
    and recorded as ``submitting``.  A driver exception becomes ``unknown``
    quarantine, never a retry.  One source failure does not stop the others.
    """
    plan = dispatch_plan(store, account, track, sources, limit, accounts=accounts, registry=registry)
    if not allow_live:
        return {**plan, "executed": 0, "results": [], "summary": {"dry_run": plan["planned_count"]}}

    run_id = str(run_id or uuid.uuid4().hex[:12])
    drivers = dict(drivers or {})
    results: list[dict[str, Any]] = []
    for row in plan["planned"]:
        source = str(row.get("source") or "")
        source_account = str(row.get("account") or account)
        vacancy_id = str(row.get("id") or "")
        channel_key = str(row.get("channel_key") or f"{source}:{vacancy_id}")
        driver = drivers.get(source)
        if driver is None:
            results.append({
                **row, "action": "skipped", "status": "needs_live_validation",
                "reason": "no verified driver for this source",
            })
            continue
        try:
            preflight = (
                driver.preflight(row) if hasattr(driver, "preflight")
                else ApplyOutcome("ready")
            )
        except Exception as exc:  # noqa: BLE001 - a driver must not stop the batch
            results.append({
                **row, "action": "skipped", "status": "preflight_failed",
                "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
            })
            continue
        if preflight.status != "ready":
            results.append({
                **row, "action": "skipped", "status": preflight.status,
                "reason": preflight.note or preflight.evidence or "preflight blocked",
            })
            continue

        reserved, reservation_note = store.reserve_source_application(
            source_account, source, vacancy_id, track, channel_key, run_id,
        )
        if not reserved:
            results.append({
                **row, "action": "blocked", "status": "channel_conflict",
                "reason": reservation_note,
            })
            continue
        if not store.record_source_application(
            source_account, source, vacancy_id, track, channel_key, "submitting",
            note=preflight.note, run_id=run_id,
        ):
            results.append({
                **row, "action": "blocked", "status": "transition_rejected",
                "reason": "could not record submitting state",
            })
            continue

        try:
            outcome = driver.apply(row)
        except Exception as exc:  # noqa: BLE001 - ambiguous outcome is quarantined
            outcome = ApplyOutcome("unknown", f"{type(exc).__name__}: {str(exc)[:200]}")
        ledger_status = LIVE_STATUS_MAP.get(outcome.status, "unknown")
        note = outcome.note or outcome.evidence
        if outcome.receipt:
            note = f"{note} | receipt={dict(outcome.receipt)}".strip(" |")
        recorded = store.record_source_application(
            source_account, source, vacancy_id, track, channel_key, ledger_status,
            note=note, run_id=run_id, mode="run",
        )
        results.append({
            **row, "action": "applied" if outcome.status == "success" else "attempted",
            "status": outcome.status, "ledger_status": ledger_status,
            "note": outcome.note, "evidence": outcome.evidence,
            "receipt": dict(outcome.receipt), "recorded": bool(recorded),
        })

    summary: dict[str, int] = {}
    for row in results:
        status = str(row.get("status") or "unknown")
        summary[status] = summary.get(status, 0) + 1
    return {
        **plan, "mode": "live", "run_id": run_id,
        "executed": len(results), "results": results, "summary": summary,
    }


class HHPageDriver:
    """Adapter around the verified HH Playwright ``apply_one`` runner.

    The caller owns the page/browser lifecycle.  This driver never invents a
    selector or a success receipt; it forwards the runner's classified result.
    """

    def __init__(self, page_factory: Any, resume: str, cover_letter: str = "",
                 confirmation_timeout_seconds: float = 15.0) -> None:
        self.page_factory = page_factory
        self.resume = str(resume or "")
        self.cover_letter = str(cover_letter or "")
        self.confirmation_timeout_seconds = float(confirmation_timeout_seconds)

    def preflight(self, row: Mapping[str, Any]) -> ApplyOutcome:
        from .autoapply import allowed_hh_url

        if not allowed_hh_url(str(row.get("url") or "")):
            return ApplyOutcome("needs_manual", "URL is not an allowed HH hostname")
        if not self.resume.strip():
            return ApplyOutcome("needs_manual", "resume is not selected")
        return ApplyOutcome("ready")

    def apply(self, row: Mapping[str, Any]) -> ApplyOutcome:
        from .autoapply import apply_one

        page = self.page_factory()
        result = apply_one(
            page, dict(row), self.resume, self.cover_letter,
            confirmation_timeout_seconds=self.confirmation_timeout_seconds,
        )
        return ApplyOutcome(str(result.status), str(result.note))
