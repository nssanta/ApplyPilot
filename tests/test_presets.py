from applypilot.presets import resolve_search


def test_preset_is_used_when_private_config_does_not_override_queries():
    result = resolve_search({}, "go-backend")

    assert "Go Developer" in result["queries"]
    assert "golang" in result["include_terms"]


def test_private_search_values_override_preset():
    result = resolve_search({"preset": "ai-llm", "queries": ["Custom role"], "areas": [54]})

    assert result["queries"] == ["Custom role"]
    assert result["areas"] == [54]


def test_explicit_cli_preset_uses_only_declared_additional_queries():
    result = resolve_search({"queries": ["Private role"], "areas": [54]}, "go-backend")

    assert result["queries"] == ["Go Developer", "Golang Developer", "Go Backend", "Go Engineer"]
    assert result["areas"] == [54]

    extended = resolve_search({"additional_queries": ["Private role"]}, "go-backend")
    assert extended["queries"][-1] == "Private role"


def test_role_preset_replaces_role_specific_guards_and_selects_role_resume():
    result = resolve_search({
        "exclude_title_patterns": ["AI-only"],
        "exclude_location_patterns": ["AI-only"],
        "salary": {"from": 120000},
    }, "go-backend")

    assert result["resume_key"] == "go"
    assert "exclude_title_patterns" not in result
    assert "exclude_location_patterns" not in result
    assert result["salary"] == {"from": 120000}


def test_role_preset_selects_resume_configured_in_private_profile(tmp_path):
    from applypilot.cli import _profile
    from applypilot.config import AppConfig, effective_search

    profile_path = tmp_path / "profile.toml"
    profile_path.write_text(
        '[resumes]\ndefault = "Default Resume"\ngo = "Go Resume"\n',
        encoding="utf-8",
    )
    config = AppConfig(
        tmp_path, tmp_path / "private" / "data", profile_path,
        tmp_path / "search.toml",
    )

    profile = _profile(config, effective_search({}, "go-backend"))

    assert profile["resume"] == "Go Resume"


def test_ai_agents_llmops_preset_is_narrower_than_the_general_ai_preset():
    result = resolve_search({}, "ai-agents-llmops")

    assert "LLMOps Engineer" in result["queries"]
    assert "qa" in result["exclude_titles"]
    assert "security" in result["exclude_titles"]


def test_ai_agents_llmops_excludes_adjacent_non_agent_roles_but_keeps_llm_engineering():
    from applypilot.scoring import score_vacancy

    preset = resolve_search({}, "ai-agents-llmops")
    profile = {"search": preset}
    description = "Python FastAPI LLM agents RAG MCP Docker tools OpenAI integration."

    for title in (
        "DevOps-engineer в GenAI",
        "Site Reliability Engineer (SRE) in AI platform",
        "Ведущий Java Backend-разработчик AI / LLM Platform",
        "Старший разработчик на Java (AI агент)",
        "Senior .NET Developer (LLM / RAG)",
        "AI Engineer (Computer Vision / Bioinformatics)",
        "AI Marketing Engineer / Growth Engineer",
    ):
        item = {"id": title, "name": title, "description": description}
        result = score_vacancy(item, profile)
        assert result.decision == "reject", title
        assert any("preset_noise:" in reason for reason in result.hard_killers)

    result = score_vacancy({
        "id": "true-llm",
        "name": "AI/LLM Engineer — Python и AI Agents",
        "description": description,
    }, profile)
    assert result.decision == "pass"


def test_python_backend_excludes_rust_cpp_as_primary_title_stack():
    from applypilot.scoring import score_vacancy

    preset = resolve_search({}, "python-backend")
    profile = {"search": preset}
    description = "Backend API Python FastAPI Docker PostgreSQL integration."
    for title in ("Middle Backend разработчик (Rust/С++)", "Rust Backend Engineer", "C++ Backend Developer"):
        result = score_vacancy({"id": title, "name": title, "description": description}, profile)
        assert result.decision == "reject", title
        assert "stack:title_incompatible" in result.hard_killers or "role:title_not_target" in result.hard_killers

    correct = score_vacancy({
        "id": "python-role",
        "name": "Python Backend Developer (FastAPI)",
        "description": description,
    }, profile)
    assert correct.decision == "pass"
