"""Persistent campaign orchestration. Receipt ledgers, not console totals, count success."""
from __future__ import annotations

import fcntl
import io
import json
import sys
import uuid
from argparse import Namespace
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from .config import AppConfig
from .storage import Store


class _Tee(io.StringIO):
    def __init__(self):
        super().__init__()
        self.output = sys.stdout

    def write(self, value):
        self.output.write(value)
        self.output.flush()
        return super().write(value)


def report_from_output(output: str) -> dict[str, Any]:
    path = next((line.removeprefix("private_report: ").strip()
                 for line in output.splitlines() if line.startswith("private_report: ")), "")
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cycle_state(report: dict[str, Any], source: str, *, scan_only: bool = False) -> str:
    """An empty send batch is not evidence of an exhausted search."""
    if not report:
        return "report_unavailable"
    live = report.get("live", {}).get(source, {})
    summary = live.get("summary") or {}
    run = summary.get("run", {})
    stops = report.get("stops", {})
    if stops.get(source) or stops.get("campaign"):
        return "platform_blocked"
    if "daily budget exhausted" in str(run.get("stop_reason", "")):
        return "configured_budget"
    if run.get("status") in {"stopped_captcha", "session_unavailable", "error"}:
        return "platform_blocked"
    dispatch = live.get("dispatch") or {}
    outcomes = dispatch.get("results", [])
    from .dispatch import ApplyOutcome

    if any(ApplyOutcome(str(row.get("status", "")), str(row.get("note", "")),
                        str(row.get("evidence", ""))).blocks_source for row in outcomes):
        return "platform_blocked"
    if source != "hh.ru" and live and not dispatch and "mirror" not in str(live):
        return "platform_blocked"
    if (dispatch.get("planned_count", 0) and not dispatch.get("send_attempts", 0)
            and any(row.get("status") == "needs_live_validation" for row in outcomes)):
        return "platform_blocked"
    if any(row.get("status") == "limit_reached"
           and "per-day" in str(row.get("reason", "")) for row in outcomes):
        return "configured_budget"
    rows = report.get("scan", {}).get("sources", [])
    if not rows or any(row.get("status") in {"failed", "blocked"} for row in rows):
        return "platform_blocked"
    if any(row.get("status") in {"partial", "truncated"} for row in rows):
        return "pending_search"
    queue = report.get("queue", {}).get(source)
    if queue is None:
        return "report_unavailable"
    if not scan_only and queue.get("ready", 0):
        # Drain independent ready rows before stopping for an unavailable card.
        sent = summary.get("counts", {}).get("success", 0)
        sent += dispatch.get("summary", {}).get("success", 0)
        if sent:
            return "pending_queue"
    if queue.get("needs_description", 0):
        return "pending_enrichment"
    if scan_only:
        return "user_required" if queue.get("description_deferred", 0) else "exhausted"
    if queue.get("ready", 0):
        return "user_required"
    if queue.get("review", 0) or queue.get("requires_user", 0) or queue.get("description_deferred", 0):
        return "user_required"
    return "exhausted"


def run_go(config: AppConfig, args: Namespace, main: Callable[[list[str]], int]) -> int:
    from .providers import default_registry
    from .tracks import load_tracks

    store = Store(config.db_path)
    # Serialize go invocations while allowing the existing individual runner locks.
    with (config.data_dir / "campaign-execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another campaign is running") from exc
        return _run_locked(config, args, main, store, default_registry(), load_tracks(config.root))


