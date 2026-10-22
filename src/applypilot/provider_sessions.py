"""Independent browser sessions for every ApplyPilot provider and account.

The module is deliberately separate from :mod:`applypilot.session`, which owns
the verified HH login/check flow.  Every provider gets its own persistent
Chrome profile below ``private/data/providers/<source>/<account>/profile``;
no system Chrome ``Default`` profile is ever opened.

Security invariants:

* one writer per profile, enforced with a non-blocking ``flock``;
* ``storage_state.json`` is written atomically with mode ``0600``;
* cookie/auth contents are never read back, copied into results or logged;
* a missing Playwright install and a busy profile are explicit statuses.

The non-HH login/check URLs are navigation targets taken from the public
provider pages.  They are infrastructure, not proof that an authenticated
submit form works; ``live_validated`` in the source catalogue records that
distinction.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from .browser import DEFAULT_WINDOW, real_user_agent
from .session import classify_session_page

try:  # pragma: no cover - the supported host is Linux
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

DEFAULT_ACCOUNT = "default"
PROFILE_DIRNAME = "profile"
STATE_FILENAME = "storage_state.json"
LOCK_FILENAME = ".profile.lock"
PLAYWRIGHT_HINT = "Install browser support: pip install -e '.[browser]'"
_COMPONENT_RE = re.compile(r"^[^\W_][\w.-]{0,63}$", re.UNICODE)

_COMMON_BLOCKED_BODY = (
    "captcha",
    "капч",
    "подтвердите, что вы не робот",
    "i'm not a robot",
    "are you a robot",
    "access denied",
    "доступ запрещ",
    "too many requests",
    "cloudflare",
    "cf-challenge",
    "ddos-guard",
    "запросы заблокированы",
)
_COMMON_BLOCKED_URL = ("/captcha", "/challenge")
_COMMON_EXPIRED_URL = (
    "/login",
    "/signin",
    "/sign-in",
    "/account/login",
    "/oauth/authorize",
)


class SessionStatus(str, Enum):
    """Stable, JSON-friendly outcomes for CLI and admin callers."""

    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    MISSING = "missing"
    SAVED = "saved"
    PLAYWRIGHT_MISSING = "playwright_missing"
    PROFILE_LOCKED = "profile_locked"
    ERROR = "error"


class ProviderSessionError(RuntimeError):
    """Base class for session infrastructure failures."""


class PlaywrightUnavailable(ProviderSessionError):
    """Playwright is not installed or its browser executable is absent."""


class ProfileLocked(ProviderSessionError):
    """Another ApplyPilot process already owns this source/account profile."""


@dataclass(frozen=True)
class ProviderSessionConfig:
    """Navigation and classification data for one provider.

    ``account_label`` is a display label for the default account; callers may
    still pass another account id to keep several independent profiles.
    """

    source: str
    label: str
    login_url: str
    check_url: str
    account_label: str = "Основной"
    default_account: str = DEFAULT_ACCOUNT
    allowed_hosts: tuple[str, ...] = ()
    expired_hosts: tuple[str, ...] = ()
    confirmed_body_markers: tuple[str, ...] = ()
    expired_body_markers: tuple[str, ...] = ()
    blocked_body_markers: tuple[str, ...] = _COMMON_BLOCKED_BODY
    expired_url_markers: tuple[str, ...] = _COMMON_EXPIRED_URL
    blocked_url_markers: tuple[str, ...] = _COMMON_BLOCKED_URL
    delegate_hh: bool = False
    live_validated: bool = False
    login_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": self.label,
            "login_url": self.login_url,
            "check_url": self.check_url,
            "account_label": self.account_label,
            "default_account": self.default_account,
            "allowed_hosts": list(self.allowed_hosts),
            "live_validated": self.live_validated,
            "login_hint": self.login_hint,
        }


_SOURCE_CONFIGS: dict[str, ProviderSessionConfig] = {
    "hh.ru": ProviderSessionConfig(
        source="hh.ru",
        label="HH.ru",
        login_url="https://hh.ru/account/login",
        check_url="https://hh.ru/applicant/resumes",
        allowed_hosts=("hh.ru",),
        delegate_hh=True,
        live_validated=True,
        login_hint="Sign in to HH in the opened Chrome window.",
    ),
    "habr": ProviderSessionConfig(
        source="habr",
        label="Habr Career",
        login_url="https://career.habr.com/login",
        check_url="https://career.habr.com/",
        allowed_hosts=("career.habr.com", "habr.com", "account.habr.com"),
        confirmed_body_markers=(
            "выйти",
            "мои отклики",
            "мои резюме",
            "my applications",
            "log out",
        ),
        expired_body_markers=(
            "войти через хабр аккаунт",
            "войти через хабр",
            "зарегистрироваться",
        ),
        login_hint="Sign in with Habr Account in the opened Chrome window.",
    ),
    "getmatch": ProviderSessionConfig(
        source="getmatch",
        label="Getmatch",
        login_url="https://getmatch.ru/profile",
        check_url="https://getmatch.ru/profile",
        allowed_hosts=("getmatch.ru",),
        confirmed_body_markers=("выйти", "мои отклики", "редактировать профиль", "logout"),
        expired_body_markers=("для просмотра профиля нужно", "войти в getmatch"),
        login_hint="Sign in through the Telegram widget on the profile page.",
    ),
    "geekjob": ProviderSessionConfig(
        source="geekjob",
        label="GeekJob",
        login_url="https://geekjob.ru/signin",
        check_url="https://my.geekjob.ru/",
        allowed_hosts=("geekjob.ru",),
        confirmed_body_markers=("выйти", "мои отклики", "мои резюме", "личный кабинет"),
        expired_body_markers=("войти как", "войти / регистрация"),
        login_hint="Sign in to GeekJob; the personal cabinet is my.geekjob.ru.",
    ),
    "yandex": ProviderSessionConfig(
        source="yandex",
        label="Яндекс Карьера",
        login_url="https://yandex.ru/jobs/profile",
        check_url="https://yandex.ru/jobs/profile",
        allowed_hosts=(
            "yandex.ru",
            "yandex.com",
            "passport.yandex.ru",
            "passport.yandex.com",
        ),
        expired_hosts=("passport.yandex.ru", "passport.yandex.com"),
        confirmed_body_markers=("выйти", "мои отклики", "мой профиль", "личный кабинет"),
        expired_body_markers=("войти в яндекс", "войти с яндекс", "войти через яндекс"),
        login_hint="Sign in with Yandex ID in the opened Chrome window.",
    ),
    "dreamjob": ProviderSessionConfig(
        source="dreamjob",
        label="Dream Job",
        login_url="https://dreamjob.ru/site/login",
        check_url="https://dreamjob.ru/i/",
        allowed_hosts=("dreamjob.ru", "hh.ru"),
        confirmed_body_markers=("выйти", "мои отклики", "личный кабинет"),
        expired_body_markers=("403 forbidden", "вход в личный кабинет", "войти через hh.ru"),
        login_hint=(
            "Dream Job mirrors HH; sign in with the HH account. Applications remain HH-only."
        ),
    ),
}

_SOURCE_ALIASES = {
    "hh": "hh.ru",
    "hh.ru": "hh.ru",
    "habr": "habr",
    "habr_career": "habr",
    "career.habr.com": "habr",
    "getmatch": "getmatch",
    "getmatch.ru": "getmatch",
    "geekjob": "geekjob",
    "geekjob.ru": "geekjob",
    "yandex": "yandex",
    "yandex.ru": "yandex",
    "yandex_jobs": "yandex",
    "dreamjob": "dreamjob",
    "dreamjob.ru": "dreamjob",
}


def default_profile_root() -> Path:
    """Returns the private profile root, overridable for isolated deployments."""
    env = os.getenv("APPLYPILOT_PROVIDER_PROFILE_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "private" / "data" / "providers"


def _safe_component(value: str, kind: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or ".." in text or not _COMPONENT_RE.fullmatch(text):
        raise ValueError(f"invalid {kind}: {value!r}")
    return text


def _canonical_source(source: str) -> str:
    key = str(source or "").strip().lower()
    canonical = _SOURCE_ALIASES.get(key, key)
    if canonical not in _SOURCE_CONFIGS:
        raise KeyError(f"unknown source: {source}")
    return canonical


def _host_matches(host: str, allowed: tuple[str, ...]) -> bool:
    return any(host == item or host.endswith(f".{item}") for item in allowed)


def _path_matches(path: str, marker: str) -> bool:
    marker = marker.rstrip("/") or "/"
    return path == marker or path.startswith(f"{marker}/") or path.endswith(marker)


def _safe_url(url: str) -> str:
    """Drops query/fragment and credentials so tokens never reach CLI output."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    netloc = parsed.hostname.lower()
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((parsed.scheme.lower(), netloc, parsed.path or "/", "", "", ""))


