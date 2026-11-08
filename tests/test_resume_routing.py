"""Fail-closed resume routing for a unified HH campaign."""

from applypilot.cli import _permitted_hh_resumes

PRIMARY = "Example AI Engineer"
GO = "Example Backend Engineer"


def test_single_track_resume_is_default():
    profile = {"reviewed": True, "resumes": {"go": GO}}
    assert _permitted_hh_resumes(profile, PRIMARY) == {PRIMARY}


def test_reviewed_unified_profile_allows_exact_configured_resumes():
    profile = {
        "reviewed": True,
        "apply": {"allow_profile_resumes": True},
        "resumes": {"default": PRIMARY, "go": GO},
    }
    assert _permitted_hh_resumes(profile, PRIMARY) == {PRIMARY, GO}


def test_unreviewed_profile_cannot_expand_allowed_resumes():
    profile = {
        "reviewed": False,
        "apply": {"allow_profile_resumes": True},
        "resumes": {"go": GO},
    }
    assert _permitted_hh_resumes(profile, PRIMARY) == {PRIMARY}


def test_missing_primary_resume_is_fail_closed():
    profile = {
        "reviewed": True,
        "apply": {"allow_profile_resumes": True},
        "resumes": {"go": GO},
    }
    assert _permitted_hh_resumes(profile, "") == set()


def test_malformed_resume_mapping_is_ignored():
    profile = {"reviewed": True, "apply": {"allow_profile_resumes": True}, "resumes": [GO]}
    assert _permitted_hh_resumes(profile, PRIMARY) == {PRIMARY}


def test_reviewed_profile_routes_resume_by_vacancy_title():
    from applypilot.scoring import route_resume

    profile = {"reviewed": True, "apply": {"allow_profile_resumes": True}}
    assert route_resume(PRIMARY, GO, profile) == GO
    assert route_resume(PRIMARY, "", profile) == PRIMARY


def test_track_resume_stays_fixed_without_per_role_opt_in():
    from applypilot.scoring import route_resume

    assert route_resume(PRIMARY, GO, {"reviewed": True}) == PRIMARY
    assert route_resume(PRIMARY, GO, {"reviewed": False, "apply": {"allow_profile_resumes": True}}) == PRIMARY


def test_hh_scan_routes_resume_by_title_but_other_sources_keep_track_resume():
    from datetime import UTC, datetime, timedelta

    from applypilot.multisource import _prepare_scored_items

    profile = {"reviewed": True, "apply": {"allow_profile_resumes": True},
               "resumes": {"default": PRIMARY, "ai_agent": PRIMARY, "python": GO}}
    item = {"id": "1", "name": "Python-разработчик", "description": "Python FastAPI",
            "published": datetime.now(UTC).isoformat()}
    cutoff = datetime.now(UTC) - timedelta(days=30)
    hh = _prepare_scored_items([dict(item)], source="hh.ru", profile=profile, search={},
                               cutoff=cutoff, track_resume=PRIMARY)
    other = _prepare_scored_items([dict(item)], source="geekjob.ru", profile=profile, search={},
                                  cutoff=cutoff, track_resume=PRIMARY)
    assert hh[0]["resume"] == GO
    assert other[0]["resume"] == PRIMARY


def test_source_specific_resume_name_wins_for_other_sources():
    from datetime import UTC, datetime, timedelta

    from applypilot.multisource import _prepare_scored_items

    profile = {"resumes": {"default": PRIMARY, "geekjob.ru": "Only GeekJob resume"}}
    item = {"id": "1", "name": "Python-разработчик", "description": "Python FastAPI",
            "published": datetime.now(UTC).isoformat()}
    rows = _prepare_scored_items([item], source="geekjob.ru", profile=profile, search={},
                                 cutoff=datetime.now(UTC) - timedelta(days=30), track_resume=GO)
    assert rows[0]["resume"] == "Only GeekJob resume"
