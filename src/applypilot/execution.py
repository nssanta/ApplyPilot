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
    if run.get("status") in {"stopped_captcha", "session_unavailable"}:
        return "platform_blocked"
    if run.get("status") == "error":
        return "campaign_error"
    dispatch = live.get("dispatch") or {}
    outcomes = dispatch.get("results", [])
    from .dispatch import ApplyOutcome

    if any(ApplyOutcome(str(row.get("status", "")),
                        str(row.get("note") or row.get("reason", "")),
                        str(row.get("evidence", ""))).blocks_source for row in outcomes):
        return "platform_blocked"
    if any(row.get("status") == "wait_source" for row in outcomes):
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
    if not rows:
        return "campaign_error"
    scan_errors = " ".join(
        str(error) for row in rows for error in row.get("errors", [])
    ).casefold()
    if any(row.get("status") in {"blocked", "captcha"} for row in rows):
        return "platform_blocked"
    if any(row.get("status") == "failed" for row in rows):
        from .multisource import classify_source_error

        return ("platform_blocked" if classify_source_error(scan_errors) == "blocked"
                else "source_unavailable")
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
        if args.days is not None and not 1 <= args.days <= 365:
            raise ValueError("days must be 1..365")
        # Daily freshness is an operator-controlled scan window, not a new
        # application campaign. Keep the target, receipts and source roster.
        changed_days = args.days is not None and args.days != values.get("days")
        values.update(campaign_id=execution_id, max_cycles=args.max_cycles,
                      days=(args.days if args.days is not None else values.get("days", 30)),
                      queue_only=bool(getattr(args, "queue_only", False) or values.get("queue_only", False)),
                      visible=(args.visible if args.visible or args.headless else values.get("visible", False)))
        if changed_days:
            store.update_execution_spec(execution_id, {**saved, "args": values})
        args = Namespace(**values)
        tracks, sources = saved["tracks"], saved["sources"]
    else:
        if args.days is None:
            args.days = 30
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
    if args.scan_only and getattr(args, "queue_only", False):
        raise ValueError("--scan-only and --queue-only are incompatible")
    if not args.scan_only and policy.get("live_apply") is not True:
        raise ValueError("operating policy does not grant live_apply")
    pass_request_value = getattr(args, "pass_request_budget", None)
    pass_request_budget = max(1, min(10000, int(
        pass_request_value if pass_request_value is not None else 40
    )))
    pass_details_value = getattr(args, "pass_details_limit", None)
    pass_details_limit = max(0, min(1000, int(
        pass_details_value if pass_details_value is not None else 20
    )))
    pass_seconds = max(0.0, min(1800.0, float(getattr(args, "pass_seconds", 0) or 240)))
    source_retries = max(0, min(5, int(getattr(args, "source_retries", 2) or 0)))
    retry_delay = max(0.0, min(60.0, float(getattr(args, "retry_delay", 5) or 0)))
    if not previous:
        spec = {"args": vars(args), "tracks": tracks, "sources": sources}
        # Convert optional Path arguments before persisting the contract.
        spec = json.loads(json.dumps(spec, default=str))
        store.create_execution(execution_id, spec)
    else:
        # The campaign lock is held, so a previous `running` row cannot still
        # have a live writer. Quarantine any interrupted submit before resume.
        recovered = store.recover_abandoned_execution(execution_id)
        if recovered.get("recovered"):
            print(f"campaign recovery: {json.dumps(recovered, ensure_ascii=False)}", flush=True)
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
    sticky_queue_only = bool(getattr(args, "queue_only", False))
    transient_failures: dict[str, int] = {}
    source_reasons: dict[str, str] = {}
    terminal_states = {
        "platform_blocked", "configured_budget", "campaign_error",
        "source_unavailable_exhausted", "report_unavailable",
    }
    source_order = ["hh.ru", "career.habr.com", "getmatch.ru", "geekjob.ru",
                    "dreamjob.ru", "yandex.ru/jobs"]
    sources = sorted(sources, key=lambda value: source_order.index(value)
                     if value in source_order else len(source_order))

    def remaining_for(source: str) -> int:
        counts = store.execution_counts(execution_id)
        successes = {key: value.get("success", 0) for key, value in counts.items()}
        relevant = (sum(successes.values()) if args.target_scope != "per-source"
                    else successes.get(source, 0))
        return (args.target - relevant) if args.target else args.limit

    def run_child(track: str, source: str, budget: int, *, queue_only: bool) -> tuple[int, dict[str, Any], str]:
        argv = [*common, "run", "campaign", "--track", track, "--source", source,
                "--days", str(args.days), "--limit", str(budget),
                "--execution-id", execution_id]
        if not queue_only:
            argv.extend(("--until-complete", "--resume",
                         "--max-passes", str(min(2, max(1, int(getattr(args, "max_passes", 1) or 1)))),
                         "--request-budget", str(pass_request_budget),
                         "--details-limit", str(pass_details_limit),
                         "--source-retries", str(source_retries),
                         "--retry-delay", str(retry_delay)))
            if pass_seconds:
                argv.extend(("--pass-seconds", str(pass_seconds)))
        if queue_only:
            argv.append("--queue-only")
        if not args.scan_only:
            argv.extend(("--apply", "--allow-live"))
        argv.append("--visible" if args.visible else "--headless")
        for query in getattr(args, "query", None) or []:
            argv.extend(("--query", query))
        for area in getattr(args, "area", None) or []:
            argv.extend(("--area", str(area)))
        if getattr(args, "pages", None) is not None:
            argv.extend(("--pages", str(args.pages)))
        if getattr(args, "salary_from", None) is not None:
            argv.extend(("--salary-from", str(args.salary_from)))
        if getattr(args, "preset", None):
            argv.extend(("--preset", str(args.preset)))
        if getattr(args, "remote", None) is not None:
            argv.append("--remote" if args.remote else "--no-remote")
        buffer = _Tee()
        with redirect_stdout(buffer):
            code = main(argv)
        output = buffer.getvalue()
        report = report_from_output(output)
        return code, report, output

    def update_state(track: str, source: str, code: int, report: dict[str, Any]) -> str:
        key = f"{track}:{source}"
        state = cycle_state(report, source, scan_only=args.scan_only)
        stop_reason = str((report.get("stops") or {}).get(source) or "")
        scan_rows = (report.get("scan") or {}).get("sources") or []
        scan_reason = " ".join(
            str(error) for row in scan_rows if row.get("source") == source
            for error in row.get("errors", [])
        )
        if stop_reason or scan_reason:
            source_reasons[source] = (stop_reason or scan_reason)[:500]
        if state == "source_unavailable":
            transient_failures[key] = transient_failures.get(key, 0) + 1
            if transient_failures[key] > source_retries:
                state = "source_unavailable_exhausted"
        elif state not in {"exhausted", "user_required"}:
            transient_failures[key] = 0
        states[key] = state
        nonlocal progress
        progress = {"states": states, "counts": store.execution_counts(execution_id),
                    "last_report": report, "last_exit_code": code,
                    "source_reasons": source_reasons,
                    "cycles": int(progress.get("cycles", 0)) + 1}
        store.update_execution(execution_id, "running", state, progress)
        return state

    try:
        for cycle in range(cycles):
            did_work = False
            stop_round = False
            for track in tracks:
                for source in sources:
                    key = f"{track}:{source}"
                    if any(k.endswith(":" + source) and v in {
                        "platform_blocked", "configured_budget", "campaign_error",
                        "source_unavailable_exhausted",
                    } for k, v in states.items()):
                        continue
                    if states.get(key) in terminal_states or states.get(key) == "exhausted":
                        continue
                    if args.target and remaining_for(source) <= 0:
                        continue
                    # 1. Send already verified READY rows before any new scan.
                    if (not getattr(args, "scan_only", False)
                            and (sticky_queue_only
                                 or store.has_sendable_ready_any_account(track, source))):
                        budget = max(1, min(args.limit, remaining_for(source)))
                        code, report, _ = run_child(track, source, budget, queue_only=True)
                        did_work = True
                        state = update_state(track, source, code, report)
                        if not report:
                            states[key] = "campaign_error"
                            stop_round = True
                            break
                        if state == "campaign_error":
                            stop_round = True
                            break
                        if state in {"platform_blocked", "configured_budget"}:
                            continue
                        if args.target and remaining_for(source) <= 0:
                            if args.target_scope == "per-source":
                                continue
                            stop_round = True
                            break
                    if sticky_queue_only or (getattr(args, "scan_only", False)
                                             and states.get(key) == "exhausted"):
                        continue
                    if sticky_queue_only:
                        continue
                    # 2. One bounded discovery/enrichment pass, then its own
                    #    READY sends. No provider gets to finish the whole market.
                    budget = max(1, min(args.limit, remaining_for(source)))
                    code, report, _ = run_child(track, source, budget, queue_only=False)
                    did_work = True
                    state = update_state(track, source, code, report)
                    if not report:
                        states[key] = "campaign_error"
                        stop_round = True
                        break
                    if state in {"campaign_error", "report_unavailable"}:
                        stop_round = True
                        break
                    if args.target and remaining_for(source) <= 0:
                        if args.target_scope == "per-source":
                            continue
                        stop_round = True
                        break
                if stop_round:
                    break
            counts = store.execution_counts(execution_id)
            successes = {s: c.get("success", 0) for s, c in counts.items()}
            reached = bool(args.target) and (
                sum(successes.values()) >= args.target if args.target_scope != "per-source" else
                all(successes.get(source, 0) >= args.target for source in sources))
            if reached:
                status = "target_reached"
                break
            if "campaign_error" in states.values() or "report_unavailable" in states.values():
                status = "campaign_error"
                break
            pending = any(v.startswith("pending_") or v == "source_unavailable"
                          for v in states.values())
            if args.scan_only and not pending:
                status = ("scan_complete" if states and all(v == "exhausted" for v in states.values())
                          else "scan_partial")
                break
            if not did_work or not pending:
                if "campaign_error" in states.values() or "report_unavailable" in states.values():
                    status = "campaign_error"
                elif "platform_blocked" in states.values():
                    status = "platform_blocked"
                elif "configured_budget" in states.values():
                    status = "configured_budget"
                elif "source_unavailable_exhausted" in states.values() or "source_unavailable" in states.values():
                    status = "source_unavailable"
                elif "user_required" in states.values():
                    status = "user_required"
                else:
                    status = "exhausted"
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
                    source_reasons=source_reasons,
                    funnel=store.execution_funnel(execution_id))
    store.update_execution(execution_id, status, status, progress)
    print(f"campaign result: {status}; new_confirmed={confirmed}; target={args.target}; "
          f"scope={args.target_scope or 'unbounded'}; counts={json.dumps(counts, ensure_ascii=False)}")
    print(f"campaign funnel: {json.dumps(progress['funnel'], ensure_ascii=False)}")
    print(f"campaign states: {json.dumps(states, ensure_ascii=False, sort_keys=True)}")
    if source_reasons:
        print(f"campaign source reasons: {json.dumps(source_reasons, ensure_ascii=False, sort_keys=True)}")
    return 0 if status in {"target_reached", "scan_complete"} or (status == "exhausted" and not args.target) else 2
