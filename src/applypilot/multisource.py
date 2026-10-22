"""Multi-source discovery, scoring, persistence and safe queue planning.

The module deliberately keeps source discovery read-only.  It writes normalized
observations and review scores to the central SQLite database, but it never
submits an application.  Non-HH application forms remain ``needs_live_validation``
until a real authenticated UI has been inspected.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .campaigns import _review_problems
from .config import AppConfig, search_groups
from .parser import ScanSegment
from .providers import ProviderRegistry, default_registry
from .providers.base import SourceCapabilities
from .scoring import evaluate_search_filter, score_vacancy
from .storage import Store

SOURCE_LABELS = {
    "hh.ru": "HH.ru",
    "career.habr.com": "Habr Career",
    "getmatch.ru": "Getmatch",
    "geekjob.ru": "GeekJob",
    "yandex.ru/jobs": "Карьера Яндекса",
    "dreamjob.ru": "DreamJob (HH mirror)",
}
DEFAULT_SOURCES = list(SOURCE_LABELS)


def source_slug(source: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", source.casefold()).strip("-") or "source"


def resolve_source_account(config: AppConfig, profile: Mapping[str, Any], source: str) -> str:
    """Resolves an explicit per-source account, falling back to the track account.

    The private file is intentionally small and contains labels, never cookies:
    ``[accounts] "career.habr.com" = "habr-primary"``.  DreamJob normally shares
    the HH identity because it is a mirror, not a second application channel.
    """
    paths = (
        config.root / "private/config/sources.toml",
        config.root / "config/sources.toml",
        config.root / "sources.toml",
    )
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        accounts = data.get("accounts", data)
        if isinstance(accounts, dict) and source in accounts:
            value = str(accounts[source] or "").strip()
            if value:
                return value
    default = str(profile.get("account") or "default").strip() or "default"
    return default


def review_lane(filter_status: str, score_decision: str, *, applied: bool = False,
                description_status: str = "") -> str:
    """Separates a strong-but-unverified vacancy from a true rejection.

    ``GOOD_UNKNOWN`` is the important lane: the role/stack score passed, but a
    non-ranking condition (salary, publication date, remote proof) is missing.
    It remains reviewable and is never counted as a confirmed FIT.
    """
    if applied:
        return "APPLIED"
    if "reject" in {str(filter_status), str(score_decision)}:
        return "SKIP"
    if filter_status == "pass" and score_decision == "pass":
        return "FIT"
    if score_decision == "pass" and filter_status == "unknown":
        return "GOOD_UNKNOWN"
    if score_decision == "maybe" and filter_status in {"pass", "unknown"}:
        return "NEEDS_ENRICHMENT"
    if description_status in {"missing", "provisional"}:
        return "NEEDS_ENRICHMENT"
    return "MANUAL_REVIEW"


def lane_reasons(item: Mapping[str, Any], lane: str) -> list[str]:
    reasons = [str(value) for value in item.get("filter_reasons", []) if str(value).strip()]
    problems = [str(value) for value in item.get("problems", []) if str(value).strip()]
    if lane == "FIT":
        return ["role_and_conditions_verified"]
    if lane == "GOOD_UNKNOWN":
        return list(dict.fromkeys(reasons + problems + ["manual_condition_check"]))
    if lane == "NEEDS_ENRICHMENT":
        return list(dict.fromkeys(reasons + problems + ["needs_description"]))
    if lane == "SKIP":
        return list(dict.fromkeys(reasons + problems + ["rejected"]))
    return list(dict.fromkeys(reasons + problems + ["manual_review"]))


def _fixture_defaults(source: str) -> tuple[str, str]:
    return {
        "career.habr.com": ("habr_list.html", "habr_detail.html"),
        "geekjob.ru": ("geekjob_list.html", "geekjob_detail.html"),
        "getmatch.ru": ("getmatch_active_llm.html", ""),
        "yandex.ru/jobs": ("yandex_genai_remote_list.html", ""),
        "dreamjob.ru": ("dreamjob_hh_mirror.html", ""),
    }.get(source, ("", ""))


def _provider_filters(source: str, *, queries: list[str], pages: int,
                      request_budget: int, details_limit: int,
                      fixture_dir: str | Path | None = None,
                      fixture_path: str | Path | None = None,
                      detail_fixture_path: str | Path | None = None,
                      remote: bool | None = None, date_from: str = "") -> dict[str, Any]:
    filters: dict[str, Any] = {
        "queries": queries,
        "pages": max(1, int(pages)),
        "request_budget": max(1, int(request_budget)),
        "details_limit": max(0, int(details_limit)),
        "remote": bool(remote) if remote is not None else False,
        "date_from": date_from,
        "pause_seconds": 1.0,
    }
    if fixture_dir:
        root = Path(fixture_dir)
        filters["fixture_dir"] = str(root)
        list_name, detail_name = _fixture_defaults(source)
        if fixture_path:
            filters["fixture_path"] = str(fixture_path)
        elif list_name and (root / list_name).is_file():
            filters["fixture_path"] = str(root / list_name)
        if detail_fixture_path:
            filters["detail_fixture_path"] = str(detail_fixture_path)
        elif detail_name and (root / detail_name).is_file():
            filters["detail_fixture_path"] = str(root / detail_name)
        # Getmatch discovers through the public sitemap, not through a
        # server-rendered catalog.  A saved sitemap must win over the
        # single-detail fallback.
        sitemap_fixture = root / "getmatch_sitemap_sample.xml"
        if source == "getmatch.ru" and sitemap_fixture.is_file():
            filters["sitemap_fixture_path"] = str(sitemap_fixture)
    elif fixture_path:
        filters["fixture_path"] = str(fixture_path)
        if detail_fixture_path:
            filters["detail_fixture_path"] = str(detail_fixture_path)
    return filters


def _segment_status(segments: Iterable[ScanSegment], item_count: int) -> str:
    statuses = {str(segment.status).casefold() for segment in segments}
    if "blocked" in statuses or "captcha" in statuses:
        return "blocked"
    if "failed" in statuses:
        return "partial" if item_count else "failed"
    if "partial" in statuses:
        return "partial"
    if "truncated" in statuses:
        return "truncated"
    return "ok" if item_count else "empty_verified"


def _stamp_source_first_seen(items: list[dict[str, Any]], path: Path) -> None:
    """First-seen namespace is per source; IDs from different sites cannot collide."""
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(registry, dict):
            registry = {}
    except (OSError, json.JSONDecodeError):
        registry = {}
    today = datetime.now(UTC).date().isoformat()
    changed = False
    for item in items:
        native_id = str(item.get("id") or item.get("native_id") or "")
        if not native_id:
            continue
        first_seen = str(registry.get(native_id) or "")
        if not first_seen:
            first_seen = today
            registry[native_id] = first_seen
            changed = True
        item["first_seen"] = first_seen
        item["is_new"] = first_seen == today
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(registry, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _save_source_snapshot(config: AppConfig, source: str, campaign_id: str, items: list[dict[str, Any]],
                          segments: list[ScanSegment], status: str, errors: list[str],
                          metadata: Mapping[str, Any], *, account: str = "default",
                          track: str = "default") -> Path:
    directory = config.snapshots_dir / "providers" / source_slug(source) / source_slug(account) / source_slug(track)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{timestamp}-{campaign_id[:8]}.json"
    payload = {
        "schema_version": 4,
        "source": source,
        "status": status,
        "items": items,
        "segments": [
            {"query": s.query, "area": s.area, "pages": s.pages, "status": s.status,
             "items": s.items, "error": s.error, "requests": s.requests}
            for s in segments
        ],
        "errors": errors,
        "metadata": dict(metadata),
    }
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temp, path)
    if status in {"ok", "empty_verified"}:
        pointer = directory / "last_successful.json"
        pointer_temp = directory / ".last_successful.tmp"
        pointer_temp.write_text(path.name, encoding="utf-8")
        os.replace(pointer_temp, pointer)
    return path


def _prepare_scored_items(items: list[dict[str, Any]], *, source: str, profile: dict[str, Any],
                          search: dict[str, Any], cutoff: datetime, track_resume: str) -> list[dict[str, Any]]:
    groups = search_groups(search)
    group = groups[0] if groups else dict(search)
    result: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        native_id = str(item.get("id") or item.get("native_id") or "").strip()
        if not native_id:
            continue
        item["id"] = native_id
        item["source"] = source
        if item.get("archived") or str(item.get("source_status") or "").casefold() in {
            "archived", "removed", "closed",
        }:
            continue
        scoring_profile = {**profile, "search": search, "rescore": True}
        candidate = score_vacancy(item, scoring_profile)
        item.update(candidate.to_dict())
        item["source"] = source
        item["fit_score"] = candidate.score
        item["score_decision"] = candidate.decision
        item["score"] = candidate.score
        item["resume"] = track_resume or candidate.resume
        filter_result = evaluate_search_filter(item, group)
        filter_status = filter_result.status
        filter_reasons = list(filter_result.reasons)
        salary_cfg = group.get("salary") or {}
        try:
            salary_threshold = int(salary_cfg.get("from", 0) or 0)
        except (TypeError, ValueError):
            salary_threshold = 0
        if (filter_status == "pass" and salary_threshold > 0 and not item.get("salary")
                and str(salary_cfg.get("missing", "include")) == "include"):
            filter_status = "unknown"
            filter_reasons.append("salary:unpublished_not_verified")
        published = str(item.get("published") or "").strip()
        if published:
            try:
                published_at = datetime.fromisoformat(published)
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=UTC)
                if published_at.astimezone(UTC) < cutoff:
                    filter_status = "reject"
                    filter_reasons.append("publication:outside_requested_window")
            except ValueError:
                filter_status = "unknown" if filter_status == "pass" else filter_status
                filter_reasons.append("publication:time_not_verified")
        else:
            if filter_status == "pass":
                filter_status = "unknown"
            filter_reasons.append("publication:time_not_verified")
        item["filter_status"] = filter_status
        item["filter_reasons"] = list(dict.fromkeys(filter_reasons))
        item["filter_fields"] = list(filter_result.fields)
        item["published_window_status"] = "unknown" if not published else (
            "outside_window" if filter_status == "reject" and "publication:outside_requested_window" in filter_reasons
            else "within_window"
        )
        item["problems"] = _review_problems(item, group)
        item["salary_confidence"] = next(
            (value.split(":", 1)[1] for value in item["problems"] if value.startswith("salary:")),
            "unknown",
        )
        lane = review_lane(
            str(item.get("filter_status") or "unknown"),
            str(item.get("score_decision") or "unknown"),
            applied=str(item.get("application_state") or "") in {
                "APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN",
            },
            description_status=str(item.get("description_status") or ""),
        )
        item["review_lane"] = lane
        item["lane_reasons"] = lane_reasons(item, lane)
        result.append(item)
    return result


def scan_source(config: AppConfig, track: str, profile: dict[str, Any], search: dict[str, Any],
                source: str, *, pages: int | None = None, request_budget: int | None = None,
                details_limit: int | None = None, limit: int = 100,
                fixture_dir: str | Path | None = None,
                fixture_path: str | Path | None = None,
                detail_fixture_path: str | Path | None = None,
                telegram_hints: bool = False,
                registry: ProviderRegistry | None = None) -> dict[str, Any]:
    """Runs one provider and persists its normalized/scored observations.

    Exceptions are converted into a failed source result so a caller scanning
    several sources can continue with the remaining providers.
    """
    if not 1 <= int(limit) <= 100:
        raise ValueError("limit must be between 1 and 100")
    fixture_mode = bool(fixture_dir or fixture_path or detail_fixture_path)
    if source == "hh.ru":
        if fixture_mode:
            return {
                "source": source, "status": "skipped", "account": resolve_source_account(config, profile, source),
                "track": track, "items": 0, "fixture": True,
                "errors": ["HH requires a live scan; saved fixtures are not a substitute"],
            }
        # HH keeps its verified campaign pipeline: approvals, UNKNOWN state,
        # resume selection and the legacy attempts ledger must not be reimplemented.
        try:
            from .campaigns import run_campaign
            result = run_campaign(
                config, track, profile, search,
                days=max(1, min(365, int(search.get("days", 3) or 3))),
                pages=pages, request_budget=request_budget,
                details_limit=details_limit, limit=limit,
                track_resume=str(profile.get("resume") or ""),
            )
            summary = result.get("summary") or {}
            return {
                "campaign_id": result.get("campaign_id", ""), "source": source,
                "status": result.get("status", "failed"), "account": result.get("account", ""),
                "track": track, "fixture": False,
                "items": int(summary.get("items", 0) or 0),
                "segments": int(result.get("segment_count", 0) or 0),
                "errors": list(result.get("segment_errors") or []),
                "snapshot": result.get("snapshot", ""), "summary": summary,
                "top": result.get("top", []), "score_counts": result.get("score_counts", {}),
                "legacy_hh": True,
            }
        except Exception as exc:  # noqa: BLE001 - one source must not stop the rest
            return {
                "source": source, "status": "failed", "account": resolve_source_account(config, profile, source),
                "track": track, "items": 0, "fixture": False,
                "errors": [f"{type(exc).__name__}: {exc}"],
            }
    registry = registry or default_registry()
    try:
        provider = registry.get(source)
    except KeyError as exc:
        return {"source": source, "status": "failed", "error": str(exc), "items": 0}
    capabilities = getattr(provider, "capabilities", SourceCapabilities())
    if not capabilities.discover:
        return {"source": source, "status": "skipped", "error": "discovery not supported",
                "items": 0}
    groups = search_groups(search)
    group = groups[0] if groups else dict(search)
    queries = [str(value).strip() for value in group.get("queries", []) if str(value).strip()]
    effective_pages = int(pages if pages is not None else group.get("max_pages", 1))
    effective_budget = int(request_budget if request_budget is not None else group.get("request_budget", 100))
    effective_details = int(details_limit if details_limit is not None else group.get("details_limit", 100))
    if not 1 <= effective_pages <= 100:
        raise ValueError("pages must be between 1 and 100")
    if not 1 <= effective_budget <= 10000:
        raise ValueError("request_budget must be between 1 and 10000")
    if not 0 <= effective_details <= 1000:
        raise ValueError("details_limit must be between 0 and 1000")
    started = datetime.now(UTC)
    cutoff = started - timedelta(days=int(group.get("days", 7) or 7))
    campaign_id = uuid.uuid4().hex
    account = resolve_source_account(config, profile, source)
    store = Store(config.db_path)
    safe_config = {
        "track": track, "source": source, "queries": queries,
        "pages": effective_pages, "request_budget": effective_budget,
        "details_limit": effective_details, "limit": int(limit),
        "fixture": bool(fixture_dir or fixture_path),
    }
    store.start_campaign(campaign_id, account, track, safe_config, source=source)
    try:
        filters = _provider_filters(
            source, queries=queries, pages=effective_pages,
            request_budget=effective_budget, details_limit=effective_details,
            fixture_dir=fixture_dir, fixture_path=fixture_path,
            detail_fixture_path=detail_fixture_path,
            remote=group.get("only_remote"), date_from=cutoff.date().isoformat(),
        )
        if source == "getmatch.ru" and not fixture_mode:
            # Namespace incremental sitemap cursor per source and account.
            # Telegram-specific scans use explicit URLs, not the sitemap.
            filters["state_path"] = str(
                config.data_dir / "providers" / source_slug(source)
                / source_slug(account) / "sitemap_seen.json"
            )
        # The user already owns these messages. Extract public job IDs only:
        # no bot commands are interpreted, no callbacks are executed.
        if telegram_hints and source == "getmatch.ru" and not fixture_mode:
            candidate_urls: list[str] = []
            seen_ids: set[str] = set()
            for message in store.telegram_messages(account, 1000):
                for ref in message.get("vacancy_refs", []):
                    native_id = str(ref.get("id") or "").strip()
                    if (ref.get("source") != source or not native_id.isdecimal()
                            or native_id in seen_ids):
                        continue
                    seen_ids.add(native_id)
                    candidate_urls.append(f"https://getmatch.ru/vacancies/{native_id}")
            if candidate_urls:
                filters["detail_urls"] = candidate_urls
        discovered = provider.discover(filters)
        if hasattr(discovered, "items"):
            raw_items = list(discovered.items)
            segments = list(discovered.segments)
            provider_error = str(getattr(discovered, "error", "") or "")
        else:
            raw_items, segments = discovered
            provider_error = ""
        normalized: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            try:
                item = provider.normalize(raw) if hasattr(provider, "normalize") else dict(raw)
            except Exception:  # noqa: BLE001 - a bad card must not erase the page result
                item = dict(raw)
            item["source"] = source
            normalized.append(item)
        items = _prepare_scored_items(
            normalized, source=source, profile=profile, search=search,
            cutoff=cutoff, track_resume=str(profile.get("resume") or ""),
        )
        _stamp_source_first_seen(
            items, config.data_dir / "seen" / source_slug(source) / f"{source_slug(account)}.json",
        )
        errors = [f"{s.query}: {s.error}" for s in segments if s.error]
        if provider_error:
            errors.append(provider_error)
        status = _segment_status(segments, len(items))
        if provider_error and status == "empty_verified":
            status = "failed"
        snapshot = _save_source_snapshot(
            config, source, campaign_id, items, segments, status, errors,
            {"campaign_id": campaign_id, "track": track, "source": source,
             "fixture": bool(fixture_dir or fixture_path), "observed_at": started.isoformat()},
            account=account, track=track,
        )
        summary = store.save_campaign_items(
            campaign_id, account, track, items, status=status,
            snapshot_path=str(snapshot), error="\n".join(errors), error_count=len(errors),
            source=source,
        )
        telegram_linked = 0
        if telegram_hints and source == "getmatch.ru" and not fixture_mode:
            # Re-attach the saved bot message metadata after scanner enrichment.
            from .telegram_import import import_telegram_vacancies

            imported = import_telegram_vacancies(
                store, account=account, ledger_account=account,
                track=track, limit=1000, create_missing=False,
            )
            telegram_linked = imported["linked_count"]
        top = store.list_campaign_vacancies(account, track, "top", min(10, max(1, limit)), source=source)
        return {
            "campaign_id": campaign_id, "source": source, "status": status,
            "account": account, "track": track, "fixture": bool(fixture_dir or fixture_path),
            "items": len(items), "segments": len(segments), "errors": errors,
            "snapshot": str(snapshot), "summary": summary, "top": top,
            "telegram_links": telegram_linked,
        }
    except Exception as exc:  # noqa: BLE001 - source failures are isolated by contract
        message = f"{type(exc).__name__}: {exc}"
        store.fail_campaign(campaign_id, message)
        return {"campaign_id": campaign_id, "source": source, "status": "failed",
                "account": account, "track": track, "items": 0, "errors": [message],
                "fixture": bool(fixture_dir or fixture_path)}


def scan_sources(config: AppConfig, track: str, profile: dict[str, Any], search: dict[str, Any],
                 sources: Iterable[str] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Scans selected sources independently and returns one aggregate receipt."""
    registry = kwargs.pop("registry", None) or default_registry()
    selected = list(dict.fromkeys(str(source) for source in (sources or DEFAULT_SOURCES)))
    results = []
    for source in selected:
        results.append(scan_source(config, track, profile, search, source, registry=registry, **kwargs))
    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "failed")
        counts[status] = counts.get(status, 0) + 1
    return {
        "track": track,
        "account": str(profile.get("account") or "default"),
        "accounts": {row["source"]: row.get("account", "") for row in results},
        "sources": results,
        "counts": counts,
        "ok": sum(int(result.get("items") or 0) for result in results),
        "failed_sources": [r["source"] for r in results if r.get("status") in {"failed", "blocked"}],
    }


