import json
import tomllib

from applypilot.admin import (
    TRACKS_CONFIG,
    AdminApp,
    _over_experience,
    _track_snapshot,
    _write_tracks_config,
)
from applypilot.config import AppConfig
from applypilot.storage import Store


def test_delete_track_keeps_other_tracks_and_files(tmp_path):
    config_path = tmp_path / TRACKS_CONFIG
    entries = [
        {"key": "keep", "label": "Keep", "type": "ai",
         "profile": "private/config/profile-keep.toml",
         "search": "private/config/search-keep.toml",
         "screen_report": "private/reports/custom-keep.json",
         "accepted": "private/data/snapshots/custom-keep.json"},
        {"key": "remove", "label": "Remove", "type": "infra",
         "profile": "private/config/profile-remove.toml",
         "search": "private/config/search-remove.toml"},
    ]
    _write_tracks_config(config_path, entries)
    profile = tmp_path / entries[1]["profile"]
    profile.write_text("reviewed = false\n", encoding="utf-8")
    app = AdminApp(AppConfig.discover(root=tmp_path))

    assert app.delete_track("remove") == {"ok": True, "key": "remove"}
    assert profile.exists()
    assert [t["key"] for t in tomllib.loads(config_path.read_text(encoding="utf-8"))["track"]] == ["keep"]
    assert app.tracks_overview()["tracks"][0]["key"] == "keep"
    assert app.settings_public()["tracks"][0]["key"] == "keep"
    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["track"][0]["screen_report"] == "private/reports/custom-keep.json"

    # Reloading the app must not restore removed tracks from built-in defaults.
    fresh = AdminApp(AppConfig.discover(root=tmp_path))
    assert [t["key"] for t in fresh.tracks_overview()["tracks"]] == ["keep"]


def test_delete_track_rejects_unknown_and_last_track(tmp_path):
    config_path = tmp_path / TRACKS_CONFIG
    _write_tracks_config(config_path, [{"key": "only"}])
    app = AdminApp(AppConfig.discover(root=tmp_path))

    assert app.delete_track("missing") == {"error": "трек не найден"}
    assert app.delete_track("only") == {"error": "нельзя удалить последний трек"}
    assert [t["key"] for t in tomllib.loads(config_path.read_text(encoding="utf-8"))["track"]] == ["only"]


def test_track_snapshot_does_not_fall_back_to_another_track(tmp_path):
    entries = [
        {"key": "alpha", "profile": "private/config/profile-alpha.toml",
         "search": "private/config/search-alpha.toml",
         "accepted": "private/data/snapshots/accepted-alpha.json"},
        {"key": "beta", "profile": "private/config/profile-beta.toml",
         "search": "private/config/search-beta.toml",
         "accepted": "private/data/snapshots/accepted-beta.json"},
    ]
    _write_tracks_config(tmp_path / TRACKS_CONFIG, entries)
    for key, query in (("alpha", "Rust"), ("beta", "Python")):
        search = tmp_path / f"private/config/search-{key}.toml"
        search.parent.mkdir(parents=True, exist_ok=True)
        search.write_text(f'queries = ["{query}"]\n', encoding="utf-8")
    AdminApp(AppConfig.discover(root=tmp_path))
    snapshot_dir = tmp_path / "private/data/snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "hh_vacancies_beta.json").write_text(
        '{"segments":[{"query":"Python"}],"items":[{"id":"1"}]}', encoding="utf-8")

    assert _track_snapshot(tmp_path, "alpha") == str(tmp_path / entries[0]["accepted"])
    assert _track_snapshot(tmp_path, "beta") == str(snapshot_dir / "hh_vacancies_beta.json")


