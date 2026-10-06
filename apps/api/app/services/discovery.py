from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.contracts import ConnectorManifest, RiskLevel, ToolDefinition
from ..domain.discovery import DiscoveredConnectorCandidate, OpenAPIRegistrationRequest
from ..domain.tool_registry import ToolRegistry, tool_registry
from ..models import ConnectorDefinitionModel
from .connectors import ConnectorRegistry, DiscoveredConnector, connector_registry


class DiscoveryError(ValueError):
    pass


HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _normalize_operation_id(category: str, operation_id: str) -> str:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", operation_id).lower()
    snake = re.sub(r"[^a-z0-9]+", "_", snake).strip("_")
    if not snake:
        raise DiscoveryError("OpenAPI operationId cannot be normalized into a capability")
    return f"{category}.{snake}"


def _base_url(document: dict[str, Any]) -> str | None:
    servers = document.get("servers") or []
    if not servers:
        return None
    url = str(servers[0].get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DiscoveryError("OpenAPI server URL must use http or https")
    return url.rstrip("/")


def inspect_openapi(payload: OpenAPIRegistrationRequest) -> DiscoveredConnectorCandidate:
    document = payload.document
    version = str(document.get("openapi", ""))
    if not version.startswith("3."):
        raise DiscoveryError("only OpenAPI 3.x documents are supported")
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise DiscoveryError("OpenAPI document has no paths")

    tools: list[ToolDefinition] = []
    warnings: list[str] = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            method = method.lower()
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if not operation_id:
                warnings.append(f"Skipped {method.upper()} {path}: missing operationId")
                continue
            capability = operation.get("x-agentforge-capability") or _normalize_operation_id(
                payload.category, str(operation_id)
            )
            is_read = method == "get"
            is_delete = method == "delete"
            tools.append(
                ToolDefinition(
                    name=str(capability),
                    description=str(
                        operation.get("summary")
                        or operation.get("description")
                        or f"{method.upper()} {path}"
                    )[:500],
                    category=payload.category,
                    connector=payload.name,
                    risk_level=(
                        RiskLevel.LOW
                        if is_read
                        else RiskLevel.HIGH
                        if is_delete
                        else RiskLevel.MEDIUM
                    ),
                    reversible=is_read,
                    approval_required=not is_read,
                    required_scopes=list(payload.required_scopes),
                    argument_schema={
                        "parameters": operation.get("parameters", []),
                        "requestBody": operation.get("requestBody"),
                    },
                    source="openapi",
                )
            )

    if not tools:
        raise DiscoveryError("OpenAPI document contains no registerable operations")
    if any(tool.approval_required for tool in tools):
        warnings.append("Write operations were marked as requiring human approval.")

    manifest = ConnectorManifest(
        name=payload.name,
        category=payload.category,
        authentication_type=payload.authentication_type,
        capabilities=[tool.name for tool in tools],
        required_scopes=payload.required_scopes,
        risk_metadata={
            "generated": True,
            "write_operations_require_approval": True,
            "execution_ready": False,
        },
        transport="openapi",
        source="discovered",
        base_url=_base_url(document),
    )
    if manifest.base_url is None:
        warnings.append("No server URL was declared; execution configuration is required.")
    return DiscoveredConnectorCandidate(manifest=manifest, tools=tools, warnings=warnings)


def register_candidate(
    db: Session,
    candidate: DiscoveredConnectorCandidate,
    document: dict[str, Any],
    connectors: ConnectorRegistry = connector_registry,
    tools: ToolRegistry = tool_registry,
) -> DiscoveredConnectorCandidate:
    name = candidate.manifest.name
    if connectors.get(name) is not None or db.scalar(
        select(ConnectorDefinitionModel).where(ConnectorDefinitionModel.name == name)
    ):
        raise DiscoveryError(f"connector already exists: {name}")
    conflicts = [tool.name for tool in candidate.tools if tools.get(tool.name) is not None]
    if conflicts:
        raise DiscoveryError(f"capabilities already registered: {sorted(conflicts)}")

    connectors.register(DiscoveredConnector(candidate.manifest))
    for tool in candidate.tools:
        tools.register(tool)
    db.add(
        ConnectorDefinitionModel(
            name=name,
            source="openapi",
            manifest=candidate.manifest.model_dump(mode="json"),
            tool_definitions=[tool.model_dump(mode="json") for tool in candidate.tools],
            configuration={"openapi": document},
        )
    )
    db.commit()
    return candidate.model_copy(update={"registration_status": "registered_needs_authorization"})


def load_discovered_connectors(
    db: Session,
    connectors: ConnectorRegistry = connector_registry,
    tools: ToolRegistry = tool_registry,
) -> None:
    definitions = db.scalars(
        select(ConnectorDefinitionModel).where(ConnectorDefinitionModel.enabled.is_(True))
    ).all()
    for definition in definitions:
        manifest = ConnectorManifest.model_validate(definition.manifest)
        if connectors.get(manifest.name) is not None:
            continue
        parsed_tools = [
            ToolDefinition.model_validate(item) for item in definition.tool_definitions
        ]
        if any(tools.get(tool.name) is not None for tool in parsed_tools):
            continue
        connectors.register(DiscoveredConnector(manifest))
        for tool in parsed_tools:
            tools.register(tool)
