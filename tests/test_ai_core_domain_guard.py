"""Configured semantic guards are independent from track names."""

from datetime import UTC, datetime
from pathlib import Path

from applypilot.multisource import _prepare_scored_items, approve_scanned_fits
from applypilot.scoring import auto_apply_title_eligible
from applypilot.storage import Store


def _score(raw: dict, track: str, *, guards: bool = False) -> dict:
    search = {"queries": ["LLM", "AI Agents"], "only_remote": True, "days": 30}
    if guards:
        search.update({
            "exclude_title_patterns": [
                r"(?:обучени[а-яё]*.{0,45}модел|post.training.{0,25}llm|pre.train.{0,25}llm)",
            ],
            "exclude_description_patterns": [
                r"(?:already relocated from russia|already moved out of russia|only candidates outside russia|не рассматриваем.{0,30}из рф)",
            ],
            "exclude_location_patterns": [
                r"(?:location\s*:\s*any\s*,?\s*except\s+(?:russia|russian federation)|(?:location|candidates|applicants)\s*:\s*[^.\n]{0,90}(?:except|excluding)\s+(?:russia|russian federation))",
            ],
        })
    output = _prepare_scored_items(
        [raw], source="getmatch.ru",
        profile={"resumes": {"default": "Example"}, "scoring": {}},
        search=search,
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
    ai = _score(raw, "ai_core", guards=True)
    assert ai["score_decision"] == "reject"
    assert ai["filter_status"] == "reject"
    assert "role:configured_title_exclusion" in ai["filter_reasons"]
    ml = _score(raw, "ml")
    assert "role:configured_title_exclusion" not in ml["filter_reasons"]


def test_worldwide_remote_relocate_first_is_not_remote_from_russia() -> None:
    raw = {
        "id": "34801", "name": "AI Agent Engineer",
        "description": "We are hiring remote specialists who have already relocated from Russia.",
        "is_remote": True, "area": "Worldwide | Remote",
        "published": "2026-10-18T10:00:00+00:00",
    }
    ai = _score(raw, "ai_core", guards=True)
    assert ai["filter_status"] == "reject"
    assert "description:configured_exclusion" in ai["filter_reasons"]


def test_regular_llm_agent_integrations_are_not_excluded() -> None:
    raw = {
        "id": "123", "name": "AI Agent Engineer",
        "description": "Remote from Russia, Python, MCP, LangGraph and production LLM APIs.",
        "is_remote": True, "area": "Россия",
        "published": "2026-10-18T10:00:00+00:00",
    }
    ai = _score(raw, "ai_core")
    assert not any(
        reason.startswith(("role:configured_title_exclusion", "description:configured_exclusion"))
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


def test_geekjob_mad_devs_explicit_location_exclusion_trumps_remote_badge() -> None:
    raw = {
        "id": "6ac37fb023481b12a406c4a5",
        "name": "Applied AI / LLM Engineer (Python)",
        "description": (
            "LLM pipelines, agents, RAG, and production Python. "
            "Location: any, except Russia and Belarus."
        ),
        "is_remote": True, "area": "Бишкек, Кыргызстан",
        "published": "2026-10-05T12:00:00+00:00",
    }
    scored = _score(raw, "ai_core", guards=True)
    assert scored["filter_status"] == "reject"
    assert "geo:configured_exclusion" in scored["filter_reasons"]


def test_regular_remote_from_russia_never_trips_geographic_exclusion() -> None:
    raw = {
        "id": "geography-positive",
        "name": "Python LLM Engineer",
        "description": (
            "Location: Russia. Remote worldwide, including Russia. "
            "Building APIs, RAG and LLM agents."
        ),
        "is_remote": True, "area": "Россия",
    }
    scored = _score(raw, "ai_core", guards=True)
    assert "geo:configured_exclusion" not in scored["filter_reasons"]


def test_generic_ai_title_stays_manual_when_auto_apply_requires_specific_role(tmp_path: Path) -> None:
    search = {
        "queries": ["AI agents"], "only_remote": True,
        "auto_apply_title_terms": ["agent", "llm", "rag", "mcp", "ai evaluation"],
    }
    raw = {
        "id": "manual-review", "name": "AI Engineer",
        "description": "Build production LLM agent applications with RAG.",
        "description_status": "ok",
        "is_remote": True, "area": "Россия",
        "published": "2026-10-18T10:00:00+00:00",
        "salary": {"from": 200000, "currency": "RUR"},
    }
    output = _prepare_scored_items(
        [raw], source="hh.ru", profile={
            "resumes": {"default": "Example"}, "role_terms": ["llm"],
            "primary_role_terms": ["llm"], "title_role_terms": ["ai"],
            "required_role_terms": ["engineer"],
        },
        search=search, cutoff=datetime(2026, 10, 1, tzinfo=UTC),
        track_resume="Example", track="ai_core",
    )[0]
    assert output["review_lane"] == "FIT"
    assert output["auto_apply_eligible"] is False
    store = Store(tmp_path / "review.sqlite3")
    store.start_campaign("campaign-1", "default", "ai_core", {}, source="hh.ru")
    store.save_campaign_items(
        "campaign-1", "default", "ai_core", [output], status="ok",
        snapshot_path="snapshot.json", source="hh.ru",
    )
    approval = approve_scanned_fits(
        store, "default", "ai_core", "hh.ru", ["manual-review"],
    )
    assert approval["approved"] == 0
    assert approval["blocked"]["manual-review"] == "title requires manual role review"


def test_explicit_ai_engineering_roles_pass_the_configured_title_gate() -> None:
    terms = [
        "ai engineer", "ai ml инженер", "ai solutions", "ии инженер",
        "ai-first developer",
    ]
    for title in (
        "AI Engineer", "AI/ML-инженер", "AI Solutions Engineer",
        "ИИ - инженер", "AI-first Developer / Python Developer",
    ):
        assert auto_apply_title_eligible({"name": title}, {"auto_apply_title_terms": terms})


def test_ai_evaluation_title_can_pass_private_automatic_role_gate() -> None:
    search = {
        "queries": ["AI agents"], "only_remote": True,
        "auto_apply_title_terms": ["agent", "llm", "rag", "mcp", "ai evaluation"],
    }
    raw = {
        "id": "eval-role", "name": "AI Evaluation Engineer",
        "description": "Evaluate LLM agents and improve model quality benchmarks.",
        "is_remote": True, "area": "Россия",
        "published": "2026-10-18T10:00:00+00:00",
        "salary": {"from": 200000, "currency": "RUR"},
    }
    output = _prepare_scored_items(
        [raw], source="hh.ru", profile={
            "resumes": {"default": "Example"}, "role_terms": ["llm"],
            "primary_role_terms": ["llm"], "title_role_terms": ["ai"],
            "required_role_terms": ["engineer"],
        },
        search=search, cutoff=datetime(2026, 10, 1, tzinfo=UTC),
        track_resume="Example", track="ai_core",
    )[0]
    assert output["auto_apply_eligible"] is True
    assert output["review_lane"] == "FIT"
