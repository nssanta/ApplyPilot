from __future__ import annotations

import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from applypilot.accounts import telegram_queue_plan, unified_accounts
from applypilot.hh_oauth import (
    HhOAuthAccount,
    HhOAuthCredentials,
    HhOAuthStateError,
    hh_oauth_status,
)
from applypilot.hh_oauth import (
    LiveActionDisabled as HhLiveDisabled,
)
from applypilot.hh_oauth import (
    save_credentials as save_hh_credentials,
)
from applypilot.hh_oauth import (
    state_path as hh_state_path,
)
from applypilot.hh_oauth import (
    token_path as hh_token_path,
)
from applypilot.secure_store import read_private_json, write_private_json
from applypilot.storage import Store
from applypilot.telegram_client import (
    LiveActionDisabled as TelegramLiveDisabled,
)
from applypilot.telegram_client import (
    TelegramAccount,
    TelegramCredentials,
    classify_button,
    session_path,
)
from applypilot.telegram_client import (
    credentials_status as telegram_credentials_status,
)
from applypilot.telegram_client import (
    save_credentials as save_tg_credentials,
)


class FakeButton:
    def __init__(self, text: str, data: bytes | None = None, url: str | None = None,
                 raw: object | None = None) -> None:
        self.text = text
        self.data = data
        self.url = url
        self.button = raw


class FakeMessage:
    def __init__(self, message_id: int, text: str, buttons: list[list[FakeButton]]) -> None:
        self.id = message_id
        self.text = text
        self.date = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        self.buttons = buttons
        self.out = False


class FakeTelegramClient:
    def __init__(self, session: Path, api_id: int, api_hash: str,
                 messages: list[FakeMessage], state: dict[str, object]) -> None:
        # These assertions catch the audit defect: Telethon must never receive
        # a session path whose account directory does not exist yet.
        assert session.parent.is_dir(), "account directory must exist before Telethon"
        assert session.is_file(), "session file must be pre-created with 0600"
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.messages = messages
        self.state = state
        self.authorized = bool(state["authorized"])
        self.disconnect_calls = 0
        self.fail_start = False
        self.fail_get_me = False

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.state["disconnects"] = int(self.state["disconnects"]) + 1

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> SimpleNamespace:
        if self.fail_get_me:
            raise RuntimeError("get_me failed")
        return SimpleNamespace(id=7, username="tester", first_name="Test")

    async def get_entity(self, name: str) -> SimpleNamespace:
        assert name == "@g_jobbot"
        return SimpleNamespace(username="g_jobbot")

    async def iter_messages(self, entity: object, limit: int = 50):
        for message in self.messages[:limit]:
            yield message

    async def start(self, **kwargs: object) -> None:
        if self.fail_start:
            raise RuntimeError("start failed")
        self.authorized = True
        self.state["authorized"] = True
        self.start_kwargs = kwargs


class FakeTelegramFactory:
    """One shared session state, a fresh client object per call like Telethon."""

    def __init__(self, messages: list[FakeMessage]) -> None:
        self.messages = list(messages)
        self.state: dict[str, object] = {"authorized": False, "disconnects": 0}
        self.instances: list[FakeTelegramClient] = []
        self.fail_start = False
        self.fail_get_me = False

    def __call__(self, session: Path, api_id: int, api_hash: str) -> FakeTelegramClient:
        client = FakeTelegramClient(session, api_id, api_hash, self.messages, self.state)
        client.fail_start = self.fail_start
        client.fail_get_me = self.fail_get_me
        self.instances.append(client)
        return client


def _fake_factory(messages: list[FakeMessage]) -> FakeTelegramFactory:
    return FakeTelegramFactory(messages)


