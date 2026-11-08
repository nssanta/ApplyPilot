from applypilot.scoring import (
    choose_resume,
    evaluate_search_filter,
    filter_candidates,
    matches_search_filters,
    prioritize_for_enrichment,
    score_vacancy,
)


def test_score_and_resume_selection_are_explainable():
    item = {"id": 7, "name": "Python Backend Engineer", "company": "Acme"}
    result = score_vacancy(item, {"resumes": {"python": "Python Backend", "default": "Default"}})
    assert result.id == "7"
    assert result.resume == "Python Backend"
    assert "keyword:python" in result.reasons


def test_filter_deduplicates_and_sorts():
    items = [{"id": "2", "name": "LLM Engineer", "description": "Python LLM"},
             {"id": "1", "name": "AI Agent", "description": "Python agents"},
             {"id": "1", "name": "duplicate", "description": "Python agents"}]
    result = filter_candidates(items, {"search": {"role_terms": ["llm", "agent"],
                                                   "title_role_terms": ["llm", "agent"]}}, limit=10)
    assert [item.id for item in result] == ["1", "2"]


def test_historical_score_is_not_added_to_again():
    result = score_vacancy({"id": "1", "name": "Python LLM Engineer", "score": 17}, {})
    assert result.score == 17
    assert not any(reason.startswith("keyword:") for reason in result.reasons)


def test_rescore_keeps_legacy_score_in_a_separate_field():
    result = score_vacancy({"id": "1", "name": "Python Developer", "description": "Python FastAPI", "score": 17}, {
        "rescore": True,
        "role_terms": ["python"],
        "title_role_terms": ["python"],
    })

    assert result.score_source == "calculated"
    assert result.legacy_score == 17
    assert result.score != 17


def test_history_is_applied_before_limit():
    result = filter_candidates(
        [{"id": "1", "name": "AI Agent", "score": 100},
         {"id": "2", "name": "AI Agent", "score": 90}],
        {}, limit=1, blocked_ids={"1"},
    )
    assert [item.id for item in result] == ["2"]


def test_remote_does_not_make_unrelated_assistant_a_match():
    result = filter_candidates([{
        "id": "1", "name": "Удалённый ассистент руководителя", "schedule": "remote",
    }], {"search": {"exclude_titles": ["ассистент"], "role_terms": ["python"]}}, limit=10)

    assert result == []


def test_score_components_sum_to_bounded_score_and_go_has_word_boundary():
    result = score_vacancy({"id": "1", "name": "Go Backend Engineer", "description": "gRPC"},
                           {"role_terms": ["go"], "title_role_terms": ["go"],
                            "required_role_terms": ["engineer"]})

    assert 0 <= result.score <= 100
    assert sum(result.components.values()) == result.score
    assert result.component_evidence["role"] == ("go",)
    assert "keyword:go" not in result.reasons


def test_search_constraints_filter_salary_and_experience():
    search = {"salary": {"from": 180000, "currency": "RUR", "missing": "exclude"},
              "experience": {"allowed": ["between1And3"]}}

    assert matches_search_filters({"id": "1", "salary": {"from": 200000, "currency": "RUR"},
                                  "experience": "between1And3"}, search)
    assert not matches_search_filters({"id": "2", "salary": {"from": 100000, "currency": "RUR"},
                                      "experience": "between1And3"}, search)
    assert not matches_search_filters({"id": "3", "experience": "between1And3"}, search)


def test_salary_missing_only_and_guaranteed_policy_are_explicit():
    only = {"salary": {"missing": "only"}}
    assert evaluate_search_filter({"id": "1"}, only).status == "pass"
    assert evaluate_search_filter({"id": "2", "salary": {"from": 1, "currency": "RUR"}}, only).status == "reject"

    possible = {"salary": {"currency": "RUR", "from": 200, "policy": "possible"}}
    guaranteed = {"salary": {"currency": "RUR", "from": 200, "policy": "guaranteed"}}
    item = {"id": "3", "salary": {"from": 100, "to": 250, "currency": "RUR"}}
    assert evaluate_search_filter(item, possible).status == "pass"
    assert evaluate_search_filter(item, guaranteed).status == "reject"


def test_enrichment_keeps_borderline_but_final_plan_requires_confirmation():
    item = {"id": "1", "name": "AI Engineer", "description": ""}
    profile = {"search": {"role_terms": ["ai"], "title_role_terms": ["ai"]}}

    assert [candidate.id for candidate in prioritize_for_enrichment([item], profile)] == ["1"]
    assert filter_candidates([item], profile) == []


def test_go_never_falls_back_to_ai_resume_and_python_rejects_explicit_php_title():
    go = score_vacancy({"id": "go", "name": "Go Backend Developer", "description": "Go gRPC"}, {
        "resumes": {"default": "AI Agent Engineer"},
        "search": {"role_terms": ["go"], "primary_role_terms": ["go"], "title_role_terms": ["go"]},
    })
    python = score_vacancy({"id": "php", "name": "Senior backend developer (PHP, Symfony)",
                            "description": "Python API PostgreSQL"}, {
        "search": {"role_terms": ["python", "backend"], "primary_role_terms": ["python"],
                   "incompatible_title_terms": ["php", "symfony"]},
    })

    assert go.resume == ""
    assert "stack:title_incompatible" in python.hard_killers