def _run_locked(config, args, main, store, registry, roster) -> int:
    policy = config.load_policy()
    execution_id = args.campaign_id or uuid.uuid4().hex
    previous = store.execution(execution_id) if args.campaign_id else None
    if args.campaign_id and previous is None:
        raise ValueError(f"unknown campaign: {execution_id}")
    if previous:
        saved = previous["spec"]
        # Saved CLI filters and target are stable; profile facts and mutable
        # defaults are loaded again and recorded in each cycle's effective report.
        values = dict(saved["args"])
        for field in ("query", "area", "salary_from", "remote", "preset", "pages",
                      "details_limit", "request_budget", "target_scope"):
            value = getattr(args, field, None)
            if value is not None and value != values.get(field):
                raise ValueError(f"campaign already has a saved {field}; start a new campaign to change CLI filters")
        if args.target and args.target != values.get("target"):
            raise ValueError("campaign already has a saved target")
        if args.track and args.track != values.get("track"):
            raise ValueError("campaign already has saved tracks")
        if args.sources != "all" and args.sources != values.get("sources"):
            raise ValueError("campaign already has saved sources")
        values.update(campaign_id=execution_id, max_cycles=args.max_cycles,
                      visible=(args.visible if args.visible or args.headless else values.get("visible", False)))
        args = Namespace(**values)
        tracks, sources = saved["tracks"], saved["sources"]
    else:
        active = list(policy.get("active_tracks", []))
        tracks = active if not args.track or args.track == "all" else [args.track]
        sources = ([*registry.sources()] if args.sources == "all" else
                   [value.strip() for value in args.sources.split(",") if value.strip()])
        sources = list(dict.fromkeys([*sources, *(args.source or [])]))
        if args.target and args.target_scope is None:
            raise ValueError("--target requires --target-scope total or per-source; clarify the owner's intent")
    if not tracks or any(track not in roster or not roster[track].get("enabled") for track in tracks):
        raise ValueError("choose enabled tracks before starting the campaign")
    if not sources or any(source not in registry.sources() for source in sources):
        raise ValueError("unknown or empty sources")
    if args.target and args.target_scope == "per-source" and "dreamjob.ru" in sources:
        raise ValueError("DreamJob is an HH mirror; choose native sources for per-source targets")
    if not 1 <= args.limit <= 100 or not 1 <= args.max_cycles <= 1000 or args.target < 0:
        raise ValueError("limit must be 1..100, max-cycles 1..1000, target non-negative")
    if args.scan_only and args.target:
        raise ValueError("--target is meaningful only for a live campaign")
    if not args.scan_only and policy.get("live_apply") is not True:
        raise ValueError("operating policy does not grant live_apply")
    spec = {"args": vars(args), "tracks": tracks, "sources": sources}
    if not previous:
        # Convert optional Path arguments before persisting the contract.
        spec = json.loads(json.dumps(spec, default=str))
        store.create_execution(execution_id, spec)
    progress = previous["progress"] if previous else {}
    states = progress.get("states", {})
    # Recheck environmental/user blockers on explicit resume; preserve proven exhaustion.
    if previous:
        states = {k: v for k, v in states.items() if v == "exhausted"}
    print(f"campaign_id: {execution_id}", flush=True)
    common = ["--data-dir", str(config.data_dir)]
    for flag in ("profile", "search"):
        if getattr(args, flag, None):
            common.extend((f"--{flag}", str(getattr(args, flag))))
    cycles = 1 if args.once else args.max_cycles
    status = "technical_budget"
    try:
        for cycle in range(cycles):
            did_work = False
            for track in tracks:
                for source in sources:
                    key = f"{track}:{source}"
                    if any(k.endswith(":" + source) and v in {"platform_blocked", "configured_budget"}
                           for k, v in states.items()):
                        continue
                    if states.get(key) in {"exhausted", "platform_blocked", "user_required", "report_unavailable", "configured_budget"}:
                        continue
                    counts = store.execution_counts(execution_id)
                    successes = {s: c.get("success", 0) for s, c in counts.items()}
                    confirmed = sum(successes.values())
                    relevant = confirmed if args.target_scope != "per-source" else successes.get(source, 0)
                    remaining = args.target - relevant if args.target else args.limit
                    if args.target and remaining <= 0:
                        continue
                    budget = min(args.limit, remaining)
                    argv = [*common, "run", "campaign", "--track", track, "--source", source,
                            "--days", str(args.days), "--limit", str(budget),
                            "--until-complete", "--resume", "--max-passes", str(args.max_passes),
                            "--execution-id", execution_id]
                    if states.get(key) in {"pending_enrichment", "pending_queue"}:
                        argv.append("--queue-only")
                    if not args.scan_only:
                        argv.extend(("--apply", "--allow-live"))
                    argv.append("--visible" if args.visible else "--headless")
                    for query in args.query or []:
                        argv.extend(("--query", query))
                    for area in args.area or []:
                        argv.extend(("--area", str(area)))
                    for name in ("salary_from", "pages", "details_limit", "preset", "request_budget"):
                        value = getattr(args, name, None)
                        if value is not None:
                            argv.extend(("--" + name.replace("_", "-"), str(value)))
                    if args.remote is not None:
                        argv.append("--remote" if args.remote else "--no-remote")
                    buffer = _Tee()
                    with redirect_stdout(buffer):
                        code = main(argv)
                    output = buffer.getvalue()
                    report = report_from_output(output)
                    states[key] = cycle_state(report, source, scan_only=args.scan_only)
                    progress = {"states": states, "counts": store.execution_counts(execution_id),
                                "last_report": report, "last_exit_code": code,
                                "cycles": int(progress.get("cycles", 0)) + 1}
                    store.update_execution(execution_id, "running", states[key], progress)
                    did_work = True
            counts = store.execution_counts(execution_id)
            successes = {s: c.get("success", 0) for s, c in counts.items()}
            reached = bool(args.target) and (
                sum(successes.values()) >= args.target if args.target_scope != "per-source" else
                all(successes.get(source, 0) >= args.target for source in sources))
            if reached:
                status = "target_reached"
                break
            if args.scan_only and not any(v.startswith("pending_") for v in states.values()):
                status = "scan_complete" if all(v == "exhausted" for v in states.values()) else "scan_partial"
                break
            if not did_work or not any(v.startswith("pending_") for v in states.values()):
                status = ("report_unavailable" if "report_unavailable" in states.values() else
                          "platform_blocked" if "platform_blocked" in states.values() else
                          "configured_budget" if "configured_budget" in states.values() else
                          "user_required" if "user_required" in states.values() else "exhausted")
                break
            print(f"campaign cycle {cycle + 1}: confirmed={sum(successes.values())}; {states}", flush=True)
    except BaseException:
        store.update_execution(execution_id, "interrupted", "resume with --campaign-id",
                               {**progress, "states": states, "counts": store.execution_counts(execution_id)})
        raise
    if args.scan_only and status == "technical_budget":
        status = "scan_partial"
    counts = store.execution_counts(execution_id)
    confirmed = sum(c.get("success", 0) for c in counts.values())
    progress.update(states=states, counts=counts, confirmed=confirmed,
                    funnel=store.execution_funnel(execution_id))
    store.update_execution(execution_id, status, status, progress)
    print(f"campaign result: {status}; new_confirmed={confirmed}; target={args.target}; "
          f"scope={args.target_scope or 'unbounded'}; counts={json.dumps(counts, ensure_ascii=False)}")
    print(f"campaign funnel: {json.dumps(progress['funnel'], ensure_ascii=False)}")
    return 0 if status in {"target_reached", "scan_complete"} or (status == "exhausted" and not args.target) else 2
