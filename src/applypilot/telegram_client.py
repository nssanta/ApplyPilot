"""Local Telegram MTProto client for the official Getmatch candidate bot.

The client reads the owner's own chat with @g_jobbot through Telethon.  It is a
separate transport, not a replacement for the browser clickers: the existing
Getmatch scanner and the source-aware queue remain authoritative for vacancy
data.  Sending a callback is disabled by default and is marked
``needs_live_validation`` until the owner verifies the real bot UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .secure_store import ensure_private_dir, read_private_json, secret_state, write_private_json

BOT_USERNAME = "@g_jobbot"
TELEGRAM_ROOT = Path("private/data/telegram")
APPLICATION_HINTS = re.compile(
    r"отклик|откликнуться|apply|отправить|interested|хочу\s+отклик|собеседован",
    re.IGNORECASE,
)
VACANCY_LINK = re.compile(r"https?://(?:www\.)?(getmatch\.ru|hh\.ru)/(?:vacancies|vacancy)/(\d+)")


class TelegramUnavailable(RuntimeError):
    """Raised when the optional Telethon dependency is not installed."""


class TelegramNotConfigured(RuntimeError):
    """Raised when local API_ID/API_HASH are missing."""


class LiveActionDisabled(RuntimeError):
    """Raised for any Telegram write action that has not been live-verified."""


@dataclass(frozen=True)
class TelegramCredentials:
    api_id: int
    api_hash: str

    def public_dict(self) -> dict[str, Any]:
        return {"api_id": self.api_id, "api_hash": "***", "api_hash_state": "set"}


def account_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().casefold()).strip("-") or "default"


def credentials_path(root: Path) -> Path:
    return root / TELEGRAM_ROOT / "credentials.json"


def session_path(root: Path, account: str) -> Path:
    return root / TELEGRAM_ROOT / account_slug(account) / "telegram.session"


def load_credentials(root: Path) -> TelegramCredentials | None:
    """Loads API credentials from env first, then a local 0600 file."""
    env_id = os.getenv("APPLYPILOT_TELEGRAM_API_ID", "").strip()
    env_hash = os.getenv("APPLYPILOT_TELEGRAM_API_HASH", "").strip()
    if env_id and env_hash:
        try:
            return TelegramCredentials(int(env_id), env_hash)
        except ValueError:
            return None
    payload = read_private_json(credentials_path(root))
    if not payload:
        return None
    try:
        api_id = int(payload.get("api_id") or 0)
    except (TypeError, ValueError):
        return None
    api_hash = str(payload.get("api_hash") or "").strip()
    return TelegramCredentials(api_id, api_hash) if api_id > 0 and api_hash else None


def save_credentials(root: Path, api_id: int, api_hash: str) -> Path:
    if int(api_id) <= 0:
        raise ValueError("api_id must be a positive integer")
    if not str(api_hash or "").strip():
        raise ValueError("api_hash is required")
    return write_private_json(
        credentials_path(root),
        {"api_id": int(api_id), "api_hash": str(api_hash).strip()},
    )


def credentials_status(root: Path) -> dict[str, Any]:
    credentials = load_credentials(root)
    if credentials is None:
        return {"status": "missing", "api_id": None, "api_hash_state": "missing"}
    return {"status": "configured", **credentials.public_dict()}


def _telethon_client(session: Path, api_id: int, api_hash: str) -> Any:
    try:
        from telethon import TelegramClient
    except ImportError as exc:  # pragma: no cover - exercised via a fake module
        raise TelegramUnavailable(
            "Telethon is not installed; install the optional extra: "
            "pip install 'applypilot[telegram]'"
        ) from exc
    return TelegramClient(str(session), api_id, api_hash)


def _run(coroutine: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise RuntimeError("Telegram client is synchronous; do not call it inside a running loop")


def _identity(me: Any) -> dict[str, Any]:
    return {
        "user_id": getattr(me, "id", None),
        "username": getattr(me, "username", "") or "",
        "first_name": getattr(me, "first_name", "") or "",
    }


def classify_button(button: Any) -> dict[str, Any]:
    """Classifies an inline button without exposing callback payloads."""
    data = getattr(button, "data", None)
    url = getattr(button, "url", None)
    raw = getattr(button, "button", None)
    raw_name = type(raw).__name__ if raw is not None else ""
    if data:
        kind = "callback"
    elif "WebView" in raw_name:
        # Telethon exposes WebView buttons with a URL too; the raw type is the
        # only reliable signal that opening it stays inside Telegram.
        kind = "webview"
    elif url:
        kind = "url"
    else:
        kind = "text"
    return {
        "text": str(getattr(button, "text", "") or ""),
        "kind": kind,
        "url": str(url or ""),
        "callback_present": bool(data),
        "callback_sha256": hashlib.sha256(bytes(data)).hexdigest()[:16] if data else "",
        "raw_type": raw_name,
    }


def extract_vacancy_refs(text: str, buttons: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """Extracts only public vacancy links; never guesses an ID from message text."""
    haystack = [str(text or "")]
    for button in buttons or []:
        if button.get("url"):
            haystack.append(str(button["url"]))
    refs: dict[tuple[str, str], dict[str, str]] = {}
    for chunk in haystack:
        for match in VACANCY_LINK.finditer(chunk):
            host, native_id = match.group(1), match.group(2)
            source = "getmatch.ru" if host == "getmatch.ru" else "hh.ru"
            refs[(source, native_id)] = {"source": source, "id": native_id}
    return list(refs.values())


def message_to_dict(message: Any, chat: str = BOT_USERNAME) -> dict[str, Any]:
    raw_buttons = getattr(message, "buttons", None) or []
    buttons: list[dict[str, Any]] = []
    for row in raw_buttons:
        for button in row:
            buttons.append(classify_button(button))
    text = str(getattr(message, "text", None) or getattr(message, "message", "") or "")
    sent = getattr(message, "date", None)
    if isinstance(sent, datetime):
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=UTC)
        sent_at = sent.astimezone(UTC).isoformat()
    else:
        sent_at = ""
    refs = extract_vacancy_refs(text, buttons)
    application_likely = bool(APPLICATION_HINTS.search(text)) or any(
        APPLICATION_HINTS.search(str(button.get("text") or "")) for button in buttons
    )
    return {
        "chat": chat,
        "message_id": int(getattr(message, "id", 0) or 0),
        "sent_at": sent_at,
        "text": text,
        "buttons": buttons,
        "vacancy_refs": refs,
        "application_likely": application_likely,
        "has_callback": any(button["kind"] == "callback" for button in buttons),
        # None = unknown for synthetic/legacy imports. Incoming bot receipts
        # must have out=False; never trust a user's own copied confirmation.
        "outgoing": getattr(message, "out", None),
    }


def _message_public(message: Mapping[str, Any]) -> dict[str, Any]:
    """Drops the callback payload hash from outward-facing payloads."""
    return {
        key: value for key, value in message.items()
        if key not in {"buttons"}
    } | {
        "buttons": [
            {key: value for key, value in button.items() if key != "callback_sha256"}
            for button in message.get("buttons", [])
        ],
    }


class TelegramAccount:
    """One isolated Telegram user session per account label."""

    def __init__(
        self,
        root: Path,
        account: str = "getmatch-primary",
        *,
        credentials: TelegramCredentials | None = None,
        client_factory: Callable[[Path, int, str], Any] | None = None,
    ) -> None:
        self.root = root
        self.account = account_slug(account)
        self._credentials = credentials
        self._client_factory = client_factory
        self.session = session_path(root, self.account)

    def _credentials_or_raise(self) -> TelegramCredentials:
        credentials = self._credentials or load_credentials(self.root)
        if credentials is None:
            raise TelegramNotConfigured(
                "Telegram API credentials are missing; run 'applypilot accounts telegram-setup'"
            )
        return credentials

    def _prepare_session_path(self) -> None:
        """Creates the account directory 0700 and an empty session file 0600.

        Telethon creates its SQLite auth key on first connect.  Pre-creating the
        file with an explicit mode closes the window where a default umask could
        leave the auth key world-readable.  An empty file is a valid empty
        SQLite database, so Telethon initialises it normally.
        """
        # Harden the whole local chain: private/ -> private/data/ ->
        # private/data/telegram/ -> private/data/telegram/<account>/.
        ensure_private_dir(self.session.parent.parent.parent.parent)
        ensure_private_dir(self.session.parent.parent.parent)
        ensure_private_dir(self.session.parent.parent)
        ensure_private_dir(self.session.parent)
        if not self.session.exists():
            descriptor = os.open(self.session, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        else:
            try:
                self.session.chmod(0o600)
            except OSError:
                pass

    def _harden_session_files(self) -> None:
        """Re-applies 0600 to the session and any SQLite journal/WAL files."""
        ensure_private_dir(self.session.parent)
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(f"{self.session}{suffix}")
            if candidate.exists():
                try:
                    candidate.chmod(0o600)
                except OSError:
                    pass

    def _make_client(self) -> Any:
        credentials = self._credentials_or_raise()
        self._prepare_session_path()
        factory = self._client_factory or _telethon_client
        return factory(self.session, credentials.api_id, credentials.api_hash)

    def _public_base(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "session": str(self.session),
            "session_present": self.session.is_file(),
            "bot": BOT_USERNAME,
            "apply_status": "needs_live_validation",
        }

    def login(
        self,
        phone: str,
        *,
        code_callback: Callable[[], str] | None = None,
        password_callback: Callable[[], str] | None = None,
    ) -> dict[str, Any]:
        """Performs a local interactive login; code/password stay in this process."""
        if not str(phone or "").strip():
            return {**self._public_base(), "status": "needs_interactive_login",
                    "detail": "phone is required for a local login"}
        try:
            client = self._make_client()
        except (TelegramNotConfigured, TelegramUnavailable) as exc:
            return {**self._public_base(), "status": "not_configured", "detail": str(exc)}

        async def run() -> Any:
            try:
                await client.start(
                    phone=str(phone).strip(),
                    code_callback=code_callback,
                    password=password_callback,
                )
                me = await client.get_me()
                return me
            finally:
                # Always flush and close the SQLite auth key, including when
                # login fails or the user cancels the code prompt.
                # Never mask the login result, but always attempt the close.
                with contextlib.suppress(Exception):
                    await client.disconnect()

        try:
            me = _run(run())
        except TelegramUnavailable as exc:
            return {**self._public_base(), "status": "telethon_missing", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - report a local status, never a secret
            return {**self._public_base(), "status": "error",
                    "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
        finally:
            self._harden_session_files()
        return {**self._public_base(), "status": "confirmed", **_identity(me),
                "detail": "local Telegram session saved"}

    def status(self) -> dict[str, Any]:
        if not self.session.is_file():
            return {**self._public_base(), "status": "missing",
                    "detail": "no local Telegram session"}
        try:
            client = self._make_client()
        except (TelegramNotConfigured, TelegramUnavailable) as exc:
            return {**self._public_base(), "status": "not_configured", "detail": str(exc)}

        async def run() -> dict[str, Any]:
            await client.connect()
            try:
                authorized = await client.is_user_authorized()
                if not authorized:
                    return {**self._public_base(), "status": "expired",
                            "detail": "Telegram session is not authorized"}
                me = await client.get_me()
                return {**self._public_base(), "status": "confirmed", **_identity(me)}
            finally:
                await client.disconnect()

        try:
            return _run(run())
        except Exception as exc:  # noqa: BLE001
            return {**self._public_base(), "status": "error",
                    "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
        finally:
            self._harden_session_files()

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 500:
            raise ValueError("limit must be between 1 and 500")
        client = self._make_client()

        async def run() -> list[dict[str, Any]]:
            await client.connect()
            try:
                entity = await client.get_entity(BOT_USERNAME)
                messages: list[dict[str, Any]] = []
                async for message in client.iter_messages(entity, limit=int(limit)):
                    messages.append(message_to_dict(message, chat=BOT_USERNAME))
                return messages
            finally:
                await client.disconnect()

        try:
            return _run(run())
        finally:
            self._harden_session_files()

    def sync(self, store: Any, limit: int = 50) -> dict[str, Any]:
        messages = self.history(limit)
        saved = store.save_telegram_messages(self.account, messages)
        return {
            **self._public_base(),
            "status": "ok",
            "messages": len(messages),
            "saved": saved,
        }

    @staticmethod
    def plan_action(message: Mapping[str, Any], button_index: int = 0) -> dict[str, Any]:
        """Builds a dry-run plan for one stored message/button; sends nothing."""
        buttons = list(message.get("buttons") or [])
        if button_index < 0 or button_index >= len(buttons):
            return {
                "status": "invalid_button",
                "detail": f"button index {button_index} is outside 0..{max(0, len(buttons) - 1)}",
                "apply_status": "needs_live_validation",
            }
        button = buttons[button_index]
        return {
            "account": message.get("account", ""),
            "chat": message.get("chat", BOT_USERNAME),
            "message_id": message.get("message_id"),
            "button_index": button_index,
            "button": {
                key: value for key, value in button.items() if key != "callback_sha256"
            },
            "vacancy_refs": list(message.get("vacancy_refs") or []),
            "action": "needs_live_validation",
            "can_send": False,
            "reason": (
                "Telegram callback submit is not live-verified; "
                "open the bot manually and confirm the button behaviour"
            ),
        }

    def click(self, message_id: int, button_index: int = 0, *, allow_live: bool = False) -> dict[str, Any]:
        """Guarded live callback. Disabled unless both opt-ins are present."""
        if not allow_live or os.getenv("APPLYPILOT_TELEGRAM_LIVE") != "1":
            raise LiveActionDisabled(
                "Telegram live actions are disabled; this method remains "
                "needs_live_validation until the owner verifies @g_jobbot"
            )
        client = self._make_client()

        async def run() -> dict[str, Any]:
            await client.connect()
            try:
                entity = await client.get_entity(BOT_USERNAME)
                message = await client.get_messages(entity, ids=int(message_id))
                if message is None:
                    return {"status": "missing", "detail": "message not found"}
                await message.click(i=int(button_index))
                return {"status": "clicked", "message_id": int(message_id),
                        "button_index": int(button_index),
                        "verification": "needs_live_validation"}
            finally:
                await client.disconnect()

        try:
            return _run(run())
        finally:
            self._harden_session_files()


def telegram_status(root: Path, account: str = "getmatch-primary") -> dict[str, Any]:
    """Local-only status: credentials, session file, and apply capability."""
    credentials = credentials_status(root)
    session = session_path(root, account)
    return {
        "provider": "telegram",
        "account": account_slug(account),
        "credentials": credentials,
        "session_present": session.is_file(),
        "session": str(session),
        "bot": BOT_USERNAME,
        "status": "configured" if credentials["status"] == "configured" and session.is_file()
        else ("missing_session" if credentials["status"] == "configured" else "missing_credentials"),
        "apply_status": "needs_live_validation",
        "secret_state": secret_state(credentials.get("api_hash")),
    }