def test_ml_resume_matches_hyphenated_russian_title_and_python_backend_rejects_qa():
    from applypilot.config import effective_search

    resumes = {"ml": "ML Engineer", "default": "AI Agent Engineer"}
    qa = score_vacancy({"id": "qa", "name": "QA Engineer (Python)",
                        "description": "Python API testing"},
                       {"search": effective_search({}, "python-backend")})

    assert choose_resume("ML-инженер", {"resumes": resumes}) == "ML Engineer"
    assert "preset_noise:qa" in qa.hard_killers


def test_plain_python_titles_use_backend_resume_and_ai_titles_keep_ai_resume():
    profile = {"resumes": {"default": "AI", "ai_agent": "AI", "python": "Backend", "go": "Backend", "ml": "AI"}}

    assert choose_resume("Python-разработчик", profile) == "Backend"
    assert choose_resume("Senior Python Developer", profile) == "Backend"
    assert choose_resume("Бэкенд-разработчик (FastAPI)", profile) == "Backend"
    assert choose_resume("Python-разработчик AI-агентов", profile) == "AI"
    assert choose_resume("Python LLM Engineer", profile) == "AI"
    assert choose_resume("Backend-разработчик (ИИ, RAG)", profile) == "AI"
    assert choose_resume("Golang developer", profile) == "Backend"
    assert choose_resume("AI-инженер", profile) == "AI"
    assert choose_resume("Mail agent integrator", profile) == "AI"


def test_ai_agents_llmops_rejects_qa_and_security_but_keeps_llmops_engineering():
    from applypilot.config import effective_search

    search = effective_search({}, "ai-agents-llmops")
    qa = score_vacancy({"id": "qa", "name": "QA Engineer (LLM)",
                        "description": "LLM RAG testing"}, {"search": search})
    security = score_vacancy({"id": "security", "name": "AI Security Engineer",
                              "description": "LLM RAG security"}, {"search": search})
    llmops = score_vacancy({"id": "llmops", "name": "LLMOps Engineer",
                            "description": "Python LLM RAG MCP Kubernetes"}, {"search": search})

    assert "preset_noise:qa" in qa.hard_killers
    assert "preset_noise:security" in security.hard_killers
    assert llmops.decision == "pass"


def test_salary_soft_threshold_keeps_open_ended_and_other_currency_for_review():
    search = {"salary": {"currency": "RUR", "from": 120000, "policy": "possible"}}
    open_low = evaluate_search_filter(
        {"id": "open", "salary": {"from": 100000, "to": None, "currency": "RUR"}},
        search,
    )
    assert open_low.status == "unknown"
    assert "salary:open_range_below_target" in open_low.reasons
    owner_floor = evaluate_search_filter(
        {"id": "open", "salary": {"from": 100000, "to": None, "currency": "RUR"}},
        {"salary": {**search["salary"], "open_range_min": 100000}},
    )
    assert owner_floor.status == "pass"
    assert "salary:open_range_accepted" in owner_floor.reasons

    usd = evaluate_search_filter(
        {"id": "usd", "salary": {"from": 3000, "to": 5000, "currency": "USD"}},
        search,
    )
    assert usd.status == "unknown"
    assert "salary:currency_mismatch_check" in usd.reasons

    bounded_below = evaluate_search_filter(
        {"id": "low", "salary": {"from": 70000, "to": 110000, "currency": "RUR"}},
        search,
    )
    assert bounded_below.status == "reject"

    guaranteed = evaluate_search_filter(
        {"id": "fixed", "salary": {"from": 120000, "currency": "RUR"}},
        search,
    )
    assert guaranteed.status == "pass"


def test_priority_salary_ranks_above_low_published_salary():
    search = {"role_terms": ["python"], "primary_role_terms": ["python"],
              "salary": {"currency": "RUR", "from": 170000, "priority_from": 200000}}
    base = {"name": "Python Developer", "description": "Python FastAPI PostgreSQL", "is_remote": True}
    high = score_vacancy({**base, "id": "h", "salary": {"from": 200000, "to": 260000, "currency": "RUR"}},
                         {"search": search, "rescore": True})
    low = score_vacancy({**base, "id": "l", "salary": {"from": 150000, "to": 180000, "currency": "RUR"}},
                        {"search": search, "rescore": True})
    usd = score_vacancy({**base, "id": "u", "salary": {"from": 3000, "to": 5000, "currency": "USD"}},
                        {"search": search, "rescore": True})
    assert high.score == low.score + 5
    assert usd.score == low.score


def test_foreign_salary_is_converted_with_configured_rate():
    from applypilot.scoring import evaluate_search_filter, normalize_salary

    cfg = {"currency": "RUR", "from": 170000, "fx": {"USD": 80, "EUR": 90}}
    usd = normalize_salary({"from": 3000, "to": 5000, "currency": "USD"}, cfg)
    assert usd == {"from": 240000, "to": 400000, "currency": "RUR",
                   "original": {"from": 3000, "to": 5000, "currency": "USD"}}
    assert normalize_salary(usd, cfg) == usd
    assert normalize_salary({"from": 100, "currency": "GBP"}, cfg)["currency"] == "GBP"
    assert evaluate_search_filter({"id": "1", "name": "Python", "salary": usd}, {"salary": cfg}).status == "pass"
    low = normalize_salary({"from": 1000, "to": 1500, "currency": "USD"}, cfg)
    assert evaluate_search_filter({"id": "2", "name": "Python", "salary": low}, {"salary": cfg}).status == "reject"
