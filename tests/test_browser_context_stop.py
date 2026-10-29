"""A lost browser must stop HH dispatch after quarantining one attempt."""

from applypilot.cli import _browser_context_lost


def test_browser_closed_errors_are_source_wide():
    assert _browser_context_lost(
        "browser operation ended ambiguously: Page.goto: "
        "Target page, context or browser has been closed"
    )
    assert _browser_context_lost("Target closed")


def test_other_ambiguous_submit_keeps_source_available():
    assert not _browser_context_lost("first response action had no follow-up dialog")
    assert not _browser_context_lost("CAPTCHA detected after submission")
