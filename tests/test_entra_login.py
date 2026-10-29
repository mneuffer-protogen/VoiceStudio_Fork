"""Microsoft Entra sign-in: config gating, flow binding, token checks, session."""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OID = "99999999-8888-7777-6666-555555555555"
PUBLIC = "https://voice.example.com"
CSRF_HEADERS = {"Origin": "http://voice.test", "X-VoiceStudio-CSRF": "1"}


@pytest.fixture(autouse=True)
def entra_environment(monkeypatch):
    from api.routers.auth import _exchange_attempt_limiter
    from core import entra
    from services.admin_sessions import admin_session_store

    admin_session_store.clear()
    _exchange_attempt_limiter.reset()
    entra.flow_store.clear()
    monkeypatch.setenv("OMNIVOICE_ENTRA_TENANT_ID", TENANT)
    monkeypatch.setenv("OMNIVOICE_ENTRA_CLIENT_ID", CLIENT)
    monkeypatch.setenv("OMNIVOICE_ENTRA_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("OMNIVOICE_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("OMNIVOICE_SERVER_MODE", "1")
    monkeypatch.delenv("OMNIVOICE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIVOICE_ENTRA_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("OMNIVOICE_TRUSTED_NETWORKS", raising=False)
    yield
    admin_session_store.clear()
    entra.flow_store.clear()


def _client():
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(
        app, base_url="http://voice.test", client=("10.0.0.5", 1), follow_redirects=False
    )


def _jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}."


def _claims(nonce: str, **over) -> dict:
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "tid": TENANT,
        "aud": CLIENT,
        "nonce": nonce,
        "oid": OID,
        "preferred_username": "alice@example.com",
        "exp": time.time() + 3600,
    }
    claims.update(over)
    return claims


def _start(client):
    response = client.get("/api/auth/entra/login")
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    return response, query


def _complete(client, monkeypatch, *, mutate=None, state=None, code="abc"):
    from core import entra

    response, query = _start(client)

    async def fake_redeem(config, got_code, flow):
        assert got_code == code
        assert flow.verifier
        claims = _claims(flow.nonce)
        if mutate:
            claims = mutate(claims)
        return _jwt(claims)

    monkeypatch.setattr(entra, "redeem_code", fake_redeem)
    return client.get(
        "/api/auth/entra/callback",
        params={"code": code, "state": state or query["state"][0]},
    )


def _authenticated(client) -> bool:
    response = client.post("/api/auth/ws-ticket", json={"path": "/ws/events"}, headers=CSRF_HEADERS)
    return response.status_code == 201


def test_config_reports_entra_only_when_fully_configured(monkeypatch):
    assert _client().get("/api/auth/config").json() == {"entra": True}
    monkeypatch.delenv("OMNIVOICE_ENTRA_CLIENT_SECRET")
    assert _client().get("/api/auth/config").json() == {"entra": False}


@pytest.mark.parametrize("tenant", ["common", "organizations", "contoso.onmicrosoft.com"])
def test_only_a_concrete_tenant_guid_is_accepted(monkeypatch, tenant):
    monkeypatch.setenv("OMNIVOICE_ENTRA_TENANT_ID", tenant)
    assert _client().get("/api/auth/config").json() == {"entra": False}


def test_login_is_404_when_unconfigured(monkeypatch):
    monkeypatch.delenv("OMNIVOICE_ENTRA_TENANT_ID")
    monkeypatch.delenv("OMNIVOICE_ENTRA_CLIENT_ID")
    monkeypatch.delenv("OMNIVOICE_ENTRA_CLIENT_SECRET")
    assert _client().get("/api/auth/entra/login").status_code == 404


def test_entra_alone_makes_remote_clients_authenticate():
    assert _client().get("/v1/audio/voices").status_code == 401


