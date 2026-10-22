"""Unified local account status and Telegram-to-queue planning.

No network calls live here.  Status reads only local session/token files; the
Telegram plan reads messages already synced into SQLite and never clicks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .hh_oauth import CAPABILITIES as HH_CAPABILITIES
from .hh_oauth import hh_oauth_status
from .storage import Store, review_lane_for
from .telegram_client import telegram_status

DEFAULT_TELEGRAM_ACCOUNT = "getmatch-primary"
DEFAULT_HH_ACCOUNT = "hh-primary"


def unified_accounts(
    root: Path,
    *,
    telegram_account: str = DEFAULT_TELEGRAM_ACCOUNT,
    hh_account: str = DEFAULT_HH_ACCOUNT,
) -> dict[str, Any]:
    """Local-only account inventory; never returns secrets."""
    return {
        "telegram": telegram_status(root, telegram_account),
        "hh_oauth": hh_oauth_status(root, hh_account),
        "capabilities": {
            "telegram": {
                "read_history": "documented_local",
                "read_buttons": "documented_local",
                "apply": "needs_live_validation",
            },
            "hh_oauth": HH_CAPABILITIES,
        },
    }


def telegram_queue_plan(
    store: Store,
    account: str = DEFAULT_TELEGRAM_ACCOUNT,
    track: str = "ai_core",
    limit: int = 50,
) -> dict[str, Any]:
    """Maps synced @g_jobbot messages to vacancies already in the ApplyPilot queue.

    The result is a dry-run: every application action is
    ``needs_live_validation`` and ``can_send`` is always false.
    """
    messages = store.telegram_messages(account, limit)
    planned: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for message in messages:
        if not message.get("application_likely"):
            continue
        refs = list(message.get("vacancy_refs") or [])
        buttons = list(message.get("buttons") or [])
        callback_index = next(
            (index for index, button in enumerate(buttons) if button.get("kind") == "callback"),
            None,
        )
        if not refs:
            unmatched.append({
                "message_id": message.get("message_id"),
                "reason": "no_public_vacancy_link",
            })
            continue
        for ref in refs:
            source = str(ref.get("source") or "")
            vacancy_id = str(ref.get("id") or "")
            vacancy = store.vacancy_track_row(account, source, vacancy_id, track)
            if vacancy is None:
                unmatched.append({
                    "message_id": message.get("message_id"),
                    "source": source,
                    "vacancy_id": vacancy_id,
                    "reason": "vacancy_not_in_queue",
                })
                continue
            lane = review_lane_for(
                str(vacancy.get("filter_status") or ""),
                str(vacancy.get("score_decision") or ""),
                application_state=str(vacancy.get("application_state") or ""),
            )
            button = buttons[callback_index] if callback_index is not None else {}
            planned.append({
                "message_id": message.get("message_id"),
                "chat": message.get("chat", "@g_jobbot"),
                "sent_at": message.get("sent_at", ""),
                "source": source,
                "vacancy_id": vacancy_id,
                "vacancy_name": vacancy.get("name", ""),
                "company": vacancy.get("company", ""),
                "score": vacancy.get("score", 0),
                "review_lane": lane,
                "button_index": callback_index,
                "button_text": button.get("text", ""),
                "button_kind": button.get("kind", "none"),
                "action": "needs_live_validation",
                "can_send": False,
                "reason": (
                    "Telegram callback submit is not live-verified; "
                    "open @g_jobbot manually and confirm the button behaviour"
                ),
            })
    planned.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("sent_at") or "")))
    return {
        "account": account,
        "track": track,
        "messages": len(messages),
        "planned_count": len(planned),
        "unmatched_count": len(unmatched),
        "planned": planned,
        "unmatched": unmatched,
        "apply_status": "needs_live_validation",
        "mode": "dry_run",
    }
