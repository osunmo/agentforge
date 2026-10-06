from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..domain.oauth import (
    OAuthConnectionStatus,
    OAuthProviderConfiguration,
    OAuthProviderResponse,
    OAuthStartResponse,
)
from ..models import (
    AuditLogModel,
    ConnectorConnectionModel,
    CredentialModel,
    OAuthProviderModel,
    OAuthSessionModel,
    utc_now,
)
from .connectors import connector_registry
from .credential_broker import CredentialBroker, CredentialBrokerError


USER_ID = "local-user"
PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,79}$")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
AUTHORIZATION_QUERY_KEYS = {
    "response_type",
    "client_id",
    "redirect_uri",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
}


class OAuthFlowError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class OAuthAccessGrant:
    access_token: str
    scopes: tuple[str, ...]
    expires_at: datetime | None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_url(value: str, label: str) -> str:
    parsed = urlparse(value)
    if not parsed.hostname or parsed.username or parsed.password:
        raise OAuthFlowError("invalid_configuration", f"{label} is not a valid URL")
    if parsed.fragment:
        raise OAuthFlowError("invalid_configuration", f"{label} must not contain a fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS
    ):
        raise OAuthFlowError(
            "invalid_configuration",
            f"{label} must use HTTPS (HTTP is allowed only for loopback development)",
        )
    return value


def _normalized_scopes(scopes: list[str]) -> list[str]:
    normalized: list[str] = []
    for scope in scopes:
        cleaned = scope.strip()
        if not cleaned or len(cleaned) > 200 or any(char.isspace() for char in cleaned):
            raise OAuthFlowError("invalid_configuration", "OAuth scopes must be non-empty tokens")
        if cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def _broker(db: Session) -> CredentialBroker:
    try:
        return CredentialBroker(db, settings.master_key)
    except CredentialBrokerError as exc:
        raise OAuthFlowError("credential_broker_unavailable", str(exc), 503) from exc


def configure_provider(
    db: Session, provider: str, payload: OAuthProviderConfiguration
) -> OAuthProviderResponse:
    if not PROVIDER_PATTERN.fullmatch(provider):
        raise OAuthFlowError("invalid_provider", "provider name is invalid")
    if connector_registry.get(provider) is None:
        raise OAuthFlowError("connector_not_found", "connector not found", 404)

    authorization_endpoint = _validate_url(
        payload.authorization_endpoint, "authorization endpoint"
    )
    token_endpoint = _validate_url(payload.token_endpoint, "token endpoint")
    redirect_uri = _validate_url(payload.redirect_uri, "redirect URI")
    authorization_query = dict(parse_qsl(urlparse(authorization_endpoint).query))
    if AUTHORIZATION_QUERY_KEYS.intersection(authorization_query):
        raise OAuthFlowError(
            "invalid_configuration",
            "authorization endpoint contains reserved OAuth query parameters",
        )
    scopes = _normalized_scopes(payload.scopes)
    broker = _broker(db)
    existing_secret = broker.get(
        user_id=USER_ID, provider=provider, kind="oauth_client_secret"
    )
    supplied_secret = (
        payload.client_secret.get_secret_value() if payload.client_secret is not None else None
    )
    if payload.client_auth_method != "none" and not supplied_secret and existing_secret is None:
        raise OAuthFlowError(
            "invalid_configuration",
            "a client secret is required for the selected client authentication method",
        )

    secret_credential_id: str | None = None
    if payload.client_auth_method == "none":
        broker.delete(user_id=USER_ID, provider=provider, kind="oauth_client_secret")
    elif supplied_secret:
        secret_credential = broker.put(
            user_id=USER_ID,
            provider=provider,
            kind="oauth_client_secret",
            payload={"client_secret": supplied_secret},
        )
        secret_credential_id = secret_credential.id
    elif existing_secret is not None:
        secret_credential_id = existing_secret.id

    configuration = db.scalar(
        select(OAuthProviderModel).where(OAuthProviderModel.provider == provider)
    )
    if configuration is None:
        configuration = OAuthProviderModel(provider=provider)
        db.add(configuration)
    configuration.authorization_endpoint = authorization_endpoint
    configuration.token_endpoint = token_endpoint
    configuration.client_id = payload.client_id
    configuration.redirect_uri = redirect_uri
    configuration.scopes = scopes
    configuration.client_auth_method = payload.client_auth_method
    configuration.client_secret_credential_id = secret_credential_id
    configuration.enabled = True
    db.add(
        AuditLogModel(
            user_id=USER_ID,
            event_type="oauth.provider_configured",
            decision="allow",
            details={
                "provider": provider,
                "client_auth_method": payload.client_auth_method,
                "scopes": scopes,
                "has_client_secret": secret_credential_id is not None,
            },
        )
    )
    db.commit()
    return OAuthProviderResponse(
        provider=provider,
        configured=True,
        client_auth_method=payload.client_auth_method,
        redirect_uri=redirect_uri,
        scopes=scopes,
        has_client_secret=secret_credential_id is not None,
    )


