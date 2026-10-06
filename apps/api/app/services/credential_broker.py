from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CredentialModel


class CredentialBrokerError(RuntimeError):
    pass


class CredentialBroker:
    """Encrypts credential payloads before they reach persistent storage."""

    def __init__(self, db: Session, master_key: str | None) -> None:
        self.db = db
        if not master_key:
            raise CredentialBrokerError(
                "credential storage is unavailable; AGENTFORGE_MASTER_KEY is not configured"
            )
        try:
            self._cipher = Fernet(master_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise CredentialBrokerError("AGENTFORGE_MASTER_KEY is invalid") from exc

    def encrypt(self, payload: dict[str, Any]) -> str:
        serialized = json.dumps(
            payload, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return self._cipher.encrypt(serialized).decode("ascii")

    def decrypt(self, encrypted_payload: str) -> dict[str, Any]:
        try:
            raw = self._cipher.decrypt(encrypted_payload.encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialBrokerError("stored credential cannot be decrypted") from exc
        if not isinstance(payload, dict):
            raise CredentialBrokerError("stored credential payload is invalid")
        return payload

    def put(
        self,
        *,
        user_id: str,
        provider: str,
        kind: str,
        payload: dict[str, Any],
        scopes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> CredentialModel:
        credential = self.db.scalar(
            select(CredentialModel).where(
                CredentialModel.user_id == user_id,
                CredentialModel.provider == provider,
                CredentialModel.kind == kind,
            )
        )
        if credential is None:
            credential = CredentialModel(
                user_id=user_id,
                provider=provider,
                kind=kind,
                encrypted_payload="",
            )
            self.db.add(credential)
        credential.encrypted_payload = self.encrypt(payload)
        credential.scopes = sorted(set(scopes or []))
        credential.metadata_json = metadata or {}
        credential.expires_at = expires_at
        self.db.flush()
        return credential

    def get(self, *, user_id: str, provider: str, kind: str) -> CredentialModel | None:
        return self.db.scalar(
            select(CredentialModel).where(
                CredentialModel.user_id == user_id,
                CredentialModel.provider == provider,
                CredentialModel.kind == kind,
            )
        )

    def delete(self, *, user_id: str, provider: str, kind: str) -> bool:
        credential = self.get(user_id=user_id, provider=provider, kind=kind)
        if credential is None:
            return False
        self.db.delete(credential)
        return True
