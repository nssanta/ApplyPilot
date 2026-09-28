from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from applypilot.cli import main
from applypilot.screen import (
    ScreenError,
    candidate_context,
    parse_verdict,
    screen_messages,
    screen_vacancies,
)


def test_parse_verdict_plain():
    out = parse_verdict('{"verdict":"FIT","fit_score":85,"reason":"agents"}')
    assert out == {"verdict": "FIT", "fit_score": 85, "reason": "agents"}


def test_parse_verdict_code_fence_and_noise():
    out = parse_verdict('```json\n{"verdict":"skip","fit_score":10,"reason":"ml"}\n```')
    assert out["verdict"] == "SKIP" and out["fit_score"] == 10
    out = parse_verdict('Вот ответ: {"verdict":"MAYBE","fit_score":50,"reason":"mix"} — всё.')
    assert out["verdict"] == "MAYBE"


def test_parse_verdict_clamps_and_validates():
    assert parse_verdict('{"verdict":"FIT","fit_score":250,"reason":""}')["fit_score"] == 100
    assert parse_verdict('{"verdict":"FIT","fit_score":"x","reason":""}')["fit_score"] == 0
    with pytest.raises(ValueError):
        parse_verdict('{"verdict":"GREAT","fit_score":90}')
    with pytest.raises(ValueError):
        parse_verdict("   ")


class _FakeResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_screen_vacancies_merges_and_caches(tmp_path):
    calls = {"n": 0}

    def fake_post(url, json, headers, timeout):
        calls["n"] += 1
        assert headers["Authorization"] == "Bearer test-key"
        return _FakeResponse('{"verdict":"FIT","fit_score":80,"reason":"LLM-агенты"}')

    items = [{"id": "1", "name": "AI Agent Engineer", "company": "Acme",
              "url": "https://hh.ru/vacancy/1", "description": "RAG, LLM, FastAPI", "score": 70}]
    profile = {"name": "Test", "location": "Remote", "answers": {}}

    first = screen_vacancies(items, profile, tmp_path, track="ai",
                             api_key="test-key", post=fake_post)
    assert first[0]["verdict"] == "FIT" and first[0]["fit_score"] == 80
    assert first[0]["source"] == "generated" and first[0]["url"] == "https://hh.ru/vacancy/1"
    assert calls["n"] == 1

    # Second run must hit the cache and not call the model again.
    second = screen_vacancies(items, profile, tmp_path, track="ai",
                              api_key="test-key", post=fake_post)
    assert second[0]["source"] == "cache"
    assert calls["n"] == 1


def test_screen_requires_key(tmp_path):
    with pytest.raises(ScreenError):
        screen_vacancies([{"id": "1", "description": "x"}], {"answers": {}}, tmp_path,
                         api_key="", post=lambda *a, **k: None)


def test_screen_uses_candidate_preferences_only_when_configured():
    profile = {
        "name": "Example Candidate",
        "location": "Office-based",
        "english_level": "C1",
        "screen": {
            "constraints": "Office work is acceptable; prefer Go roles.",
            "salary_expectation": "At least 250000 RUB.",
            "experience_years": 12,
        },
    }
    candidate = candidate_context(profile)
    messages = screen_messages({"name": "Senior Go Developer", "description": "Office role"},
                               candidate, "General role fit")
    prompt = "\n".join(message["content"] for message in messages)

    assert candidate["experience_years"] == 12
    assert candidate["salary_expectation"] == "At least 250000 RUB."
    assert "Office-based" in prompt and "Office work is acceptable" in prompt
    assert "100 000" not in prompt
    assert "B1" not in prompt
    assert "1.3 года" not in prompt
    assert "live-coding" not in prompt
    assert "40–60к" not in prompt
    assert "только удалёнка" not in prompt


def test_empty_profile_does_not_add_candidate_or_job_preferences():
    candidate = candidate_context({})
    messages = screen_messages({"name": "Experienced Engineer", "description": "Office work"},
                               candidate, "")
    prompt = "\n".join(message["content"] for message in messages)

    assert candidate == {}
    assert "~1.3" not in prompt
    assert "B1" not in prompt
    assert "100 000" not in prompt
    assert "Junior/Middle" not in prompt
    assert "частичная занятость" not in prompt
    assert "зарплата не влияет на вердикт" in prompt


