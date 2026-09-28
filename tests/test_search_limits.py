import json

import pytest
import requests

from applypilot.cli import main
from applypilot.parser import save_snapshot, scan, scan_many
from applypilot.scoring import filter_candidates
from applypilot.storage import Store


class Response:
    def __init__(self, status=200, state=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = '<template id="HH-Lux-InitialState">' + json.dumps(
            state if state is not None else {"vacancySearchResult": {
                "vacancies": [{"vacancyId": "1", "name": "Go Developer"}], "totalResults": 1,
            }}
        ) + '</template>'


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.params = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        self.params.append(kwargs.get("params", {}))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_one_request_budget_does_not_retry_429(monkeypatch):
    monkeypatch.setattr("applypilot.parser.time.sleep", lambda _: None)
    client = Client([Response(429), Response()])

    _, segments = scan_many(["Go"], [113], session=client, request_budget=1, pause_seconds=0)

    assert len(client.calls) == 1
    assert segments[0].requests == 1


def test_redirect_consumes_request_budget():
    client = Client([Response(302, headers={"Location": "https://hh.ru/search/redirected"}), Response()])

    _, segments = scan_many(["Go"], [113], session=client, request_budget=1, pause_seconds=0)

    assert client.calls == ["https://hh.ru/search/vacancy"]
    assert segments[0].status == "truncated"


def test_network_failure_consumes_request_budget(monkeypatch):
    monkeypatch.setattr("applypilot.parser.time.sleep", lambda _: None)
    client = Client([requests.ConnectionError("offline"), Response()])

    scan_many(["Go"], [113], session=client, request_budget=1, pause_seconds=0)

    assert len(client.calls) == 1


@pytest.mark.parametrize("state", [{}, {"vacancySearchResult": {}}, {"vacancySearchResult": {"vacancies": None}}])
def test_unrecognized_search_structure_is_not_empty_success(state):
    result = scan("Go", session=Client([Response(state=state)]))

    assert result.status == "failed"


def test_cli_shares_actual_request_budget_across_groups(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("applypilot.parser.time.sleep", lambda _: None)
    client = Client([Response(), Response()])
    monkeypatch.setattr("applypilot.parser.requests.Session", lambda: client)
    search = tmp_path / "search.toml"
    search.write_text('''request_budget = 2
details_limit = 0
[[groups]]
name = "first"
queries = ["Go"]
[[groups]]
name = "second"
queries = ["Python"]
''', encoding="utf-8")
    data = tmp_path / "data"

    result = main(["--search", str(search), "--data-dir", str(data), "scan"])

    assert result == 0
    assert len(client.calls) == 2
    snapshot = json.loads(next((data / "snapshots").glob("hh_vacancies_*.json")).read_text())
    assert snapshot["status"] == "ok"
    assert sum(segment["requests"] for segment in snapshot["segments"]) == 2
    assert {segment["order_by"] for segment in snapshot["segments"]} == {"relevance"}


def test_cli_searches_by_date_and_relevance_then_deduplicates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("applypilot.parser.time.sleep", lambda _: None)
    client = Client([Response(), Response()])
    monkeypatch.setattr("applypilot.parser.requests.Session", lambda: client)
    search = tmp_path / "search.toml"
    search.write_text('''queries = ["Go"]
max_pages = 1
request_budget = 2
details_limit = 0
''', encoding="utf-8")
    data = tmp_path / "data"

    result = main([
        "--search", str(search), "--data-dir", str(data),
        "scan", "--sort-mode", "balanced",
    ])

    assert result == 0
    assert [params["order_by"] for params in client.params] == [
        "publication_time", "relevance",
    ]
    snapshot = json.loads(next((data / "snapshots").glob("hh_vacancies_*.json")).read_text())
    assert [item["id"] for item in snapshot["items"]] == ["1"]
    assert snapshot["items"][0]["sort_sources"] == ["publication_time", "relevance"]


def test_scan_keeps_seen_and_journaled_vacancies_in_snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("applypilot.parser.time.sleep", lambda _: None)
    monkeypatch.setattr("applypilot.parser.requests.Session", lambda: Client([Response(), Response()]))
    search = tmp_path / "search.toml"
    search.write_text('''queries = ["Go"]
details_limit = 0
''', encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "seen.json").write_text('{"1":"2026-09-20"}', encoding="utf-8")
    snapshots = data / "snapshots"
    snapshots.mkdir()
    (snapshots / "hh_vacancies_previous.json").write_text(
        json.dumps({"items": [{"id": "1"}]}), encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "screen-other-track.json").write_text(
        json.dumps({"results": [{"id": "1", "verdict": "SKIP"}]}), encoding="utf-8")
    Store(data / "applypilot.sqlite3").record({"id": "1"}, "prepared", account="default")

    result = main(["--search", str(search), "--data-dir", str(data), "scan"])

    assert result == 0
    newest = max(snapshots.glob("hh_vacancies_*.json"), key=lambda path: path.stat().st_mtime_ns)
    snapshot = json.loads(newest.read_text(encoding="utf-8"))
    assert [item["id"] for item in snapshot["items"]] == ["1"]
    assert snapshot["items"][0]["first_seen"] == "2026-09-20"
    assert snapshot["items"][0]["description_status"] == "provisional"
    assert snapshot["status"] == "ok"


def test_scan_keeps_candidate_when_description_fetch_fails(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("applypilot.parser.time.sleep", lambda _: None)
    client = Client([Response(), Response()])
    monkeypatch.setattr("applypilot.parser.requests.Session", lambda: client)
    monkeypatch.setattr("applypilot.cli.prioritize_for_enrichment",
                        lambda *_args, **_kwargs: [SimpleNamespace(id="1")])

    def failed_detail(items, *_args, **_kwargs):
        for item in items:
            item["description_status"] = "network_error"
        return items, ["1: network error"]

    monkeypatch.setattr("applypilot.cli.enrich_items", failed_detail)
    search = tmp_path / "search.toml"
    search.write_text('''queries = ["Go"]
max_pages = 1
request_budget = 2
details_limit = 10
''', encoding="utf-8")
    data = tmp_path / "data"

    result = main(["--search", str(search), "--data-dir", str(data), "scan"])

    assert result == 0
    snapshot = json.loads(next((data / "snapshots").glob("hh_vacancies_*.json")).read_text())
    assert [item["id"] for item in snapshot["items"]] == ["1"]
    assert snapshot["items"][0]["description_status"] == "network_error"
    assert "1: network error" in snapshot["error"]


@pytest.mark.parametrize("allowed", [None, ["moreThan6"]])
def test_senior_experience_can_be_selected(allowed):
    search = {"role_terms": ["go"], "primary_role_terms": ["go"]}
    if allowed is not None:
        search["experience"] = {"allowed": allowed}
    item = {"id": "1", "name": "Go Backend Engineer", "description": "Golang gRPC PostgreSQL",
            "experience": "moreThan6"}

    selected = filter_candidates([item], {"search": search}, min_score=30)

    assert [candidate.id for candidate in selected] == ["1"]


def test_experience_allowlist_still_excludes_senior_vacancies():
    item = {"id": "1", "name": "Go Backend Engineer", "description": "Golang gRPC PostgreSQL",
            "experience": "moreThan6"}
    search = {"role_terms": ["go"], "experience": {"allowed": ["between1And3"]}}

    assert filter_candidates([item], {"search": search}, min_score=30) == []


def test_empty_truncated_scan_does_not_replace_last_successful_snapshot(tmp_path):
    previous = save_snapshot([{"id": "1"}], tmp_path, "Go", "ok")

    save_snapshot([], tmp_path, "Go", "truncated", "search request budget reached")

    assert (tmp_path / "last_successful.json").read_text() == previous.name
