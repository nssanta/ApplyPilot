"""Mock the Telegram transport: verify callback selection and actual bot receipts.

No real Telegram credentials, network or applications are used.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from applypilot.getmatch_apply import click_and_confirm, probe_getmatch_callback
from applypilot.storage import Store
from applypilot.telegram_client import save_credentials


class CallbackButton:
    def __init__(self, label: str, data: bytes | None) -> None:
        self.text = label
        self.data = data
        self.url = None
        self.button = SimpleNamespace()


class FakeMessage:
    def __init__(self, message_id: int, text: str, buttons: list[CallbackButton], *,
                 sender_id: int = 42, outgoing: bool = False) -> None:
        self.id = message_id
        self.text = text
        self.message = text
        self.date = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)
        self.buttons = [buttons] if buttons else []
        self.out = outgoing
        self.sender_id = sender_id
        self.edit_date = None
        self.click_count = 0
        self.on_click = None

    async def click(self, *, i: int) -> None:
        assert i == 0
        self.click_count += 1
        if self.on_click:
            self.on_click()


class FakeBotClient:
    def __init__(self, *, proof: bool = True, sender_id: int = 42) -> None:
        button = CallbackButton("👉 Откликнуться в боте", b"apply:35881")
        self.original = FakeMessage(
            100, "LLM Developer https://getmatch.ru/vacancies/35881", [button],
            sender_id=sender_id,
        )
        self.confirmation = FakeMessage(
            101, "✅ Отклик отправлен. https://getmatch.ru/vacancies/35881", [],
        )
        self.proof = proof
        self.connected = False
        self.disconnects = 0
        self.original.on_click = self._after_click

    def _after_click(self):
        self.sent = True

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnects += 1

    async def get_entity(self, username):
        assert username == "@g_jobbot"
        return SimpleNamespace(id=42)

    async def get_messages(self, entity, *, ids=None, limit=None):
        if ids is not None:
            return self.original
        if getattr(self, "sent", False) and self.proof:
            return [self.confirmation, self.original]
        return [self.original]


def plan() -> dict:
    return {
        "vacancy_id": "35881",
        "message_id": 100,
        "button_index": 0,
        "callback_sha256": hashlib.sha256(b"apply:35881").hexdigest()[:16],
    }


def client_factory_for(client: FakeBotClient):
    def factory(*_args):
        return client
    return factory


def test_readonly_probe_checks_exact_live_hash_without_click(tmp_path: Path) -> None:
    save_credentials(tmp_path, 1, "synthetic-api-hash")
    client = FakeBotClient()
    result = probe_getmatch_callback(
        tmp_path, "getmatch-primary", plan(),
        client_factory=client_factory_for(client),
    )
    assert result["status"] == "live_callback_present"
    assert result["sent"] is False
    assert client.original.click_count == 0
    assert client.disconnects == 1


def test_verified_bot_reply_confirms_exact_vacancy_and_persists(tmp_path: Path) -> None:
    save_credentials(tmp_path, 1, "synthetic-api-hash")
    store = Store(tmp_path / "jobs.sqlite3")
    client = FakeBotClient()
    result = click_and_confirm(
        tmp_path, "getmatch-primary", store, plan(), max_checks=1,
        client_factory=client_factory_for(client),
    )
    assert result["status"] == "success"
    assert result["receipt"]["vacancy_id"] == "35881"
    assert result["receipt"]["confirmation_message_id"] == 101
    assert client.original.click_count == 1
    assert client.disconnects == 1
    saved = store.telegram_messages("getmatch-primary")
    assert saved[0]["outgoing"] is False


def test_callback_click_without_fresh_proof_is_unknown(tmp_path: Path) -> None:
    save_credentials(tmp_path, 1, "synthetic-api-hash")
    store = Store(tmp_path / "jobs.sqlite3")
    client = FakeBotClient(proof=False)
    result = click_and_confirm(
        tmp_path, "getmatch-primary", store, plan(), max_checks=1,
        client_factory=client_factory_for(client),
    )
    assert result["status"] == "unknown"
    assert client.original.click_count == 1


def test_callback_sender_mismatch_aborts_before_click(tmp_path: Path) -> None:
    save_credentials(tmp_path, 1, "synthetic-api-hash")
    store = Store(tmp_path / "jobs.sqlite3")
    client = FakeBotClient(sender_id=999)
    result = click_and_confirm(
        tmp_path, "getmatch-primary", store, plan(), max_checks=1,
        client_factory=client_factory_for(client),
    )
    assert result["status"] == "failed_before_submit"
    assert client.original.click_count == 0


def test_changed_callback_fingerprint_aborts_before_click(tmp_path: Path) -> None:
    save_credentials(tmp_path, 1, "synthetic-api-hash")
    store = Store(tmp_path / "jobs.sqlite3")
    client = FakeBotClient()
    expected = {**plan(), "callback_sha256": "0" * 16}
    result = click_and_confirm(
        tmp_path, "getmatch-primary", store, expected, max_checks=1,
        client_factory=client_factory_for(client),
    )
    assert result["status"] == "failed_before_submit"
    assert client.original.click_count == 0
