"""Управляемый ручной поиск и постоянная очередь ApplyPilot."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import AppConfig, search_groups, validate_search
from .parser import ScanSegment, enrich_items, save_snapshot, stamp_first_seen
from .scoring import evaluate_search_filter, prioritize_for_enrichment, score_vacancy
from .sources import HhSearchAdapter
from .storage import Store, campaign_fit_bucket


def _published_status(value: Any, cutoff: datetime) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    try:
        published = datetime.fromisoformat(text)
    except ValueError:
        return "unknown"
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    return "within_window" if published.astimezone(UTC) >= cutoff else "outside_window"


def _merge_known_fields(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Не затирает ранее загруженное полное описание кратким или пустым ответом HH."""
    merged = dict(current)
    for key in ("salary", "experience", "area", "schedule", "work_format", "is_remote"):
        if not merged.get(key) and previous.get(key):
            merged[key] = previous[key]
    old_description = str(previous.get("description") or "")
    new_description = str(merged.get("description") or "")
    if old_description and (
        not new_description
        or (previous.get("description_status") == "ok"
            and (merged.get("description_status") != "ok" or len(new_description) < len(old_description)))
    ):
        merged["description"] = old_description
        for key in ("description_status", "description_source"):
            if previous.get(key):
                merged[key] = previous[key]
    for key in ("name", "company", "url", "published"):
        if not merged.get(key) and previous.get(key):
            merged[key] = previous[key]
    return merged


def _review_problems(item: dict[str, Any], search: dict[str, Any]) -> list[str]:
    """Возвращает сигналы для ручной проверки, а не вердикты о кандидате."""
    problems: list[str] = []
    title = str(item.get("name") or "")
    description = str(item.get("description") or "")
    raw_salary = item.get("salary")
    salary = raw_salary if isinstance(raw_salary, dict) else {}
    try:
        minimum = int((search.get("salary") or {}).get("from", 0) or 0)
    except (TypeError, ValueError):
        minimum = 0
    if not salary:
        problems.append("salary:unpublished")
    elif minimum:
        lower, upper = salary.get("from"), salary.get("to")
        currency = str(salary.get("currency") or "").upper()
        wanted = str((search.get("salary") or {}).get("currency") or "").upper()
        try:
            lower_value = int(lower) if lower is not None else None
            upper_value = int(upper) if upper is not None else None
        except (TypeError, ValueError):
            lower_value = upper_value = None
            problems.append("salary:malformed")
        if wanted and currency and wanted != currency:
            problems.append("salary:currency_mismatch")
        elif lower_value is not None and lower_value >= minimum:
            problems.append("salary:lower_bound_meets_threshold")
        elif upper_value is not None and upper_value >= minimum:
            problems.append("salary:range_overlaps_threshold")
        else:
            problems.append("salary:below_or_unverified")
    if re.search(r"\b(senior|lead|principal|staff|head|chief)\b|старш\w+|ведущ\w+|руководител\w+|архитектор", title, re.IGNORECASE):
        problems.append("grade:seniority_check")
    if re.search(r"\b(english|английск\w*)\b.{0,100}\b(b2|c1|c2|advanced|fluent|upper[- ]intermediate)\b|"
                 r"\b(b2|c1|c2|advanced|fluent)\b.{0,70}\b(english|английск\w*)\b", description, re.IGNORECASE):
        problems.append("language:english_requirement_check")
    if re.search(r"высш\w+ образован|бакалавр|магистр|bachelor|master.s degree|university degree", description, re.IGNORECASE):
        problems.append("education:requirement_check")
    if search.get("only_remote") and item.get("is_remote") is not True:
        problems.append("geo:remote_not_verified")
    if re.search(r"\b(java|rust|php|\.net|c\+\+|go)\b", title, re.IGNORECASE):
        problems.append("stack:title_needs_review")
    return list(dict.fromkeys(problems))


