"""Optional Microsoft Entra ID sign-in for the browser UI of a remote deployment.

Off unless the operator sets all of ``OMNIVOICE_ENTRA_TENANT_ID``,
``OMNIVOICE_ENTRA_CLIENT_ID``, ``OMNIVOICE_ENTRA_CLIENT_SECRET`` and
``OMNIVOICE_PUBLIC_URL``. Nothing here runs, and no request is made to
Microsoft, on a desktop or an unconfigured server.

Authorization-code flow with PKCE against a single tenant. The ID token comes
straight from Microsoft's token endpoint over TLS in exchange for the code,
the client secret and the PKCE verifier, so OIDC Core 3.1.3.7 lets us rely on
the TLS server check instead of verifying the JWT signature; that keeps this
free of a JWT/JWKS dependency. Every claim that binds the token to this
tenant, this app and this login attempt is still checked.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

TENANT_ENV = "OMNIVOICE_ENTRA_TENANT_ID"
CLIENT_ID_ENV = "OMNIVOICE_ENTRA_CLIENT_ID"
CLIENT_SECRET_ENV = "OMNIVOICE_ENTRA_CLIENT_SECRET"
PUBLIC_URL_ENV = "OMNIVOICE_PUBLIC_URL"
ALLOWED_USERS_ENV = "OMNIVOICE_ENTRA_ALLOWED_USERS"

CALLBACK_PATH = "/api/auth/entra/callback"
FLOW_COOKIE = "ov_entra_flow"
FLOW_TTL_SECONDS = 10 * 60
MAX_FLOWS = 1024

_AUTHORITY = "https://login.microsoftonline.com"
_GUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_CLOCK_SKEW_SECONDS = 120
_TOKEN_TIMEOUT_SECONDS = 10.0


class EntraError(Exception):
    """A sign-in step failed. The message is for logs, never for the browser."""


@dataclass(frozen=True)
class EntraConfig:
    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)
    public_url: str
    allowed_users: frozenset[str]

    @property
    def redirect_uri(self) -> str:
        return self.public_url + CALLBACK_PATH

    @property
    def issuer(self) -> str:
        return f"{_AUTHORITY}/{self.tenant_id}/v2.0"


_warned: set[str] = set()


def _warn_once(reason: str) -> None:
    if reason not in _warned:
        _warned.add(reason)
        logger.warning("Entra sign-in is disabled: %s", reason)


def load_config() -> EntraConfig | None:
    """The active configuration, or None. Read per call so rotation just works.

    A partial or invalid configuration disables Entra rather than guessing, and
    never falls back to a wider tenant: only a concrete tenant GUID is accepted,
    not ``common``/``organizations``/``consumers``.
    """
    tenant = os.environ.get(TENANT_ENV, "").strip()
    client_id = os.environ.get(CLIENT_ID_ENV, "").strip()
    secret = os.environ.get(CLIENT_SECRET_ENV, "").strip()
    public_url = os.environ.get(PUBLIC_URL_ENV, "").strip().rstrip("/")
    if not (tenant or client_id or secret):
        return None
    if not (tenant and client_id and secret and public_url):
        _warn_once(
            f"set all of {TENANT_ENV}, {CLIENT_ID_ENV}, {CLIENT_SECRET_ENV} and {PUBLIC_URL_ENV}"
        )
        return None
    if not _GUID.fullmatch(tenant):
        _warn_once(f"{TENANT_ENV} must be the directory (tenant) ID GUID")
        return None
    if not _GUID.fullmatch(client_id):
        _warn_once(f"{CLIENT_ID_ENV} must be the application (client) ID GUID")
        return None
    if not public_url.startswith("https://") and not public_url.startswith("http://localhost"):
        _warn_once(f"{PUBLIC_URL_ENV} must be an https:// URL")
        return None
    allowed = frozenset(
        item.strip().lower()
        for item in os.environ.get(ALLOWED_USERS_ENV, "").split(",")
        if item.strip()
    )
    return EntraConfig(tenant.lower(), client_id.lower(), secret, public_url, allowed)


def entra_configured() -> bool:
    return load_config() is not None


def session_binding() -> str | None:
    """Stable, non-secret tag tying sessions to this tenant and app.

    Used as the session store's generation key when no API key is set, so
    pointing the server at a different tenant or app drops existing sessions.
    """
    config = load_config()
    return f"entra:{config.tenant_id}:{config.client_id}" if config else None


@dataclass(frozen=True)
class Flow:
    state: str
    nonce: str
    verifier: str = field(repr=False)
    expires: float

    @property
    def challenge(self) -> str:
        digest = hashlib.sha256(self.verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class FlowStore:
    """Bounded, process-local record of sign-ins awaiting their callback."""

    def __init__(self, *, clock=time.monotonic, ttl: int = FLOW_TTL_SECONDS, cap: int = MAX_FLOWS):
        self._clock = clock
        self._ttl = ttl
        self._cap = cap
        self._flows: OrderedDict[str, Flow] = OrderedDict()
        self._lock = threading.Lock()

    def start(self) -> Flow:
        now = self._clock()
        flow = Flow(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            verifier=secrets.token_urlsafe(64),
            expires=now + self._ttl,
        )
        with self._lock:
            while self._flows and next(iter(self._flows.values())).expires <= now:
                self._flows.popitem(last=False)
            while len(self._flows) >= self._cap:
                self._flows.popitem(last=False)
            self._flows[flow.state] = flow
        return flow

    def consume(self, state: str | None) -> Flow | None:
        """Single-use: a state can complete at most one callback."""
        if not state:
            return None
        with self._lock:
            flow = self._flows.pop(state, None)
        if flow is None or flow.expires <= self._clock():
            return None
        return flow

    def clear(self) -> None:
        with self._lock:
            self._flows.clear()


flow_store = FlowStore()


def authorize_url(config: EntraConfig, flow: Flow) -> str:
    query = urlencode(
        {
            "client_id": config.client_id,
            "response_type": "code",
            "redirect_uri": config.redirect_uri,
            "response_mode": "query",
            "scope": "openid profile email",
            "state": flow.state,
            "nonce": flow.nonce,
            "code_challenge": flow.challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{_AUTHORITY}/{config.tenant_id}/oauth2/v2.0/authorize?{query}"


async def redeem_code(config: EntraConfig, code: str, flow: Flow) -> str:
    """Exchange the authorization code for an ID token (the raw JWT)."""
    url = f"{_AUTHORITY}/{config.tenant_id}/oauth2/v2.0/token"
    form = {
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_uri,
        "code_verifier": flow.verifier,
        "scope": "openid profile email",
    }
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT_SECONDS) as client:
            response = await client.post(url, data=form)
    except httpx.HTTPError as exc:
        raise EntraError(f"token endpoint unreachable: {type(exc).__name__}") from None
    if response.status_code != 200:
        code_name = "unknown"
        try:
            code_name = str(response.json().get("error", "unknown"))[:64]
        except ValueError:
            pass
        raise EntraError(f"token endpoint refused the code: HTTP {response.status_code} {code_name}")
    try:
        id_token = response.json().get("id_token")
    except ValueError:
        id_token = None
    if not isinstance(id_token, str) or not id_token:
        raise EntraError("token response had no id_token")
    return id_token


def _claims(id_token: str) -> dict:
    parts = id_token.split(".")
    if len(parts) != 3:
        raise EntraError("id_token is not a JWT")
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, UnicodeDecodeError):
        raise EntraError("id_token payload is unreadable") from None
    if not isinstance(claims, dict):
        raise EntraError("id_token payload is not an object")
    return claims


def validate_id_token(
    config: EntraConfig,
    id_token: str,
    flow: Flow,
    *,
    now: float | None = None,
) -> dict:
    """Return the claims if the token is for this tenant, app and sign-in."""
    claims = _claims(id_token)
    current = time.time() if now is None else now
    if claims.get("iss") != config.issuer:
        raise EntraError("issuer mismatch")
    if str(claims.get("tid", "")).lower() != config.tenant_id:
        raise EntraError("tenant mismatch")
    audience = claims.get("aud")
    if audience != config.client_id and not (
        isinstance(audience, list) and config.client_id in audience
    ):
        raise EntraError("audience mismatch")
    if not secrets.compare_digest(str(claims.get("nonce", "")), flow.nonce):
        raise EntraError("nonce mismatch")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp + _CLOCK_SKEW_SECONDS < current:
        raise EntraError("token expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and nbf - _CLOCK_SKEW_SECONDS > current:
        raise EntraError("token not yet valid")
    if not claims.get("oid"):
        raise EntraError("token has no subject")
    return claims


def is_allowed(config: EntraConfig, claims: dict) -> bool:
    """Everyone in the tenant, unless an allow-list narrows it.

    Entries match the object ID, e-mail or user principal name, case-folded.
    """
    if not config.allowed_users:
        return True
    identities = {
        str(claims.get(name, "")).strip().lower()
        for name in ("oid", "email", "preferred_username", "upn")
    }
    identities.discard("")
    return bool(identities & config.allowed_users)
