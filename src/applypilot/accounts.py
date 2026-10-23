"""Unified local account status and Telegram-to-queue planning.

No network calls live here.  Status reads only local session/token files; the
Telegram plan reads messages already synced into SQLite and never clicks.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .hh_oauth import CAPABILITIES as HH_CAPABILITIES
from .hh_oauth import hh_oauth_status
from .storage import Store, review_lane_for
from .telegram_client import telegram_status

DEFAULT_TELEGRAM_ACCOUNT = "getmatch-primary"
DEFAULT_HH_ACCOUNT = "hh-primary"

# Match a positive candidate application action, never an unrelated callback
# such as country selection or "Нет, спасибо".
GETMATCH_APPLY_LABEL = re.compile(
    r"откликнуться|(?:^|\b)apply(?: now)?(?:$|\b)", re.IGNORECASE,
)


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
    *,
    ledger_account: str | None = None,
) -> dict[str, Any]:
    """Plan only relevant Getmatch callback candidates; never send anything.

    Matched SKIP/manual-enrichment vacancies are retained as explicitly blocked
    diagnostics, not mislabeled as ready application candidates. A callback must
    have a positive "Откликнуться" label; country-choice buttons are never apply.
    """
    messages = store.telegram_messages(account, limit)
    queue_account = ledger_account or account
    planned: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    seen_vacancies: set[tuple[str, str]] = set()
    for message in messages:
        if not message.get("application_likely"):
            continue
        if message.get("outgoing") is not False:
            unmatched.append({
                "message_id": message.get("message_id"),
                "reason": "unverified_incoming_message",
            })
            continue
        if str(message.get("chat") or "").strip().casefold().lstrip("@") != "g_jobbot":
            unmatched.append({
                "message_id": message.get("message_id"),
                "reason": "unexpected_telegram_chat",
            })
            continue
        refs = list(message.get("vacancy_refs") or [])
        buttons = list(message.get("buttons") or [])
        callback_index = next(
            (
                index for index, button in enumerate(buttons)
                if button.get("kind") == "callback"
                and GETMATCH_APPLY_LABEL.search(str(button.get("text") or ""))
            ),
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
            if source != "getmatch.ru":
                unmatched.append({
                    "message_id": message.get("message_id"),
                    "source": source, "vacancy_id": vacancy_id,
                    "reason": "unsupported_application_source",
                })
                continue
            if callback_index is None:
                unmatched.append({
                    "message_id": message.get("message_id"),
                    "source": source, "vacancy_id": vacancy_id,
                    "reason": "no_positive_apply_callback",
                })
                continue
            key = (source, vacancy_id)
            if key in seen_vacancies:
                continue
            seen_vacancies.add(key)
            vacancy = store.vacancy_track_row(queue_account, source, vacancy_id, track)
            if vacancy is None or vacancy.get("score_decision") is None:
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
            button = buttons[callback_index]
            item = {
                "message_id": message.get("message_id"),
                "chat": message.get("chat", "@g_jobbot"),
                "sent_at": message.get("sent_at", ""),
                "source": source,
                "vacancy_id": vacancy_id,
                "vacancy_name": vacancy.get("name", ""),
                "company": vacancy.get("company", ""),
                "score": vacancy.get("score", 0),
                "review_lane": lane,
                "review_state": vacancy.get("review_state", ""),
                "application_state": vacancy.get("application_state", ""),
                "button_index": callback_index,
                "button_text": button.get("text", ""),
                "button_kind": button.get("kind", "none"),
                "action": "needs_live_validation",
                "can_send": False,
                "reason": (
                    "Telegram callback submit is not live-verified; "
                    "open @g_jobbot manually and confirm the button behaviour"
                ),
            }
            if lane not in {"FIT", "GOOD_UNKNOWN"}:
                blocked.append({
                    **item, "reason": f"not_relevant_for_ai_core:{lane}",
                })
            elif vacancy.get("application_state") in {
                "APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN",
            }:
                blocked.append({
                    **item, "reason": f"application_state:{vacancy['application_state']}",
                })
            else:
                planned.append(item)
    planned.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("sent_at") or "")))
    return {
        "account": account,
        "ledger_account": queue_account,
        "track": track,
        "messages": len(messages),
        "planned_count": len(planned),
        "blocked_count": len(blocked),
        "unmatched_count": len(unmatched),
        "planned": planned,
        "blocked": blocked,
        "unmatched": unmatched,
        "apply_status": "needs_live_validation",
        "mode": "dry_run",
    }
