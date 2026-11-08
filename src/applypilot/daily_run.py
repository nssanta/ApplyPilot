"""One daily command over the existing stages, plus a readable queue board.

Every stage stays an ordinary CLI command with its own guards: this module only
orders them, so scan, sweep and the clickers keep a single implementation.
"""
from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import AppConfig
from .providers import default_registry
from .storage import Store

HH = "hh.ru"
ENABLE_FLAGS = {
    "career.habr.com": "--enable-habr",
    "getmatch.ru": "--enable-getmatch",
    "geekjob.ru": "--enable-geekjob",
    "yandex.ru/jobs": "--enable-yandex",
}
SCORE_BANDS = ((70, "70+"), (45, "45-69"), (0, "<45"))
MAX_PASSES = 20


def active_tracks(config: AppConfig) -> list[str]:
    """Backend goes first: Python roles use that resume and HH dedup is shared."""
    policy = config.load_policy()
    section = policy.get("policy", policy)
    tracks = [str(value) for value in section.get("active_tracks", [])]
    return sorted(tracks or ["ai"], key=lambda value: value != "backend")


def _sources(requested: Iterable[str]) -> list[str]:
    chosen = list(requested)
    if chosen:
        return chosen
    return [source for source in default_registry().sources()
            if source == HH or source in ENABLE_FLAGS]


def _band(score: int) -> str:
    return next(label for floor, label in SCORE_BANDS if score >= floor)


def board(config: AppConfig, tracks: Iterable[str], days: int = 1) -> dict[str, Any]:
    """Read-only summary per track and source, built from the same rows as the queue."""
    from .cli import _account, _profile, _track_config, effective_search
    from .multisource import resolve_source_account
    from .queue_actions import next_action

    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    report: dict[str, Any] = {"new_since": cutoff, "tracks": {}}
    for track in tracks:
        selected, _info = _track_config(config, track, profile_override=False, search_override=False)
        profile = _profile(selected, effective_search(selected.load_search()))
        store = Store(selected.db_path)
        report["tracks"][track] = {}
        for source in default_registry().sources():
            account = _account(profile) if source == HH else resolve_source_account(selected, profile, source)
            rows = store.list_campaign_vacancies(account, track, "all", 5000, source=source)
            if not rows:
                continue
            ready = {str(item.get("id")) for item in store.approved_campaign_items(
                account, track, 100, source=source,
            )}
            new = [row for row in rows if str(row.get("first_seen_at") or "") >= cutoff]
            actions = Counter(next_action(row)["next_action"] for row in rows)
            scores = Counter(_band(int(row.get("score") or 0)) for row in new)
            report["tracks"][track][source] = {
                "total": len(rows), "new": len(new), "old": len(rows) - len(new),
                "new_scores": {label: scores.get(label, 0) for _floor, label in SCORE_BANDS},
                "ready": len(ready),
                "review": sum(str(row.get("decision") or "") == "REVIEW" for row in rows),
                "unknown": actions.get("verify_unknown", 0),
            }
    return report


def print_board(config: AppConfig, tracks: Iterable[str], days: int = 1, as_json: bool = False) -> int:
    report = board(config, tracks, days)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    for track, sources in report["tracks"].items():
        print(f"track {track}:")
        for source, row in sources.items():
            scores = " ".join(f"{label}={count}" for label, count in row["new_scores"].items())
            print(f"  {source}: new={row['new']} ({scores}) old={row['old']} "
                  f"ready={row['ready']} review={row['review']} unknown={row['unknown']}")
    return 0


def _confirmed(config: AppConfig) -> int:
    day = datetime.now(UTC).date().isoformat()
    outcomes = Store(config.db_path).day_outcomes(day)
    return sum(values.get("success", 0) for values in outcomes.values())


def _send_hh(call: Callable[[list[str]], int], config: AppConfig, track: str, budget: int) -> str:
    """Sends in clicker-sized batches until the budget, queue or CAPTCHA stops it."""
    sent = 0
    for _attempt in range(MAX_PASSES):
        if sent >= budget:
            return "limit"
        before = _confirmed(config)
        code = call(["queue", "apply", "--track", track, "--source", HH,
                     "--limit", str(min(100, budget - sent)), "--run", "--visible"])
        gained = _confirmed(config) - before
        sent += gained
        if code == 2:
            return "queue_empty"
        if code == 5:
            return "stopped"
        if gained == 0:
            return "no_progress"
    return "max_passes"


