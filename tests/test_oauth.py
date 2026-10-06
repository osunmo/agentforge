from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from apps.api.app.config import settings
from apps.api.app.db import SessionLocal
from apps.api.app.models import AuditLogModel, CredentialModel, OAuthSessionModel
from apps.api.app.services.credential_broker import CredentialBroker, CredentialBrokerError
from apps.api.app.services.oauth import (
    OAuthFlowError,
    complete_authorization,
    get_access_grant,
)


CLIENT_SECRET = "super-secret-client-value"
ACCESS_TOKEN = "access-token-must-never-leak"
REFRESH_TOKEN = "refresh-token-must-never-leak"


def _configuration() -> dict[str, object]:
    return {
        "authorization_endpoint": "https://accounts.example.com/oauth/authorize",
        "token_endpoint": "https://accounts.example.com/oauth/token",
        "client_id": "agentforge-test-client",
        "client_secret": CLIENT_SECRET,
        "client_auth_method": "client_secret_post",
        "redirect_uri": "http://127.0.0.1:8000/v1/oauth/gmail/callback",
        "scopes": ["mail.read", "mail.metadata"],
    }


def _configure(client) -> None:
    response = client.post("/v1/oauth/gmail/configure", json=_configuration())
    assert response.status_code == 200, response.text


def _start(client) -> tuple[str, dict[str, list[str]]]:
    response = client.post("/v1/oauth/gmail/start")
    assert response.status_code == 200, response.text
    url = response.json()["authorization_url"]
    return url, parse_qs(urlparse(url).query)


def test_configure_encrypts_client_secret_and_never_returns_it(client) -> None:
    response = client.post("/v1/oauth/gmail/configure", json=_configuration())

    assert response.status_code == 200
    assert response.json()["has_client_secret"] is True
    assert CLIENT_SECRET not in response.text
    with SessionLocal() as db:
        credential = db.scalar(
            select(CredentialModel).where(CredentialModel.kind == "oauth_client_secret")
        )
        assert credential is not None
        assert CLIENT_SECRET not in credential.encrypted_payload
        assert CredentialBroker(db, settings.master_key).decrypt(
            credential.encrypted_payload
        ) == {"client_secret": CLIENT_SECRET}


def test_start_uses_s256_and_does_not_store_plaintext_state_or_verifier(client) -> None:
    _configure(client)
    url, query = _start(client)

    assert url.startswith("https://accounts.example.com/oauth/authorize?")
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert query["include_granted_scopes"] == ["true"]
    state = query["state"][0]
    with SessionLocal() as db:
        session = db.scalar(select(OAuthSessionModel))
        assert session is not None
        assert session.state_digest == hashlib.sha256(state.encode("ascii")).hexdigest()
        assert state not in session.state_digest
        assert state not in session.encrypted_code_verifier
        verifier = CredentialBroker(db, settings.master_key).decrypt(
            session.encrypted_code_verifier
        )["code_verifier"]
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert query["code_challenge"] == [expected_challenge]
    assert 43 <= len(verifier) <= 128


