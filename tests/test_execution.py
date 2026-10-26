"""Campaign acceptance scenarios with real SQLite receipt ledgers, no external submits."""
import json
import uuid
from pathlib import Path

import pytest

from applypilot.cli import build_parser
from applypilot.config import AppConfig, application_limits
from applypilot.execution import cycle_state, run_go
from applypilot.storage import Store


def config(tmp_path):
    root = tmp_path
    (root / "private/config").mkdir(parents=True)
    (root / "private/config/operating-policy.toml").write_text(
        '[policy]\nlive_apply = true\nactive_tracks = ["first", "second"]\n')
    (root / "private/config/tracks.toml").write_text(
        '[[track]]\nkey = "first"\n[[track]]\nkey = "second"\n')
    return AppConfig.discover(root)


def args(*values):
    return build_parser().parse_args(["go", *values])


def fake_runner(config, *, interrupted=False, first_empty=False):
    store = Store(config.db_path)
    calls = []
    sent = set()

    def main(argv):
        parsed = build_parser().parse_args(argv)
        calls.append(parsed)
        run_id = uuid.uuid4().hex
        source = parsed.source[0]
        store.execution_run(parsed.execution_id, run_id, source, parsed.track)
        empty = first_empty and len(calls) == 1
        items = [{"id": str(len(sent) + i), "name": "Example Engineer"}
                 for i in range(0 if empty else parsed.limit)]
        store.start_run(run_id, "example", "apply", Path("synthetic.json"), parsed.limit, items)
        for item in items:
            assert item["id"] not in sent
            sent.add(item["id"])
            if source == "hh.ru":
                store.record(item, "success", "synthetic verified receipt", run_id, "example")
            else:
                # Exercise the source-aware receipt ledger, not a forged report counter.
                with store.connect() as conn:
                    conn.execute("INSERT INTO source_application_attempts "
                                 "(attempt_id,account,source,vacancy_id,track,channel_key,channel_type,"
                                 "status,mode,note,run_id,created_at,updated_at) "
                                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (uuid.uuid4().hex, "example", source, item["id"], parsed.track,
                                  f"{source}:{item['id']}", "native", "success", "live", "receipt",
                                  run_id, "2026-01-01", "2026-01-01"))
        store.finish_run(run_id, "completed")
        if interrupted and len(calls) == 1:
            raise KeyboardInterrupt
        report = {
            "sources": [source], "stops": {},
            "scan": {"sources": [{"source": source, "status": "truncated" if empty else "ok"}]},
            "queue": {source: {"ready": 1000}},
            "live": {source: {"summary": {"counts": {"success": len(items)}}}},
        }
        if source != "hh.ru":
            report["live"][source] = {"dispatch": {"summary": {"success": len(items)},
                                                   "send_attempts": len(items)}}
        path = config.data_dir / f"{run_id}.json"
        path.write_text(json.dumps(report))
        print(f"private_report: {path}")
        return 0

    return main, calls, sent


def test_300_confirmed_across_batches_tracks_and_sources(tmp_path):
    cfg = config(tmp_path)
    main, calls, sent = fake_runner(cfg)
    # Old historical successes must not contribute to this campaign.
    store = Store(cfg.db_path)
    store.start_run("historical", "example", "apply", Path("old.json"), 1, [{"id": "old"}])
    store.record({"id": "old"}, "success", "old", "historical", "example")
    assert run_go(cfg, args("--sources", "hh.ru,career.habr.com", "--target", "300",
                            "--target-scope", "total"), main) == 0
    assert len(sent) == 300
    assert [call.limit for call in calls] == [100, 100, 100]
    with store.connect() as conn:
        row = conn.execute("SELECT execution_id,status FROM execution_campaigns").fetchone()
    assert row["status"] == "target_reached"
    assert sum(c.get("success", 0) for c in store.execution_counts(row["execution_id"]).values()) == 300


def test_target_smaller_than_batch_never_overshoots(tmp_path):
    cfg = config(tmp_path)
    main, calls, sent = fake_runner(cfg)
    assert run_go(cfg, args("--sources", "hh.ru", "--target", "7", "--target-scope", "total"), main) == 0
    assert len(sent) == 7 and calls[0].limit == 7 and len(calls) == 1


def test_resume_after_receipts_before_report_keeps_exact_remaining_target(tmp_path):
    cfg = config(tmp_path)
    main, calls, sent = fake_runner(cfg, interrupted=True)
    with pytest.raises(KeyboardInterrupt):
        run_go(cfg, args("--track", "first", "--sources", "hh.ru", "--target", "137",
                         "--target-scope", "total"), main)
    store = Store(cfg.db_path)
    with store.connect() as conn:
        execution_id = conn.execute("SELECT execution_id FROM execution_campaigns").fetchone()[0]
    assert store.execution(execution_id)["status"] == "interrupted"
    assert run_go(cfg, args("--campaign-id", execution_id), main) == 0
    assert len(sent) == 137 and [call.limit for call in calls] == [100, 37]
    assert run_go(cfg, args("--campaign-id", execution_id), main) == 0
    assert len(calls) == 2