def test_login_redirects_to_the_tenant_with_pkce_and_binds_the_browser():
    client = _client()
    response, query = _start(client)

    location = urlparse(response.headers["location"])
    assert location.netloc == "login.microsoftonline.com"
    assert location.path == f"/{TENANT}/oauth2/v2.0/authorize"
    assert query["client_id"] == [CLIENT]
    assert query["redirect_uri"] == [f"{PUBLIC}/api/auth/entra/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0] and query["nonce"][0] and query["state"][0]
    cookie = response.headers["set-cookie"]
    assert f"ov_entra_flow={query['state'][0]}" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Path=/api/auth/entra" in cookie


def test_login_refuses_cross_site_navigation():
    response = _client().get("/api/auth/entra/login", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


def test_successful_sign_in_issues_the_admin_session_cookie(monkeypatch):
    client = _client()
    assert not _authenticated(client)

    response = _complete(client, monkeypatch)

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert response.headers["cache-control"] == "no-store"
    assert client.cookies.get("ov_session", domain="voice.test")
    assert _authenticated(client)


def test_callback_without_the_flow_cookie_is_refused(monkeypatch):
    starter = _client()
    _, query = _start(starter)
    stranger = _client()

    response = stranger.get(
        "/api/auth/entra/callback", params={"code": "abc", "state": query["state"][0]}
    )

    assert response.headers["location"] == "/?auth_error=failed"
    assert not _authenticated(stranger)


def test_state_is_single_use(monkeypatch):
    client = _client()
    _, query = _start(client)
    state = query["state"][0]
    from core import entra

    async def redeem(config, code, flow):
        return _jwt(_claims(flow.nonce))

    monkeypatch.setattr(entra, "redeem_code", redeem)
    first = client.get("/api/auth/entra/callback", params={"code": "c", "state": state})
    assert first.headers["location"] == "/"
    client.cookies.set("ov_entra_flow", state, domain="voice.test", path="/api/auth/entra")
    replay = client.get("/api/auth/entra/callback", params={"code": "c", "state": state})
    assert replay.headers["location"] == "/?auth_error=failed"


def test_provider_error_is_a_generic_failure():
    client = _client()
    _, query = _start(client)
    response = client.get(
        "/api/auth/entra/callback",
        params={"error": "access_denied", "state": query["state"][0]},
    )
    assert response.headers["location"] == "/?auth_error=failed"
    assert not _authenticated(client)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: {**c, "tid": "00000000-0000-0000-0000-000000000000"},
        lambda c: {**c, "iss": "https://login.microsoftonline.com/other/v2.0"},
        lambda c: {**c, "aud": "someone-else"},
        lambda c: {**c, "nonce": "wrong"},
        lambda c: {**c, "exp": time.time() - 3600},
        lambda c: {k: v for k, v in c.items() if k != "oid"},
    ],
    ids=["tenant", "issuer", "audience", "nonce", "expired", "no-subject"],
)
def test_bad_id_tokens_do_not_sign_in(monkeypatch, mutate):
    client = _client()
    response = _complete(client, monkeypatch, mutate=mutate)
    assert response.headers["location"] == "/?auth_error=failed"
    assert not _authenticated(client)


def test_token_endpoint_failure_does_not_sign_in(monkeypatch):
    from core import entra

    client = _client()
    _, query = _start(client)

    async def boom(config, code, flow):
        raise entra.EntraError("token endpoint unreachable")

    monkeypatch.setattr(entra, "redeem_code", boom)
    response = client.get(
        "/api/auth/entra/callback", params={"code": "c", "state": query["state"][0]}
    )
    assert response.headers["location"] == "/?auth_error=failed"


def test_allow_list_narrows_the_tenant(monkeypatch):
    monkeypatch.setenv("OMNIVOICE_ENTRA_ALLOWED_USERS", "Bob@example.com, " + OID.upper())

    allowed = _client()
    assert _complete(allowed, monkeypatch).headers["location"] == "/"
    assert _authenticated(allowed)

    monkeypatch.setenv("OMNIVOICE_ENTRA_ALLOWED_USERS", "bob@example.com")
    denied = _client()
    response = _complete(denied, monkeypatch)
    assert response.headers["location"] == "/?auth_error=denied"
    assert not _authenticated(denied)


def test_api_key_still_works_as_a_bearer_alongside_entra(monkeypatch):
    monkeypatch.setenv("OMNIVOICE_API_KEY", "KEY_for_scripts_1234")
    client = _client()
    assert client.get("/v1/audio/voices").status_code == 401
    ok = client.post(
        "/api/auth/session",
        json={"transport": "bearer"},
        headers={"Authorization": "Bearer KEY_for_scripts_1234"},
    )
    assert ok.status_code == 201


def test_entra_session_is_dropped_when_the_app_registration_changes(monkeypatch):
    client = _client()
    _complete(client, monkeypatch)
    assert _authenticated(client)

    monkeypatch.setenv("OMNIVOICE_ENTRA_CLIENT_ID", "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert not _authenticated(client)