def _seed_queue(store: Store, account: str = "getmatch-primary") -> None:
    stamp = "2026-10-20T00:00:00+00:00"
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO vacancies
            (account,source,vacancy_id,title,company,url,created_at,first_seen_at,last_seen_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (account, "getmatch.ru", "36887", "AI Engineer", "Acme",
             "https://getmatch.ru/vacancies/36887", stamp, stamp, stamp, stamp),
        )
        conn.execute(
            """INSERT INTO vacancy_track_scores
            (account,source,vacancy_id,track,score,score_decision,filter_status,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (account, "getmatch.ru", "36887", "ai_core", 88, "pass", "pass", stamp),
        )


def test_private_json_is_0600(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "token.json"
    write_private_json(path, {"token": "abc"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert read_private_json(path) == {"token": "abc"}


def test_telegram_credentials_are_redacted(tmp_path: Path) -> None:
    path = save_tg_credentials(tmp_path, 12345, "hash-value")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    status = telegram_credentials_status(tmp_path)
    assert status["status"] == "configured"
    assert status["api_hash"] == "***"
    assert "hash-value" not in json.dumps(status)


def test_telegram_missing_dependency_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_tg_credentials(tmp_path, 1, "hash")
    session = session_path(tmp_path, "getmatch-primary")
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text("fake", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "telethon", None)
    account = TelegramAccount(
        tmp_path, "getmatch-primary",
        credentials=TelegramCredentials(1, "hash"),
    )
    result = account.status()
    assert result["status"] == "not_configured"
    assert "Telethon" in result["detail"]


def test_telegram_button_classification() -> None:
    assert classify_button(FakeButton("Apply", data=b"cb"))["kind"] == "callback"
    assert classify_button(FakeButton("Open", url="https://getmatch.ru"))["kind"] == "url"
    assert classify_button(FakeButton("Open", raw=type("KeyboardButtonWebView", (), {})()))["kind"] == "webview"
    mixed = classify_button(FakeButton(
        "Open", url="https://getmatch.ru/vacancies/1",
        raw=type("KeyboardButtonWebView", (), {})(),
    ))
    assert mixed["kind"] == "webview"
    assert mixed["url"] == "https://getmatch.ru/vacancies/1"
    assert classify_button(FakeButton("Plain"))["kind"] == "text"


def test_telegram_history_sync_and_dry_run_plan(tmp_path: Path) -> None:
    save_tg_credentials(tmp_path, 1, "hash")
    message = FakeMessage(
        10,
        "Вакансия AI Engineer\nhttps://getmatch.ru/vacancies/36887\nОткликнуться",
        [[FakeButton("Откликнуться", data=b"apply:36887")], [FakeButton("Открыть", url="https://getmatch.ru/vacancies/36887")]],
    )
    account = TelegramAccount(
        tmp_path, "getmatch-primary",
        credentials=TelegramCredentials(1, "hash"),
        client_factory=_fake_factory([message]),
    )
    login = account.login("+10000000000")
    assert login["status"] == "confirmed"
    assert login["user_id"] == 7
    assert account.status()["status"] == "confirmed"

    history = account.history(10)
    assert history[0]["message_id"] == 10
    assert history[0]["application_likely"] is True
    assert history[0]["vacancy_refs"] == [{"source": "getmatch.ru", "id": "36887"}]
    assert history[0]["buttons"][0]["kind"] == "callback"

    store = Store(tmp_path / "applypilot.sqlite3")
    assert account.sync(store, 10)["saved"] == 1
    _seed_queue(store)

    plan = telegram_queue_plan(store, "getmatch-primary", "ai_core", 10)
    assert plan["planned_count"] == 1
    assert plan["mode"] == "dry_run"
    assert plan["planned"][0]["vacancy_id"] == "36887"
    assert plan["planned"][0]["can_send"] is False
    assert plan["planned"][0]["action"] == "needs_live_validation"

    stored = store.telegram_messages("getmatch-primary", 10)[0]
    action = TelegramAccount.plan_action(stored, 0)
    assert action["can_send"] is False
    assert action["action"] == "needs_live_validation"
    with pytest.raises(TelegramLiveDisabled):
        account.click(10, 0, allow_live=False)


def _hh_handler(calls: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/token":
            data = parse_qs(request.content.decode())
            if data["grant_type"][0] == "refresh_token":
                return httpx.Response(200, json={
                    "access_token": "access-2", "refresh_token": "refresh-2",
                    "expires_in": 3600, "token_type": "bearer",
                })
            return httpx.Response(200, json={
                "access_token": "access-1", "refresh_token": "refresh-1",
                "expires_in": 3600, "token_type": "bearer",
            })
        if request.url.path == "/me":
            return httpx.Response(200, json={"id": 42, "is_applicant": True})
        if request.url.path == "/negotiations":
            return httpx.Response(200, json={"items": [{
                "id": "neg-1", "state": "active",
                "vacancy": {"id": "123", "name": "AI Engineer"},
                "resume": {"id": "resume-1"},
            }]})
        if request.url.path == "/negotiations/neg-1/messages":
            return httpx.Response(200, json={"items": [{
                "id": "m1", "text": "Hello from recruiter",
                "created_at": "2026-10-20T10:00:00+00:00",
                "author": {"name": "Recruiter"},
            }]})
        return httpx.Response(404)
    return handler


def test_hh_oauth_authorize_state_is_local_and_checked(tmp_path: Path) -> None:
    credentials = HhOAuthCredentials("cid", "client-secret", "http://localhost:8765/callback")
    account = HhOAuthAccount(tmp_path, "hh-primary", credentials=credentials,
                             transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    result = account.authorize_url()
    assert "client_id=cid" in result["url"]
    assert result["state"]
    state_file = tmp_path / "private" / "data" / "hh_oauth" / "hh-primary" / "state.json"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    with pytest.raises(HhOAuthStateError):
        account.exchange_code("code", "wrong-state")


def test_hh_oauth_exchange_refresh_and_negotiations(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    credentials = HhOAuthCredentials("cid", "client-secret", "http://localhost:8765/callback")
    account = HhOAuthAccount(tmp_path, "hh-primary", credentials=credentials,
                             transport=httpx.MockTransport(_hh_handler(calls)))
    auth = account.authorize_url()
    redirect = f"{auth['redirect_uri']}?code=code&state={auth['state']}"
    result = account.exchange_code(redirect, None)
    assert result["status"] == "authorized"
    assert not hh_state_path(tmp_path, "hh-primary").exists()
    token_file = hh_token_path(tmp_path, "hh-primary")
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    status = account.status(check_remote=True)
    assert status["status"] == "confirmed"
    assert status["hh_user_id"] == 42
    dumped = json.dumps(status)
    assert "access-1" not in dumped
    assert "refresh-1" not in dumped
    assert "client-secret" not in dumped

    token = read_private_json(token_file)
    assert token is not None
    token["expires_at"] = "2000-01-01T00:00:00+00:00"
    write_private_json(token_file, token)
    refreshed = account.status(check_remote=True)
    assert refreshed["status"] == "confirmed"
    assert any(
        request.url.path == "/token" and "grant_type=refresh_token" in request.content.decode()
        for request in calls
    )

    rows = account.negotiations(10)
    assert rows[0]["vacancy_id"] == "123"
    assert rows[0]["resume_id"] == "resume-1"


def test_hh_oauth_apply_is_spec_only(tmp_path: Path) -> None:
    credentials = HhOAuthCredentials("cid", "client-secret", "http://localhost:8765/callback")
    account = HhOAuthAccount(tmp_path, "hh-primary", credentials=credentials,
                             transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    spec = account.apply_spec("123", "resume-1", "hello")
    assert spec["verification"] == "needs_live_validation"
    assert spec["method"] == "POST"
    assert spec["body"]["vacancy_id"] == "123"
    with pytest.raises(HhLiveDisabled):
        account.apply("123", "resume-1", "hello", allow_live=False)


def test_unified_accounts_never_expose_secrets(tmp_path: Path) -> None:
    save_tg_credentials(tmp_path, 1, "hash-value")
    save_hh_credentials(tmp_path, "cid", "client-secret", "http://localhost:8765/callback")
    payload = unified_accounts(tmp_path)
    dumped = json.dumps(payload)
    assert "hash-value" not in dumped
    assert "client-secret" not in dumped
    assert payload["telegram"]["credentials"]["api_hash"] == "***"
    assert payload["hh_oauth"]["credentials"]["client_secret"] == "***"
    assert hh_oauth_status(tmp_path)["status"] == "missing_token"


def test_admin_accounts_overview_is_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import applypilot.admin as admin_module
    from applypilot.config import AppConfig

    monkeypatch.setattr(admin_module, "load_tracks", lambda root: {})
    config = AppConfig.discover(root=tmp_path)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    write_private_json(
        tmp_path / "private" / "data" / "telegram" / "credentials.json",
        {"api_id": 1, "api_hash": "hash-value"},
    )
    app = admin_module.AdminApp(config)
    payload = app.accounts_overview()
    assert payload["telegram"]["credentials"]["api_hash"] == "***"
    assert "hash-value" not in json.dumps(payload)
    assert payload["telegram"]["apply_status"] == "needs_live_validation"
    assert payload["hh_oauth"]["apply_status"] == "needs_live_validation"


def test_telegram_first_login_on_virgin_root_creates_private_session(tmp_path: Path) -> None:
    save_tg_credentials(tmp_path, 1, "hash")
    fake = _fake_factory([])
    account = TelegramAccount(
        tmp_path, "getmatch-primary",
        credentials=TelegramCredentials(1, "hash"),
        client_factory=fake,
    )
    result = account.login("+10000000000")
    assert result["status"] == "confirmed"

    session = session_path(tmp_path, "getmatch-primary")
    assert session.is_file()
    assert stat.S_IMODE(session.stat().st_mode) == 0o600
    assert stat.S_IMODE(session.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "private/data/telegram").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "private/data").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "private").stat().st_mode) == 0o700
    # The fake asserts directory/file existence before it is constructed.
    assert fake.instances[0].session == session


def test_telegram_login_disconnects_on_success_and_failure(tmp_path: Path) -> None:
    save_tg_credentials(tmp_path, 1, "hash")
    fake = _fake_factory([])
    account = TelegramAccount(
        tmp_path, "getmatch-primary",
        credentials=TelegramCredentials(1, "hash"),
        client_factory=fake,
    )

    assert account.login("+10000000000")["status"] == "confirmed"
    assert fake.state["disconnects"] == 1

    fake.fail_get_me = True
    failed = account.login("+10000000000")
    assert failed["status"] == "error"
    assert fake.state["disconnects"] == 2

    fake.fail_get_me = False
    fake.fail_start = True
    failed_start = account.login("+10000000000")
    assert failed_start["status"] == "error"
    assert fake.state["disconnects"] == 3

    session = session_path(tmp_path, "getmatch-primary")
    assert stat.S_IMODE(session.stat().st_mode) == 0o600
    assert stat.S_IMODE(session.parent.stat().st_mode) == 0o700


def test_hh_oauth_exchange_requires_stored_state(tmp_path: Path) -> None:
    credentials = HhOAuthCredentials("cid", "client-secret", "http://localhost:8765/callback")
    account = HhOAuthAccount(
        tmp_path, "hh-primary", credentials=credentials,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with pytest.raises(HhOAuthStateError, match="missing"):
        account.exchange_code("code", "some-state")


def test_hh_oauth_exchange_requires_state_argument(tmp_path: Path) -> None:
    credentials = HhOAuthCredentials("cid", "client-secret", "http://localhost:8765/callback")
    account = HhOAuthAccount(
        tmp_path, "hh-primary", credentials=credentials,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    auth = account.authorize_url()
    with pytest.raises(HhOAuthStateError, match="required"):
        account.exchange_code("code", None)
    # A validation failure must keep the local state so the owner can retry.
    assert hh_state_path(tmp_path, "hh-primary").exists()
    assert auth["state"]


def test_hh_oauth_mismatched_state_keeps_local_state(tmp_path: Path) -> None:
    credentials = HhOAuthCredentials("cid", "client-secret", "http://localhost:8765/callback")
    account = HhOAuthAccount(
        tmp_path, "hh-primary", credentials=credentials,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    account.authorize_url()
    with pytest.raises(HhOAuthStateError, match="match"):
        account.exchange_code("code", "wrong-state")
    assert hh_state_path(tmp_path, "hh-primary").exists()


def test_hh_oauth_expired_state_is_rejected_and_removed(tmp_path: Path) -> None:
    credentials = HhOAuthCredentials("cid", "client-secret", "http://localhost:8765/callback")
    account = HhOAuthAccount(
        tmp_path, "hh-primary", credentials=credentials,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    auth = account.authorize_url()
    state_file = hh_state_path(tmp_path, "hh-primary")
    payload = read_private_json(state_file)
    assert payload is not None
    payload["created_at"] = "2000-01-01T00:00:00+00:00"
    write_private_json(state_file, payload)

    with pytest.raises(HhOAuthStateError, match="expired"):
        account.exchange_code("code", auth["state"])
    assert not state_file.exists()


def test_hh_oauth_messages_read_only(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    credentials = HhOAuthCredentials("cid", "client-secret", "http://localhost:8765/callback")
    account = HhOAuthAccount(
        tmp_path, "hh-primary", credentials=credentials,
        transport=httpx.MockTransport(_hh_handler(calls)),
    )
    auth = account.authorize_url()
    account.exchange_code(f"{auth['redirect_uri']}?code=code&state={auth['state']}")

    rows = account.messages("neg-1", 10)
    assert rows == [{
        "id": "m1",
        "text": "Hello from recruiter",
        "created_at": "2026-10-20T10:00:00+00:00",
        "author": "Recruiter",
    }]
    assert any(
        request.method == "GET" and request.url.path == "/negotiations/neg-1/messages"
        for request in calls
    )


def test_hh_oauth_messages_cli_is_read_only(tmp_path: Path, monkeypatch, capsys) -> None:
    from applypilot.cli import _accounts_command, build_parser
    from applypilot.config import AppConfig

    rows = [{
        "id": "m1", "text": "Hello", "created_at": "2026-10-20T10:00:00+00:00",
        "author": "Recruiter",
    }]
    captured: dict[str, object] = {}

    def fake_messages(self, negotiation_id: str, limit: int = 50):
        captured["negotiation_id"] = negotiation_id
        captured["limit"] = limit
        return rows

    monkeypatch.setattr(HhOAuthAccount, "messages", fake_messages)
    config = AppConfig.discover(root=tmp_path)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    args = build_parser().parse_args([
        "accounts", "hh-oauth-messages",
        "--negotiation-id", "neg-1", "--limit", "5", "--json",
    ])
    assert args.accounts_action == "hh-oauth-messages"
    assert _accounts_command(config, args) == 0
    assert captured == {"negotiation_id": "neg-1", "limit": 5}
    assert json.loads(capsys.readouterr().out) == rows
