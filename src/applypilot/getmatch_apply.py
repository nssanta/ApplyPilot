"""One-vacancy Getmatch MTProto callback pilot with exact receipt verification.

The pilot is OFF by default and cannot submit without:
1) an explicitly APPROVED, scored, ready vacancy;
2) its official incoming @g_jobbot message with the expected apply callback;
3) an explicit live CLI opt-in plus APPLYPILOT_TELEGRAM_LIVE=1.

A clicked button is NOT a success. Only a *new* matching incoming bot
confirmation can make it successful. Otherwise, the source-aware ledger
records UNKNOWN, preventing automatic repeats.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .accounts import GETMATCH_APPLY_LABEL
from .getmatch_receipts import RECEIPT
from .storage import Store
from .telegram_client import (
    BOT_USERNAME,
    LiveActionDisabled,
    TelegramAccount,
    _run,
    message_to_dict,
)


def getmatch_profile_acceptance(
    store: Store,
    telegram_account: str = "getmatch-primary",
    *,
    max_age_days: int = 7,
) -> dict[str, Any]:
    """Require a recent official incoming /profile acceptance reply.

    Acceptance, according to @g_jobbot, means this profile may send a
    one-click application. Trust badges are a separate provider feature.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    messages = store.telegram_messages(telegram_account, 1000)
    for item in messages:
        if (str(item.get("chat") or "").casefold().lstrip("@") != "g_jobbot"
                or item.get("outgoing") is not False):
            continue
        text = str(item.get("text") or "").casefold()
        accepted = (
            "ваш профиль принят" in text
            and "можно откликаться на вакансии" in text
        )
        rejected = any(marker in text for marker in (
            "профиль не принят", "профиль не прошёл верификацию",
            "профиль не прошел верификацию",
        ))
        if not (accepted or rejected):
            continue
        stamp = str(item.get("sent_at") or "")
        try:
            sent = datetime.fromisoformat(stamp)
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=UTC)
        except ValueError:
            return {"status": "unverified", "reason": "bot acceptance timestamp invalid"}
        if sent < cutoff:
            return {"status": "stale", "reason": "repeat official /profile verification"}
        if rejected:
            return {"status": "not_accepted", "reason": "bot rejected candidate profile"}
        return {
            "status": "accepted", "source": BOT_USERNAME,
            "message_id": int(item["message_id"]), "sent_at": stamp,
            "reason": "bot confirms candidate profile can apply in one click",
        }
    return {"status": "unverified", "reason": "no recent incoming bot profile acceptance"}


def _getmatch_url_matches(url: str, vacancy_id: str) -> bool:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname not in {"getmatch.ru", "www.getmatch.ru"}:
        return False
    stem = f"/vacancies/{vacancy_id}"
    return parsed.path == stem or parsed.path.startswith(stem + "-")


def _positive_button_index(buttons: list[dict[str, Any]]) -> int | None:
    return next(
        (i for i, b in enumerate(buttons)
         if b.get("kind") == "callback"
         and GETMATCH_APPLY_LABEL.search(str(b.get("text") or ""))),
        None,
    )


def _receipt_for_message(message: Any, target_id: str, bot_id: int) -> bool:
    if message is None or getattr(message, "out", None) is not False:
        return False
    if getattr(message, "sender_id", None) != bot_id:
        return False
    normalized = message_to_dict(message)
    ids = {
        str(ref.get("id") or "") for ref in normalized["vacancy_refs"]
        if ref.get("source") == "getmatch.ru"
    }
    return ids == {target_id} and bool(RECEIPT.search(normalized["text"]))