def test_empty_batch_with_unfinished_scan_continues(tmp_path):
    cfg = config(tmp_path)
    main, calls, sent = fake_runner(cfg, first_empty=True)
    assert run_go(cfg, args("--track", "first", "--sources", "hh.ru", "--target", "5",
                            "--target-scope", "total"), main) == 0
    assert len(sent) == 5 and len(calls) == 2
    assert calls[1].queue_only is False


def test_per_source_target_is_shared_by_tracks(tmp_path):
    cfg = config(tmp_path)
    main, calls, sent = fake_runner(cfg)
    assert run_go(cfg, args("--sources", "hh.ru,career.habr.com", "--target", "11",
                            "--target-scope", "per-source"), main) == 0
    assert len(sent) == 22 and len(calls) == 2


def test_ambiguous_target_fails_before_any_run(tmp_path):
    cfg = config(tmp_path)
    with pytest.raises(ValueError, match="target-scope"):
        run_go(cfg, args("--target", "100"), lambda _: pytest.fail("must not submit"))


@pytest.mark.parametrize("scan_status,queue,expected", [
    ("truncated", {}, "pending_search"),
    ("ok", {"needs_description": 1}, "pending_enrichment"),
    ("ok", {"needs_description": 1, "enrichment_errors": ["network"]}, "pending_enrichment"),
    ("ok", {"description_deferred": 1}, "user_required"),
    ("ok", {"review": 1}, "user_required"),
    ("ok", {}, "exhausted"),
    ("blocked", {}, "platform_blocked"),
])
def test_stop_reasons_require_evidence(scan_status, queue, expected):
    report = {"scan": {"sources": [{"source": "hh.ru", "status": scan_status}]},
              "queue": {"hh.ru": queue}}
    assert cycle_state(report, "hh.ru") == expected
    assert cycle_state({}, "hh.ru") == "report_unavailable"


def test_no_implicit_daily_cap_and_explicit_budget_is_preserved():
    assert application_limits({}, "hh.ru")["per_day"] == 0
    assert application_limits({}, "geekjob.ru")["per_day"] == 0
    assert application_limits({"source_limits": {"geekjob.ru": {"per_day": 17}}},
                              "geekjob.ru")["per_day"] == 17


def test_archived_restrictions_do_not_override_explicit_current_selection(tmp_path):
    cfg = config(tmp_path)
    (tmp_path / 'private/current').mkdir()
    (tmp_path / 'private/current/STATUS.md').write_text(
        'Historical note: stop after 3; second track forbidden; all work complete.')
    (tmp_path / 'private/config/operating-policy.toml').write_text(
        '[policy]\nlive_apply=true\nactive_tracks=["first"]\n')
    main, calls, sent = fake_runner(cfg)
    assert run_go(cfg, args('--track', 'second', '--sources', 'hh.ru', '--target', '9',
                            '--target-scope', 'total'), main) == 0
    assert len(sent) == 9 and calls[0].track == 'second'


def test_batch_limit_is_not_a_daily_stop():
    report = {'scan': {'sources': [{'status': 'ok'}]},
              'queue': {'geekjob.ru': {'ready': 100}},
              'live': {'geekjob.ru': {'dispatch': {
                  'summary': {'success': 25}, 'send_attempts': 25,
                  'results': [{'status': 'limit_reached', 'reason': 'source per-run limit reached'}]}}}}
    assert cycle_state(report, 'geekjob.ru') == 'pending_queue'
    report['live']['geekjob.ru']['dispatch']['results'][0]['reason'] = 'source per-day limit reached'
    assert cycle_state(report, 'geekjob.ru') == 'configured_budget'


def test_workflow_blocks_are_not_reported_as_supply_exhausted():
    report = {'scan': {'sources': [{'status': 'ok'}]},
              'queue': {'hh.ru': {'ready': 0, 'requires_user': 3}}}
    assert cycle_state(report, 'hh.ru') == 'user_required'


def test_scan_only_continues_pages_and_never_enables_apply(tmp_path):
    cfg = config(tmp_path)
    calls = []

    def main(argv):
        parsed = build_parser().parse_args(argv)
        calls.append(parsed)
        assert not parsed.apply and not parsed.allow_live
        path = cfg.data_dir / f'scan-{len(calls)}.json'
        path.write_text(json.dumps({
            'scan': {'sources': [{'status': 'truncated' if len(calls) == 1 else 'ok'}]},
            'queue': {'hh.ru': {'ready': 200, 'review': 50}}, 'live': {},
        }))
        print(f'private_report: {path}')
        return 0

    assert run_go(cfg, args('--track', 'first', '--sources', 'hh.ru', '--scan-only'), main) == 0
    assert len(calls) == 2
