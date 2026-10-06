from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.contracts import PermissionDecision
from ..models import AuditLogModel, ConnectorConnectionModel, CredentialModel, new_id
from ..services.connectors import (
    ConnectorAuthenticationError,
    ConnectorError,
    ConnectorPermissionError,
    ConnectorProviderError,
    ConnectorRateLimitError,
    connector_registry,
)
from ..services.tool_gateway import (
    ToolGatewayConnectionError,
    ToolGatewayError,
    tool_gateway,
)


router = APIRouter(prefix="/v1/connectors", tags=["connectors"])


class GmailDiagnosticRequest(BaseModel):
    query: str = Field(
        default=(
            "in:inbox newer_than:14d -category:promotions -category:social "
            "{is:important is:starred is:unread}"
        ),
        min_length=1,
        max_length=500,
    )
    max_results: int = Field(default=10, ge=1, le=10)


def _state_map(db: Session) -> dict[str, ConnectorConnectionModel]:
    return {
        item.provider: item
        for item in db.scalars(
            select(ConnectorConnectionModel).where(
                ConnectorConnectionModel.user_id == "local-user"
            )
        ).all()
    }


@router.get("")
async def list_connectors(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    states = _state_map(db)
    response = []
    for connector in connector_registry.list():
        state = states.get(connector.manifest.name)
        response.append(
            {
                "provider": connector.manifest.name,
                "status": state.status if state else "disconnected",
                "manifest": connector.manifest.model_dump(mode="json"),
                "health": await connector.health_check(),
            }
        )
    return response


@router.post("/{provider}/connect")
def connect(provider: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    connector = connector_registry.get(provider)
    if connector is None:
        raise HTTPException(status_code=404, detail="connector not found")
    if not connector.manifest.risk_metadata.get("mock", False):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "authorization_required",
                "provider": provider,
                "authentication_type": connector.manifest.authentication_type,
                "message": "A credential-broker authorization flow is required before connection.",
            },
        )
    connection = db.scalar(
        select(ConnectorConnectionModel).where(
            ConnectorConnectionModel.user_id == "local-user",
            ConnectorConnectionModel.provider == provider,
        )
    )
    if connection is None:
        connection = ConnectorConnectionModel(
            user_id="local-user",
            provider=provider,
            status="connected",
            metadata_json={"mode": "mock", "stores_secrets": False},
        )
        db.add(connection)
    else:
        connection.status = "connected"
    db.commit()
    return {"provider": provider, "status": "connected", "mode": "mock"}


@router.post("/{provider}/disconnect")
def disconnect(provider: str, db: Session = Depends(get_db)) -> dict[str, str]:
    if connector_registry.get(provider) is None:
        raise HTTPException(status_code=404, detail="connector not found")
    connection = db.scalar(
        select(ConnectorConnectionModel).where(
            ConnectorConnectionModel.user_id == "local-user",
            ConnectorConnectionModel.provider == provider,
        )
    )
    if connection is not None:
        connection.status = "disconnected"
        connection.metadata_json = {}
    token_credential = db.scalar(
        select(CredentialModel).where(
            CredentialModel.user_id == "local-user",
            CredentialModel.provider == provider,
            CredentialModel.kind == "oauth_tokens",
        )
    )
    if token_credential is not None:
        db.delete(token_credential)
    db.add(
        AuditLogModel(
            user_id="local-user",
            event_type="connector.disconnected",
            decision="allow",
            details={
                "provider": provider,
                "oauth_credential_removed": token_credential is not None,
            },
        )
    )
    db.commit()
    return {"provider": provider, "status": "disconnected"}


@router.get("/{provider}/health")
async def connector_health(provider: str) -> dict[str, Any]:
    connector = connector_registry.get(provider)
    if connector is None:
        raise HTTPException(status_code=404, detail="connector not found")
    return {"provider": provider, **(await connector.health_check())}


@router.post("/gmail/test")
async def test_live_gmail(
    payload: GmailDiagnosticRequest, db: Session = Depends(get_db)
) -> dict[str, Any]:
    connection = db.scalar(
        select(ConnectorConnectionModel).where(
            ConnectorConnectionModel.user_id == "local-user",
            ConnectorConnectionModel.provider == "gmail",
            ConnectorConnectionModel.status == "connected",
        )
    )
    if connection is None or connection.metadata_json.get("mode") != "oauth2":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "live_gmail_not_connected",
                "message": "Authorize Gmail with OAuth before running the live test.",
            },
        )
    diagnostic_id = new_id()
    try:
        result = await tool_gateway.execute(
            db,
            capability="email.search",
            arguments={"query": payload.query, "max_results": payload.max_results},
            requested_permission=PermissionDecision.ALLOW,
            context={
                "user_id": "local-user",
                "agent_id": "gmail-diagnostic",
                "run_id": diagnostic_id,
            },
        )
        db.commit()
    except ConnectorAuthenticationError as exc:
        db.commit()
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ConnectorPermissionError as exc:
        db.commit()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ConnectorRateLimitError as exc:
        db.commit()
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except (ConnectorProviderError, ConnectorError) as exc:
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ToolGatewayConnectionError as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ToolGatewayError as exc:
        db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "status": "ok",
        "provider": "gmail",
        "diagnostic_id": diagnostic_id,
        **result.output,
    }
