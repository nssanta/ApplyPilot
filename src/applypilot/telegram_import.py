"""Links saved Telegram @g_jobbot messages to the Getmatch scanner queue.

The import is local-only: it reads messages already saved by ``telegram-sync``
and never opens a Telegram session or sends a message.  Existing scanner rows
are enriched with message/callback metadata.  Missing refs can be stored as
review-only placeholders (``score_decision='maybe'``) so they cannot enter a
live queue before the real Getmatch scanner scores them.
"""

from __future__ import annotations

import re
from typing import Any

from .storage import Store

GETMATCH_URL = re.compile(r"https?://(?:www\.)?getmatch\.ru/vacancies/(\d+)", re.IGNORECASE)


def _title_from_text(text: str) -> str:
    """Extract the visible vacancy title from a Telegram Markdown message.

    Getmatch starts many cards with ``[**Title**](url)** @ Company**``.
    Treating the whole line as a URL used to fall through to the salary line,
    which made title scoring useless.  Never guesses a vacancy ID.
    """
    for line in str(text or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        markdown = re.search(r"\[(.+?)\]\(https?://", raw)
        if markdown:
            candidate = markdown.group(1)
        elif raw.startswith("**") and raw.count("**") >= 2:
            candidate = raw.split("**")[1]
        elif "http://" in raw.lower() or "https://" in raw.lower():
            continue
        else:
            candidate = raw
        candidate = candidate.replace("**", "").strip(" \t•-—–")
        candidate = candidate.split(" @ ", 1)[0].strip()
        if candidate:
            return candidate[:117] + "..." if len(candidate) > 120 else candidate
    return ""


def _public_link(message: dict[str, Any]) -> dict[str, Any]:
    """Keeps button classification and text, never raw callback bytes."""
    buttons = []
    for button in message.get("buttons") or []:
        kind = str(button.get("kind") or "")
        if kind not in {"callback", "webview", "url"}:
            continue
        buttons.append({
            "text": str(button.get("text") or "")[:200],
            "kind": kind,
            "url": str(button.get("url") or "")[:500],
        })
    return {
        "chat": str(message.get("chat") or "@g_jobbot"),
        "message_id": int(message.get("message_id") or 0),
        "sent_at": str(message.get("sent_at") or ""),
        "text": str(message.get("text") or "")[:4000],
        "buttons": buttons,
        "application_likely": bool(message.get("application_likely")),
    }


def import_telegram_vacancies(
    store: Store,
    account: str = "getmatch-primary",
    source: str = "getmatch.ru",
    track: str = "ai_core",
    limit: int = 200,
    *,
    create_missing: bool = False,
    ledger_account: str | None = None,
) -> dict[str, Any]:
    """Imports saved message refs into the scanner ledger idempotently.

    ``account`` is the Telegram session label (where messages are stored);
    ``ledger_account`` is the scanner account resolved from sources.toml.  They
    are separate concepts and may differ in a public checkout.
    """
    ledger = str(ledger_account or account)
    messages = store.telegram_messages(account, limit)
    linked: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for message in messages:
        for ref in message.get("vacancy_refs") or []:
            if str(ref.get("source") or "") != source:
                continue
            vacancy_id = str(ref.get("id") or "").strip()
            if not vacancy_id.isdigit():
                continue
            key = (vacancy_id, int(message.get("message_id") or 0))
            if key in seen:
                continue
            seen.add(key)
            link = _public_link(message)
            payload = store.vacancy_payload(ledger, source, vacancy_id)
            if payload:
                if str(payload.get("description_status") or "") == "telegram":
                    refreshed = _title_from_text(link["text"])
                    if refreshed and refreshed != str(payload.get("name") or ""):
                        store.save_imported_vacancy(
                            ledger, source, vacancy_id,
                            {**payload, "name": refreshed, "description": link["text"],
                             "description_status": "telegram"},
                            track,
                        )
                if store.link_telegram_message(ledger, source, vacancy_id, link):
                    linked.append({
                        "id": vacancy_id,
                        "message_id": link["message_id"],
                        "title": str(payload.get("name") or payload.get("title") or ""),
                        "buttons": len(link["buttons"]),
                    })
                continue
            if not create_missing:
                missing.append({"id": vacancy_id, "message_id": link["message_id"]})
                continue
            title = _title_from_text(link["text"]) or f"Getmatch vacancy {vacancy_id}"
            item = {
                "id": vacancy_id,
                "name": title,
                "company": "",
                "url": f"https://getmatch.ru/vacancies/{vacancy_id}",
                "description": link["text"],
                "description_status": "telegram",
                "apply_status": "needs_live_validation",
                "telegram": link,
            }
            if store.save_imported_vacancy(ledger, source, vacancy_id, item, track):
                created.append({
                    "id": vacancy_id,
                    "message_id": link["message_id"],
                    "title": title,
                })

    return {
        "account": account,
        "ledger_account": ledger,
        "source": source,
        "track": track,
        "messages": len(messages),
        "linked": linked,
        "created": created,
        "missing": missing,
        "linked_count": len(linked),
        "created_count": len(created),
        "missing_count": len(missing),
    }
