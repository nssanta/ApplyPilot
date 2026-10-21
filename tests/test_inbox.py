from __future__ import annotations

import pytest

from applypilot.inbox import InboxSyncError, sync_hh_inbox
from applypilot.storage import Store


class Response:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def test_inbox_sync_reads_official_chat_api_and_deduplicates_without_sending(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    session = Session([
        Response({"items": [
            {"id": "chat/1", "type": "NEGOTIATION", "vacancy_id": "vacancy-1"},
            {"id": "bot", "type": "BOT", "vacancy_id": ""},
            {"id": "support", "type": "SUPPORT"},
        ], "pages": 1}),
        Response({"has_more": False, "messages": [
            {"id": "m1", "creation_time": "2026-10-21T09:00:00+0000", "type": "TEXT",
             "payload": {"text": "Здравствуйте, расскажите о проекте"},
             "sender_display_info": {"role": "EMPLOYER", "name": "HR"},
             "sender_participant_id": "p1"},
        ]}),
        Response({"has_more": False, "messages": [
            {"id": "m2", "creation_time": "2026-10-21T09:01:00+0000", "type": "TEXT",
             "payload": {"text": "Подтверждение"},
             "sender_display_info": {"role": "BOT", "name": "HH Bot"},
             "sender_participant_id": "p2"},
        ]}),
    ])

    first = sync_hh_inbox(store, "secret-token", "account", max_chats=3, session=session)
    again = sync_hh_inbox(store, "secret-token", "account", max_chats=3, session=Session([
        Response({"items": [
            {"id": "chat/1", "type": "NEGOTIATION", "vacancy_id": "vacancy-1"},
            {"id": "bot", "type": "BOT", "vacancy_id": ""},
        ], "pages": 1}),
        Response({"has_more": False, "messages": [{
            "id": "m1", "creation_time": "2026-10-21T09:00:00+0000", "payload": {"text": "Здравствуйте"},
            "sender_display_info": {"role": "EMPLOYER", "name": "HR"},
        }]}),
        Response({"has_more": False, "messages": [{
            "id": "m2", "creation_time": "2026-10-21T09:01:00+0000", "payload": {"text": "Подтверждение"},
            "sender_display_info": {"role": "BOT", "name": "HH Bot"},
        }]}),
    ]))

    assert first["status"] == "ok"
    assert first["messages_saved"] == 2
    assert again["messages_saved"] == 0
    messages = store.inbox_messages("account")
    assert {row["sender_type"] for row in messages} == {"recruiter", "bot"}
    assert all(row["source"] == "hh.ru" for row in messages)
    assert store.latest_sync("account", "hh.ru/messages")["source"] == "hh.ru/messages"
    assert all(call[0].startswith("https://api.hh.ru/common/chats") for call in session.calls)
    assert all(call[1]["headers"]["Authorization"] == "Bearer secret-token" for call in session.calls)
    assert all(call[1]["timeout"] > 0 for call in session.calls)


def test_inbox_sync_is_bounded_and_marks_message_window_truncated(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    session = Session([
        Response({"items": [{"id": "1", "type": "NEGOTIATION"}], "pages": 1}),
        Response({"has_more": True, "messages": []}),
    ])

    result = sync_hh_inbox(store, "token", max_chats=1, messages_per_chat=50, session=session)

    assert result["status"] == "partial"
    assert result["truncated"] is True
    assert len(session.calls) == 2
    assert session.calls[1][1]["params"] == {"order": "prev", "limit": 50}


def test_inbox_sync_never_persists_raw_attachment_url_or_guesses_bot_from_text(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    session = Session([
        Response({"items": [{"id": "1", "type": "NEGOTIATION", "vacancy_id": "v1"}], "pages": 1}),
        Response({"has_more": False, "messages": [{
            "id": "m1", "creation_time": "2026-10-21T09:00:00+0000", "type": "TEXT",
            "payload": {"text": None, "attachments": [{"url": "https://secret.invalid/file"}]},
            "sender_display_info": {"name": "Auto Helper"},
        }]}),
    ])

    sync_hh_inbox(store, "token", max_chats=1, session=session)
    message = store.inbox_messages("default")[0]

    assert message["sender_type"] == "unknown"
    assert message["body"] == "[вложение без текста]"
    assert "secret.invalid" not in message["raw_json"]


def test_inbox_sync_requires_oauth_token_and_enforces_limits(tmp_path):
    store = Store(tmp_path / "state.sqlite3")

    with pytest.raises(InboxSyncError, match="HH_ACCESS_TOKEN"):
        sync_hh_inbox(store, "")
    with pytest.raises(InboxSyncError, match="max_chats"):
        sync_hh_inbox(store, "token", max_chats=101)
    with pytest.raises(InboxSyncError, match="messages_per_chat"):
        sync_hh_inbox(store, "token", messages_per_chat=51)
