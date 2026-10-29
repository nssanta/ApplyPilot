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
