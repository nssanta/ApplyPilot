from pathlib import Path

from applypilot.config import AppConfig
from applypilot.multisource import queue_plan, scan_sources
from applypilot.providers import default_registry
from applypilot.providers.base import parse_salary, remote_info
from applypilot.storage import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/public"


def test_salary_parser_handles_k_suffix_and_prediction():
    assert parse_salary("2K — 2.5K $") == {
        "from": 2000, "to": 2500, "currency": "USD", "gross": None, "provenance": "published",
    }
    assert parse_salary("300 000 — 400 000 ₽/мес на руки")["to"] == 400000
    assert parse_salary("Похожие специалисты получают 200 000–300 000 ₽") is None


def test_remote_except_russia_is_not_eligible():
    info = remote_info("Удалённо, кроме РФ")
    assert info["is_remote"] is False
    assert info["remote_confidence"] == "ineligible"


def test_all_fixture_providers_parse_without_network():
    registry = default_registry()
    cases = {
        "career.habr.com": ("habr_list.html", 8),
        "geekjob.ru": ("geekjob_list.html", 8),
        "getmatch.ru": ("getmatch_active_llm.html", 1),
        "yandex.ru/jobs": ("yandex_genai_remote_list.html", 3),
        "dreamjob.ru": ("dreamjob_hh_mirror.html", 1),
    }
    for source, (name, expected) in cases.items():
        items, segments = registry.get(source).discover({"fixture_path": str(FIXTURES / name)})
        assert len(items) == expected, source
        assert all(item["source"] == source for item in items)
        assert all(segment.status == "ok" for segment in segments)
    dream = registry.get("dreamjob.ru").discover({"fixture_path": str(FIXTURES / "dreamjob_hh_mirror.html")})[0][0]
    assert dream["application_channel"] == "hh.ru:138413511"
    assert dream["application_channel_type"] == "mirror"
    assert dream["mirror_source"] == "hh.ru"


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(tmp_path, tmp_path / "data", FIXTURES / "profile.toml", FIXTURES / "search.toml")


def test_offline_multisource_scan_persists_and_plans_without_attempts(tmp_path):
    config = _config(tmp_path)
    profile = {"account": "default", "resume": "ML"}
    search = {"queries": ["LLM"], "days": 3650, "max_pages": 1,
              "salary": {"from": 0, "missing": "include"}}
    sources = ["career.habr.com", "geekjob.ru", "getmatch.ru", "yandex.ru/jobs", "dreamjob.ru"]
    result = scan_sources(config, "ai_core", profile, search, sources=sources,
                          fixture_dir=FIXTURES, limit=100)
    assert result["failed_sources"] == []
    assert {row["source"] for row in result["sources"]} == set(sources)
    store = Store(config.db_path)
    total = 0
    for source in sources:
        rows = store.list_campaign_vacancies("default", "ai_core", "all", 100, source=source)
        assert rows, source
        total += len(rows)
    assert total >= 20
    ids = []
    for source in sources:
        ids.extend(f"{row['source']}:{row['id']}" for row in store.list_campaign_vacancies(
            "default", "ai_core", "all", 100, source=source,
        ))
    assert len(ids) == len(set(ids)), "duplicate source/native IDs in SQLite"
    with store.connect() as conn:
        conn.execute("UPDATE vacancy_track_scores SET review_state='APPROVED' "
                     "WHERE account='default' AND track='ai_core'")
    plan = queue_plan(store, "default", "ai_core", "getmatch.ru", 100)
    assert plan["mode"] == "dry_run"
    assert plan["planned_count"] == 0
    assert plan["blocked_count"] >= 1
    assert all("needs_live_validation" in row["reason"] for row in plan["blocked"])
    assert store.source_attempts("default", "getmatch.ru") == []


def test_hh_attempt_blocks_dreamjob_mirror_but_not_same_numeric_getmatch_id(tmp_path):
    config = _config(tmp_path)
    profile = {"account": "default"}
    search = {"queries": ["LLM"], "days": 3650,
              "salary": {"from": 0, "missing": "include"}}
    store = Store(config.db_path)
    for vacancy_id in ("138413511", "36887"):
        channel = f"hh.ru:{vacancy_id}"
        assert store.record_source_application(
            "default", "hh.ru", vacancy_id, "ai_core", channel, "submitting",
        )
        assert store.record_source_application(
            "default", "hh.ru", vacancy_id, "ai_core", channel, "success",
        )
    scan_sources(config, "ai_core", profile, search, sources=["dreamjob.ru", "getmatch.ru"],
                 fixture_dir=FIXTURES)
    dream = store.list_campaign_vacancies("default", "ai_core", "all", 10, source="dreamjob.ru")[0]
    getmatch = store.list_campaign_vacancies("default", "ai_core", "all", 10, source="getmatch.ru")[0]
    assert dream["channel_duplicate"] is True
    assert dream["duplicate_of_source"] == "hh.ru"
    assert getmatch["channel_duplicate"] is False
    assert getmatch["applied_before"] is False
