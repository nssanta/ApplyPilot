"""Official HH OAuth API client (additive; the browser clicker stays intact).

Only documented read endpoints are used for real requests.  The applicant
apply operation exists in HH's OpenAPI, but its exact schema and account
eligibility have not been verified live, so this module builds an explicit
request spec and refuses to send until a separate live-validation pass.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from .secure_store import read_private_json, secret_state, write_private_json

HH_API_BASE = "https://api.hh.ru"
HH_AUTHORIZE_URL = "https://hh.ru/oauth/authorize"
HH_TOKEN_URL = "https://api.hh.ru/token"
HH_OAUTH_ROOT = Path("private/data/hh_oauth")
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
TOKEN_REFRESH_SKEW_SECONDS = 60
OAUTH_STATE_TTL_SECONDS = 600

CAPABILITIES: dict[str, dict[str, str]] = {
    "me": {
        "status": "documented_read",
        "method": "GET /me",
        "source": "https://api.hh.ru/openapi/redoc",
    },
    "negotiations": {
        "status": "documented_read",
        "method": "GET /negotiations",
        "source": "https://github.com/hhru/api/blob/master/docs/negotiations.md",
    },
    "messages": {
        "status": "documented_read",
        "method": "GET /negotiations/{id}/messages",
        "source": "https://github.com/hhru/api/blob/master/docs/negotiations.md",
    },
    "apply": {
        "status": "needs_live_validation",
        "method": "POST /negotiations",
        "source": "https://api.hh.ru/openapi/redoc#tag/Vakansii/operation/apply-to-vacancy",
        "note": "Exact body schema and applicant eligibility require a live OAuth pass.",
    },
}


class HhOAuthError(RuntimeError):
    """Base error for the local OAuth client."""


class HhOAuthNotConfigured(HhOAuthError):
    """Raised when client credentials or a token are missing."""


class HhOAuthStateError(HhOAuthError):
    """Raised when the OAuth state does not match the local authorization."""


class LiveActionDisabled(HhOAuthError):
    """Raised for a real apply request before live validation."""


@dataclass(frozen=True)
class HhOAuthCredentials:
    client_id: str
    client_secret: str
    redirect_uri: str = DEFAULT_REDIRECT_URI

    def public_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "client_secret": "***",
            "client_secret_state": secret_state(self.client_secret),
            "redirect_uri": self.redirect_uri,
        }


def account_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().casefold()).strip("-") or "default"


def credentials_path(root: Path) -> Path:
    return root / HH_OAUTH_ROOT / "credentials.json"


def token_path(root: Path, account: str) -> Path:
    return root / HH_OAUTH_ROOT / account_slug(account) / "token.json"


def state_path(root: Path, account: str) -> Path:
    return root / HH_OAUTH_ROOT / account_slug(account) / "state.json"


def load_credentials(root: Path) -> HhOAuthCredentials | None:
    env_id = os.getenv("APPLYPILOT_HH_CLIENT_ID", "").strip()
    env_secret = os.getenv("APPLYPILOT_HH_CLIENT_SECRET", "").strip()
    env_redirect = os.getenv("APPLYPILOT_HH_REDIRECT_URI", "").strip()
    if env_id and env_secret:
        return HhOAuthCredentials(env_id, env_secret, env_redirect or DEFAULT_REDIRECT_URI)
    payload = read_private_json(credentials_path(root))
    if not payload:
        return None
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    redirect_uri = str(payload.get("redirect_uri") or DEFAULT_REDIRECT_URI).strip()
    if not client_id or not client_secret:
        return None
    return HhOAuthCredentials(client_id, client_secret, redirect_uri)


def save_credentials(root: Path, client_id: str, client_secret: str,
                     redirect_uri: str = DEFAULT_REDIRECT_URI) -> Path:
    if not str(client_id or "").strip():
        raise ValueError("client_id is required")
    if not str(client_secret or "").strip():
        raise ValueError("client_secret is required")
    if not str(redirect_uri or "").strip():
        raise ValueError("redirect_uri is required")
    return write_private_json(
        credentials_path(root),
        {
            "client_id": str(client_id).strip(),
            "client_secret": str(client_secret).strip(),
            "redirect_uri": str(redirect_uri).strip(),
        },
    )


def credentials_status(root: Path) -> dict[str, Any]:
    credentials = load_credentials(root)
    if credentials is None:
        return {"status": "missing", "client_secret_state": "missing"}
    return {"status": "configured", **credentials.public_dict()}


def _iso_expiry(expires_in: Any) -> str:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = 0
    return (datetime.now(UTC) + timedelta(seconds=max(0, seconds))).isoformat()


def _is_expired(expires_at: str) -> bool:
    try:
        parsed = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC) <= datetime.now(UTC) + timedelta(seconds=TOKEN_REFRESH_SKEW_SECONDS)


def _state_is_expired(created_at: str) -> bool:
    """OAuth state is short-lived: an old authorize URL must not be reusable."""
    try:
        parsed = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC) + timedelta(seconds=OAUTH_STATE_TTL_SECONDS) <= datetime.now(UTC)


def parse_redirect_input(value: str) -> tuple[str, str, str]:
    """Splits a pasted redirect URL into ``(code, state, error)``.

    A bare authorization code is returned as-is with empty state.  HH may send
    ``error=access_denied`` instead of a code; that is surfaced without echoing
    the whole URL, which can contain a one-time code.
    """
    raw = str(value or "").strip()
    if not raw:
        return "", "", ""
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"} and (parsed.query or parsed.fragment):
        params = parse_qs(parsed.query or parsed.fragment, keep_blank_values=True)
        return (
            str((params.get("code") or [""])[0]).strip(),
            str((params.get("state") or [""])[0]).strip(),
            str((params.get("error") or [""])[0]).strip(),
        )
    return raw, "", ""


class HhOAuthAccount:
    """One local HH OAuth token set, isolated per account label."""

    def __init__(
        self,
        root: Path,
        account: str = "hh-primary",
        *,
        credentials: HhOAuthCredentials | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.root = root
        self.account = account_slug(account)
        self._credentials = credentials
        self._transport = transport
        self._timeout = timeout

    def _credentials_or_raise(self) -> HhOAuthCredentials:
        credentials = self._credentials or load_credentials(self.root)
        if credentials is None:
            raise HhOAuthNotConfigured(
                "HH OAuth client credentials are missing; run "
                "'applypilot accounts hh-oauth-setup'"
            )
        return credentials

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self._timeout, transport=self._transport)

    def _public_base(self) -> dict[str, Any]:
        return {
            "provider": "hh_oauth",
            "account": self.account,
            "token_path": str(token_path(self.root, self.account)),
            "apply_status": "needs_live_validation",
        }

    def authorize_url(self) -> dict[str, Any]:
        credentials = self._credentials_or_raise()
        state = secrets.token_urlsafe(24)
        write_private_json(
            state_path(self.root, self.account),
            {
                "state": state,
                "created_at": datetime.now(UTC).isoformat(),
                "redirect_uri": credentials.redirect_uri,
            },
        )
        query = urlencode({
            "response_type": "code",
            "client_id": credentials.client_id,
            "redirect_uri": credentials.redirect_uri,
            "state": state,
        })
        return {
            **self._public_base(),
            "status": "authorization_required",
            "url": f"{HH_AUTHORIZE_URL}?{query}",
            "state": state,
            "redirect_uri": credentials.redirect_uri,
            "detail": "open the URL locally, approve access, then exchange the returned code",
        }

    def _save_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise HhOAuthError("HH token response has no access_token")
        token = {
            "access_token": access_token,
            "refresh_token": str(payload.get("refresh_token") or "").strip(),
            "token_type": str(payload.get("token_type") or "bearer").strip(),
            "scope": str(payload.get("scope") or "").strip(),
            "expires_at": _iso_expiry(payload.get("expires_in")),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        write_private_json(token_path(self.root, self.account), token)
        return token

    def _load_token(self) -> dict[str, Any] | None:
        return read_private_json(token_path(self.root, self.account))

    def _refresh(self, token: dict[str, Any]) -> dict[str, Any]:
        refresh_token = str(token.get("refresh_token") or "").strip()
        if not refresh_token:
            raise HhOAuthError("HH token has no refresh_token")
        credentials = self._credentials_or_raise()
        with self._client() as client:
            response = client.post(
                HH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
        return self._save_token(payload)

    def _valid_token(self) -> dict[str, Any]:
        token = self._load_token()
        if token is None:
            raise HhOAuthNotConfigured("HH OAuth token is missing; authorize the account first")
        if _is_expired(str(token.get("expires_at") or "")):
            token = self._refresh(token)
        return token

    def _auth_headers(self, token: dict[str, Any]) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token['access_token']}",
            "HH-User-Agent": "ApplyPilot/0.1 (local OAuth)",
        }

    def _clear_state(self) -> None:
        try:
            state_path(self.root, self.account).unlink()
        except OSError:
            pass

    def exchange_code(self, code: str, state: str | None = None) -> dict[str, Any]:
        """Exchanges an authorization code after mandatory CSRF state checks.

        ``code`` may be a bare code (then ``state`` is mandatory) or the full
        local redirect URL, whose ``code`` and ``state`` query parameters are
        extracted.  The stored state must exist, match, and be younger than
        ``OAUTH_STATE_TTL_SECONDS`` on every exchange.
        """
        raw_code, url_state, url_error = parse_redirect_input(code)
        if url_error:
            raise HhOAuthError(f"HH authorization was not granted: {url_error}")
        if not raw_code:
            raise ValueError("authorization code is required")
        supplied_state = str(state or "").strip() or url_state
        stored = read_private_json(state_path(self.root, self.account)) or {}
        expected = str(stored.get("state") or "").strip()
        if not expected:
            raise HhOAuthStateError(
                "OAuth state is missing; run 'applypilot accounts hh-oauth-authorize' again"
            )
        if not supplied_state:
            raise HhOAuthStateError(
                "OAuth state is required; paste the full redirect URL or pass --state"
            )
        if not secrets.compare_digest(supplied_state, expected):
            raise HhOAuthStateError("OAuth state does not match the local authorization")
        if _state_is_expired(str(stored.get("created_at") or "")):
            self._clear_state()
            raise HhOAuthStateError(
                "OAuth state has expired; run 'applypilot accounts hh-oauth-authorize' again"
            )
        credentials = self._credentials_or_raise()
        with self._client() as client:
            response = client.post(
                HH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": raw_code,
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                    "redirect_uri": credentials.redirect_uri,
                },
            )
            response.raise_for_status()
            payload = response.json()
        self._save_token(payload)
        # One-time state: a captured redirect URL cannot be replayed.
        self._clear_state()
        return {**self._public_base(), "status": "authorized",
                "token_state": "set", "expires_at": self._load_token().get("expires_at", "")}

    def status(self, *, check_remote: bool = False) -> dict[str, Any]:
        token = self._load_token()
        if token is None:
            return {**self._public_base(), "status": "missing",
                    "detail": "no local HH OAuth token"}
        base = {
            **self._public_base(),
            "status": "token_present",
            "token_state": "set",
            "refresh_token_state": secret_state(token.get("refresh_token")),
            "expires_at": str(token.get("expires_at") or ""),
            "scope": str(token.get("scope") or ""),
        }
        if not check_remote:
            if _is_expired(str(token.get("expires_at") or "")):
                base["status"] = "expired"
            return base
        try:
            token = self._valid_token()
            with self._client() as client:
                response = client.get(f"{HH_API_BASE}/me", headers=self._auth_headers(token))
                response.raise_for_status()
                payload = response.json()
        except (HhOAuthError, httpx.HTTPError) as exc:
            return {**base, "status": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
        return {
            **base,
            "status": "confirmed",
            "hh_user_id": payload.get("id"),
            "is_applicant": payload.get("is_applicant"),
        }

    def negotiations(self, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 100:
            raise ValueError("limit must be between 1 and 100")
        token = self._valid_token()
        with self._client() as client:
            response = client.get(
                f"{HH_API_BASE}/negotiations",
                params={"per_page": int(limit)},
                headers=self._auth_headers(token),
            )
            response.raise_for_status()
            payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else []
        result: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            vacancy = item.get("vacancy") if isinstance(item.get("vacancy"), dict) else {}
            resume = item.get("resume") if isinstance(item.get("resume"), dict) else {}
            result.append({
                "id": item.get("id"),
                "state": item.get("state", ""),
                "created_at": item.get("created_at", ""),
                "updated_at": item.get("updated_at", ""),
                "has_updates": bool(item.get("has_updates")),
                "vacancy_id": vacancy.get("id", ""),
                "vacancy_name": vacancy.get("name", ""),
                "resume_id": resume.get("id", ""),
            })
        return result

    def messages(self, negotiation_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if not str(negotiation_id or "").strip():
            raise ValueError("negotiation_id is required")
        token = self._valid_token()
        with self._client() as client:
            response = client.get(
                f"{HH_API_BASE}/negotiations/{quote(str(negotiation_id), safe='')}/messages",
                params={"per_page": int(limit)},
                headers=self._auth_headers(token),
            )
            response.raise_for_status()
            payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else []
        result: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            author = item.get("author") if isinstance(item.get("author"), dict) else {}
            result.append({
                "id": item.get("id"),
                "text": item.get("text", ""),
                "created_at": item.get("created_at", ""),
                "author": author.get("name", ""),
            })
        return result

    @staticmethod
    def apply_spec(vacancy_id: str, resume_id: str, message: str = "") -> dict[str, Any]:
        """Builds the documented applicant apply request; sends nothing."""
        if not str(vacancy_id or "").strip():
            raise ValueError("vacancy_id is required")
        if not str(resume_id or "").strip():
            raise ValueError("resume_id is required")
        return {
            "method": "POST",
            "url": f"{HH_API_BASE}/negotiations",
            "body": {
                "vacancy_id": str(vacancy_id).strip(),
                "resume_id": str(resume_id).strip(),
                "message": str(message or ""),
            },
            "verification": "needs_live_validation",
            "documentation": CAPABILITIES["apply"]["source"],
            "note": (
                "Exact body schema and applicant eligibility must be confirmed "
                "with a live OAuth account; this builder sends nothing."
            ),
        }

    def apply(self, vacancy_id: str, resume_id: str, message: str = "",
              *, allow_live: bool = False) -> dict[str, Any]:
        """Guarded live apply. Disabled until a separate live-validation pass."""
        if not allow_live or os.getenv("APPLYPILOT_HH_OAUTH_LIVE") != "1":
            raise LiveActionDisabled(
                "HH OAuth apply is not live-verified; use the existing browser "
                "clicker or confirm the API schema in a separate pass"
            )
        spec = self.apply_spec(vacancy_id, resume_id, message)
        token = self._valid_token()
        with self._client() as client:
            response = client.post(
                spec["url"], json=spec["body"], headers=self._auth_headers(token),
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}
        return {
            "status": "submitted",
            "verification": "needs_live_validation",
            "negotiation_id": payload.get("id") if isinstance(payload, dict) else None,
        }


def hh_oauth_status(root: Path, account: str = "hh-primary") -> dict[str, Any]:
    """Local-only status: credentials, token file, expiry, capabilities."""
    credentials = credentials_status(root)
    token = read_private_json(token_path(root, account))
    base = {
        "provider": "hh_oauth",
        "account": account_slug(account),
        "credentials": credentials,
        "token_present": token is not None,
        "token_path": str(token_path(root, account)),
        "capabilities": CAPABILITIES,
        "apply_status": "needs_live_validation",
    }
    if token is None:
        return {**base, "status": "missing_token"}
    if _is_expired(str(token.get("expires_at") or "")):
        return {**base, "status": "expired", "expires_at": token.get("expires_at", "")}
    return {
        **base,
        "status": "token_present",
        "expires_at": token.get("expires_at", ""),
        "refresh_token_state": secret_state(token.get("refresh_token")),
        "scope": token.get("scope", ""),
    }
