"""Явная read-only синхронизация переписок через официальный HH API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import requests

from .storage import Store

API_ROOT = "https://api.hh.ru"
USER_AGENT = "ApplyPilot/0.1"
CHAT_PAGE_SIZE = 20
MESSAGE_PAGE_SIZE = 50


class InboxSyncError(RuntimeError):
    """Переписку не удалось прочитать с достоверным контрактом."""


def _sender(message: Mapping[str, Any]) -> tuple[str, str]:
    info = message.get("sender_display_info")
    if not isinstance(info, Mapping):
        return "unknown", ""
    role = str(info.get("role") or "").upper()
    kind = {"APPLICANT": "applicant", "EMPLOYER": "recruiter", "BOT": "bot"}.get(role, "unknown")
    return kind, str(info.get("name") or "")


def _normalize_message(chat: Mapping[str, Any], message: Mapping[str, Any]) -> dict[str, Any] | None:
    message_id = str(message.get("id") or "").strip()
    thread_id = str(chat.get("id") or "").strip()
    if not message_id or not thread_id:
        return None
    payload = message.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    sender_type, sender_name = _sender(message)
    attachments = payload.get("attachments")
    body = payload.get("text")
    if not isinstance(body, str):
        body = "" if not attachments else "[вложение без текста]"
    return {
        "thread_id": thread_id,
        "message_id": message_id,
        "vacancy_id": str(chat.get("vacancy_id") or ""),
        "sent_at": str(message.get("creation_time") or ""),
        "sender_type": sender_type,
        "sender_name": sender_name,
        "body": body,
        # Храним лишь безопасные метаданные. Токены и URL вложений не копируем.
        "raw": {"type": str(message.get("type") or ""),
                "last_change_time": str(message.get("last_change_time") or ""),
                "sender_participant_id": str(message.get("sender_participant_id") or ""),
                "has_attachments": bool(attachments)},
    }


def sync_hh_inbox(store: Store, access_token: str, account: str = "default", *,
                  max_chats: int = 20, messages_per_chat: int = 20,
                  timeout: float = 20.0, session: requests.Session | None = None) -> dict[str, Any]:
    """Читает ограниченную порцию чатов/сообщений; не помечает прочитанным и не отправляет."""
    token = str(access_token or "").strip()
    if not token:
        raise InboxSyncError("HH_ACCESS_TOKEN is required for the official read-only API")
    if not 1 <= int(max_chats) <= 100:
        raise InboxSyncError("max_chats must be between 1 and 100")
    if not 1 <= int(messages_per_chat) <= MESSAGE_PAGE_SIZE:
        raise InboxSyncError(f"messages_per_chat must be between 1 and {MESSAGE_PAGE_SIZE}")
    client = session or requests.Session()
    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    chats: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    errors: list[str] = []
    truncated = False
    try:
        page = 0
        pages_total = 1
        while page < pages_total and len(chats) < max_chats:
            response = client.get(
                f"{API_ROOT}/common/chats", params={"page": page, "per_page": CHAT_PAGE_SIZE},
                headers=headers, timeout=timeout,
            )
            if response.status_code != 200:
                raise InboxSyncError(f"GET /common/chats failed with HTTP {response.status_code}")
            try:
                data = response.json()
            except (ValueError, requests.JSONDecodeError) as exc:
                raise InboxSyncError("GET /common/chats returned malformed JSON") from exc
            if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
                raise InboxSyncError("GET /common/chats returned an unrecognized response")
            page_chats = [row for row in data["items"] if isinstance(row, dict) and row.get("id")]
            chats.extend(page_chats[:max_chats - len(chats)])
            try:
                pages_total = max(1, int(data.get("pages", page + 1)))
            except (TypeError, ValueError):
                pages_total = page + 1
            page += 1
        if page < pages_total:
            truncated = True

        supported_chats = [chat for chat in chats if str(chat.get("type") or "").upper()
                           in {"NEGOTIATION", "BOT"}]
        for chat in supported_chats:
            chat_id = quote(str(chat["id"]), safe="")
            response = client.get(
                f"{API_ROOT}/common/chats/{chat_id}/messages",
                params={"order": "prev", "limit": int(messages_per_chat)},
                headers=headers, timeout=timeout,
            )
            if response.status_code != 200:
                errors.append(f"chat {chat_id}: HTTP {response.status_code}")
                continue
            try:
                data = response.json()
            except (ValueError, requests.JSONDecodeError):
                errors.append(f"chat {chat_id}: malformed JSON")
                continue
            if not isinstance(data, Mapping) or not isinstance(data.get("messages"), list):
                errors.append(f"chat {chat_id}: unrecognized response")
                continue
            for raw_message in data["messages"]:
                if isinstance(raw_message, Mapping):
                    normalized = _normalize_message(chat, raw_message)
                    if normalized:
                        messages.append(normalized)
            if data.get("has_more") is True:
                truncated = True
    except requests.RequestException as exc:
        errors.append(f"network error: {type(exc).__name__}")
    finally:
        if session is None:
            client.close()

    saved = store.save_inbox_messages(account, "hh.ru", messages)
    status = "partial" if errors or truncated else "ok" if saved else "empty"
    error = "; ".join(errors)[:1000]
    store.save_sync_snapshot("hh.ru/messages", status, saved, error, account=account)
    return {"status": status, "chats_checked": len(supported_chats),
            "messages_received": len(messages), "messages_saved": saved,
            "truncated": truncated, "errors": errors}
