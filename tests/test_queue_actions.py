from applypilot.queue_actions import next_action


def test_repeated_decisions_do_not_hide_ready_package():
    row = {'decision': 'APPLY', 'application_state': 'READY_TO_APPLY',
           'review_state': 'APPROVED', 'decision_streak': 8}
    assert next_action(row)['next_action'] == 'send'


def test_repeated_decisions_keep_missing_description_actionable():
    row = {'decision': 'REVIEW', 'application_state': 'NOT_STARTED',
           'description_status': 'missing', 'decision_streak': 8}
    assert next_action(row)['next_action'] == 'enrich'


def test_unknown_stays_quarantined_even_when_approved():
    row = {'decision': 'APPLY', 'application_state': 'UNKNOWN',
           'review_state': 'APPROVED', 'decision_streak': 8}
    assert next_action(row)['next_action'] == 'verify_unknown'