def start_authorization(db: Session, provider: str) -> OAuthStartResponse:
    configuration = db.scalar(
        select(OAuthProviderModel).where(
            OAuthProviderModel.provider == provider,
            OAuthProviderModel.enabled.is_(True),
        )
    )
    if configuration is None:
        raise OAuthFlowError("provider_not_configured", "OAuth provider is not configured", 404)

    state = secrets.token_urlsafe(32)
    state_digest = hashlib.sha256(state.encode("ascii")).hexdigest()
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    expires_at = utc_now() + timedelta(seconds=settings.oauth_session_ttl_seconds)
    session = OAuthSessionModel(
        user_id=USER_ID,
        provider=provider,
        state_digest=state_digest,
        encrypted_code_verifier=_broker(db).encrypt({"code_verifier": code_verifier}),
        status="pending",
        expires_at=expires_at,
    )
    db.add(session)
    db.flush()
    db.add(
        AuditLogModel(
            user_id=USER_ID,
            event_type="oauth.authorization_started",
            decision="allow",
            details={"provider": provider, "session_id": session.id},
        )
    )
    db.commit()

    parsed = urlparse(configuration.authorization_endpoint)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(
        [
            ("response_type", "code"),
            ("client_id", configuration.client_id),
            ("redirect_uri", configuration.redirect_uri),
            ("scope", " ".join(configuration.scopes)),
            ("state", state),
            ("code_challenge", code_challenge),
            ("code_challenge_method", "S256"),
        ]
    )
    if provider == "gmail":
        query.extend(
            [
                ("access_type", "offline"),
                ("include_granted_scopes", "true"),
                ("prompt", "consent"),
            ]
        )
    authorization_url = urlunparse(parsed._replace(query=urlencode(query)))
    return OAuthStartResponse(
        provider=provider,
        authorization_url=authorization_url,
        expires_at=expires_at,
    )


