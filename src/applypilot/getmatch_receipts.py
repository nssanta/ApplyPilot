"""Reconcile verified incoming @g_jobbot application receipts into ApplyPilot.

A bot message is authoritative only when:
- received (not sent by the user), from the official direct chat;
- names an exact Getmatch vacancy ID via its public vacancy URL;
- explicitly confirms "Отклик отправлен".
Do not infer receipts from callback clicks, marketing text, or profile options.
"""

from __future__ import annotations

import re
from typing import Any

from .storage import Store

RECEIPT = re.compile(r"(?:✅\s*)?(?:ваш\s+)?отклик\s+отправлен(?:[.!\s]|$)", re.IGNORECASE)


def reconcile_getmatch_receipts(
    store: Store,
    telegram_account: str = "getmatch-primary",
    *,
    track: str = "ai_core",
    limit: int = 1000,
    ledger_account: str | None = None,
) -> dict[str, Any]:
    """Read local messages and record only provider-confirmed historical applies."""
    account = ledger_account or telegram_account
    messages = store.telegram_messages(telegram_account, limit)
    receipts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for message in messages:
        if (str(message.get("chat") or "").casefold().lstrip("@") != "g_jobbot"
                or message.get("outgoing") is not False
                or not RECEIPT.search(str(message.get("text") or ""))):
            continue
        refs = [
            str(ref.get("id") or "")
            for ref in message.get("vacancy_refs") or []
            if ref.get("source") == "getmatch.ru" and str(ref.get("id") or "").isdigit()
        ]
        # One unambiguous vacancy only. Never attribute a generic message to
        # a previous or subsequent job in the same conversation.
        if len(set(refs)) != 1:
            skipped.append({"message_id": message["message_id"], "reason": "ambiguous_vacancy"})
            continue
        vacancy_id = refs[0]
        if vacancy_id in seen_ids:
            continue
        seen_ids.add(vacancy_id)
        vacancy = store.vacancy_track_row(account, "getmatch.ru", vacancy_id, track)
        if vacancy is None or vacancy.get("score_decision") is None:
            skipped.append({"vacancy_id": vacancy_id, "reason": "vacancy_not_in_track"})
            continue
        channel = f"getmatch.ru:{vacancy_id}"
        evidence = (
            f"Official incoming @g_jobbot message #{int(message['message_id'])}: "
            "Отклик отправлен, matching public vacancy URL; "
            f"sent_at={message.get('sent_at', '')}"
        )
        previous = [
            row for row in store.source_attempts(account, "getmatch.ru", limit=1000)
            if str(row["vacancy_id"]) == vacancy_id
        ]
        previous_status = str(previous[0]["status"]) if previous else ""
        if previous_status in {"success", "already_applied"}:
            receipts.append({"vacancy_id": vacancy_id, "status": "already_recorded"})
            continue
        if previous_status == "unknown":
            recorded = store.reconcile_source_application(
                account, "getmatch.ru", vacancy_id, track, channel, evidence,
                note="Bot-confirmed historical application",
                run_id=f"getmatch-bot-{message['message_id']}",
            )
            status = "reconciled_success" if recorded else "needs_manual"
        elif previous_status and previous_status not in {"needs_manual", "failed_before_submit", "prepared"}:
            skipped.append({"vacancy_id": vacancy_id, "reason": "unsupported_existing_attempt_state"})
            continue
        elif previous_status:
            # Retain the old ledger and ask for explicit reconciliation:
            # never mutate a preflight or operator-written attempt blindly.
            skipped.append({"vacancy_id": vacancy_id, "reason": "prior_attempt_needs_manual"})
            continue
        else:
            recorded = store.record_source_application(
                account, "getmatch.ru", vacancy_id, track, channel,
                "already_applied", note=evidence,
                run_id=f"getmatch-bot-{message['message_id']}", mode="reconcile",
            )
            status = "recorded_already_applied" if recorded else "needs_manual"
        receipts.append({
            "vacancy_id": vacancy_id,
            "message_id": int(message["message_id"]),
            "status": status,
        })
    return {
        "telegram_account": telegram_account,
        "ledger_account": account,
        "track": track,
        "checked_messages": len(messages),
        "confirmed_count": sum(row["status"] in {
            "recorded_already_applied", "reconciled_success",
        } for row in receipts),
        "receipts": receipts,
        "skipped": skipped,
        "mode": "incoming_bot_evidence_only",
    }
