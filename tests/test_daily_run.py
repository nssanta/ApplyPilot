from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from applypilot import daily_run
from applypilot.decision import decide_row


def _config(tmp_path: Path):
    return SimpleNamespace(root=tmp_path, db_path=tmp_path / "db.sqlite3")


def _patch(monkeypatch, confirmed: list[int]):
    monkeypatch.setattr(daily_run, "_confirmed", lambda config: confirmed[0])
    monkeypatch.setattr(daily_run, "print_board", lambda *a, **k: 0)


def test_run_orders_stages_and_stops_on_captcha(tmp_path, monkeypatch):
    confirmed = [0]
    _patch(monkeypatch, confirmed)
    calls: list[list[str]] = []

    def call(argv):
        calls.append(argv)
        if argv[:2] == ["queue", "apply"]:
            confirmed[0] += 3
            return 5
        return 0

    args = Namespace(limit=10, track=["backend", "ai"], source=["hh.ru"], scan=False, dry_run=False)
    assert daily_run.run_cycle(_config(tmp_path), args, call) == 0

    assert calls[0][0] == "sync"
    assert calls[1] == ["queue", "unknowns", "--release"]
    applies = [argv for argv in calls if argv[:2] == ["queue", "apply"]]
    assert len(applies) == 1 and applies[0][3] == "backend" and "--visible" in applies[0]


def test_run_respects_limit_across_batches(tmp_path, monkeypatch):
    confirmed = [0]
    _patch(monkeypatch, confirmed)
    limits: list[str] = []

    def call(argv):
        if argv[:2] == ["queue", "apply"]:
            limits.append(argv[argv.index("--limit") + 1])
            confirmed[0] += int(limits[-1])
        return 0

    args = Namespace(limit=150, track=["ai"], source=["hh.ru"], scan=False, dry_run=False)
    daily_run.run_cycle(_config(tmp_path), args, call)

    assert limits == ["100", "50"]


def test_dry_run_never_sends_or_releases(tmp_path, monkeypatch):
    _patch(monkeypatch, [0])
    calls: list[list[str]] = []
    args = Namespace(limit=5, track=["ai"], source=["hh.ru"], scan=False, dry_run=True)

    daily_run.run_cycle(_config(tmp_path), args, lambda argv: calls.append(argv) or 0)

    assert calls == [["queue", "sweep", "--track", "ai", "--source", "hh.ru"]]


def _aged_row(last_seen: str) -> dict:
    return {
        "score_decision": "pass", "filter_status": "reject", "score": 80,
        "filter_reasons": ["remote:verified", "publication:outside_requested_window"],
        "description_status": "ok", "description": "Python", "last_seen_at": last_seen,
        "name": "Python Developer",
    }


def test_age_alone_does_not_reject_a_vacancy_still_seen():
    from datetime import UTC, datetime

    decision = decide_row(_aged_row(datetime.now(UTC).isoformat()))
    assert decision.reason_code != "score_or_filter_reject"


def test_long_unseen_old_vacancy_stays_rejected():
    assert decide_row(_aged_row("2026-01-01T00:00:00+00:00")).reason_code == "score_or_filter_reject"