def classify_provider_page(
    config: ProviderSessionConfig, url: str, body: str
) -> tuple[SessionStatus, str]:
    """Classifies an authenticated page by source-specific URL/body markers.

    Cookie presence is deliberately not part of this decision.  HH delegates
    its applicant-page semantics to the existing :func:`classify_session_page`
    after the shared anti-bot check.
    """
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    low_body = str(body or "").lower()

    if parsed.scheme not in {"http", "https"} or not host:
        return SessionStatus.UNKNOWN, "unexpected page"

    if not _host_matches(host, config.allowed_hosts):
        return SessionStatus.UNKNOWN, f"unexpected host: {host}"

    if any(marker in low_body for marker in config.blocked_body_markers):
        return SessionStatus.BLOCKED, "captcha or anti-bot markers found"
    if any(_path_matches(path, marker) for marker in config.blocked_url_markers):
        return SessionStatus.BLOCKED, "anti-bot challenge URL"

    if any(_host_matches(host, (item,)) for item in config.expired_hosts):
        return SessionStatus.EXPIRED, f"redirected to identity provider: {_safe_url(url)}"
    if any(_path_matches(path, marker) for marker in config.expired_url_markers):
        return SessionStatus.EXPIRED, f"redirected to login: {_safe_url(url)}"

    if config.delegate_hh:
        existing = classify_session_page(url, body)
        if existing.status == "confirmed":
            return SessionStatus.CONFIRMED, existing.detail
        if existing.status == "expired":
            return SessionStatus.EXPIRED, existing.detail
        return SessionStatus.UNKNOWN, existing.detail

    confirmed = [marker for marker in config.confirmed_body_markers if marker in low_body]
    expired = [marker for marker in config.expired_body_markers if marker in low_body]
    if confirmed and expired:
        return SessionStatus.UNKNOWN, "ambiguous account and login markers"
    if confirmed:
        return SessionStatus.CONFIRMED, _safe_url(url)
    if expired:
        return SessionStatus.EXPIRED, "login markers found"
    return SessionStatus.UNKNOWN, "account markers not found"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        LOGGER.debug("could not chmod %s", path, exc_info=True)