def run_campaign(config: AppConfig, track: str, profile: dict[str, Any], search: dict[str, Any], *,
                 queries: list[str] | None = None, areas: list[int] | None = None,
                 days: int = 3, pages: int | None = None, request_budget: int | None = None,
                 details_limit: int | None = None, limit: int = 100,
                 only_remote: bool | None = None, salary_from: int | None = None,
                 salary_currency: str | None = None, sort_mode: str = "newest",
                 track_resume: str = "", preset: str | None = None) -> dict[str, Any]:
    """Запускает один bounded HH scan, сохраняет snapshot и актуализирует SQLite очередь."""
    if days < 1 or days > 365 or limit < 1 or limit > 100:
        raise ValueError("days must be 1-365 and limit must be 1-100")
    if pages is not None and not 1 <= pages <= 100:
        raise ValueError("pages must be between 1 and 100")
    if request_budget is not None and not 1 <= request_budget <= 10000:
        raise ValueError("request_budget must be between 1 and 10000")
    if details_limit is not None and not 0 <= details_limit <= 1000:
        raise ValueError("details_limit must be between 0 and 1000")
    if sort_mode not in {"newest", "relevance", "balanced"}:
        raise ValueError("sort_mode must be newest, relevance or balanced")

    search = dict(search)
    command_overrides: dict[str, Any] = {}
    search["days"] = days
    search["sort_mode"] = sort_mode
    if queries:
        search["queries"] = list(dict.fromkeys(str(value).strip() for value in queries if str(value).strip()))
    if areas:
        search["areas"] = [int(value) for value in areas]
    if pages is not None:
        search["max_pages"] = pages
    if request_budget is not None:
        search["request_budget"] = request_budget
    if details_limit is not None:
        search["details_limit"] = details_limit
    if only_remote is not None:
        search["only_remote"] = only_remote
        command_overrides["only_remote"] = only_remote
    if salary_from is not None or salary_currency:
        salary = dict(search.get("salary") or {})
        if salary_from is not None:
            salary["from"] = salary_from
            command_overrides["salary.from"] = salary_from
        if salary_currency:
            salary["currency"] = salary_currency
            command_overrides["salary.currency"] = salary_currency
        search["salary"] = salary
    search = validate_search(search)
    groups = search_groups(search)
    effective_budget = request_budget if request_budget is not None else int(search.get("request_budget", 100))
    effective_details = details_limit if details_limit is not None else int(search.get("details_limit", 100))
    if not 1 <= effective_budget <= 10000:
        raise ValueError("configured request_budget must be between 1 and 10000")
    if not 0 <= effective_details <= 1000:
        raise ValueError("configured details_limit must be between 0 and 1000")
    for group in groups:
        group_pages = pages if pages is not None else int(group.get("max_pages", 1))
        if not 1 <= group_pages <= 100:
            raise ValueError("configured pages must be between 1 and 100")
    for group in groups:
        for key, value in command_overrides.items():
            if key.startswith("salary."):
                group.setdefault("salary", {})[key.split(".", 1)[1]] = value
            else:
                group[key] = value

    started = datetime.now(UTC)
    cutoff = started - timedelta(days=days)
    campaign_id = uuid.uuid4().hex
    account = str(profile.get("account") or "default").strip() or "default"
    query_list = list(search.get("queries") or [])
    if not query_list:
        query_list = [str(query) for group in groups for query in group.get("queries", []) if str(query).strip()]
    if not query_list:
        raise ValueError("search queries are empty; configure a track or pass --query")
    if queries:
        query_list = list(dict.fromkeys(str(value).strip() for value in queries if str(value).strip()))
    if len(query_list) > 100 or any(len(value) > 200 for value in query_list):
        raise ValueError("campaign supports at most 100 queries of up to 200 characters")
    requested_areas = areas or [int(value) for group in groups for value in group.get("areas", [113])]
    requested_areas = list(dict.fromkeys(int(value) for value in requested_areas))
    if not requested_areas or len(requested_areas) > 100 or any(value < 1 for value in requested_areas):
        raise ValueError("areas must contain 1-100 positive HH region IDs")

    safe_config = {
        "track": track, "source": "hh.ru", "days": days, "queries": query_list,
        "areas": requested_areas,
        "remote": only_remote if only_remote is not None else bool(search.get("only_remote", False)),
        "salary_from": (search.get("salary") or {}).get("from"),
        "salary_currency": (search.get("salary") or {}).get("currency"),
        "salary_missing": (search.get("salary") or {}).get("missing", "include"),
        "max_pages": pages if pages is not None else int(search.get("max_pages", 1)),
        "request_budget": request_budget if request_budget is not None else int(search.get("request_budget", 100)),
        "details_limit": details_limit if details_limit is not None else int(search.get("details_limit", 100)),
        "sort_mode": sort_mode, "limit": limit, "preset": preset or search.get("preset", ""),
        "groups": [{
            "name": str(group.get("group_name") or "default"),
            "queries": query_list if queries else [str(value) for value in group.get("queries", [])],
            "areas": areas or list(group.get("areas") or [113]),
            "pages": pages if pages is not None else int(group.get("max_pages", 1)),
            "remote": only_remote if only_remote is not None else bool(group.get("only_remote", False)),
            "salary": dict(group.get("salary") or {}),
        } for group in groups],
    }
    store = Store(config.db_path)
    adapter = HhSearchAdapter()
    previous_campaign_id = store.start_campaign(campaign_id, account, track, safe_config)
    all_items: list[dict[str, Any]] = []
    segments: list[ScanSegment] = []
    detail_errors: list[str] = []
    try:
        request_budget_value = effective_budget
        if sort_mode == "relevance":
            sort_budgets = (("relevance", request_budget_value),)
        elif sort_mode == "newest":
            sort_budgets = (("publication_time", request_budget_value),)
        else:
            newest_budget = (request_budget_value + 1) // 2
            sort_budgets = (("publication_time", newest_budget),
                            ("relevance", request_budget_value - newest_budget))

        group_specs: list[dict[str, Any]] = []
        for group in groups:
            group_queries = query_list if queries else [str(value).strip() for value in group.get("queries", []) if str(value).strip()]
            group_areas = areas or [int(value) for value in (group.get("areas") or [113])]
            if not group_queries:
                continue
            group_specs.append({
                "name": str(group.get("group_name") or "default"), "queries": group_queries,
                "areas": group_areas,
                "pages": pages if pages is not None else int(group.get("max_pages", 1)),
                "remote": only_remote if only_remote is not None else bool(group.get("only_remote", False)),
                "date_from": cutoff.date().isoformat(), "search": group,
            })
        for order_by, allocated_budget in sort_budgets:
            remaining = allocated_budget
            for spec in group_specs:
                if remaining <= 0:
                    segments.append(ScanSegment(spec["queries"][0], spec["areas"][0], 0,
                                                "truncated", 0, "search request budget reached",
                                                order_by=order_by))
                    break
                group_items, group_segments = adapter.discover({
                    "queries": spec["queries"], "areas": spec["areas"],
                    "pages": spec["pages"], "remote": spec["remote"],
                    "date_from": spec["date_from"], "pause_seconds": 1.0,
                    "request_budget": remaining, "order_by": order_by,
                })
                remaining -= sum(segment.requests for segment in group_segments)
                for item in group_items:
                    item["search_group"] = spec["name"]
                all_items.extend(group_items)
                segments.extend(group_segments)

        deduplicated: dict[str, dict[str, Any]] = {}
        for item in all_items:
            vacancy_id = str(item.get("id") or "").strip()
            if not vacancy_id:
                continue
            if vacancy_id not in deduplicated:
                deduplicated[vacancy_id] = item
                continue
            existing = deduplicated[vacancy_id]
            for field in ("query_sources", "area_sources", "sort_sources"):
                existing[field] = sorted(set(existing.get(field, [])) | set(item.get(field, [])))
            existing["search_groups"] = sorted(set(existing.get("search_groups", [existing.get("search_group", "")]))
                                                | {item.get("search_group", "")})
        all_items = list(deduplicated.values())

        details = effective_details
        ranking_profile = {**profile, "search": search, "rescore": True}
        enrichment = prioritize_for_enrichment(all_items, ranking_profile, details)
        candidate_ids = {candidate.id for candidate in enrichment}
        enrichment_items = [item for item in all_items if str(item.get("id") or "") in candidate_ids]
        for item in all_items:
            if str(item.get("id") or "") not in candidate_ids:
                item["description_status"] = "provisional"
        enriched, detail_errors = enrich_items(enrichment_items, details, pause_seconds=1.0)
        enriched_by_id = {str(item.get("id") or ""): item for item in enriched}
        old_payloads = store.vacancy_payloads(
            account, "hh.ru", [str(item.get("id") or "") for item in all_items],
        )
        for item in all_items:
            fresh = enriched_by_id.get(str(item.get("id") or ""))
            if fresh is not None:
                item.update(fresh)
            old = old_payloads.get(str(item.get("id") or ""), {})
            if old:
                item.update(_merge_known_fields(old, item))

        group_search = {str(group.get("group_name") or "default"): group for group in groups}
        campaign_items: list[dict[str, Any]] = []
        for raw_item in all_items:
            item = dict(raw_item)
            scoring_profile = {**profile, "search": search, "rescore": True}
            candidate = score_vacancy(item, scoring_profile)
            item.update(candidate.to_dict())
            item["fit_score"] = candidate.score
            item["score_decision"] = candidate.decision
            item["score"] = candidate.score
            item["resume"] = track_resume or candidate.resume
            group = group_search.get(str(item.get("search_group") or "default"), search)
            filter_result = evaluate_search_filter(item, group)
            filter_status = filter_result.status
            filter_reasons = list(filter_result.reasons)
            salary_filter = group.get("salary") or {}
            try:
                salary_threshold = int(salary_filter.get("from", 0) or 0)
            except (TypeError, ValueError):
                salary_threshold = 0
            if (filter_status == "pass" and salary_threshold > 0 and not item.get("salary")
                    and str(salary_filter.get("missing", "include")) == "include"):
                # Include unpublished salary in the manual review pool without
                # counting it as a verified salary match.
                filter_status = "unknown"
                filter_reasons.append("salary:unpublished_not_verified")
            item["filter_status"] = filter_status
            item["filter_reasons"] = list(dict.fromkeys(filter_reasons))
            item["filter_fields"] = list(filter_result.fields)
            published_status = _published_status(item.get("published"), cutoff)
            item["published_window_status"] = published_status
            if published_status == "outside_window":
                item["filter_status"] = "reject"
                item["filter_reasons"] = [*item["filter_reasons"], "publication:outside_requested_window"]
            elif published_status == "unknown":
                if item["filter_status"] == "pass":
                    item["filter_status"] = "unknown"
                item["filter_reasons"] = [*item["filter_reasons"], "publication:time_not_verified"]
            item["problems"] = _review_problems(item, group)
            item["salary_confidence"] = next(
                (value.split(":", 1)[1] for value in item["problems"] if value.startswith("salary:")),
                "unknown",
            )
            campaign_items.append(item)

        stamp_first_seen(campaign_items, config.data_dir / "seen.json")
        failed_segments = [segment for segment in segments if segment.status in {"failed", "captcha"}]
        truncated = any(segment.status == "truncated" for segment in segments)
        status = ("partial" if failed_segments or detail_errors else "truncated" if truncated
                  else "ok" if campaign_items else "empty")
        errors = [f"{segment.query} area {segment.area}: {segment.error}"
                  for segment in segments if segment.error]
        errors.extend(detail_errors)
        snapshot_path = save_snapshot(
            campaign_items, config.snapshots_dir, " | ".join(query_list), status,
            "\n".join(errors), segments, preset,
            metadata={"campaign_id": campaign_id, "track": track,
                      "observed_at": started.isoformat(), "campaign_config": safe_config},
        )
        summary = store.save_campaign_items(
            campaign_id, account, track, campaign_items, status=status,
            snapshot_path=str(snapshot_path), error="\n".join(errors),
            error_count=len(failed_segments) + len(detail_errors),
        )
        score_counts = {"fit": 0, "maybe": 0, "skip": 0}
        for item in campaign_items:
            bucket = campaign_fit_bucket(
                str(item.get("filter_status") or "unknown"),
                str(item.get("score_decision") or "unknown"),
            )
            score_counts[bucket] += 1
        return {
            "campaign_id": campaign_id, "previous_campaign_id": previous_campaign_id,
            "account": account, "track": track, "status": status, "snapshot": str(snapshot_path),
            "query_count": len(query_list), "segment_count": len(segments),
            "segment_errors": errors, "summary": summary, "score_counts": score_counts,
            "top": store.list_campaign_vacancies(account, track, "top", limit),
        }
    except BaseException as exc:
        store.fail_campaign(campaign_id, f"{type(exc).__name__}: {exc}")
        raise


def write_approved_input(config: AppConfig, track: str, account: str,
                         limit: int = 10) -> tuple[Path | None, list[dict[str, Any]]]:
    """Создаёт приватный apply snapshot только из одобренных и ещё не отправленных строк."""
    rows = Store(config.db_path).approved_campaign_items(account, track, limit)
    if not rows:
        return None, []
    path = config.snapshots_dir / f"approved-apply-{track}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 3, "source": "hh.ru", "status": "ok",
               "query": f"approved-queue:{track}", "items": rows}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, rows