async def _exchange_token(
    configuration: OAuthProviderModel,
    code: str,
    code_verifier: str,
    client_secret: str | None,
    http_client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": configuration.redirect_uri,
        "client_id": configuration.client_id,
        "code_verifier": code_verifier,
    }
    auth: tuple[str, str] | None = None
    if configuration.client_auth_method == "client_secret_post":
        data["client_secret"] = client_secret or ""
    elif configuration.client_auth_method == "client_secret_basic":
        auth = (configuration.client_id, client_secret or "")

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(15.0), follow_redirects=False
    )
    try:
        response = await client.post(
            configuration.token_endpoint,
            data=data,
            auth=auth,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        token_payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OAuthFlowError("token_exchange_failed", "OAuth token exchange failed", 502) from exc
    finally:
        if owns_client:
            await client.aclose()
    if not isinstance(token_payload, dict) or not isinstance(
        token_payload.get("access_token"), str
    ):
        raise OAuthFlowError(
            "invalid_token_response", "OAuth provider returned an invalid token response", 502
        )
    return token_payload


async def get_access_grant(
    db: Session,
    provider: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> OAuthAccessGrant:
    """Return a usable access grant, refreshing it without exposing tokens to callers above the gateway."""

    broker = _broker(db)
    credential = broker.get(user_id=USER_ID, provider=provider, kind="oauth_tokens")
    if credential is None:
        raise OAuthFlowError("reauthorization_required", "OAuth authorization is required", 401)
    token_payload = broker.decrypt(credential.encrypted_payload)
    access_token = token_payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthFlowError("reauthorization_required", "OAuth access token is unavailable", 401)
    if credential.expires_at is None or _aware(credential.expires_at) > utc_now() + timedelta(
        seconds=60
    ):
        return OAuthAccessGrant(
            access_token=access_token,
            scopes=tuple(credential.scopes),
            expires_at=credential.expires_at,
        )

    refresh_token = token_payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthFlowError(
            "reauthorization_required", "OAuth refresh token is unavailable", 401
        )
    configuration = db.scalar(
        select(OAuthProviderModel).where(
            OAuthProviderModel.provider == provider,
            OAuthProviderModel.enabled.is_(True),
        )
    )
    if configuration is None:
        raise OAuthFlowError("provider_not_configured", "OAuth provider is not configured", 404)

    client_secret: str | None = None
    if configuration.client_auth_method != "none":
        client_credential = broker.get(
            user_id=USER_ID, provider=provider, kind="oauth_client_secret"
        )
        if client_credential is None:
            raise OAuthFlowError(
                "missing_client_secret", "OAuth client secret is unavailable", 503
            )
        secret_payload = broker.decrypt(client_credential.encrypted_payload)
        client_secret = secret_payload.get("client_secret")
        if not isinstance(client_secret, str):
            raise OAuthFlowError(
                "invalid_client_secret", "OAuth client secret is invalid", 503
            )

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": configuration.client_id,
    }
    auth: tuple[str, str] | None = None
    if configuration.client_auth_method == "client_secret_post":
        data["client_secret"] = client_secret or ""
    elif configuration.client_auth_method == "client_secret_basic":
        auth = (configuration.client_id, client_secret or "")

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(15.0), follow_redirects=False
    )
    try:
        response = await client.post(
            configuration.token_endpoint,
            data=data,
            auth=auth,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        refreshed = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OAuthFlowError("token_refresh_failed", "OAuth token refresh failed", 502) from exc
    finally:
        if owns_client:
            await client.aclose()
    if not isinstance(refreshed, dict) or not isinstance(refreshed.get("access_token"), str):
        raise OAuthFlowError(
            "invalid_token_response", "OAuth provider returned an invalid token response", 502
        )

    merged_payload = dict(token_payload)
    merged_payload.update(refreshed)
    merged_payload["refresh_token"] = refreshed.get("refresh_token", refresh_token)
    expires_at: datetime | None = None
    expires_in = refreshed.get("expires_in")
    if isinstance(expires_in, (int, float)) and 0 < expires_in <= 31_536_000:
        expires_at = utc_now() + timedelta(seconds=float(expires_in))
    refreshed_scope = refreshed.get("scope")
    scopes = (
        _normalized_scopes(refreshed_scope.split())
        if isinstance(refreshed_scope, str) and refreshed_scope.strip()
        else list(credential.scopes)
    )
    updated = broker.put(
        user_id=USER_ID,
        provider=provider,
        kind="oauth_tokens",
        payload=merged_payload,
        scopes=scopes,
        metadata={
            "token_type": str(refreshed.get("token_type", "Bearer"))[:40],
            "has_refresh_token": True,
        },
        expires_at=expires_at,
    )
    db.add(
        AuditLogModel(
            user_id=USER_ID,
            event_type="oauth.token_refreshed",
            decision="allow",
            details={"provider": provider, "scopes": scopes},
        )
    )
    db.commit()
    return OAuthAccessGrant(
        access_token=refreshed["access_token"],
        scopes=tuple(updated.scopes),
        expires_at=updated.expires_at,
    )


def _mark_failed(db: Session, session: OAuthSessionModel, code: str) -> None:
    session.status = "failed"
    session.encrypted_code_verifier = ""
    session.completed_at = utc_now()
    db.add(
        AuditLogModel(
            user_id=USER_ID,
            event_type="oauth.authorization_failed",
            decision="deny",
            details={"provider": session.provider, "session_id": session.id, "code": code},
        )
    )
    db.commit()


async def complete_authorization(
    db: Session,
    provider: str,
    state: str,
    code: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> OAuthConnectionStatus:
    state_digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
    session = db.scalar(
        select(OAuthSessionModel).where(
            OAuthSessionModel.user_id == USER_ID,
            OAuthSessionModel.provider == provider,
            OAuthSessionModel.state_digest == state_digest,
        )
    )
    if session is None:
        raise OAuthFlowError("invalid_state", "OAuth state is invalid or expired")
    if session.status != "pending":
        raise OAuthFlowError("state_already_used", "OAuth state has already been used")
    if _aware(session.expires_at) <= utc_now():
        _mark_failed(db, session, "expired_state")
        raise OAuthFlowError("expired_state", "OAuth state has expired")

    configuration = db.scalar(
        select(OAuthProviderModel).where(
            OAuthProviderModel.provider == provider,
            OAuthProviderModel.enabled.is_(True),
        )
    )
    if configuration is None:
        _mark_failed(db, session, "provider_not_configured")
        raise OAuthFlowError("provider_not_configured", "OAuth provider is not configured", 404)

    session.status = "exchanging"
    db.commit()
    try:
        broker = _broker(db)
        verifier_payload = broker.decrypt(session.encrypted_code_verifier)
        code_verifier = verifier_payload.get("code_verifier")
        if not isinstance(code_verifier, str):
            raise OAuthFlowError("invalid_session", "OAuth session is invalid", 500)

        client_secret: str | None = None
        if configuration.client_auth_method != "none":
            secret_credential = broker.get(
                user_id=USER_ID, provider=provider, kind="oauth_client_secret"
            )
            if secret_credential is None:
                raise OAuthFlowError(
                    "missing_client_secret", "OAuth client secret is unavailable", 503
                )
            secret_payload = broker.decrypt(secret_credential.encrypted_payload)
            client_secret = secret_payload.get("client_secret")
            if not isinstance(client_secret, str):
                raise OAuthFlowError(
                    "invalid_client_secret", "OAuth client secret is invalid", 503
                )

        token_payload = await _exchange_token(
            configuration, code, code_verifier, client_secret, http_client
        )
        expires_at: datetime | None = None
        expires_in = token_payload.get("expires_in")
        if isinstance(expires_in, (int, float)) and 0 < expires_in <= 31_536_000:
            expires_at = utc_now() + timedelta(seconds=float(expires_in))
        granted_scope = token_payload.get("scope")
        scopes = (
            _normalized_scopes(granted_scope.split())
            if isinstance(granted_scope, str) and granted_scope.strip()
            else list(configuration.scopes)
        )
        credential = broker.put(
            user_id=USER_ID,
            provider=provider,
            kind="oauth_tokens",
            payload=token_payload,
            scopes=scopes,
            metadata={
                "token_type": str(token_payload.get("token_type", "Bearer"))[:40],
                "has_refresh_token": isinstance(token_payload.get("refresh_token"), str),
            },
            expires_at=expires_at,
        )
        connection = db.scalar(
            select(ConnectorConnectionModel).where(
                ConnectorConnectionModel.user_id == USER_ID,
                ConnectorConnectionModel.provider == provider,
            )
        )
        if connection is None:
            connection = ConnectorConnectionModel(user_id=USER_ID, provider=provider)
            db.add(connection)
        connection.status = "connected"
        connection.metadata_json = {
            "mode": "oauth2",
            "credential_id": credential.id,
            "scopes": scopes,
        }
        session.status = "completed"
        session.encrypted_code_verifier = ""
        session.completed_at = utc_now()
        db.add(
            AuditLogModel(
                user_id=USER_ID,
                event_type="oauth.authorization_completed",
                decision="allow",
                details={
                    "provider": provider,
                    "session_id": session.id,
                    "scopes": scopes,
                    "has_refresh_token": isinstance(token_payload.get("refresh_token"), str),
                },
            )
        )
        db.commit()
        return OAuthConnectionStatus(
            provider=provider,
            configured=True,
            status="connected",
            client_id=configuration.client_id,
            redirect_uri=configuration.redirect_uri,
            scopes=scopes,
            expires_at=expires_at,
        )
    except CredentialBrokerError as exc:
        _mark_failed(db, session, "credential_error")
        raise OAuthFlowError("credential_error", "Credential processing failed", 500) from exc
    except OAuthFlowError as exc:
        _mark_failed(db, session, exc.code)
        raise


def connection_status(db: Session, provider: str) -> OAuthConnectionStatus:
    configuration = db.scalar(
        select(OAuthProviderModel).where(OAuthProviderModel.provider == provider)
    )
    connection = db.scalar(
        select(ConnectorConnectionModel).where(
            ConnectorConnectionModel.user_id == USER_ID,
            ConnectorConnectionModel.provider == provider,
        )
    )
    credential = db.scalar(
        select(CredentialModel).where(
            CredentialModel.user_id == USER_ID,
            CredentialModel.provider == provider,
            CredentialModel.kind == "oauth_tokens",
        )
    )
    status = connection.status if connection is not None else "disconnected"
    if status == "connected" and credential is None:
        status = "reauthorization_required"
    elif (
        status == "connected"
        and credential is not None
        and credential.expires_at is not None
        and _aware(credential.expires_at) <= utc_now()
    ):
        status = "reauthorization_required"
    return OAuthConnectionStatus(
        provider=provider,
        configured=configuration is not None and configuration.enabled,
        status=status,
        client_id=configuration.client_id if configuration is not None else None,
        redirect_uri=configuration.redirect_uri if configuration is not None else None,
        scopes=list(credential.scopes) if credential is not None else [],
        expires_at=credential.expires_at if credential is not None else None,
    )
