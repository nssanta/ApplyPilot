import os
import sqlite3
import time
from pathlib import Path

from applypilot.config import AppConfig
from applypilot.maintenance import is_protected, maintenance_due, run_maintenance
from applypilot.storage import Store

DAY = 86400


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(root=tmp_path, data_dir=tmp_path / "data", profile_path=tmp_path / "profile.toml")


def _file(path: Path, age_days: float, now: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    os.utime(path, (now - age_days * DAY, now - age_days * DAY))
    return path


def _observations(db: Path) -> list[tuple[int, str]]:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT id,payload_json FROM vacancy_observations ORDER BY id").fetchall()


def _seed_db(config: AppConfig) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    Store(config.db_path).connect().close()
    with sqlite3.connect(config.db_path) as conn:
        for idx in range(3):
            conn.execute(
                "INSERT INTO vacancy_observations(campaign_id,account,source,vacancy_id,track,change_kind,"
                "content_hash,payload_json,observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"c{idx}", "acc", "hh.ru", "1", "ai", "seen", f"h{idx}", '{"name": "x"}', f"2026-01-0{idx + 1}"),
            )


def test_sessions_profiles_and_cookies_are_protected():
    for name in ("hh_chrome_profile/Default/Cookies", "hh_session.json", "providers/habr/storage_state.json",
                 "browsers/chromium", "screening-answers.json"):
        assert is_protected(Path(name)), name
    assert not is_protected(Path("hh_vacancies_20261020T203815Z.json"))


def test_cleanup_removes_only_old_snapshots_and_keeps_session_files(tmp_path):
    config = _config(tmp_path)
    now = time.time()
    old = _file(config.snapshots_dir / "hh_vacancies_old.json", 10, now)
    fresh = _file(config.snapshots_dir / "hh_vacancies_new.json", 1, now)
    session = _file(config.snapshots_dir / "hh_session.json", 30, now)
    profile = _file(config.snapshots_dir / "chrome_profile" / "Cookies", 30, now)

    report = run_maintenance(config, days=7, now=now)

    assert report["snapshots_removed"] == 1
    assert not old.exists()
    assert fresh.exists() and session.exists() and profile.exists()


def test_dry_run_changes_nothing(tmp_path):
    config = _config(tmp_path)
    now = time.time()
    old = _file(config.snapshots_dir / "hh_vacancies_old.json", 10, now)
    _seed_db(config)

    report = run_maintenance(config, days=7, dry_run=True, now=now)

    assert report["snapshots_removed"] == 1 and old.exists()
    assert report["database"]["stale_payloads"] == 2
    assert all(payload != "{}" for _id, payload in _observations(config.db_path))
    assert maintenance_due(config, now=now)


def test_compaction_keeps_newest_payload_one_backup_and_marker(tmp_path):
    config = _config(tmp_path)
    now = time.time()
    _seed_db(config)
    extra = _file(config.data_dir / "backups" / "applypilot-old.sqlite3", 1, now)

    report = run_maintenance(config, days=7, now=now)

    payloads = [payload for _id, payload in _observations(config.db_path)]
    assert payloads == ["{}", "{}", '{"name": "x"}']
    assert report["database"]["integrity"] == "ok"
    assert (config.data_dir / "backups" / "applypilot-latest.sqlite3").exists()
    assert not extra.exists()
    assert not maintenance_due(config, now=now + DAY)
    assert maintenance_due(config, now=now + 8 * DAY)
