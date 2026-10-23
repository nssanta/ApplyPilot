"""ai_core filters distinguish applied LLM engineering from model-training research."""

from datetime import UTC, datetime
from pathlib import Path

from applypilot.multisource import _prepare_scored_items
from applypilot.storage import Store


def _score(raw: dict, track: str) -> dict:
    output = _prepare_scored_items(
        [raw], source="getmatch.ru",
        profile={"resumes": {"default": "Example"}, "scoring": {}},
        search={"queries": ["LLM", "AI Agents"], "only_remote": True, "days": 30},
        cutoff=datetime(2026, 1, 1, tzinfo=UTC), track_resume="Example",
        track=track,
    )
    assert len(output) == 1
    return output[0]


def test_training_focused_ml_title_is_not_ai_core_fit() -> None:
    raw = {
        "id": "34221", "name": "Senior ML Engineer с опытом в обучении генеративных моделей: TTS, LLM, CV",
        "description": "Работа с обучением генеративных моделей.",
        "is_remote": True, "area": "Россия", "published": "2026-10-18T10:00:00+00:00",
        "salary": {"from": 500000, "currency": "RUR"},
    }
    ai = _score(raw, "ai_core")
    assert ai["score_decision"] == "reject"
    assert ai["filter_status"] == "reject"
    assert "role:foundation_training_not_ai_core" in ai["filter_reasons"]
    ml = _score(raw, "ml")
    assert "role:foundation_training_not_ai_core" not in ml["filter_reasons"]


def test_worldwide_remote_relocate_first_is_not_remote_from_russia() -> None:
    raw = {
        "id": "34801", "name": "AI Agent Engineer",
        "description": "We are hiring remote specialists who have already relocated from Russia.",
        "is_remote": True, "area": "Worldwide | Remote",
        "published": "2026-10-18T10:00:00+00:00",
    }
    ai = _score(raw, "ai_core")
    assert ai["filter_status"] == "reject"
    assert "geo:requires_relocation_from_russia" in ai["filter_reasons"]


def test_regular_llm_agent_integrations_are_not_excluded() -> None:
    raw = {
        "id": "123", "name": "AI Agent Engineer",
        "description": "Remote from Russia, Python, MCP, LangGraph and production LLM APIs.",
        "is_remote": True, "area": "Россия",
        "published": "2026-10-18T10:00:00+00:00",
    }
    ai = _score(raw, "ai_core")
    assert not any(
        reason.startswith(("role:foundation_training", "geo:requires_relocation"))
        for reason in ai["filter_reasons"]
    )


def test_review_view_uses_live_score_instead_of_cached_fit_lane(tmp_path: Path) -> None:
    store = Store(tmp_path / "score.sqlite3")
    stamp = datetime.now(UTC).isoformat()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO vacancies
            (account,source,vacancy_id,title,company,url,payload_json,created_at,
             first_seen_at,last_seen_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("default", "getmatch.ru", "34221",
             "Senior ML Engineer с обучением генеративных моделей", "Example",
             "https://getmatch.ru/vacancies/34221",
             '{"review_lane":"FIT","description_status":"ok"}',
             stamp, stamp, stamp, stamp),
        )
        conn.execute(
            """INSERT INTO vacancy_track_scores
            (account,source,vacancy_id,track,score,score_decision,filter_status,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            ("default", "getmatch.ru", "34221", "ai_core",
             80, "reject", "reject", stamp),
        )
    rows = store.list_campaign_vacancies(
        "default", "ai_core", "all", 20, source="getmatch.ru",
    )
    assert len(rows) == 1 and rows[0]["review_lane"] == "SKIP"
    assert not store.list_campaign_vacancies(
        "default", "ai_core", "top", 20, source="getmatch.ru",
    )
    assert not store.list_campaign_vacancies(
        "default", "ai_core", "review", 20, source="getmatch.ru",
    )
