"""Source-specific clicker contracts for authorized-form validation.

This module deliberately contains no guessed CSS selectors and no submit
implementation.  It provides the shared preflight/inspection contract that the
live validation pass will fill in with DOM captured from an authorized session.

``inspect_page`` walks generic accessible elements (buttons, links, inputs,
textareas, selects) and returns their visible labels.  It never clicks, types or
submits, so it is safe to run on a logged-in page before a clicker is approved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dispatch import ApplyOutcome


@dataclass(frozen=True)
class ClickerSpec:
    source: str
    label: str
    login_url: str
    profile_required: bool = True
    submit_verified: bool = False
    apply_status: str = "needs_live_validation"
    selectors: tuple[str, ...] = ()
    notes: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": self.label,
            "login_url": self.login_url,
            "profile_required": self.profile_required,
            "submit_verified": self.submit_verified,
            "apply_status": self.apply_status,
            "selectors": list(self.selectors),
            "notes": self.notes,
        }


CLICKERS: dict[str, ClickerSpec] = {
    "career.habr.com": ClickerSpec(
        source="career.habr.com", label="Habr Career",
        login_url="https://career.habr.com/login",
        notes="Authorized form DOM not captured yet; no selector or submit is claimed.",
    ),
    "geekjob.ru": ClickerSpec(
        source="geekjob.ru", label="GeekJob",
        login_url="https://geekjob.ru/login",
        notes="Authorized form DOM not captured yet; no selector or submit is claimed.",
    ),
    "yandex.ru/jobs": ClickerSpec(
        source="yandex.ru/jobs", label="Yandex Jobs",
        login_url="https://yandex.ru/jobs/",
        notes="Authorized form DOM not captured yet; no selector or submit is claimed.",
    ),
    "getmatch.ru": ClickerSpec(
        source="getmatch.ru", label="Getmatch / Telegram",
        login_url="https://getmatch.ru/",
        profile_required=False,
        notes="Applications arrive through @g_jobbot WebView/callback; inspect the Telegram WebView first.",
    ),
}


def clicker_specs(sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
    selected = list(sources) if sources else list(CLICKERS)
    return [CLICKERS[source].public() for source in selected if source in CLICKERS]


def preflight(source: str, profile_dir: Path | str | None) -> dict[str, Any]:
    """Checks only local prerequisites; never claims a form is ready to submit."""
    spec = CLICKERS.get(source)
    if spec is None:
        return {"source": source, "status": "unsupported", "ready_to_submit": False}
    if not spec.profile_required:
        return {
            "source": source, "status": "telegram_session_required",
            "ready_to_submit": False, "apply_status": spec.apply_status,
            "detail": "use accounts telegram-status and inspect the WebView",
        }
    path = Path(profile_dir) if profile_dir else None
    if path is None or not path.is_dir():
        return {
            "source": source, "status": "profile_missing",
            "ready_to_submit": False, "apply_status": spec.apply_status,
            "detail": "run sources login and complete the authorized browser login",
        }
    return {
        "source": source, "status": "ready_for_inspection",
        "ready_to_submit": False, "apply_status": spec.apply_status,
        "detail": "profile exists; inspect the authorized DOM before enabling submit",
    }


def inspect_page(page: Any, *, limit: int = 200) -> dict[str, Any]:
    """Reads generic accessible elements without clicking or submitting."""
    elements: list[dict[str, Any]] = []
    try:
        locator = page.locator("button, a, input, textarea, select")
        count = min(int(locator.count()), max(1, int(limit)))
    except Exception as exc:  # noqa: BLE001 - a broken page is a reported status
        return {"status": "inspection_failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "elements": [], "submitted": False}
    for index in range(count):
        item = locator.nth(index)
        try:
            tag = str(item.evaluate("el => el.tagName.toLowerCase()"))
        except Exception:  # noqa: BLE001
            tag = ""
        try:
            text = str(item.inner_text(timeout=500) or "").strip()[:200]
        except Exception:  # noqa: BLE001
            text = ""
        try:
            href = str(item.get_attribute("href") or "")[:500]
        except Exception:  # noqa: BLE001
            href = ""
        try:
            input_type = str(item.get_attribute("type") or "")
        except Exception:  # noqa: BLE001
            input_type = ""
        try:
            aria = str(item.get_attribute("aria-label") or "")[:200]
        except Exception:  # noqa: BLE001
            aria = ""
        elements.append({
            "tag": tag, "text": text, "href": href,
            "type": input_type, "aria_label": aria,
        })
    return {
        "status": "inspected",
        "submitted": False,
        "clicked": False,
        "count": len(elements),
        "elements": elements,
    }


def apply_with_clicker(source: str, row: Mapping[str, Any]) -> ApplyOutcome:
    """Placeholder that never submits until an authorized DOM is captured."""
    spec = CLICKERS.get(source)
    if spec is None:
        return ApplyOutcome("needs_live_validation", f"unsupported source: {source}")
    return ApplyOutcome(
        "needs_live_validation",
        f"{spec.label}: authorized form DOM is not captured; submit stays disabled",
    )