def test_cli_screen_keeps_vacancy_reported_by_another_track(tmp_path, monkeypatch):
    data = tmp_path / "private" / "data"
    reports = tmp_path / "private" / "reports"
    reports.mkdir(parents=True)
    (reports / "screen-infra.json").write_text(json.dumps({
        "results": [{"id": "1", "verdict": "SKIP"}],
    }), encoding="utf-8")
    source = tmp_path / "scan.json"
    source.write_text(json.dumps({"items": [{
        "id": "1", "name": "AI Agent Engineer", "description": "Python LLM RAG",
    }]}), encoding="utf-8")
    screened = []
    monkeypatch.setattr("applypilot.cli.filter_candidates", lambda items, *_args, **_kwargs:
                        [SimpleNamespace(id=str(item["id"]), score=80) for item in items])
    monkeypatch.setattr("applypilot.cli.screen_vacancies", lambda items, *args, **kwargs:
                        screened.extend(items) or [{**item, "verdict": "FIT", "fit_score": 80}
                                                   for item in items])

    result = main(["--data-dir", str(data), "screen", "--input", str(source), "--track", "ai",
                   "--output", str(reports / "screen-ai.json")])

    assert result == 0
    assert [item["id"] for item in screened] == ["1"]


def test_cli_screen_retry_errors_can_revisit_existing_report_row(tmp_path, monkeypatch):
    data = tmp_path / "private" / "data"
    reports = tmp_path / "private" / "reports"
    reports.mkdir(parents=True)
    report = reports / "screen-ai.json"
    report.write_text(json.dumps({
        "results": [{"id": "1", "verdict": "ERROR", "reason": "timeout"}],
    }), encoding="utf-8")
    source = tmp_path / "scan.json"
    source.write_text(json.dumps({"items": [{
        "id": "1", "name": "AI Agent Engineer", "description": "Python LLM RAG",
    }]}), encoding="utf-8")
    screened = []
    monkeypatch.setattr("applypilot.cli.filter_candidates", lambda items, *_args, **_kwargs:
                        [SimpleNamespace(id=str(item["id"]), score=80) for item in items])
    monkeypatch.setattr("applypilot.cli.screen_vacancies", lambda items, *args, **kwargs:
                        screened.extend(items) or [{**item, "verdict": "FIT", "fit_score": 80}
                                                   for item in items])

    result = main(["--data-dir", str(data), "screen", "--input", str(source), "--track", "ai",
                   "--output", str(report), "--emit-snapshot", str(data / "accepted.json"),
                   "--retry-errors"])

    assert result == 0
    assert [item["id"] for item in screened] == ["1"]
    assert json.loads(report.read_text(encoding="utf-8"))["results"][0]["verdict"] == "FIT"


def test_retry_errors_preserves_other_accepted_rows_from_the_previous_snapshot(tmp_path, monkeypatch):
    data = tmp_path / "private" / "data"
    reports = tmp_path / "private" / "reports"
    reports.mkdir(parents=True)
    report = reports / "screen-ai.json"
    report.write_text(json.dumps({
        "results": [
            {"id": "1", "verdict": "ERROR", "reason": "timeout"},
            {"id": "2", "verdict": "FIT", "fit_score": 90},
        ],
    }), encoding="utf-8")
    accepted = data / "accepted.json"
    accepted.parent.mkdir(parents=True)
    accepted.write_text(json.dumps({"items": [{"id": "2", "name": "Previously accepted"}]}),
                        encoding="utf-8")
    source = tmp_path / "scan.json"
    source.write_text(json.dumps({"items": [
        {"id": "1", "name": "Retry this", "description": "Python"},
        {"id": "2", "name": "Keep accepted", "description": "Go"},
    ]}), encoding="utf-8")
    screened = []
    monkeypatch.setattr("applypilot.cli.filter_candidates", lambda items, *_args, **_kwargs:
                        [SimpleNamespace(id=str(item["id"]), score=80) for item in items])
    monkeypatch.setattr("applypilot.cli.screen_vacancies", lambda items, *args, **kwargs:
                        screened.extend(items) or [{**item, "verdict": "FIT", "fit_score": 80}
                                                   for item in items])

    result = main(["--data-dir", str(data), "screen", "--input", str(source), "--track", "ai",
                   "--output", str(report), "--emit-snapshot", str(accepted), "--retry-errors"])

    assert result == 0
    assert [item["id"] for item in screened] == ["1"]
    assert {item["id"] for item in json.loads(accepted.read_text(encoding="utf-8"))["items"]} == {"1", "2"}