def test_track_snapshot_matches_queries_inside_search_groups(tmp_path):
    entries = [{
        "key": "grouped", "profile": "private/config/profile.toml",
        "search": "private/config/search.toml",
        "accepted": "private/data/snapshots/accepted.json",
    }]
    _write_tracks_config(tmp_path / TRACKS_CONFIG, entries)
    AdminApp(AppConfig.discover(root=tmp_path))
    search = tmp_path / entries[0]["search"]
    search.parent.mkdir(parents=True, exist_ok=True)
    search.write_text('[[groups]]\nname = "north"\nqueries = ["Rust Engineer"]\nareas = [54]\n',
                      encoding="utf-8")
    snapshot_dir = tmp_path / "private/data/snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "hh_vacancies_group.json").write_text(
        '{"segments":[{"query":"Rust Engineer","area":54}],"items":[]}',
        encoding="utf-8")

    assert _track_snapshot(tmp_path, "grouped") == str(snapshot_dir / "hh_vacancies_group.json")


def test_experience_warning_requires_private_profile_years():
    assert not _over_experience({}, "moreThan6")
    assert not _over_experience({"screen": {"experience_years": 4}}, "between3And6")
    assert _over_experience({"screen": {"experience_years": 1.5}}, "between3And6")
    assert not _over_experience({"screen": {"experience_years": 1.5}}, "between1And3")


def test_scan_screen_uses_snapshot_path_returned_by_its_scan(tmp_path, monkeypatch):
    config_path = tmp_path / TRACKS_CONFIG
    _write_tracks_config(config_path, [{
        "key": "alpha", "profile": "private/config/profile-alpha.toml",
        "search": "private/config/search-alpha.toml",
    }])
    app = AdminApp(AppConfig.discover(root=tmp_path))
    captured = {}
    monkeypatch.setattr(app, "_api_key", lambda: "test-key")
    monkeypatch.setattr(app.runner, "start", lambda argv, label, env=None:
                        captured.update(argv=argv, label=label) or (True, "started"))

    assert app.start_job({"action": "scan_screen", "track": "alpha"}) == (True, "started")
    command = captured["argv"][-1]
    assert "ls -t private/data/snapshots" not in command
    assert 'tee "$scan_log"' in command
    assert 'screen --input "$snap"' in command


def test_apply_queue_and_input_exclude_viewed_bad_and_handled_items(tmp_path):
    entries = [{
        "key": "only", "profile": "private/config/profile-only.toml",
        "search": "private/config/search-only.toml",
        "screen_report": "private/reports/screen-only.json",
        "accepted": "private/data/snapshots/accepted-only.json",
    }]
    _write_tracks_config(tmp_path / TRACKS_CONFIG, entries)
    app = AdminApp(AppConfig.discover(root=tmp_path))
    ids = ("viewed", "bad", "manual", "blocked", "open")
    rows = [{"id": vacancy_id, "name": f"{vacancy_id} backend role", "verdict": "FIT", "fit_score": 90,
             "url": f"https://hh.ru/vacancy/{index}"}
            for index, vacancy_id in enumerate(ids, 1)]
    report = tmp_path / entries[0]["screen_report"]
    accepted = tmp_path / entries[0]["accepted"]
    report.parent.mkdir(parents=True, exist_ok=True)
    accepted.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"results": rows}), encoding="utf-8")
    accepted.write_text(json.dumps({"items": rows}), encoding="utf-8")
    app.config.data_dir.mkdir(parents=True, exist_ok=True)
    (app.config.data_dir / "viewed.json").write_text('["viewed"]', encoding="utf-8")
    (app.config.data_dir / "bad.json").write_text('["bad"]', encoding="utf-8")
    (app.config.data_dir / "manual-applied.json").write_text('["manual"]', encoding="utf-8")
    Store(app.config.db_path).record({"id": "blocked"}, "unknown", account="default")

    queue = app.apply_queue("only", limit=10)
    queue_ids = {row["id"] for row in queue["rows"]}
    sendable_ids = {row["id"] for row in queue["rows"]
                    if not row["blocked"] and not row["applied"]}
    apply_input = app._build_apply_input("only", "all", None)
    assert apply_input is not None
    apply_ids = {item["id"] for item in json.loads(apply_input.read_text(encoding="utf-8"))["items"]}

    assert queue_ids == {"manual", "blocked", "open"}
    assert sendable_ids == {"open"}
    assert apply_ids == {"open"}