def send_plan(config: AppConfig, tracks: Iterable[str], sources: Iterable[str]) -> list[dict[str, Any]]:
    """The exact rows a live cycle would send, through the clickers' own selectors."""
    from .campaigns import write_approved_input
    from .cli import _account, _permitted_hh_resumes, _profile, _track_config, effective_search
    from .multisource import queue_plan, resolve_source_account

    plan: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for track in tracks:
        selected, info = _track_config(config, track, profile_override=False, search_override=False)
        profile = _profile(selected, effective_search(selected.load_search()))
        store = Store(selected.db_path)
        for source in sources:
            if source == HH:
                permitted = _permitted_hh_resumes(profile, str(info.get("resume") or ""))
                _path, rows = write_approved_input(selected, track, _account(profile), 100)
                rows = [row for row in rows if str(row.get("resume") or "").strip() in permitted]
            else:
                account = resolve_source_account(selected, profile, source)
                rows = queue_plan(store, account, track, source, 100,
                                  verified_driver=source in ENABLE_FLAGS)["planned"]
            for row in rows:
                key = (source, str(row.get("id")))
                if key not in seen:
                    seen.add(key)
                    plan.append({"source": source, "track": track, "id": key[1],
                                 "score": int(row.get("score") or 0),
                                 "title": str(row.get("name") or ""),
                                 "company": str(row.get("company") or "")})
    return plan


def print_plan(plan: list[dict[str, Any]], limit: int) -> None:
    print(f"plan: {len(plan)} ready, this limit sends about {min(limit, len(plan))}")
    for row in plan:
        print(f"  {row['source']} {row['track']} {row['id']} · {row['score']} · "
              f"{row['title'][:70]} — {row['company'][:30]}")
    print("drop any of them: queue skip --id ID --reason TEXT")


def _reconcile(call: Callable[[list[str]], int], track: str) -> None:
    if call(["sync", "--track", track]) == 0:
        call(["queue", "unknowns", "--release"])
    else:
        print("cycle: HH sync failed; UNKNOWN stay quarantined")


def run_cycle(config: AppConfig, args: Namespace, call: Callable[[list[str]], int]) -> int:
    if args.limit < 1:
        print("cycle: --limit must be positive")
        return 2
    tracks = args.track or active_tracks(config)
    sources = _sources(args.source)
    others = [source for source in sources if source != HH]
    if args.scan:
        for track in tracks:
            if HH in sources:
                call(["scan", "--track", track, "--source", HH, "--remote",
                      "--sort-mode", "newest", "--json"])
            if others:
                scan_args = [item for source in others for item in ("--source", source)]
                call(["sources", "scan", "--track", track, *scan_args, "--days", "14", "--json"])
    if HH in sources:
        _reconcile(call, tracks[0])
    for track in tracks:
        for source in sources:
            call(["queue", "sweep", "--track", track, "--source", source])
    print_board(config, tracks)
    if args.dry_run:
        print_plan(send_plan(config, tracks, sources), args.limit)
        return 0
    start = _confirmed(config)
    stops: dict[str, str] = {}
    if HH in sources:
        for track in tracks:
            left = args.limit - (_confirmed(config) - start)
            if left <= 0:
                break
            stops[f"{HH}/{track}"] = _send_hh(call, config, track, left)
            if stops[f"{HH}/{track}"] == "stopped":
                break
    # One source per dispatch: each driver owns a Playwright instance, and two
    # in one call make the later login checks fail.
    for track in tracks:
        for source in others:
            left = args.limit - (_confirmed(config) - start)
            if left <= 0:
                break
            if source not in ENABLE_FLAGS or not send_plan(config, [track], [source]):
                continue
            call(["sources", "dispatch-apply", "--track", track, "--source", source,
                  "--allow-live", ENABLE_FLAGS[source], "--limit", str(min(100, left)), "--json"])
    if HH in sources:
        _reconcile(call, tracks[0])
    print(f"cycle: confirmed this run={_confirmed(config) - start} stops={stops}")
    print_board(config, tracks)
    call(["queue", "today"])
    call(["queue", "unknowns"])
    call(["queue", "forms", "--track", tracks[-1]])
    _weekly_maintenance(config)
    return 0


def _weekly_maintenance(config: AppConfig) -> None:
    """Keep the database small once a week; sessions and cookies are never touched."""
    from .maintenance import maintenance_due, run_maintenance

    try:
        if not maintenance_due(config):
            return
        report = run_maintenance(config)
    except (AttributeError, OSError, ValueError, sqlite3.Error) as exc:  # never fail a finished cycle
        print(f"cycle: maintenance skipped: {exc}")
        return
    database = report.get("database", {})
    print(f"cycle: maintenance snapshots_removed={report['snapshots_removed']} "
          f"db {database.get('size_before', 0) // 2**20}->{database.get('size_after', 0) // 2**20} MiB "
          f"integrity={database.get('integrity')}")
