"""Short source passes, source isolation and resumable cursor ordering."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from applypilot.cli import build_parser
from applypilot.config import AppConfig
from applypilot.execution import run_go
from applypilot.multisource import (
    CampaignFatalError,
    _commit_deferred_cursors,
    scan_sources,
)
from applypilot.providers.getmatch import _persist_seen
from applypilot.storage import Store


def _config(tmp_path: Path) -> AppConfig:
    (tmp_path / "private/config").mkdir(parents=True)
    (tmp_path / "private/config/operating-policy.toml").write_text(
        '[policy]\nlive_apply=true\nactive_tracks=["first"]\n')
    (tmp_path / "private/config/tracks.toml").write_text(
        '[[track]]\nkey="first"\n')
    return AppConfig.discover(tmp_path)


def _args(*values: str):
    return build_parser().parse_args(["go", *values])


def _fake_runner(config: AppConfig):
    store = Store(config.db_path)
    calls = []
    sent = set()

    def main(argv: list[str]) -> int:
        parsed = build_parser().parse_args(argv)
        calls.append(parsed)
        run_id = uuid.uuid4().hex
        source = parsed.source[0]
        store.execution_run(parsed.execution_id, run_id, source, parsed.track)
        item = {"id": f"{source}-{len(sent)}", "name": "AI Engineer"}
        store.start_run(run_id, "example", "apply", Path("synthetic.json"), 1, [item])
        if source == "hh.ru":
            store.record(item, "success", "synthetic receipt", run_id, "example")
        else:
            with store.connect() as conn:
                conn.execute(
                    "INSERT INTO source_application_attempts "
                    "(attempt_id,account,source,vacancy_id,track,channel_key,channel_type,"
                    "status,mode,note,run_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "example", source, item["id"], parsed.track,
                     f"{source}:{item['id']}", "native", "success", "live", "receipt",
                     run_id, "2026-01-01", "2026-01-01"),
                )
        store.finish_run(run_id, "completed")
        report = {
            "sources": [source], "stops": {},
            "scan": {"sources": [{"source": source, "status": "truncated",
                                  "items": 1, "errors": []}]},
            "queue": {source: {"ready": 1000}},
            "live": {source: {
                "summary": {"success": 1},
                "dispatch": {"summary": {"success": 1}, "send_attempts": 1},
            }},
        }
        path = config.data_dir / f"{run_id}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        print(f"private_report: {path}")
        return 0

    return main, calls, sent


def test_round_robin_moves_to_next_source_after_each_bounded_pass(tmp_path: Path):
    config = _config(tmp_path)
    main, calls, _ = _fake_runner(config)
    assert run_go(config, _args(
        "--track", "first", "--sources", "getmatch.ru,geekjob.ru",
        "--target", "2", "--target-scope", "total", "--once",
        "--pass-request-budget", "11", "--pass-details-limit", "7",
    ), main) == 0
    assert [call.source[0] for call in calls] == ["getmatch.ru", "geekjob.ru"]
    assert all(call.request_budget == 11 for call in calls)
    assert all(call.details_limit == 7 for call in calls)


def test_one_source_exception_does_not_abort_other_sources(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)

    def fake_impl(_config, _track, _profile, _search, sources=None, **_kwargs):
        source = sources[0]
        if source == "career.habr.com":
            raise RuntimeError("provider exploded")
        return {"sources": [{"source": source, "status": "ok", "items": 3,
                             "errors": [], "account": "example"}]}

    monkeypatch.setattr("applypilot.multisource._scan_sources_impl", fake_impl)
    result = scan_sources(config, "first", {"account": "example"}, {},
                          sources=["career.habr.com", "geekjob.ru"],
                          source_retries=0)
    assert [row["source"] for row in result["sources"]] == ["career.habr.com", "geekjob.ru"]
    assert result["sources"][0]["status"] == "failed"
    assert result["sources"][1]["items"] == 3


def test_transient_source_failure_is_retried_but_block_is_not(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    attempts = {"geekjob.ru": 0, "career.habr.com": 0}

    def fake_impl(_config, _track, _profile, _search, sources=None, **_kwargs):
        source = sources[0]
        attempts[source] += 1
        if source == "geekjob.ru" and attempts[source] == 1:
            return {"sources": [{"source": source, "status": "failed",
                                 "errors": ["network error"], "items": 0}]}
        if source == "career.habr.com":
            return {"sources": [{"source": source, "status": "blocked",
                                 "errors": ["CAPTCHA"], "items": 0}]}
        return {"sources": [{"source": source, "status": "ok", "items": 1,
                             "errors": [], "account": "example"}]}

    monkeypatch.setattr("applypilot.multisource._scan_sources_impl", fake_impl)
    result = scan_sources(config, "first", {"account": "example"}, {},
                          sources=["geekjob.ru", "career.habr.com"],
                          source_retries=2, retry_delay=0)
    assert attempts["geekjob.ru"] == 2
    assert attempts["career.habr.com"] == 1
    assert result["sources"][0]["status"] == "ok"
    assert result["sources"][1]["status"] == "blocked"


def test_database_contract_failure_stops_campaign(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)

    def fake_impl(*_args, **_kwargs):
        raise CampaignFatalError("database is broken")

    monkeypatch.setattr("applypilot.multisource._scan_sources_impl", fake_impl)
    with pytest.raises(CampaignFatalError):
        scan_sources(config, "first", {"account": "example"}, {}, sources=["hh.ru"])


def test_getmatch_cursor_is_pending_until_cards_are_committed(tmp_path: Path):
    state = tmp_path / "sitemap_seen.json"
    _persist_seen(state, {"100"}, {"https://getmatch.ru/vacancies/100-x"}, {
        "defer_cursor_persist": True,
    })
    pending = state.with_name(state.name + ".pending")
    assert pending.is_file()
    assert not state.exists()
    assert _commit_deferred_cursors({"state_path": str(state)}) == [str(state)]
    assert state.is_file()
    assert not pending.exists()


@pytest.mark.parametrize("ready_first", [False, True])
@pytest.mark.parametrize("blocked_state", ["platform_blocked", "configured_budget"])
def test_go_continues_other_sources_after_source_stop(tmp_path, monkeypatch, ready_first, blocked_state):
    config = _config(tmp_path)
    main, calls, _ = _fake_runner(config)
    seen = []
    monkeypatch.setattr(
        Store, "has_sendable_ready_any_account",
        lambda self, track, source: ready_first and source == "getmatch.ru",
    )

    def runner(argv):
        parsed = build_parser().parse_args(argv)
        source = parsed.source[0]
        seen.append(source)
        if source != "getmatch.ru":
            return main(argv)
        report = {
            "stops": {source: "CAPTCHA"} if blocked_state == "platform_blocked" else {},
            "scan": {"sources": [{"source": source, "status": "ok"}]},
            "queue": {source: {}},
            "live": {source: {"summary": {"run": {
                "stop_reason": "daily budget exhausted" if blocked_state == "configured_budget" else "",
            }}}},
        }
        path = config.data_dir / "blocked.json"
        path.write_text(json.dumps(report))
        print(f"private_report: {path}")
        return 2

    run_go(config, _args(
        "--track", "first", "--sources", "getmatch.ru,geekjob.ru", "--once",
    ), runner)
    assert seen == ["getmatch.ru", "geekjob.ru"]
    assert len(calls) == 1


def test_go_preserves_explicit_salary_filter_in_child_run(tmp_path):
    config = _config(tmp_path)
    main, calls, _ = _fake_runner(config)
    run_go(config, _args(
        "--track", "first", "--sources", "hh.ru", "--salary-from", "180000", "--once",
    ), main)
    assert calls[0].salary_from == 180000


def test_per_source_target_does_not_skip_later_sources(tmp_path):
    config = _config(tmp_path)
    main, calls, _ = _fake_runner(config)
    assert run_go(config, _args(
        "--track", "first", "--sources", "getmatch.ru,geekjob.ru",
        "--target", "1", "--target-scope", "per-source", "--once",
    ), main) == 0
    assert [call.source[0] for call in calls] == ["getmatch.ru", "geekjob.ru"]
