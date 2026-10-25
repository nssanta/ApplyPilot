from datetime import UTC, datetime
from hashlib import sha256

import pytest

from applypilot.readiness import CHANNELS, readiness_snapshot
from applypilot.storage import Store

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


def test_cli_exposes_readiness_and_review_view():
    from applypilot.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["sources", "readiness", "--track", "test"]).sources_action == "readiness"
    assert parser.parse_args(["queue", "list", "--view", "review"]).view == "review"


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing" / "db.sqlite3"
    assert readiness_snapshot(path, "test", {}, now=NOW)["status"] == "database_missing"
    assert not path.parent.exists()


@pytest.mark.parametrize("status,stamp,fresh", [
    ("ok", "2026-10-25T11:00:00+00:00", True),
    ("truncated", "2026-10-25T11:00:00+00:00", False),
    ("ok", "2026-10-22T11:00:00+00:00", False),
    ("ok", "2026-10-26T11:00:00+00:00", False),
    ("ok", "2026-10-25T11:00:00", False),
])
def test_inventory_preserves_database_and_requires_complete_fresh_sync(tmp_path, status, stamp, fresh):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    with store.connect() as conn:
        conn.execute("""INSERT INTO sync_snapshots(account,source,status,created_at)
                     VALUES('owner','hh.ru',?,?)""", (status, stamp))
        for vid, account, state in [("1", "owner", "READY_TO_APPLY"),
                                    ("2", "owner", "UNKNOWN"),
                                    ("3", "other", "READY_TO_APPLY")]:
            conn.execute("""INSERT INTO vacancies
                (account,source,vacancy_id,application_state,created_at,first_seen_at,last_seen_at,updated_at)
                VALUES(?,'hh.ru',?,?,?, ?,?,?)""", (account, vid, state, stamp, stamp, stamp, stamp))
            conn.execute("""INSERT INTO vacancy_track_scores
                (account,source,vacancy_id,track,review_state,score_decision,filter_status,last_seen_at)
                VALUES(?,'hh.ru',?,'test','APPROVED','pass','pass',?)""", (account, vid, stamp))
    before = sha256(path.read_bytes()).hexdigest()
    result = readiness_snapshot(path, "test", {"hh.ru": "owner"}, now=NOW)
    assert sha256(path.read_bytes()).hexdigest() == before
    assert len(result["sources"]) == len(CHANNELS)
    hh = result["sources"][0]
    assert hh["hh_sync"]["fresh"] is fresh
    assert hh["queue"]["total"] == 2
    assert hh["queue"]["approved_eligible_ready"] == 1
    assert hh["queue"]["unknown"] == 1
    assert "session_not_checked" in hh["gates"]
    assert result["sources"][-1]["gates"] == ["use_hh_channel_no_independent_submission"]
