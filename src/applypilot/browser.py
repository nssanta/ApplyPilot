"""Consistent Chrome browser boundary for HH operations.

The mass-apply path uses a real installed Google Chrome with a persistent
profile instead of Playwright's temporary headless Chromium context. Cookies
remain in the profile; ``hh_session.json`` is still written as a portable,
read-only-compatible export for sync/inspect tooling.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_PROFILE_DIRNAME = "hh_chrome_profile"
DEFAULT_LOCALE = "ru-RU"
# HH sees the direct Russian ISP route (Krasnoyarsk, UTC+7), not the US proxy
# used for general web traffic. Keep the browser clock aligned with that route.
DEFAULT_TIMEZONE = "Asia/Krasnoyarsk"
DEFAULT_WINDOW = {"width": 1366, "height": 768}
_CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)
_UA_RE = re.compile(r"Chrome/(\d+)\.")


def _chrome_binary() -> str | None:
    explicit = os.getenv("APPLYPILOT_CHROME_EXECUTABLE", "").strip()
    if explicit:
        return explicit
    for name in _CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def chrome_version() -> str:
    binary = _chrome_binary()
    if not binary:
        return ""
    try:
        output = subprocess.check_output([binary, "--version"], text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = _UA_RE.search(output) or re.search(r"(\d+\.\d+\.\d+\.\d+)", output)
    return match.group(1) if match else ""


def real_user_agent() -> str:
    """Returns the installed Chrome's genuine Linux User-Agent string."""
    version = chrome_version()
    major = version.split(".", 1)[0] if version else "156"
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


@dataclass
class BrowserSession:
    """Owns either a persistent Chrome context or a test/fallback context."""

    context: Any
    browser: Any | None = None
    persistent: bool = False
    profile_dir: Path | None = None

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            if self.browser is not None:
                self.browser.close()


def browser_settings(profile: dict[str, Any] | None) -> dict[str, Any]:
    """Reads optional ``[browser]`` settings without changing profile semantics."""
    raw = (profile or {}).get("browser")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "headless": bool(raw.get("headless", False)),
        "locale": str(raw.get("locale") or DEFAULT_LOCALE),
        "timezone": str(raw.get("timezone") or DEFAULT_TIMEZONE),
        "profile_dir": str(raw.get("profile_dir") or "").strip(),
        "channel": str(raw.get("channel") or "chrome").strip(),
        "manual_captcha_timeout_seconds": max(
            0, int(raw.get("manual_captcha_timeout_seconds", 300))
        ),
        "captcha_mode": str(raw.get("captcha_mode") or "manual").strip().lower(),
    }


def _looks_seeded(profile_dir: Path) -> bool:
    return (profile_dir / ".applypilot_seeded").exists() or (profile_dir / ".seeded").exists()


