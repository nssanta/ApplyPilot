from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from applypilot.config import AppConfig, effective_search
from applypilot.multisource import (
    _prepare_scored_items,
    resolve_source_account,
    scan_sources,
    source_status,
)
from applypilot.providers.base import make_item
from applypilot.storage import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/public"
PROFILE = FIXTURES / "profile.toml"
SEARCH = FIXTURES / "search.toml"
SOURCES = ["career.habr.com", "geekjob.ru", "getmatch.ru", "yandex.ru/jobs", "dreamjob.ru"]


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(tmp_path, tmp_path / "data", PROFILE, SEARCH)


def test_ai_core_has_reviewable_top_and_unknown_lane(tmp_path):
    config = _config(tmp_path)
    profile = config.load_profile()
    search = effective_search(config.load_search())
    result = scan_sources(
        config, "ai_core", profile, search, sources=SOURCES,
        fixture_dir=FIXTURES, limit=100,
    )
    assert result["failed_sources"] == []
    store = Store(config.db_path)
    account = str(profile.get("account") or "default")
    top = store.list_campaign_vacancies(account, "ai_core", "top", 100)
    review = store.list_campaign_vacancies(account, "ai_core", "review", 100)
    all_rows = store.list_campaign_vacancies(account, "ai_core", "all", 100)
    assert len(all_rows) >= 20
    assert len(top) >= 2
    assert len(review) >= 4
    assert all(row["review_lane"] != "SKIP" for row in top)
    getmatch = next(row for row in all_rows
                    if row["source"] == "getmatch.ru" and row["id"] == "36887")
    assert getmatch["review_lane"] in {"FIT", "GOOD_UNKNOWN"}
    assert any(row["source"] == "getmatch.ru" and row["id"] == "36887" for row in review)
    ids = [f"{row['source']}:{row['id']}" for row in all_rows]
    assert len(ids) == len(set(ids))


def test_source_accounts_are_explicit_and_namespace_artifacts(tmp_path):
    config = _config(tmp_path)
    mapping = {
        "hh.ru": "hh-primary",
        "career.habr.com": "habr-primary",
        "getmatch.ru": "getmatch-primary",
        "geekjob.ru": "geekjob-primary",
        "yandex.ru/jobs": "yandex-primary",
        "dreamjob.ru": "hh-primary",
    }
    source_config = tmp_path / "private/config/sources.toml"
    source_config.parent.mkdir(parents=True, exist_ok=True)
    source_config.write_text(
        "[accounts]\n" + "".join(f'"{key}" = "{value}"\n' for key, value in mapping.items()),
        encoding="utf-8",
    )
    profile = config.load_profile()
    for source in SOURCES:
        assert resolve_source_account(config, profile, source) == mapping[source]
    search = effective_search(config.load_search())
    result = scan_sources(
        config, "ai_core", profile, search, sources=SOURCES,
        fixture_dir=FIXTURES, limit=100,
    )
    assert result["accounts"]["career.habr.com"] == "habr-primary"
    assert result["accounts"]["dreamjob.ru"] == "hh-primary"
    store = Store(config.db_path)
    for source in SOURCES:
        rows = store.list_campaign_vacancies(
            mapping[source], "ai_core", "all", 100, source=source,
        )
        assert rows, source
    status = source_status(config, "ai_core", None)
    by_source = {row["source"]: row for row in status["sources"]}
    assert by_source["career.habr.com"]["account"] == "habr-primary"
    assert by_source["dreamjob.ru"]["account"] == "hh-primary"
    assert (tmp_path / "data/seen/career-habr-com/habr-primary.json").is_file()
    snapshots = list((tmp_path / "data/snapshots/providers/career-habr-com/habr-primary/ai-core").glob("*.json"))
    assert snapshots


def test_currency_mismatch_is_review_not_fit(tmp_path):
    config = _config(tmp_path)
    profile = config.load_profile()
    search = effective_search(config.load_search())
    item = make_item(
        source="getmatch.ru", native_id="currency-1", name="AI Engineer",
        company="Example", url="https://getmatch.ru/vacancies/currency-1",
        description="LLM RAG Python agent", salary={
            "from": 5000, "to": 8000, "currency": "USD", "gross": None,
            "provenance": "published",
        }, area="Remote", remote_text="remote",
    )
    rows = _prepare_scored_items(
        [item], source="getmatch.ru", profile=profile, search=search,
        cutoff=datetime.now(UTC) - timedelta(days=30), track_resume="",
    )
    assert rows
    assert rows[0]["filter_status"] == "unknown"
    assert rows[0]["review_lane"] != "FIT"
    assert any("currency" in reason for reason in rows[0]["filter_reasons"])


def test_old_publication_is_skipped(tmp_path):
    config = _config(tmp_path)
    profile = config.load_profile()
    search = effective_search(config.load_search())
    item = make_item(
        source="getmatch.ru", native_id="old-1", name="AI Engineer",
        company="Example", url="https://getmatch.ru/vacancies/old-1",
        description="LLM RAG Python agent", published="2020-01-01T00:00:00+00:00",
        area="Remote", remote_text="remote",
    )
    rows = _prepare_scored_items(
        [item], source="getmatch.ru", profile=profile, search=search,
        cutoff=datetime.now(UTC) - timedelta(days=30), track_resume="",
    )
    assert rows
    assert rows[0]["filter_status"] == "reject"
    assert rows[0]["review_lane"] == "SKIP"
    assert "publication:outside_requested_window" in rows[0]["filter_reasons"]
