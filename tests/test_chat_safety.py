import pytest

from applypilot.chat_safety import latest_inbound_key, numeric_id, validate_binding


def thread():
    return {
        "thread_id": "1", "vacancy_id": "2", "company": "Company", "complete": True,
        "messages": [{"id": "message-3", "direction": "inbound", "text": "Question?"}],
    }


def test_message_fingerprint_and_binding():
    value = thread()
    key = latest_inbound_key(value["messages"], True)
    validate_binding(value, "1", "2", "Company", key)
    value["messages"][-1]["text"] = "Changed question"
    with pytest.raises(ValueError):
        validate_binding(value, "1", "2", "Company", key)


def test_direction_completeness_and_attachment_failclosed():
    value = thread()
    assert latest_inbound_key(value["messages"], False) is None
    value["messages"][-1]["direction"] = "outbound"
    assert latest_inbound_key(value["messages"], True) is None
    value["messages"][-1]["direction"] = "inbound"
    value["messages"][-1]["text"] = ""
    assert latest_inbound_key(value["messages"], True) is None


def test_wrong_company_binding_rejected():
    value = thread()
    key = latest_inbound_key(value["messages"], True)
    with pytest.raises(ValueError):
        validate_binding(value, "1", "2", "Other company", key)


@pytest.mark.parametrize("value", ["", "-1", "1/2", "１２３", " 1", "1 "])
def test_numeric_identifier_is_strict(value):
    with pytest.raises(ValueError):
        numeric_id(value)


def test_fingerprint_changes_with_attachments():
    first = thread()
    second = thread()
    second["messages"][-1]["attachments"] = [{"name": "file.pdf"}]
    assert latest_inbound_key(first["messages"], True) != latest_inbound_key(
        second["messages"], True
    )
