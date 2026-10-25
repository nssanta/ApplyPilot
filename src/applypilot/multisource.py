"""Multi-source discovery, scoring, persistence and safe queue planning.

The module deliberately keeps source discovery read-only.  It writes normalized
observations and review scores to the central SQLite database, but it never
submits an application.  Non-HH application forms remain ``needs_live_validation``
until a real authenticated UI has been inspected.
"""

from __future__ import annotations

import hashlib
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

import requests

from .campaigns import _review_problems, _salary_confidence
from .config import AppConfig, search_groups
from .parser import ScanSegment
from .providers import ProviderRegistry, default_registry
from .providers.base import SourceCapabilities
from .scoring import auto_apply_title_eligible, evaluate_search_filter, score_vacancy
from .storage import AUTO_APPROVAL_REASON, Store, needs_manual_reapproval

SOURCE_LABELS = {
    "hh.ru": "HH.ru",
    "career.habr.com": "Habr Career",
    "getmatch.ru": "Getmatch",
    "geekjob.ru": "GeekJob",
    "yandex.ru/jobs": "Карьера Яндекса",
    "dreamjob.ru": "DreamJob (HH mirror)",
}
DEFAULT_SOURCES = list(SOURCE_LABELS)

# Discovery priority only, not a rejection filter: after AI/LLM/agent roles
# are fetched, subsequent incremental runs also cover every other linked job.
GETMATCH_AI_TITLE_HINT = re.compile(
    r"(?i)(?:\bllm\b|\bai\b|\brag\b|\bgpt\b|\bnlp\b|"
    r"agent|агент|генератив|нейросет|разработчик\s+ии|инженер\s+ии)"
)


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
                      primary_role_terms: Iterable[str] = (),
                      start_page: int = 1,
                      until_complete: bool = False,
                      fixture_dir: str | Path | None = None,
                      fixture_path: str | Path | None = None,
                      detail_fixture_path: str | Path | None = None,
                      remote: bool | None = None, date_from: str = "") -> dict[str, Any]:
    filters: dict[str, Any] = {
        "queries": queries,
        "primary_role_terms": list(primary_role_terms),
        "pages": max(1, int(pages)),
        "request_budget": max(1, int(request_budget)),
        "details_limit": max(0, int(details_limit)),
        "start_page": max(1, int(start_page)),
        "until_complete": bool(until_complete),
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
                          search: dict[str, Any], cutoff: datetime, track_resume: str,
                          track: str = "default") -> list[dict[str, Any]]:
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
        item["auto_apply_eligible"] = auto_apply_title_eligible(item, group)
        if not item["auto_apply_eligible"]:
            item["auto_apply_reason"] = "title_requires_manual_role_review"
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
        title = str(item.get("name") or "")
        description = str(item.get("description") or "")
        configured_guards = (
            ("exclude_title_patterns", title, "role:configured_title_exclusion", True),
            ("exclude_description_patterns", description,
             "description:configured_exclusion", False),
            ("exclude_location_patterns", description, "geo:configured_exclusion", False),
        )
        for setting, text, reason, reject_score in configured_guards:
            if any(re.search(pattern, text, re.IGNORECASE)
                   for pattern in group.get(setting, []) or []):
                filter_status = "reject"
                filter_reasons.append(reason)
                if reject_score:
                    item["score_decision"] = "reject"
        item["filter_status"] = filter_status
        item["filter_reasons"] = list(dict.fromkeys(filter_reasons))
        item["filter_fields"] = list(filter_result.fields)
        item["published_window_status"] = "unknown" if not published else (
            "outside_window" if filter_status == "reject" and "publication:outside_requested_window" in filter_reasons
            else "within_window"
        )
        item["problems"] = _review_problems(item, group)
        item["salary_confidence"] = _salary_confidence(item, group)
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
                details_limit: int | None = None, limit: int = 100, start_page: int = 1,
                until_complete: bool = False,
                hh_start_pages: dict[str, int | None] | None = None,
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
                sort_mode=str(search.get("sort_mode") or "newest"),
                track_resume=str(profile.get("resume") or ""),
                start_pages=hh_start_pages,
            )
            summary = result.get("summary") or {}
            return {
                "campaign_id": result.get("campaign_id", ""), "source": source,
                "status": result.get("status", "failed"), "account": result.get("account", ""),
                "track": track, "fixture": False,
                "items": int(summary.get("items", 0) or 0),
                "segments": int(result.get("segment_count", 0) or 0),
                "query_count": int(result.get("query_count", 0) or 0),
                "covered_query_count": int(result.get("covered_query_count", 0) or 0),
                "uncovered_queries": list(result.get("uncovered_queries") or []),
                "segments_detail": list(result.get("segments") or []),
                "errors": list(result.get("segment_errors") or []),
                "snapshot": result.get("snapshot", ""), "summary": summary,
                "top": result.get("top", []), "score_counts": result.get("score_counts", {}),
                "candidate_ids": result.get("candidate_ids", []),
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
        "start_page": max(1, int(start_page)),
        "fixture": bool(fixture_dir or fixture_path),
    }
    store.start_campaign(campaign_id, account, track, safe_config, source=source)
    try:
        filters = _provider_filters(
            source, queries=queries, pages=effective_pages,
            request_budget=effective_budget, details_limit=effective_details,
            primary_role_terms=group.get("primary_role_terms", []),
            start_page=start_page,
            until_complete=until_complete,
            fixture_dir=fixture_dir, fixture_path=fixture_path,
            detail_fixture_path=detail_fixture_path,
            remote=group.get("only_remote"), date_from=cutoff.date().isoformat(),
        )
        if source in {"career.habr.com", "getmatch.ru", "geekjob.ru"} and not fixture_mode:
            cached: dict[str, dict[str, Any]] = {}
            cache_cutoff = datetime.now(UTC) - timedelta(days=30)
            for old in store.list_campaign_vacancies(account, track, "all", 5000, source=source):
                description = str(old.get("description") or "").strip()
                if old.get("description_status") != "ok" or not description:
                    continue
                try:
                    checked = datetime.fromisoformat(str(old.get("last_checked_at") or ""))
                    if checked.tzinfo is None:
                        checked = checked.replace(tzinfo=UTC)
                    if checked.astimezone(UTC) < cache_cutoff:
                        continue
                except ValueError:
                    continue
                cached[str(old.get("id") or "")] = old
            filters["known_descriptions"] = cached
        if source == "getmatch.ru" and not fixture_mode:
            # Namespace incremental sitemap cursor per source and account.
            # Telegram-specific scans use explicit URLs, not the sitemap.
            scope = json.dumps({
                "queries": sorted(queries),
                "primary_role_terms": sorted(str(value) for value in group.get("primary_role_terms", [])),
            }, ensure_ascii=False, sort_keys=True)
            scope_id = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
            filters["state_path"] = str(
                config.data_dir / "providers" / source_slug(source)
                / source_slug(account) / f"sitemap_seen-{scope_id}.json"
            )
            if telegram_hints:
                filters["telegram_state_path"] = str(
                    config.data_dir / "providers" / source_slug(source)
                    / source_slug(account) / f"telegram_seen-{scope_id}.json"
                )
                filters["include_sitemap"] = True
        # The user already owns these messages. Extract public job IDs only:
        # no bot commands are interpreted, no callbacks are executed.
        if telegram_hints and source == "getmatch.ru" and not fixture_mode:
            candidates: list[tuple[int, str]] = []
            seen_ids: set[str] = set()
            for message in store.telegram_messages(account, 1000):
                # Role title is only an ordering hint. Full on-site text and
                # ai_core scoring remain the eligibility source of truth.
                headline = str(message.get("text") or "").splitlines()[0][:220]
                priority = 0 if GETMATCH_AI_TITLE_HINT.search(headline) else 1
                for ref in message.get("vacancy_refs", []):
                    native_id = str(ref.get("id") or "").strip()
                    if (ref.get("source") != source or not native_id.isdecimal()
                            or native_id in seen_ids):
                        continue
                    seen_ids.add(native_id)
                    candidates.append(
                        (priority, f"https://getmatch.ru/vacancies/{native_id}")
                    )
            if candidates:
                # Python's stable sort preserves newest-first within each tier.
                filters["detail_urls"] = [
                    url for _, url in sorted(candidates, key=lambda item: item[0])
                ]
                # GetMatchProvider routes these links through telegram_state_path.
                # Keep state_path on the independent sitemap cursor so the
                # two discovery channels cannot advance or suppress each other.
        scan_session = None
        if not fixture_mode and source in {"career.habr.com", "getmatch.ru", "geekjob.ru"}:
            # A provider pass can make hundreds of list/detail GETs. Reuse its
            # connection pool instead of paying a new TCP/TLS setup per URL.
            scan_session = requests.Session()
            filters["session"] = scan_session
        try:
            discovered = provider.discover(filters)
        except KeyboardInterrupt:
            # Ctrl-C must not leave a permanent `running` campaign row. The
            # provider cursor is the resume point; the partially fetched page
            # is deliberately retried on the next invocation.
            store.fail_campaign(campaign_id, "scan interrupted by user; resume is safe")
            raise
        finally:
            if scan_session is not None:
                scan_session.close()
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
            track=track,
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
            "candidate_ids": [str(item.get("id") or "") for item in items if item.get("id")],
            "start_page": max(1, int(start_page)),
        }
    except Exception as exc:  # noqa: BLE001 - source failures are isolated by contract
        message = f"{type(exc).__name__}: {exc}"
        store.fail_campaign(campaign_id, message)
        return {"campaign_id": campaign_id, "source": source, "status": "failed",
                "account": account, "track": track, "items": 0, "errors": [message],
                "fixture": bool(fixture_dir or fixture_path)}