def queue_plan(store: Store, account: str, track: str, source: str, limit: int = 100,
               registry: ProviderRegistry | None = None,
               *, verified_driver: bool = False) -> dict[str, Any]:
    """Builds a dry-run plan; it never reserves, writes an event or clicks submit."""
    registry = registry or default_registry()
    # A source plan is read-only and must include every human-approved row,
    # including native forms that have no HH cover-letter workflow state yet.
    # Fetch beyond the requested batch size before filtering. Otherwise an
    # already-applied high-score row can consume the 100-row window and hide a
    # lower-score READY_TO_APPLY vacancy that should be sent this run.
    eligible = [
        row for row in store.list_campaign_vacancies(
            account, track, "approved", 5000, source=source,
        )
        if not row.get("applied_before")
        and row.get("filter_status") in {"pass", "unknown"}
        and row.get("score_decision") == "pass"
    ]
    # Keep scoring order *within* each group, but inspect ready work before
    # unfinished forms/letters when the batch limit is reached.
    eligible.sort(key=lambda row: row.get("application_state") != "READY_TO_APPLY")
    rows = eligible[:min(100, max(1, int(limit)))]
    planned: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    try:
        capabilities = getattr(registry.get(source), "capabilities", SourceCapabilities())
    except KeyError:
        capabilities = SourceCapabilities(discover=False, apply=False, apply_status="unavailable")
    for row in rows:
        vacancy_id = str(row.get("id") or "")
        channel_key = row.get("channel_key") or f"{source}:{vacancy_id}"
        item = {
            "id": vacancy_id, "source": source, "track": track,
            "name": row.get("name", ""), "company": row.get("company", ""),
            "url": row.get("url", ""), "score": row.get("score", 0),
            "channel_key": channel_key,
            "application_state": row.get("application_state", ""),
            "application_url": row.get("application_url", ""),
            "channel_type": row.get("channel_type", ""),
            "review_state": row.get("review_state", ""),
            "score_decision": row.get("score_decision", ""),
            "filter_status": row.get("filter_status", ""),
        }
        # Re-read the live ledger at planning time.  The duplicate flag stored
        # during the scan can be stale after an HH sync or a manual decision.
        conflict = store.channel_conflict(
            account, source, vacancy_id, channel_key,
            mirror_source=str(row.get("mirror_source") or ""),
            mirror_native_id=str(row.get("mirror_native_id") or ""),
        )
        reason = ""
        if conflict:
            if conflict.get("kind") == "same_run":
                reason = f"reservation_exists:{conflict.get('run_id', '')}"
            elif conflict.get("kind") == "reserved":
                reason = (f"channel_reserved:{conflict.get('source')}:"
                          f"{conflict.get('vacancy_id')}")
            elif conflict.get("kind") == "applied_or_quarantined":
                reason = (f"channel_duplicate:{conflict.get('source')}:"
                          f"{conflict.get('vacancy_id')}")
            else:
                reason = f"channel_conflict:{conflict.get('kind')}"
        elif row.get("application_state") in {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN"}:
            reason = f"application_state:{row.get('application_state')}"
        elif row.get("application_state") in {"NEEDS_FORM", "NEEDS_LETTER", "NEEDS_MANUAL"}:
            reason = f"needs_workflow:{row.get('application_state')}"
        elif source == "hh.ru" and row.get("application_state") != "READY_TO_APPLY":
            # Approval is not proof that the HH form, letter and resume are ready.
            reason = f"not_ready:{row.get('application_state')}"
        elif source != "hh.ru" and not capabilities.apply and not verified_driver:
            reason = f"needs_live_validation:{capabilities.apply_status}"
        elif row.get("application_channel_type") == "external_ats":
            reason = "external_ats_manual_review"
        elif not capabilities.apply and source != "hh.ru" and not verified_driver:
            reason = "apply_not_available"
        if reason:
            blocked.append({**item, "action": "blocked", "reason": reason})
        else:
            planned.append({**item, "action": "would_apply", "reason": "dry_run"})
    return {
        "account": account, "track": track, "source": source,
        "mode": "dry_run", "candidate_count": len(rows),
        "planned_count": len(planned), "blocked_count": len(blocked),
        "planned": planned, "blocked": blocked,
        "apply_status": ("verified_driver" if verified_driver else capabilities.apply_status),
    }


def source_status(config: AppConfig, track: str, account: str | None = None,
                  registry: ProviderRegistry | None = None) -> dict[str, Any]:
    registry = registry or default_registry()
    profile = config.load_profile()
    store = Store(config.db_path)
    rows = []
    for entry in registry.catalogue():
        source = entry["source"]
        source_account = account or resolve_source_account(config, profile, source)
        queue = store.list_campaign_vacancies(source_account, track, "all", 5000, source=source)
        approved = sum(1 for row in queue if row.get("review_state") == "APPROVED")
        reviewable = sum(1 for row in queue if row.get("review_lane") in {
            "FIT", "GOOD_UNKNOWN", "NEEDS_ENRICHMENT", "MANUAL_REVIEW",
        })
        rows.append({
            **entry, "account": source_account, "queue": len(queue),
            "approved": approved, "reviewable": reviewable,
        })
    return {"account": account or str(profile.get("account") or "default"),
            "track": track, "sources": rows}
