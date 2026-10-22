from __future__ import annotations

from pathlib import Path

from applypilot.config import AppConfig
from applypilot.multisource import scan_source

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/public"
PROFILE = FIXTURES / "profile.toml"
SEARCH = FIXTURES / "search.toml"


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(tmp_path, tmp_path / "data", PROFILE, SEARCH)


def test_hh_scan_routes_through_legacy_campaign(monkeypatch, tmp_path):
    from applypilot import campaigns

    called = {}

    def fake_run(config, track, profile, search, **kwargs):
        called["track"] = track
        called["kwargs"] = kwargs
        return {
            "campaign_id": "legacy-campaign", "status": "ok", "account": "hh-primary",
            "summary": {"items": 4}, "segment_count": 2, "segment_errors": [],
            "snapshot": "snapshot.json", "top": [{"id": "1"}], "score_counts": {"fit": 1},
        }

    monkeypatch.setattr(campaigns, "run_campaign", fake_run)
    config = _config(tmp_path)
    result = scan_source(
        config, "ai_core", config.load_profile(), config.load_search(), "hh.ru",
        pages=2, request_budget=50, details_limit=20, limit=100,
    )
    assert called["track"] == "ai_core"
    assert result["legacy_hh"] is True
    assert result["items"] == 4
    assert result["status"] == "ok"


def test_hh_fixture_mode_is_skipped_not_faked(tmp_path):
    config = _config(tmp_path)
    result = scan_source(
        config, "ai_core", config.load_profile(), config.load_search(), "hh.ru",
        fixture_dir=FIXTURES,
    )
    assert result["status"] == "skipped"
    assert result["items"] == 0
    assert "live scan" in result["errors"][0]
