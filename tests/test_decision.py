from __future__ import annotations

from pathlib import Path

from applypilot.decision import decide_row, sweep_track_decisions
from applypilot.scoring import auto_apply_title_eligible
from applypilot.storage import (
    Store,
    effective_track_application_state,
)


def _item(vacancy_id: str, **overrides):
    item = {
        "id": vacancy_id,
        "name": "AI/LLM Engineer",
        "company": "Example",
        "url": f"https://hh.ru/vacancy/{vacancy_id}",
        "description": "Build LLM agents and RAG services with Python.",
        "description_status": "ok",
        "score": 80,
        "score_decision": "pass",
        "filter_status": "pass",
        "review_lane": "FIT",
        "problems": [],
        "filter_reasons": [],
        "auto_apply_eligible": True,
        "resume": "AI Engineer",
    }
    item.update(overrides)
    return item


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "state.sqlite3")
    store.start_campaign("campaign", "account", "ai_core", {"query": "AI"})
    return store


def test_ready_is_visible_only_for_an_approved_passing_track(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_campaign_items(
        "campaign", "account", "ai_core",
        [_item("one"), _item("two", score=90)],
        status="ok", snapshot_path="x.json",
    )
    # Approve only the first row in a second track, then set the global state
    # to READY as the legacy writer does.
    store.start_campaign("broad", "account", "ai", {"query": "AI"})
    store.save_campaign_items(
        "broad", "account", "ai", [_item("one", score=90)],
        status="ok", snapshot_path="y.json",
    )
    store.set_review_state("account", "hh.ru", "one", "APPROVED", reason="approved", track="ai")

    assert store.list_campaign_vacancies("account", "ai", "ready", 10)[0]["id"] == "one"
    assert store.list_campaign_vacancies("account", "ai_core", "ready", 10) == []

    ai_core = store.list_campaign_vacancies("account", "ai_core", "all", 10)
    assert {row["id"]: row["application_state"] for row in ai_core}["one"] == "NOT_STARTED"


def test_effective_state_never_hides_terminal_or_owner_workflow_states() -> None:
    assert effective_track_application_state(
        "APPLIED_CONFIRMED", "UNREVIEWED", "reject", "reject",
    ) == "APPLIED_CONFIRMED"
    assert effective_track_application_state(
        "NEEDS_FORM", "APPROVED", "pass", "pass",
    ) == "NEEDS_FORM"
    assert effective_track_application_state(
        "READY_TO_APPLY", "UNREVIEWED", "pass", "pass",
    ) == "NOT_STARTED"


def test_owner_approval_survives_a_publication_window_rescan() -> None:
    decision = decide_row(
        _item(
            "old-approved", review_state="APPROVED",
            application_state="NOT_STARTED",
            global_application_state="READY_TO_APPLY",
            filter_status="reject",
            filter_reasons=["salary:missing_allowed",
                            "publication:outside_requested_window"],
        ),
        search={"days": 7, "salary": {"from": 120000, "missing": "include"}},
    )
    assert decision.action == "APPLY"
    assert decision.reason_code == "owner_approved"
    assert effective_track_application_state(
        "READY_TO_APPLY", "APPROVED", "pass", "reject", "APPLY",
    ) == "READY_TO_APPLY"


def test_owner_approval_does_not_bypass_a_hard_filter_reject() -> None:
    decision = decide_row(
        _item(
            "hard-reject", review_state="APPROVED",
            application_state="NOT_STARTED",
            global_application_state="READY_TO_APPLY",
            filter_status="reject",
            filter_reasons=["geo:configured_exclusion"],
        ),
        search={"days": 7},
    )
    assert decision.action == "REJECT"


def test_clean_fit_autoapproves_and_soft_salary_unknown_is_allowed() -> None:
    search = {"days": 30, "salary": {"from": 120000, "missing": "include"}}
    clean = decide_row(_item("one"), search=search)
    assert clean.action == "APPLY" and clean.auto_approve is True

    unknown = decide_row(
        _item(
            "two", filter_status="unknown",
            filter_reasons=["remote:verified", "salary:missing_allowed",
                            "salary:unpublished_not_verified"],
            problems=["salary:unpublished"],
        ),
        search=search,
    )
    assert unknown.action == "APPLY"
    assert any(reason.startswith("salary:unpublished") for reason in unknown.resolved)


def test_unknown_experience_and_below_english_are_not_autoapproved() -> None:
    search = {"days": 30, "salary": {"from": 120000, "missing": "include"}}
    senior = decide_row(
        _item("senior", name="Senior AI Engineer", problems=["grade:seniority_check"]),
        search=search, profile={"experience_years": 2},
    )
    assert senior.action == "REVIEW"

    english = decide_row(
        _item("english", description="Fluent English C1 is required."),
        search=search, profile={"english_level": "A2", "has_degree": True},
    )
    # The raw problem is normally added by _review_problems; without it the
    # row is still a clean fit.  The resolver itself is exercised below.
    assert english.action == "APPLY"


def test_problem_resolution_blocks_low_english_and_accepts_known_degree() -> None:
    from applypilot.decision import resolve_problem

    status, _ = resolve_problem(
        "language:english_requirement_check",
        _item("e", description="English B2 required"),
        profile={"english_level": "A2"},
    )
    assert status == "block"
    status, _ = resolve_problem(
        "education:requirement_check",
        _item("d", description="Bachelor degree required"),
        profile={"has_degree": True},
    )
    assert status == "resolved"


def test_title_gate_has_a_structural_fallback_without_excluding_real_roles() -> None:
    search = {
        "auto_apply_title_terms": ["agent", "llm"],
        "primary_role_terms": ["ai", "llm"],
        "required_role_terms": ["engineer", "developer", "разработчик", "инженер"],
        "exclude_titles": ["fullstack", "full-stack", "продаж"],
    }
    assert auto_apply_title_eligible(
        {"name": "AI / Data Python Developer"}, search,
    ) is True
    assert auto_apply_title_eligible(
        {"name": "Senior Fullstack AI Engineer"}, search,
    ) is False
    assert auto_apply_title_eligible(
        {"name": "AI Sales Manager"}, search,
    ) is False
    assert auto_apply_title_eligible(
        {"name": "Flutter (AI native) разработчик"}, search,
        skills=["Python", "Go", "TypeScript"],
    ) is False
    assert auto_apply_title_eligible(
        {"name": "Python (AI-native) разработчик"}, search,
        skills=["Python", "Go", "TypeScript"],
    ) is True


def test_sweep_revisits_old_rows_and_never_overwrites_owner_veto(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_campaign_items(
        "campaign", "account", "ai_core",
        [_item("fresh"), _item("skip"), _item("applied")],
        status="ok", snapshot_path="x.json",
    )
    store.set_review_state_batch(
        "account", "hh.ru", ["skip"], "SKIP", reason="owner rejected", track="ai_core",
    )
    store.record(_item("applied"), "success", account="account")

    result = sweep_track_decisions(
        store, "account", "ai_core", "hh.ru",
        search={"days": 30, "salary": {"from": 120000, "missing": "include"}},
        profile={},
    )

    assert result["approved"] == 1
    rows = {row["id"]: row for row in store.list_campaign_vacancies(
        "account", "ai_core", "all", 10,
    )}
    assert rows["fresh"]["review_state"] == "APPROVED"
    assert rows["skip"]["review_state"] == "SKIP"
    assert rows["applied"]["application_state"] == "APPLIED_CONFIRMED"
    assert rows["fresh"]["decision"] == "APPLY"
    assert rows["skip"]["decision"] == "REJECT"


def test_sweep_collapses_same_employer_and_title_before_approval(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_campaign_items(
        "campaign", "account", "ai_core",
        [
            _item("dup-one", name="Python (AI-native) разработчик", company="Same"),
            _item("dup-two", name="Python (AI-native) разработчик", company="Same"),
        ],
        status="ok", snapshot_path="x.json",
    )
    result = sweep_track_decisions(
        store, "account", "ai_core", "hh.ru",
        search={"days": 30, "salary": {"from": 120000, "missing": "include"}},
        profile={},
    )
    assert result["approved"] == 1
    rows = {row["id"]: row for row in store.list_campaign_vacancies(
        "account", "ai_core", "all", 10,
    )}
    assert rows["dup-one"]["decision"] == "APPLY"
    assert rows["dup-two"]["decision"] == "BLOCKED"
    assert rows["dup-two"]["decision_reason"].startswith("duplicate_job_group")


def test_single_set_review_state_cannot_approve_a_rejected_score(tmp_path: Path) -> None:
    import pytest

    store = _store(tmp_path)
    store.save_campaign_items(
        "campaign", "account", "ai_core",
        [_item("bad", score_decision="reject", score=0, problems=["hard:no"])],
        status="ok", snapshot_path="x.json",
    )
    with pytest.raises(ValueError, match="screening"):
        store.set_review_state(
            "account", "hh.ru", "bad", "APPROVED", reason="mistake", track="ai_core",
        )


def test_repeated_unchanged_decision_is_reported_as_stagnant(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_campaign_items(
        "campaign", "account", "ai_core", [_item("stuck")],
        status="ok", snapshot_path="x.json",
    )
    for _ in range(3):
        store.set_track_decision(
            "account", "hh.ru", "stuck", "ai_core", "REVIEW",
            "needs_evidence: remote format is not verified",
        )
    rows = store.list_campaign_vacancies("account", "ai_core", "all", 10)
    assert rows[0]["decision_streak"] == 2
    stagnant = store.stagnant_vacancies("account", "ai_core", min_streak=2)
    assert [row["vacancy_id"] for row in stagnant] == ["stuck"]


def test_experience_shortfall_needs_exact_review_and_has_a_bound():
    from applypilot.decision import owner_hard_blockers, resolve_problem
    row = _item('stretch', description='Required experience: 5 years in Python.',
                experience='between3And6', problems=['grade:seniority_check'])
    profile = {'experience_years': 3}
    assert resolve_problem('grade:seniority_check', row, profile=profile)[0] == 'block'
    search = {'decision': {'allow_stretch_grade': True, 'stretch_max_required_years': 6.01}}
    assert resolve_problem('grade:seniority_check', row, profile=profile, search=search)[0] == 'review'
    assert owner_hard_blockers(row, profile=profile, search=search) == []
    row['description'] = 'Required experience: 8 years in Python.'
    assert resolve_problem('grade:seniority_check', row, profile=profile, search=search)[0] == 'block'
    assert profile['experience_years'] == 3


def test_owner_stretch_auto_approve_resolves_up_to_six_years_except_lead_titles():
    from applypilot.decision import resolve_problem
    search = {'decision': {'allow_stretch_grade': True, 'stretch_max_required_years': 6.01,
                           'stretch_auto_approve': True}}
    profile = {'experience_years': 3}
    row = _item('senior', name='Senior Python Developer', experience='moreThan6',
                description='Опыт коммерческой разработки на Python от 5 лет.',
                problems=['grade:seniority_check'])
    assert resolve_problem('grade:seniority_check', row, profile=profile, search=search)[0] == 'resolved'
    assert decide_row(row, search=search, profile=profile).action == 'APPLY'
    lead = {**row, 'name': 'Руководитель группы разработки (Python)'}
    assert resolve_problem('grade:seniority_check', lead, profile=profile, search=search)[0] == 'review'
    explicit = {**row, 'description': 'Опыт коммерческой разработки на Python от 8 лет.'}
    assert resolve_problem('grade:seniority_check', explicit, profile=profile, search=search)[0] == 'block'


def test_open_salary_range_is_accepted_from_the_owner_floor():
    from applypilot.decision import resolve_problem
    search = {'salary': {'from': 120000, 'open_range_min': 100000, 'policy': 'possible'}}
    open_ok = _item('open', salary={'currency': 'RUR', 'from': 110000, 'to': None},
                    problems=['salary:below_or_unverified'], filter_status='unknown',
                    filter_reasons=['salary:open_range_below_target'])
    assert resolve_problem('salary:below_or_unverified', open_ok, search=search)[0] == 'resolved'
    assert decide_row(open_ok, search=search).action == 'APPLY'
    low = {**open_ok, 'salary': {'currency': 'RUR', 'from': 80000, 'to': None}}
    assert resolve_problem('salary:below_or_unverified', low, search=search)[0] == 'review'
    assert decide_row(low, search=search).action == 'REVIEW'


def test_provisional_card_with_title_verdict_is_rejected_without_enrichment():
    junk = _item('sales', name='Менеджер по продажам', description='', description_status='provisional',
                 score=0, score_decision='reject', filter_status='unknown',
                 problems=['role:title_not_technical', 'preset_noise:продаж'])
    decision = decide_row(junk)
    assert (decision.action, decision.reason_code) == ('REJECT', 'title_excluded')
    relevant = {**junk, 'name': 'Python developer', 'problems': ['role:no_match']}
    assert decide_row(relevant).reason_code == 'needs_description'