@contextmanager
def _exclusive_profile_lock(account_dir: Path) -> Iterator[None]:
    """Holds a non-blocking cross-process lock for one source/account."""
    _ensure_private_dir(account_dir)
    lock_path = account_dir / LOCK_FILENAME
    if fcntl is None:  # pragma: no cover - Linux has fcntl
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ProfileLocked("profile is already open by another process") from exc
        try:
            yield
        finally:
            os.close(fd)
            try:
                lock_path.unlink()
            except OSError:
                pass
        return

    with open(lock_path, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ProfileLocked("profile is already open by another process") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                LOGGER.debug("could not unlock profile", exc_info=True)


def _save_storage_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically writes a Playwright storage state with private permissions."""
    _ensure_private_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        try:
            path.chmod(0o600)
        except OSError:
            LOGGER.debug("could not chmod %s", path, exc_info=True)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _valid_storage_state(state: Any) -> bool:
    return isinstance(state, dict) and isinstance(state.get("cookies"), list)


def _looks_like_profile_lock(exc: BaseException) -> bool:
    message = str(exc).lower()
    markers = (
        "processsingleton",
        "singletonlock",
        "profile appears to be in use",
        "user data directory is already in use",
        "profile is already open",
    )
    return any(marker in message for marker in markers)


def _looks_like_missing_playwright(exc: BaseException) -> bool:
    message = str(exc).lower()
    markers = (
        "executable doesn't exist",
        "playwright install",
        "please run the following command to download new browsers",
        "browsertype.launch: executable",
    )
    return any(marker in message for marker in markers)


def _clean_detail(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:240]


def _default_playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _default_playwright_factory() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised through fakes
        raise PlaywrightUnavailable(PLAYWRIGHT_HINT) from exc
    return sync_playwright()


def _launch_kwargs(headless: bool) -> dict[str, Any]:
    args = [
        "--lang=ru-RU",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--hide-crash-restore-bubble",
        "--disable-blink-features=AutomationControlled",
        f"--window-size={DEFAULT_WINDOW['width']},{DEFAULT_WINDOW['height']}",
    ]
    kwargs: dict[str, Any] = {
        "headless": headless,
        "ignore_default_args": ["--enable-automation"],
        "args": args,
        "locale": "ru-RU",
        "timezone_id": "Asia/Krasnoyarsk",
    }
    if headless:
        kwargs["user_agent"] = real_user_agent()
        kwargs["viewport"] = dict(DEFAULT_WINDOW)
        kwargs["screen"] = dict(DEFAULT_WINDOW)
    else:
        kwargs["no_viewport"] = True
    return kwargs


def _channel_unavailable(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "channel" in message
        or ("chrome" in message and "not found" in message)
        or "executable doesn't exist" in message
    )


def _default_context_factory(playwright: Any, profile_dir: Path, headless: bool) -> Any:
    """Launches real Chrome with an explicit profile; never system Default."""
    kwargs = _launch_kwargs(headless)
    chromium = playwright.chromium
    try:
        return chromium.launch_persistent_context(str(profile_dir), channel="chrome", **kwargs)
    except Exception as exc:
        if not _channel_unavailable(exc) or _looks_like_profile_lock(exc):
            raise
        LOGGER.warning("Chrome channel unavailable; using Playwright Chromium")
        return chromium.launch_persistent_context(str(profile_dir), **kwargs)


def _close_context(context: Any) -> None:
    close = getattr(context, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            LOGGER.debug("failed to close provider browser context", exc_info=True)


class ProviderSessionManager:
    """Owns independent Playwright profiles for providers and accounts.

    Factories are injectable so the complete login/check state machine can be
    tested offline without a browser, network or real authentication bytes.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        playwright_factory: Callable[[], Any] | None = None,
        context_factory: Callable[[Any, Path, bool], Any] | None = None,
        page_factory: Callable[[Any], Any] | None = None,
        input_fn: Callable[[str], str] | None = None,
        nav_timeout_ms: int = 30_000,
        body_timeout_ms: int = 5_000,
    ) -> None:
        self.root = Path(root).expanduser() if root is not None else default_profile_root()
        self._playwright_factory = playwright_factory
        self._context_factory = context_factory or _default_context_factory
        self._page_factory = page_factory or (lambda context: context.new_page())
        self._input_fn = input_fn or input
        self._nav_timeout_ms = max(1, int(nav_timeout_ms))
        self._body_timeout_ms = max(1, int(body_timeout_ms))

    def source_config(self, source: str) -> ProviderSessionConfig:
        return _SOURCE_CONFIGS[_canonical_source(source)]

    def available_sources(self) -> list[str]:
        return list(_SOURCE_CONFIGS)

    def catalogue(self) -> list[dict[str, Any]]:
        return [config.as_dict() for config in _SOURCE_CONFIGS.values()]

    def _resolve(self, source: str, account: str | None) -> tuple[ProviderSessionConfig, str]:
        config = self.source_config(source)
        account_id = _safe_component(
            account if account is not None else config.default_account, "account"
        )
        return config, account_id

    def _account_dir(self, config: ProviderSessionConfig, account: str) -> Path:
        return self.root / config.source / account

    def profile_dir(self, source: str, account: str | None = None) -> Path:
        config, account_id = self._resolve(source, account)
        return self._account_dir(config, account_id) / PROFILE_DIRNAME

    def storage_state_path(self, source: str, account: str | None = None) -> Path:
        config, account_id = self._resolve(source, account)
        return self._account_dir(config, account_id) / STATE_FILENAME

    def session_paths(self, source: str, account: str | None = None) -> dict[str, str]:
        config, account_id = self._resolve(source, account)
        account_dir = self._account_dir(config, account_id)
        return {
            "source": config.source,
            "account": account_id,
            "account_dir": str(account_dir),
            "profile_dir": str(account_dir / PROFILE_DIRNAME),
            "storage_state": str(account_dir / STATE_FILENAME),
            "lock": str(account_dir / LOCK_FILENAME),
        }

    def _playwright_available(self) -> bool:
        if self._playwright_factory is not None:
            return True
        return _default_playwright_available()

    def _open_playwright(self) -> Any:
        factory = self._playwright_factory or _default_playwright_factory
        try:
            return factory()
        except PlaywrightUnavailable:
            raise
        except ImportError as exc:
            raise PlaywrightUnavailable(PLAYWRIGHT_HINT) from exc

    def _result(
        self,
        config: ProviderSessionConfig,
        account: str,
        status: SessionStatus,
        detail: str,
        *,
        profile_dir: Path,
        state_path: Path,
        url: str = "",
    ) -> dict[str, Any]:
        account_label = config.account_label if account == config.default_account else account
        return {
            "source": config.source,
            "label": config.label,
            "account": account,
            "account_label": account_label,
            "status": status.value,
            "ok": status in {SessionStatus.CONFIRMED, SessionStatus.SAVED},
            "detail": detail,
            "url": _safe_url(url),
            "login_url": config.login_url,
            "check_url": config.check_url,
            "login_hint": config.login_hint,
            "profile_dir": str(profile_dir),
            "storage_state": str(state_path),
            "storage_state_present": state_path.exists(),
            "live_validated": config.live_validated,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    def _failure(
        self,
        config: ProviderSessionConfig,
        account: str,
        profile_dir: Path,
        state_path: Path,
        exc: BaseException,
    ) -> dict[str, Any]:
        if isinstance(exc, ProfileLocked) or _looks_like_profile_lock(exc):
            return self._result(
                config,
                account,
                SessionStatus.PROFILE_LOCKED,
                "profile is already open by another ApplyPilot process",
                profile_dir=profile_dir,
                state_path=state_path,
            )
        if isinstance(exc, PlaywrightUnavailable) or _looks_like_missing_playwright(exc):
            return self._result(
                config,
                account,
                SessionStatus.PLAYWRIGHT_MISSING,
                PLAYWRIGHT_HINT,
                profile_dir=profile_dir,
                state_path=state_path,
            )
        return self._result(
            config,
            account,
            SessionStatus.ERROR,
            _clean_detail(exc),
            profile_dir=profile_dir,
            state_path=state_path,
        )

    def login(self, source: str, account: str | None = None, *,
              wait_seconds: int = 300, interactive: bool = True) -> dict[str, Any]:
        """Opens visible persistent Chrome and records a manual login.

        The method returns ``saved`` after ``storage_state.json`` is written.
        It does not claim that the provider considers the session confirmed;
        call :meth:`check` for the source-specific proof.
        """
        config, account_id = self._resolve(source, account)
        profile_dir = self.profile_dir(config.source, account_id)
        state_path = self.storage_state_path(config.source, account_id)

        if not self._playwright_available():
            return self._result(
                config,
                account_id,
                SessionStatus.PLAYWRIGHT_MISSING,
                PLAYWRIGHT_HINT,
                profile_dir=profile_dir,
                state_path=state_path,
            )

        try:
            with _exclusive_profile_lock(profile_dir.parent), self._open_playwright() as playwright:
                context = self._context_factory(playwright, profile_dir, False)
                try:
                    page = self._page_factory(context)
                    page.goto(
                        config.login_url,
                        wait_until="domcontentloaded",
                        timeout=self._nav_timeout_ms,
                    )
                    # A background job has no TTY.  Never call input() there:
                    # poll the real page marker instead.  An injected input_fn
                    # is still honored for the CLI and tests.
                    can_prompt = (
                        interactive
                        and (self._input_fn is not input or sys.stdin.isatty())
                    )
                    if can_prompt:
                        prompt = (
                            f"Log in to {config.label} in the Chrome window, then press Enter here: "
                        )
                        try:
                            self._input_fn(prompt)
                        except (EOFError, KeyboardInterrupt):
                            return self._result(
                                config,
                                account_id,
                                SessionStatus.UNKNOWN,
                                "login cancelled before the session was saved",
                                profile_dir=profile_dir,
                                state_path=state_path,
                                url=getattr(page, "url", config.login_url),
                            )
                    else:
                        deadline = time.monotonic() + max(0, int(wait_seconds))
                        while True:
                            body = self._read_body(page)
                            status, _detail = classify_provider_page(
                                config, getattr(page, "url", config.login_url), body,
                            )
                            if status == SessionStatus.CONFIRMED:
                                break
                            if time.monotonic() >= deadline:
                                return self._result(
                                    config,
                                    account_id,
                                    SessionStatus.UNKNOWN,
                                    "login window did not reach a confirmed marker; "
                                    "finish login and run check",
                                    profile_dir=profile_dir,
                                    state_path=state_path,
                                    url=getattr(page, "url", config.login_url),
                                )
                            time.sleep(2)
                    state = context.storage_state()
                    if not _valid_storage_state(state):
                        return self._result(
                            config,
                            account_id,
                            SessionStatus.UNKNOWN,
                            "browser returned a malformed storage state",
                            profile_dir=profile_dir,
                            state_path=state_path,
                            url=getattr(page, "url", config.login_url),
                        )
                    _save_storage_state(state_path, state)
                    return self._result(
                        config,
                        account_id,
                        SessionStatus.SAVED,
                        "manual login recorded; run check to confirm the session",
                        profile_dir=profile_dir,
                        state_path=state_path,
                        url=getattr(page, "url", config.login_url),
                    )
                finally:
                    _close_context(context)
        except Exception as exc:  # noqa: BLE001 - all browser failures become statuses
            return self._failure(config, account_id, profile_dir, state_path, exc)

    def _has_session(self, profile_dir: Path, state_path: Path) -> bool:
        if state_path.exists():
            return True
        return profile_dir.is_dir() and any(profile_dir.iterdir())

    def _read_body(self, page: Any) -> str:
        try:
            return str(page.locator("body").inner_text(timeout=self._body_timeout_ms))
        except Exception:
            LOGGER.debug("could not read provider page body", exc_info=True)
            return ""

    def check(self, source: str, account: str | None = None) -> dict[str, Any]:
        """Opens headless persistent Chrome and proves or rejects the session."""
        config, account_id = self._resolve(source, account)
        profile_dir = self.profile_dir(config.source, account_id)
        state_path = self.storage_state_path(config.source, account_id)

        if not self._playwright_available():
            return self._result(
                config,
                account_id,
                SessionStatus.PLAYWRIGHT_MISSING,
                PLAYWRIGHT_HINT,
                profile_dir=profile_dir,
                state_path=state_path,
            )

        try:
            with _exclusive_profile_lock(profile_dir.parent):
                if not self._has_session(profile_dir, state_path):
                    return self._result(
                        config,
                        account_id,
                        SessionStatus.MISSING,
                        "no saved session or browser profile for this account",
                        profile_dir=profile_dir,
                        state_path=state_path,
                    )
                with self._open_playwright() as playwright:
                    context = self._context_factory(playwright, profile_dir, True)
                    try:
                        page = self._page_factory(context)
                        page.goto(
                            config.check_url,
                            wait_until="domcontentloaded",
                            timeout=self._nav_timeout_ms,
                        )
                        body = self._read_body(page)
                        status, _detail = classify_provider_page(
                            config, getattr(page, "url", config.check_url), body
                        )
                        return self._result(
                            config,
                            account_id,
                            status,
                            _detail,
                            profile_dir=profile_dir,
                            state_path=state_path,
                            url=getattr(page, "url", config.check_url),
                        )
                    finally:
                        _close_context(context)
        except Exception as exc:  # noqa: BLE001 - all browser failures become statuses
            return self._failure(config, account_id, profile_dir, state_path, exc)