def probe_getmatch_callback(
    root: Path,
    telegram_account: str,
    plan: dict[str, Any],
    *,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Read-only proof that the saved button still exists in the official chat."""
    telegram = TelegramAccount(root, telegram_account, client_factory=client_factory)

    async def run() -> dict[str, Any]:
        client = telegram._make_client()
        try:
            await client.connect()
            bot = await client.get_entity(BOT_USERNAME)
            message = await client.get_messages(bot, ids=int(plan["message_id"]))
            if message is None or getattr(message, "out", None) is not False:
                return {"status": "bot_message_missing"}
            if getattr(message, "sender_id", None) != int(bot.id):
                return {"status": "sender_mismatch"}
            live = message_to_dict(message, chat=BOT_USERNAME)
            refs = {str(ref.get("id")) for ref in live["vacancy_refs"]
                    if ref.get("source") == "getmatch.ru"}
            if refs != {str(plan["vacancy_id"])}:
                return {"status": "vacancy_id_mismatch"}
            index = _positive_button_index(live["buttons"])
            if index != plan["button_index"]:
                return {"status": "apply_button_changed"}
            if (not isinstance(index, int)
                    or live["buttons"][index]["callback_sha256"] != plan["callback_sha256"]):
                return {"status": "callback_fingerprint_changed"}
            if RECEIPT.search(live["text"]):
                return {"status": "already_applied_in_bot"}
            return {
                "status": "live_callback_present",
                "vacancy_id": str(plan["vacancy_id"]),
                "message_id": int(plan["message_id"]),
                "button_index": int(index),
                "sent": False,
                "note": "read-only MTProto callback fingerprint matched",
            }
        finally:
            await client.disconnect()

    try:
        return _run(run())
    except Exception as exc:  # noqa: BLE001 - surface a read-only check failure
        return {"status": "probe_error", "error_type": type(exc).__name__}
    finally:
        telegram._harden_session_files()


def click_and_confirm(
    root: Path,
    account: str,
    store: Store,
    plan: dict[str, Any],
    *,
    max_checks: int = 7,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Perform one callback and read the official bot's new confirmation."""
    telegram = TelegramAccount(root, account, client_factory=client_factory)

    async def run() -> dict[str, Any]:
        client = telegram._make_client()
        try:
            await client.connect()
            bot = await client.get_entity(BOT_USERNAME)
            bot_id = int(bot.id)
            original = await client.get_messages(bot, ids=int(plan["message_id"]))
            if original is None or getattr(original, "out", None) is not False:
                return {"status": "failed_before_submit", "reason": "official bot message missing"}
            if int(getattr(original, "sender_id", 0) or 0) != bot_id:
                return {"status": "failed_before_submit", "reason": "sender mismatch"}
            normalized = message_to_dict(original, chat=BOT_USERNAME)
            ids = {str(ref.get("id")) for ref in normalized["vacancy_refs"]
                   if ref.get("source") == "getmatch.ru"}
            if ids != {str(plan["vacancy_id"])}:
                return {"status": "failed_before_submit", "reason": "bot vacancy ID mismatch"}
            index = int(plan["button_index"])
            if index < 0 or index >= len(normalized["buttons"]):
                return {"status": "failed_before_submit", "reason": "button index changed"}
            live_button = normalized["buttons"][index]
            if (_positive_button_index(normalized["buttons"]) != index
                    or live_button["callback_sha256"] != plan["callback_sha256"]
                    or not live_button["callback_present"]):
                return {"status": "failed_before_submit", "reason": "bot callback changed"}
            if RECEIPT.search(normalized["text"]):
                return {"status": "already_applied", "reason": "bot message already confirms apply"}

            latest_before = await client.get_messages(bot, limit=1)
            before_id = int(latest_before[0].id) if latest_before else 0
            original_edit_date = getattr(original, "edit_date", None)
            try:
                await original.click(i=index)
            except Exception as exc:  # noqa: BLE001 - callback may already have reached Telegram
                return {
                    "status": "unknown",
                    "reason": f"callback result ambiguous ({type(exc).__name__})",
                }

            for _ in range(max(1, min(30, max_checks))):
                await asyncio.sleep(1)
                recent = await client.get_messages(bot, limit=50)
                fresh = [
                    message for message in recent
                    if int(getattr(message, "id", 0) or 0) > before_id
                    and getattr(message, "out", None) is False
                ]
                # Edited in-place callback source can also be a receipt if the
                # previous message lacked the confirmation before clicking.
                updated = await client.get_messages(bot, ids=int(plan["message_id"]))
                if (updated is not None and
                        getattr(updated, "edit_date", None) is not None and
                        getattr(updated, "edit_date", None) != original_edit_date and
                        RECEIPT.search(message_to_dict(updated)["text"])):
                    fresh.append(updated)
                store.save_telegram_messages(
                    account, [message_to_dict(message, chat=BOT_USERNAME) for message in fresh]
                )
                for message in fresh:
                    if _receipt_for_message(message, str(plan["vacancy_id"]), bot_id):
                        return {
                            "status": "success",
                            "reason": "new official bot confirmation for exact Getmatch vacancy",
                            "receipt": {
                                "provider": BOT_USERNAME,
                                "vacancy_id": str(plan["vacancy_id"]),
                                "confirmation_message_id": int(message.id),
                            },
                        }
            return {
                "status": "unknown",
                "reason": "bot callback clicked but no exact incoming receipt verified",
            }
        finally:
            await client.disconnect()

    try:
        return _run(run())
    except Exception as exc:  # noqa: BLE001 - a sent callback may have an ambiguous result
        return {
            "status": "unknown",
            "reason": f"Telegram callback/verification failure: {type(exc).__name__}",
        }
    finally:
        telegram._harden_session_files()


def plan_getmatch_pilot(
    store: Store,
    vacancy_id: str,
    *,
    account: str = "getmatch-primary",
    telegram_account: str = "getmatch-primary",
    track: str = "ai_core",
) -> dict[str, Any]:
    """Return exact local vacancy and callback identities; never send anything."""
    result: dict[str, Any] = {
        "vacancy_id": str(vacancy_id), "source": "getmatch.ru",
        "account": account, "telegram_account": telegram_account,
        "track": track, "status": "not_ready", "can_send": False,
        "mode": "dry_run",
    }
    vacancy = store.vacancy_track_row(account, "getmatch.ru", str(vacancy_id), track)
    if vacancy is None or vacancy.get("score_decision") is None:
        return {**result, "reason": "vacancy missing from the scored source queue"}
    result.update({
        "name": vacancy["name"], "score": vacancy["score"],
        "review_state": vacancy["review_state"],
        "application_state": vacancy["application_state"],
    })
    if not _getmatch_url_matches(str(vacancy.get("url") or ""), str(vacancy_id)):
        return {**result, "reason": "vacancy has a noncanonical Getmatch URL"}
    if vacancy["application_state"] in {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN"}:
        return {**result, "reason": f"application_state:{vacancy['application_state']}"}
    if (vacancy["score_decision"] != "pass"
            or vacancy["filter_status"] not in {"pass", "unknown"}):
        return {**result, "reason": "vacancy is not eligible for ai_core"}
    if store.channel_conflict(account, "getmatch.ru", str(vacancy_id), f"getmatch.ru:{vacancy_id}"):
        return {**result, "reason": "existing application, UNKNOWN or channel reservation"}
    approved = (
        vacancy["review_state"] == "APPROVED"
        and vacancy["application_state"] == "READY_TO_APPLY"
    )
    profile_status = getmatch_profile_acceptance(store, telegram_account)
    for message in store.telegram_messages(telegram_account, 1000):
        if (str(message.get("chat") or "").casefold().lstrip("@") != "g_jobbot"
                or message.get("outgoing") is not False):
            continue
        refs = [
            str(ref.get("id")) for ref in message.get("vacancy_refs") or []
            if ref.get("source") == "getmatch.ru"
        ]
        if set(refs) != {str(vacancy_id)}:
            continue
        buttons = list(message.get("buttons") or [])
        index = _positive_button_index(buttons)
        if index is None:
            continue
        callback_hash = str(buttons[index].get("callback_sha256") or "")
        if len(callback_hash) != 16:
            continue
        ready = approved and profile_status["status"] == "accepted"
        status = (
            "ready_for_single_live_pilot" if ready else
            "needs_profile_acceptance" if profile_status["status"] != "accepted"
            else "needs_approval"
        )
        return {
            **result, "status": status,
            "profile_acceptance": profile_status,
            "reason": (
                "exact approved vacancy, accepted profile, official apply callback found"
                if ready else
                "official bot profile acceptance must be refreshed"
                if profile_status["status"] != "accepted"
                else "callback present; owner must approve this vacancy first"
            ),
            "message_id": int(message["message_id"]),
            "button_index": index,
            "button_text": buttons[index]["text"],
            "callback_sha256": callback_hash,
        }
    return {**result, "reason": "no current official bot callback for this vacancy"}


def execute_getmatch_pilot(
    store: Store,
    root: Path,
    vacancy_id: str,
    *,
    account: str = "getmatch-primary",
    telegram_account: str = "getmatch-primary",
    track: str = "ai_core",
    allow_live: bool = False,
    run_id: str | None = None,
    transport: Callable[..., dict[str, Any]] = click_and_confirm,
) -> dict[str, Any]:
    """Reserve, submit one approved Getmatch callback, verify, then quarantine."""
    plan = plan_getmatch_pilot(
        store, vacancy_id, account=account,
        telegram_account=telegram_account, track=track,
    )
    if not allow_live:
        return plan
    if os.getenv("APPLYPILOT_TELEGRAM_LIVE") != "1":
        raise LiveActionDisabled("Getmatch live pilot requires APPLYPILOT_TELEGRAM_LIVE=1")
    if plan["status"] != "ready_for_single_live_pilot":
        return plan
    channel = f"getmatch.ru:{vacancy_id}"
    run_id = run_id or "getmatch-pilot-" + uuid.uuid4().hex[:12]
    reserved, note = store.reserve_source_application(
        account, "getmatch.ru", str(vacancy_id), track, channel, run_id,
    )
    if not reserved:
        return {**plan, "status": "channel_conflict", "reason": note}
    if not store.record_source_application(
        account, "getmatch.ru", str(vacancy_id), track, channel,
        "submitting", note="One owner-approved Getmatch callback pilot",
        run_id=run_id, mode="run",
    ):
        return {**plan, "status": "ledger_rejected"}
    try:
        result = transport(root, telegram_account, store, plan)
    except Exception as exc:  # noqa: BLE001 - ambiguous action cannot be retried
        result = {"status": "unknown", "reason": type(exc).__name__}
    status = str(result.get("status") or "unknown")
    if status not in {"success", "already_applied", "unknown", "failed_before_submit"}:
        status = "unknown"
    receipt = result.get("receipt") or {}
    if status == "success" and not (
        isinstance(receipt, dict)
        and receipt.get("provider") == BOT_USERNAME
        and str(receipt.get("vacancy_id") or "") == str(vacancy_id)
        and str(receipt.get("confirmation_message_id") or "").isdigit()
        and int(receipt["confirmation_message_id"]) > 0
    ):
        status = "unknown"
    saved = store.record_source_application(
        account, "getmatch.ru", str(vacancy_id), track, channel,
        status, note=str(result.get("reason") or "bot receipt verification")[:450],
        run_id=run_id, mode="run",
    )
    return {
        "vacancy_id": str(vacancy_id), "source": "getmatch.ru",
        "status": status, "recorded": bool(saved),
        "receipt": result.get("receipt", {}) if status == "success" else {},
        "reason": str(result.get("reason") or "")[:220],
        "mode": "single_live_pilot",
    }
