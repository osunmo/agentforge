from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.discovery import (
    CapabilityResolution,
    CapabilityResolutionRequest,
    DiscoveredConnectorCandidate,
    OpenAPIRegistrationRequest,
)
from ..domain.tool_registry import tool_registry
from ..models import ConnectorConnectionModel
from ..services.connectors import connector_registry
from ..services.discovery import DiscoveryError, inspect_openapi, register_candidate


router = APIRouter(prefix="/v1/discovery", tags=["connector discovery"])


@router.post("/resolve", response_model=list[CapabilityResolution])
def resolve_capabilities(
    payload: CapabilityResolutionRequest, db: Session = Depends(get_db)
) -> list[CapabilityResolution]:
    connected = set(
        db.scalars(
            select(ConnectorConnectionModel.provider).where(
                ConnectorConnectionModel.user_id == "local-user",
                ConnectorConnectionModel.status == "connected",
            )
        ).all()
    )
    resolved: list[CapabilityResolution] = []
    for capability in payload.capabilities:
        tool = tool_registry.get(capability)
        if tool is None:
            resolved.append(
                CapabilityResolution(
                    capability=capability,
                    status="discovery_required",
                    reason="No connector is registered; search MCP or inspect an OpenAPI document.",
                )
            )
            continue
        connector = connector_registry.get(tool.connector)
        if connector is None:
            resolved.append(
                CapabilityResolution(
                    capability=capability,
                    status="unavailable",
                    provider=tool.connector,
                    source=tool.source,
                    reason="A tool is registered but its connector adapter is unavailable.",
                )
            )
            continue
        is_connected = tool.connector in connected
        execution_ready = bool(
            connector.manifest.risk_metadata.get(
                "execution_ready", connector.manifest.source == "builtin"
            )
        )
        status = (
            "available"
            if is_connected and execution_ready
            else "adapter_required"
            if is_connected
            else "authorization_required"
        )
        resolved.append(
            CapabilityResolution(
                capability=capability,
                status=status,
                provider=tool.connector,
                transport=connector.manifest.transport,
                source=connector.manifest.source,
                reason=(
                    "Connector is connected and ready."
                    if is_connected and execution_ready
                    else "Connector is authorized but still needs a sandbox-tested execution adapter."
                    if is_connected
                    else "Connector exists but must be authorized and configured before execution."
                ),
            )
        )
    return resolved


@router.post("/openapi/inspect", response_model=DiscoveredConnectorCandidate)
def inspect_openapi_connector(payload: OpenAPIRegistrationRequest) -> DiscoveredConnectorCandidate:
    try:
        return inspect_openapi(payload)
    except DiscoveryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/openapi/register", response_model=DiscoveredConnectorCandidate, status_code=201)
def register_openapi_connector(
    payload: OpenAPIRegistrationRequest, db: Session = Depends(get_db)
) -> DiscoveredConnectorCandidate:
    try:
        candidate = inspect_openapi(payload)
        return register_candidate(db, candidate, payload.document)
    except DiscoveryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
