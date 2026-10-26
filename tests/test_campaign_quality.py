from datetime import UTC, datetime, timedelta

import pytest

from applypilot import cli, multisource
from applypilot.config import AppConfig, effective_search
from applypilot.decision import decide_row, sweep_track_decisions
from applypilot.scoring import score_vacancy
from applypilot.storage import AUTO_APPROVAL_REASON, Store


def vacancy(title, vid="1"):
    return {"id": vid, "name": title, "company": "Example", "source": "hh.ru",
            "url": f"https://hh.ru/vacancy/{vid}", "description_status": "ok",
            "description": "Build Python LLM agents, RAG, APIs and FastAPI services with Docker.",
            "is_remote": True, "published": datetime.now(UTC).isoformat(),
            "salary": {"from": 200000, "to": 220000, "currency": "RUR"}}


@pytest.mark.parametrize("title", ["AQA инженер (Python)", "QA Python Engineer", "SDET Python Engineer",
                                   "Test Automation Engineer Python", "Java Backend Developer"])
def test_python_preset_excludes_foreign_primary_roles(title):
    profile = {"search": effective_search({}, "python-backend"), "rescore": True}
    assert score_vacancy(vacancy(title), profile).decision == "reject"


@pytest.mark.parametrize("title", ["AI Engineer (Fullstack)", "MLOps / DevOps Engineer (LLM, GPU)",
                                   "AI Backend Developer"])
def test_configurable_ai_roles_are_not_rejected_for_engineering_duties(title):
    search = effective_search({}, "ai-llm")
    search["title_role_terms"] += ["llmops"]
    assert score_vacancy(vacancy(title), {"search": search, "rescore": True}).decision == "pass"


def test_incomplete_description_does_not_become_a_final_rejection():
    decision = decide_row({**vacancy("Python developer"), "description": "",
                           "description_status": "provisional", "score_decision": "reject",
                           "filter_status": "unknown"})
    assert (decision.action, decision.reason_code) == ("REVIEW", "needs_description")


def test_stale_automatic_approval_does_not_bypass_current_requirements():
    row = {**vacancy("Senior Python Developer"), "review_state": "APPROVED",
           "review_reason": AUTO_APPROVAL_REASON, "application_state": "READY_TO_APPLY",
           "score_decision": "pass", "filter_status": "pass",
           "problems": ["grade:seniority_check"]}
    assert decide_row(row, profile={"experience_years": 3}).action == "REVIEW"


def test_rescore_recovers_old_rejects_and_preserves_owner_and_attempts(tmp_path):
    cfg = AppConfig.discover(tmp_path)
    store = Store(cfg.db_path)
    search = effective_search({}, "ai-llm")
    old = {**search, "exclude_titles": ["fullstack"]}
    profile = {"resume": "Example", "resumes": {"default": "Example"}}
    raw = [vacancy("AI Engineer (Fullstack)", str(i)) for i in range(4)]
    items = multisource._prepare_scored_items(raw, source="hh.ru", profile=profile, search=old,
        cutoff=datetime.now(UTC) - timedelta(days=30), track_resume="Example")
    store.start_campaign("old", "default", "default", {})
    store.save_campaign_items("old", "default", "default", items, status="ok", snapshot_path="x")
    store.set_review_state("default", "hh.ru", "1", "SKIP", reason="owner veto", track="default")
    store.record(raw[2], "unknown", "ambiguous", account="default")
    store.record(raw[3], "success", "receipt", account="default")
    # Synchronize ledger state using normal persistence before refreshing.
    store.start_campaign("sync", "default", "default", {})
    store.save_campaign_items("sync", "default", "default", items, status="ok", snapshot_path="x")
    multisource.refresh_queue(cfg, "default", profile, search, "hh.ru")
    sweep_track_decisions(store, "default", "default", "hh.ru", search=search, profile=profile)
    rows = {r["id"]: r for r in store.list_campaign_vacancies("default", "default", "all", None)}
    assert rows["0"]["decision"] == "APPLY"
    assert rows["1"]["review_state"] == "SKIP"
    assert rows["2"]["global_application_state"] == "UNKNOWN"
    assert rows["3"]["global_application_state"] == "APPLIED_CONFIRMED"