def test_callback_encrypts_tokens_rejects_replay_and_disconnect_removes_token(client) -> None:
    _configure(client)
    _, query = _start(client)
    state = query["state"][0]
    captured: dict[str, list[str]] = {}

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        captured.update(parse_qs(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "mail.read mail.metadata",
            },
        )

    async def exchange() -> object:
        transport = httpx.MockTransport(token_endpoint)
        async with httpx.AsyncClient(transport=transport) as http_client:
            with SessionLocal() as db:
                return await complete_authorization(
                    db,
                    "gmail",
                    state,
                    "one-time-authorization-code",
                    http_client=http_client,
                )

    result = asyncio.run(exchange())
    serialized_result = result.model_dump_json()
    assert result.status == "connected"
    assert ACCESS_TOKEN not in serialized_result
    assert REFRESH_TOKEN not in serialized_result
    assert captured["client_secret"] == [CLIENT_SECRET]
    assert captured["code_verifier"]

    with SessionLocal() as db:
        token = db.scalar(
            select(CredentialModel).where(CredentialModel.kind == "oauth_tokens")
        )
        assert token is not None
        assert ACCESS_TOKEN not in token.encrypted_payload
        assert REFRESH_TOKEN not in token.encrypted_payload
        decrypted = CredentialBroker(db, settings.master_key).decrypt(token.encrypted_payload)
        assert decrypted["access_token"] == ACCESS_TOKEN
        audit_text = " ".join(str(item.details) for item in db.scalars(select(AuditLogModel)))
        assert ACCESS_TOKEN not in audit_text
        assert REFRESH_TOKEN not in audit_text

        with pytest.raises(OAuthFlowError, match="already been used"):
            asyncio.run(
                complete_authorization(
                    db, "gmail", state, "replayed-code", http_client=None
                )
            )

    response = client.post("/v1/connectors/gmail/disconnect")
    assert response.status_code == 200
    with SessionLocal() as db:
        assert db.scalar(
            select(CredentialModel).where(CredentialModel.kind == "oauth_tokens")
        ) is None


def test_callback_rejects_invalid_and_expired_state(client) -> None:
    _configure(client)
    response = client.get(
        "/v1/oauth/gmail/callback",
        params={"state": "invalid-state-value-that-is-long-enough", "code": "code"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_state"

    _, query = _start(client)
    state = query["state"][0]
    with SessionLocal() as db:
        session = db.scalar(
            select(OAuthSessionModel).where(OAuthSessionModel.status == "pending")
        )
        assert session is not None
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        with pytest.raises(OAuthFlowError) as caught:
            asyncio.run(complete_authorization(db, "gmail", state, "code"))
        assert caught.value.code == "expired_state"


def test_expired_access_token_is_refreshed_without_losing_refresh_token(client) -> None:
    _configure(client)
    _, query = _start(client)
    state = query["state"][0]

    async def authorize() -> None:
        def initial_token(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "initial-access-token",
                    "refresh_token": "durable-refresh-token",
                    "expires_in": 3600,
                    "scope": "mail.read mail.metadata",
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(initial_token)
        ) as http_client:
            with SessionLocal() as db:
                await complete_authorization(
                    db, "gmail", state, "authorization-code", http_client=http_client
                )

    asyncio.run(authorize())
    with SessionLocal() as db:
        credential = db.scalar(
            select(CredentialModel).where(CredentialModel.kind == "oauth_tokens")
        )
        assert credential is not None
        credential.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    captured: dict[str, list[str]] = {}

    def refresh_endpoint(request: httpx.Request) -> httpx.Response:
        captured.update(parse_qs(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "access_token": "refreshed-access-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    async def refresh() -> object:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(refresh_endpoint)
        ) as http_client:
            with SessionLocal() as db:
                return await get_access_grant(db, "gmail", http_client=http_client)

    grant = asyncio.run(refresh())

    assert grant.access_token == "refreshed-access-token"
    assert captured["grant_type"] == ["refresh_token"]
    assert captured["refresh_token"] == ["durable-refresh-token"]
    assert captured["client_secret"] == [CLIENT_SECRET]
    with SessionLocal() as db:
        credential = db.scalar(
            select(CredentialModel).where(CredentialModel.kind == "oauth_tokens")
        )
        stored = CredentialBroker(db, settings.master_key).decrypt(
            credential.encrypted_payload
        )
        assert stored["access_token"] == "refreshed-access-token"
        assert stored["refresh_token"] == "durable-refresh-token"
        audits = " ".join(
            str(item.details) for item in db.scalars(select(AuditLogModel)).all()
        )
        assert "refreshed-access-token" not in audits
        assert "durable-refresh-token" not in audits


def test_credential_broker_fails_closed_without_a_valid_key() -> None:
    with SessionLocal() as db:
        with pytest.raises(CredentialBrokerError):
            CredentialBroker(db, None)
        with pytest.raises(CredentialBrokerError):
            CredentialBroker(db, "not-a-fernet-key")