def scan_sources(config: AppConfig, track: str, profile: dict[str, Any], search: dict[str, Any],
                 sources: Iterable[str] | None = None, *, until_complete: bool = False,
                 resume: bool = False, max_passes: int = 30, **kwargs: Any) -> dict[str, Any]:
    """Scan all configured query groups and continue bounded provider passes."""
    registry = kwargs.pop("registry", None) or default_registry()
    selected = list(dict.fromkeys(str(source) for source in (sources or DEFAULT_SOURCES)))
    if not 1 <= int(max_passes) <= 100:
        raise ValueError("max_passes must be between 1 and 100")
    drain = bool(until_complete or resume)
    base_kwargs = dict(kwargs)
    groups = search_groups(search) or [dict(search)]
    results = []
    for source in selected:
        source_groups: list[dict[str, Any]] = []
        source_account = resolve_source_account(config, profile, source)
        for group in groups:
            group_search = dict(group)
            group_name = str(group_search.get("group_name") or "default")
            scope = json.dumps({
                "source": source, "track": track, "group": group_name,
                "queries": sorted(str(v) for v in group_search.get("queries", [])),
                "primary_role_terms": sorted(str(v) for v in group_search.get("primary_role_terms", [])),
            }, ensure_ascii=False, sort_keys=True)
            scope_id = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
            cursor_path = (
                config.data_dir / "providers" / source_slug(source) / source_slug(source_account)
                / f"scan-{source_slug(track)}-{scope_id}.json"
            )
            group_kwargs = dict(base_kwargs)
            if drain and group_kwargs.get("pages") is None:
                # A pass uses the configured page window (normally one page).
                # Continuation advances the cursor between bounded passes;
                # replacing this with 50/100 pages multiplies the work by all
                # search terms and can turn a scan into an hours-long crawl.
                group_kwargs["pages"] = max(1, int(
                    group_search.get("max_pages", search.get("max_pages", 1)) or 1
                ))
            if drain:
                # Use the profile's per-pass budgets. The former generic
                # 10,000 requests / 1,000 detail pages ignored settings and
                # caused slow providers to dominate the whole scan.
                if group_kwargs.get("request_budget") is None:
                    group_kwargs["request_budget"] = int(
                        group_search.get("request_budget", search.get("request_budget", 100))
                        or 100
                    )
                if group_kwargs.get("details_limit") is None:
                    group_kwargs["details_limit"] = int(
                        group_search.get("details_limit", search.get("details_limit", 100))
                        or 100
                    )
            start_page = 1
            hh_page_cursor: dict[str, int | None] = {}
            if resume and source == "hh.ru":
                try:
                    saved = json.loads(cursor_path.read_text(encoding="utf-8"))
                    raw_cursor = saved.get("next_pages", {})
                    if isinstance(raw_cursor, dict):
                        hh_page_cursor = {
                            str(key): (None if value is None else max(0, int(value)))
                            for key, value in raw_cursor.items()
                        }
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
            if source == "hh.ru" and drain:
                # Materialize every segment before the first pass. If the
                # request budget runs out halfway through the query matrix,
                # segments that have not been requested yet must remain at
                # page zero instead of disappearing from the cursor map.
                from .sources import HhSearchAdapter

                sort_mode = str(group_search.get("sort_mode", search.get("sort_mode", "newest")))
                orders = ("publication_time", "relevance") if sort_mode == "balanced" else (
                    "publication_time",) if sort_mode == "newest" else ("relevance",)
                group_queries = [
                    str(value).strip() for value in group_search.get("queries", []) if str(value).strip()
                ]
                group_areas = [
                    int(value) for value in group_search.get("areas", search.get("areas", [113]))
                ]
                for query in group_queries:
                    for area in group_areas:
                        for order in orders:
                            hh_page_cursor.setdefault(
                                HhSearchAdapter.page_key(query, area, order), 0,
                            )
            if resume and source in {"career.habr.com", "geekjob.ru"}:
                try:
                    saved = json.loads(cursor_path.read_text(encoding="utf-8"))
                    start_page = max(1, int(saved.get("next_page", 1)))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
            passes: list[dict[str, Any]] = []
            hh_cursor_complete = False
            pass_limit = max_passes if drain and source in {
                "hh.ru", "getmatch.ru", "career.habr.com", "geekjob.ru",
            } else 1
            for _pass in range(pass_limit):
                pass_search = dict(group_search)
                previous_hh_cursor = dict(hh_page_cursor)
                result = scan_source(
                    config, track, profile, pass_search, source,
                    registry=registry, **{
                        **group_kwargs, "start_page": start_page,
                        "until_complete": drain,
                        "hh_start_pages": hh_page_cursor if source == "hh.ru" and drain else None,
                    },
                )
                passes.append(result)
                status = str(result.get("status") or "failed")
                errors = [str(value) for value in result.get("errors", [])]

                if source == "hh.ru" and drain:
                    from .sources import HhSearchAdapter

                    for segment in result.get("segments_detail", []):
                        key = HhSearchAdapter.page_key(
                            str(segment.get("query") or ""), int(segment.get("area") or 0),
                            str(segment.get("order_by") or "relevance"),
                        )
                        current = hh_page_cursor.get(key, 0)
                        if current is None:
                            continue
                        segment_pages = max(0, int(segment.get("pages") or 0))
                        segment_status = str(segment.get("status") or "failed")
                        segment_error = str(segment.get("error") or "")
                        if not segment_pages:
                            continue
                        if segment_status == "truncated" and not segment_error:
                            hh_page_cursor[key] = int(current) + segment_pages
                        elif segment_status in {"ok", "empty"}:
                            hh_page_cursor[key] = None

                    pending_hh_pages = any(value is not None for value in hh_page_cursor.values())
                    if pending_hh_pages:
                        try:
                            cursor_path.parent.mkdir(parents=True, exist_ok=True)
                            temporary = cursor_path.with_name(f".{cursor_path.name}.tmp")
                            temporary.write_text(json.dumps({
                                "track": track, "source": source, "scope": scope_id,
                                "next_pages": hh_page_cursor,
                                "updated_at": datetime.now(UTC).isoformat(),
                            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                            os.chmod(temporary, 0o600)
                            temporary.replace(cursor_path)
                        except OSError as exc:
                            result["errors"] = [*errors, f"resume cursor save failed: {exc}"]
                            break
                    else:
                        cursor_path.unlink(missing_ok=True)
                        hh_cursor_complete = True

                if not drain or status not in {"partial", "truncated", "blocked"}:
                    if source in {"career.habr.com", "geekjob.ru"} and status in {"ok", "empty_verified"}:
                        cursor_path.unlink(missing_ok=True)
                    break
                if status == "blocked" or result.get("blocked"):
                    break
                if source == "hh.ru":
                    pending_hh_pages = any(value is not None for value in hh_page_cursor.values())
                    if not pending_hh_pages or hh_page_cursor == previous_hh_cursor:
                        break
                    continue
                if source == "getmatch.ru":
                    if not int(result.get("items") or 0):
                        break
                    continue
                page_window = any("maximum page window reached" in value for value in errors)
                detail_window = any("detail limit reached" in value for value in errors)
                if source in {"career.habr.com", "geekjob.ru"} and (page_window or detail_window):
                    if page_window and not detail_window:
                        page_window = group_kwargs.get("pages")
                        if page_window is None:
                            page_window = 100 if source == "hh.ru" else 50
                        start_page += max(1, int(page_window))
                    try:
                        cursor_path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = cursor_path.with_name(f".{cursor_path.name}.tmp")
                        temporary.write_text(json.dumps({
                            "track": track, "source": source, "scope": scope_id,
                            "next_page": start_page, "updated_at": datetime.now(UTC).isoformat(),
                        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        os.chmod(temporary, 0o600)
                        temporary.replace(cursor_path)
                    except OSError as exc:
                        result["errors"] = [*errors, f"resume cursor save failed: {exc}"]
                        break
                    continue
                break
            candidate_ids = list(dict.fromkeys(
                str(value) for row in passes for value in row.get("candidate_ids", []) if str(value)
            ))
            totals: dict[str, int] = {}
            for row in passes:
                for key, value in (row.get("summary") or {}).items():
                    if isinstance(value, int):
                        totals[key] = totals.get(key, 0) + value
            final = passes[-1] if passes else {"status": "failed", "errors": ["no scan pass ran"]}
            pass_errors = [
                f"pass {index + 1}: {error}"
                for index, row in enumerate(passes) for error in row.get("errors", [])
            ]
            group_result = dict(final)
            group_result.update({
                "group": group_name, "status": final.get("status", "failed"),
                "items": len(candidate_ids), "candidate_ids": candidate_ids,
                "passes": len(passes),
                "pass_campaign_ids": [row.get("campaign_id", "") for row in passes],
                "summary": totals, "errors": pass_errors,
            })
            if source == "hh.ru":
                query_count = len({
                    str(value).strip() for value in group_search.get("queries", [])
                    if str(value).strip()
                })
                group_result["query_count"] = query_count
                if drain:
                    all_segments = [
                        segment for pass_result in passes
                        for segment in pass_result.get("segments_detail", [])
                    ]
                    group_result["segments_detail"] = all_segments
                if hh_cursor_complete:
                    # The legacy HH runner reports coverage within one bounded
                    # pass. The final pass may contain only the last unfinished
                    # query, so its local `uncovered_queries` is not a campaign
                    # coverage result. The cursor is removed only after every
                    # query/area/sort segment reached a terminal page.
                    residual_errors = [
                        error for error in pass_errors
                        if not any(marker in error.casefold() for marker in (
                            "request budget reached", "maximum page window reached",
                        ))
                    ]
                    failed_pass = any(
                        str(pass_result.get("status") or "").casefold()
                        in {"failed", "blocked", "partial"}
                        for pass_result in passes
                    )
                    complete_status = "partial" if failed_pass or residual_errors else (
                        "ok" if candidate_ids else "empty_verified"
                    )
                    group_result.update({
                        "status": complete_status,
                        "coverage_complete": True,
                        "covered_query_count": query_count,
                        "uncovered_queries": [],
                        "errors": residual_errors,
                    })
                else:
                    group_result["uncovered_queries"] = list(final.get("uncovered_queries") or [])
            source_groups.append(group_result)

        candidate_ids = list(dict.fromkeys(
            str(value) for row in source_groups for value in row.get("candidate_ids", []) if str(value)
        ))
        statuses = {str(row.get("status") or "failed") for row in source_groups}
        status = next((value for value in ("blocked", "failed", "partial", "truncated")
                       if value in statuses),
                      "empty_verified" if statuses == {"empty_verified"} else "ok")
        summary: dict[str, int] = {}
        for row in source_groups:
            for key, value in (row.get("summary") or {}).items():
                if isinstance(value, int):
                    summary[key] = summary.get(key, 0) + value
        if summary:
            summary["items"] = len(candidate_ids)
        results.append({
            "source": source, "status": status, "account": source_account,
            "track": track, "items": len(candidate_ids), "candidate_ids": candidate_ids,
            "groups": len(source_groups),
            "passes": sum(int(row.get("passes", 1)) for row in source_groups),
            "query_count": sum(int(row.get("query_count", 0) or 0) for row in source_groups),
            "covered_query_count": sum(
                int(row.get("covered_query_count", 0) or 0) for row in source_groups
            ),
            "coverage_complete": bool(source == "hh.ru" and drain and source_groups)
            and all(row.get("coverage_complete") is True for row in source_groups),
            "segments_detail": [
                segment for row in source_groups for segment in row.get("segments_detail", [])
            ] if source == "hh.ru" and drain else [],
            "uncovered_queries": list(dict.fromkeys(
                str(value) for row in source_groups for value in row.get("uncovered_queries", [])
            )),
            "campaign_ids": [value for row in source_groups
                             for value in row.get("pass_campaign_ids", [row.get("campaign_id", "")])],
            "summary": summary,
            "errors": [f"{row.get('group', 'default')}: {error}"
                       for row in source_groups for error in row.get("errors", [])],
            "partial": status in {"partial", "truncated", "blocked", "failed"},
        })
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
        "until_complete": drain,
        "failed_sources": [r["source"] for r in results if r.get("status") in {"failed", "blocked"}],
        "partial_sources": [r["source"] for r in results
                             if r.get("status") in {"partial", "truncated", "blocked", "failed"}],
    }


def approve_scanned_fits(
    store: Store, account: str, track: str, source: str, vacancy_ids: Iterable[str],
    *, search: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Approve fully described FIT rows and explicitly allowed soft unknowns.

    Missing salary is allowed only when the search profile says ``missing=include``.
    An unknown publication date is not treated as stale when the active listing was
    found in the current scan. Remote, currency, experience and personal checks stay hard.
    """
    wanted = {str(value).strip() for value in vacancy_ids if str(value).strip()}
    rows = store.list_campaign_vacancies(account, track, "all", 5000, source=source)
    groups = {
        str(group.get("group_name") or "default"): group
        for group in search_groups(search or {})
    }
    eligible: list[str] = []
    rejected: dict[str, str] = {}
    for row in rows:
        vacancy_id = str(row.get("id") or "")
        if vacancy_id not in wanted:
            continue
        group = groups.get(str(row.get("search_group") or "default"), search or {})
        reasons = {str(value) for value in row.get("filter_reasons", [])}
        soft_unknowns: set[str] = set()
        if "publication:time_not_verified" in reasons and int(group.get("days", 0) or 0) > 0:
            soft_unknowns.add("publication:time_not_verified")
        salary_cfg = group.get("salary") or {}
        if ("salary:unpublished_not_verified" in reasons
                and str(salary_cfg.get("missing", "include")) == "include"):
            soft_unknowns.add("salary:unpublished_not_verified")
        allowed_unknown_reasons = soft_unknowns | {
            "remote:verified", "work_format:matching", "experience:matching",
            "salary:matching", "salary:missing_allowed", "salary:gross_net_unknown",
        }
        soft_unknown_ok = bool(soft_unknowns) and reasons <= allowed_unknown_reasons
        problems = {str(value) for value in row.get("problems", [])}
        if soft_unknown_ok and "salary:unpublished_not_verified" in soft_unknowns:
            problems.discard("salary:unpublished")
        reason = ""
        if (row.get("score_decision") != "pass"
                or (row.get("filter_status") != "pass" and not soft_unknown_ok)):
            reason = "role or search conditions are not fully verified"
        elif row.get("review_lane") != "FIT" and not (
            soft_unknown_ok and row.get("review_lane") == "GOOD_UNKNOWN"
        ):
            reason = "not in the FIT review lane"
        elif row.get("auto_apply_eligible") is False:
            reason = "title requires manual role review"
        elif row.get("description_status") != "ok" or not str(row.get("description") or "").strip():
            reason = "full vacancy description is missing"
        elif problems:
            reason = "; ".join(sorted(problems))[:240]
        elif row.get("channel_type") == "external_ats" or row.get("application_channel_type") == "external_ats":
            reason = "external ATS requires separate review"
        elif row.get("applied_before") or row.get("channel_duplicate"):
            reason = "application channel is already used or reserved"
        elif row.get("application_state") not in {"NOT_STARTED", "FAILED_BEFORE_SUBMIT", "READY_TO_APPLY"}:
            reason = f"application state is {row.get('application_state')}"
        if reason:
            rejected[vacancy_id] = reason
        else:
            eligible.append(vacancy_id)
    updated = 0
    for offset in range(0, len(eligible), 100):
        chunk = eligible[offset:offset + 100]
        updated += store.set_review_state_batch(
            account, source, chunk, "APPROVED",
            reason=AUTO_APPROVAL_REASON,
            track=track,
        )
    return {
        "source": source, "requested": len(wanted), "eligible": len(eligible),
        "approved": updated, "blocked": rejected,
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
        if not needs_manual_reapproval(row)
        and not row.get("applied_before")
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
            "resume": row.get("resume", ""),
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
        elif row.get("channel_type") == "external_ats":
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