def test_cli_salary_remote_and_region_overrides_reach_scanner_and_rescore(tmp_path, monkeypatch):
    cfg = AppConfig.discover(tmp_path)
    cfg.profile_path.parent.mkdir(parents=True)
    cfg.profile_path.write_text('[resumes]\ndefault="Example"\n')
    cfg.search_path.write_text('queries=["Python"]\nonly_remote=false\n[salary]\nfrom=100000\ncurrency="RUR"\n')
    monkeypatch.setattr(cli, "_config", lambda _: cfg)
    monkeypatch.setattr(cli, "_track_config", lambda *a, **kw: (cfg, {"resume": "Example"}))
    seen = {}

    def scan(config, track, profile, search, **kwargs):
        seen.update(search)
        store = Store(config.db_path)
        store.start_campaign("scan", "default", track, {})
        items = multisource._prepare_scored_items([vacancy("Python Developer")], source="hh.ru",
            profile=profile, search=search, cutoff=datetime.now(UTC) - timedelta(days=30), track_resume="Example")
        store.save_campaign_items("scan", "default", track, items, status="ok", snapshot_path="x")
        return {"sources": [{"source": "hh.ru", "status": "ok", "candidate_ids": ["1"]}],
                "partial_sources": [], "failed_sources": []}

    monkeypatch.setattr(multisource, "scan_sources", scan)
    monkeypatch.setattr(cli, "_run_apply", lambda *a, **kw: pytest.fail("scan must never submit"))
    assert cli.main(["run", "campaign", "--track", "default", "--source", "hh.ru",
                     "--salary-from", "250000", "--remote", "--area", "1", "--details-limit", "0"]) == 0
    assert seen["salary"]["from"] == 250000 and seen["only_remote"] is True and seen["areas"] == [1]
    row = Store(cfg.db_path).list_campaign_vacancies("default", "default", "all", 1)[0]
    assert row["filter_status"] == "reject" and row["decision"] == "REJECT"


def test_ai_role_can_be_proven_by_duties_under_a_python_title():
    from applypilot.scoring import auto_apply_title_eligible, prioritize_for_enrichment

    search = effective_search({}, 'ai-agents-llmops')
    search['auto_apply_title_terms'] = ['llm', 'agent']
    row = vacancy('Python Backend Developer')
    profile = {'search': search, 'rescore': True}
    assert score_vacancy(row, profile).decision == 'pass'
    assert auto_apply_title_eligible(row, search)
    provisional = {**row, 'description': '', 'description_status': 'provisional'}
    assert [r.id for r in prioritize_for_enrichment([provisional], profile, 10)] == ['1']
    irrelevant = {**row, 'description': 'Maintain Python Django payment APIs.'}
    assert score_vacancy(irrelevant, profile).decision == 'reject'


def test_senior_title_does_not_invent_a_five_year_requirement():
    row = {**vacancy('Senior Python Developer'), 'experience': 'between3And6',
           'score_decision': 'pass', 'filter_status': 'pass', 'problems': ['grade:seniority_check']}
    assert decide_row(row, profile={'experience_years': 3}).action == 'APPLY'
    row['description'] = 'Required experience: 5+ years of Python development.'
    assert decide_row(row, profile={'experience_years': 3}).action == 'REJECT'


def test_missing_description_is_deferred_without_starving_other_cards(tmp_path, monkeypatch):
    cfg = AppConfig.discover(tmp_path)
    store = Store(cfg.db_path)
    search = effective_search({}, 'ai-llm')
    profile = {'resume': 'Example', 'resumes': {'default': 'Example'}}
    raw = [{**vacancy('AI Engineer', str(i)), 'description': 'LLM Python',
            'description_status': 'provisional'} for i in (1, 2)]
    store.start_campaign('pending', 'default', 'default', {})
    store.save_campaign_items('pending', 'default', 'default', raw, status='ok', snapshot_path='x')
    seen = []

    def enrich(items, limit, **kwargs):
        seen.append(items[0]['id'])
        if len(seen) == 1:
            return [{**items[0], 'description_status': 'network_error'}], ['temporary network error']
        return [{**items[0], 'description': 'Build Python LLM agents.', 'description_status': 'ok'}], []

    monkeypatch.setattr('applypilot.parser.enrich_items', enrich)
    first = multisource.refresh_queue(cfg, 'default', profile, search, 'hh.ru', details_limit=1)
    second = multisource.refresh_queue(cfg, 'default', profile, search, 'hh.ru', details_limit=1)
    assert seen == ['1', '2']
    assert first['description_deferred'] == 1 and first['needs_description'] == 1
    assert second['description_deferred'] == 1 and second['needs_description'] == 0
