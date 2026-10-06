from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .contracts import ConnectorManifest, ToolDefinition


class CapabilityResolutionRequest(BaseModel):
    capabilities: list[str] = Field(min_length=1, max_length=100)


class CapabilityResolution(BaseModel):
    capability: str
    status: Literal[
        "available",
        "authorization_required",
        "adapter_required",
        "restricted",
        "discovery_required",
        "unavailable",
    ]
    provider: str | None = None
    transport: str | None = None
    source: str | None = None
    reason: str


class OpenAPIRegistrationRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9_-]*$")
    category: str = Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    document: dict[str, Any]
    authentication_type: str = Field(default="oauth2", min_length=2, max_length=80)
    required_scopes: list[str] = Field(default_factory=list, max_length=100)


class DiscoveredConnectorCandidate(BaseModel):
    manifest: ConnectorManifest
    tools: list[ToolDefinition]
    warnings: list[str] = Field(default_factory=list)
    registration_status: Literal["candidate", "registered_needs_authorization"] = "candidate"