def _normalize_hh_zoom(profile_dir: Path) -> None:
    """Keeps HH at the browser's normal 100% zoom.

    Chrome on a HiDPI X11 display can persist an 80% per-host zoom for hh.ru.
    That makes ``innerWidth`` larger than the physical screen and creates a
    needless fingerprint inconsistency, so the dedicated HH profile is reset.
    """
    preferences = profile_dir / "Default" / "Preferences"
    if not preferences.exists():
        return
    try:
        data = json.loads(preferences.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    partition = data.get("partition")
    if not isinstance(partition, dict):
        return
    changed = False
    per_host = partition.get("per_host_zoom_levels")
    if isinstance(per_host, dict):
        for hosts in per_host.values():
            if not isinstance(hosts, dict):
                continue
            for host in list(hosts):
                if host == "hh.ru" or host.endswith(".hh.ru"):
                    hosts.pop(host)
                    changed = True
    if "default_zoom_level" in partition:
        partition["default_zoom_level"] = 0
        changed = True
    if not changed:
        return
    temporary = preferences.with_name(f".{preferences.name}.applypilot")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(preferences)
    except OSError:
        LOGGER.debug("Could not normalize HH profile zoom", exc_info=True)


def _import_storage_state(context: Any, state_path: Path) -> bool:
    """Seeds a new profile from the portable HH state, including localStorage."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("HH storage state is unavailable; persistent profile is used as-is")
        return False
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        return False
    if cookies:
        context.add_cookies(cookies)
    for origin in data.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        url = str(origin.get("origin") or "").strip()
        items = origin.get("localStorage")
        if not url or not isinstance(items, list) or not items:
            continue
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.evaluate(
                "items => { for (const item of items) localStorage.setItem(item.name, item.value); }",
                items,
            )
        except Exception:
            LOGGER.warning("Could not import localStorage for %s", url, exc_info=True)
        finally:
            page.close()
    return True


def launch_context(
    playwright: Any,
    *,
    data_dir: Path,
    state_path: Path | None = None,
    profile: dict[str, Any] | None = None,
    headless: bool | None = None,
) -> BrowserSession:
    """Launches real Chrome with a persistent profile and a coherent fingerprint.

    ``channel="chrome"`` uses the installed Chrome (not Playwright's bundled
    Chromium). The automation flag is removed so ``navigator.webdriver`` is
    false, while the real UA, Client Hints, plugins, locale and viewport stay
    untouched. Timezone is aligned with the HH-visible Russian network route.
    """
    settings = browser_settings(profile)
    if headless is not None:
        settings["headless"] = headless
    else:
        env_headless = os.getenv("APPLYPILOT_BROWSER_HEADLESS", "").strip().lower()
        if env_headless in {"1", "true", "yes", "on"}:
            settings["headless"] = True
        elif env_headless in {"0", "false", "no", "off"}:
            settings["headless"] = False
    profile_dir = Path(settings["profile_dir"]) if settings["profile_dir"] else data_dir / DEFAULT_PROFILE_DIRNAME
    if not profile_dir.is_absolute():
        if profile_dir.parts and profile_dir.parts[0] == "private":
            profile_dir = data_dir.parent.parent / profile_dir
        else:
            profile_dir = data_dir / profile_dir
    profile_dir.mkdir(parents=True, exist_ok=True)
    _normalize_hh_zoom(profile_dir)
    args = [
        f"--lang={settings['locale']}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--hide-crash-restore-bubble",
        "--disable-blink-features=AutomationControlled",
        f"--window-size={DEFAULT_WINDOW['width']},{DEFAULT_WINDOW['height']}",
    ]
    headless_mode = bool(settings["headless"])
    kwargs = {
        "headless": headless_mode,
        "ignore_default_args": ["--enable-automation"],
        "args": args,
        "locale": settings["locale"],
        "timezone_id": settings["timezone"],
    }
    if headless_mode:
        # New headless Chrome advertises HeadlessChrome in the UA and defaults
        # to an 800x600 screen. Keep the installed Chrome identity and the same
        # desktop dimensions as visible mode. CAPTCHA still cannot be solved
        # here, so the runner stops and asks for --visible instead.
        kwargs["user_agent"] = real_user_agent()
        kwargs["viewport"] = dict(DEFAULT_WINDOW)
        kwargs["screen"] = dict(DEFAULT_WINDOW)
    else:
        kwargs["no_viewport"] = True
    chromium = playwright.chromium
    if hasattr(chromium, "launch_persistent_context"):
        channel = settings["channel"]
        launch_kwargs = dict(kwargs)
        if channel:
            launch_kwargs["channel"] = channel
        try:
            context = chromium.launch_persistent_context(str(profile_dir), **launch_kwargs)
        except Exception:
            if not channel:
                raise
            LOGGER.warning("Chrome channel %r is unavailable; falling back to Playwright Chromium", channel)
            context = chromium.launch_persistent_context(str(profile_dir), **kwargs)
        if (state_path is not None and state_path.exists() and not _looks_seeded(profile_dir)
                and _import_storage_state(context, state_path)):
            (profile_dir / ".applypilot_seeded").write_text("1", encoding="utf-8")
            profile_dir.chmod(0o700)
        return BrowserSession(context=context, persistent=True, profile_dir=profile_dir)
    # Compatibility path for test doubles and installations without persistent
    # context support. Real production runs always take the branch above.
    browser = chromium.launch(headless=bool(settings["headless"]))
    context_kwargs: dict[str, Any] = {
        "viewport": dict(DEFAULT_WINDOW),
        "locale": settings["locale"],
        "timezone_id": settings["timezone"],
    }
    if state_path is not None and state_path.exists():
        context_kwargs["storage_state"] = str(state_path)
    context = browser.new_context(**context_kwargs)
    return BrowserSession(context=context, browser=browser, persistent=False, profile_dir=profile_dir)
