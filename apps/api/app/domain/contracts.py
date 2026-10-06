from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    REQUIRE_REAUTH = "require_reauth"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PROHIBITED = "prohibited"


class AgentStatus(StrEnum):
    DRAFT = "draft"
    DEPLOYED = "deployed"
    PAUSED = "paused"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScheduleTrigger(BaseModel):
    type: Literal["schedule"] = "schedule"
    cron: str = Field(min_length=9, max_length=100)
    timezone: str = Field(default="America/Chicago", min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_cron_shape(self) -> "ScheduleTrigger":
        if len(self.cron.split()) != 5:
            raise ValueError("schedule cron must contain exactly five fields")
        return self


class ManualTrigger(BaseModel):
    type: Literal["manual"] = "manual"


class ModelPolicy(BaseModel):
    private_data: Literal["local_only", "prefer_local", "cloud_allowed"] = "local_only"
    general_reasoning: Literal["local", "cloud"] = "local"


class RuntimePolicy(BaseModel):
    harness: Literal["hermes", "builtin"] = "hermes"


class RunLimits(BaseModel):
    max_steps: int = Field(default=30, ge=1, le=100)
    max_runtime_seconds: int = Field(default=300, ge=1, le=3600)
    max_tool_calls: int = Field(default=20, ge=1, le=100)


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_version: Literal[1] = 1
    name: str = Field(min_length=2, max_length=120, pattern=r"^[a-z][a-z0-9_]*$")
    objective: str = Field(min_length=10, max_length=2000)
    triggers: list[ScheduleTrigger | ManualTrigger] = Field(min_length=1, max_length=10)
    capabilities: list[str] = Field(min_length=1, max_length=50)
    connectors: dict[str, str]
    permissions: dict[str, PermissionDecision]
    memory: list[str] = Field(default_factory=list, max_length=30)
    model_policy: ModelPolicy = Field(default_factory=ModelPolicy)
    runtime: RuntimePolicy = Field(default_factory=RuntimePolicy)
    limits: RunLimits = Field(default_factory=RunLimits)

    @model_validator(mode="after")
    def validate_internal_references(self) -> "AgentSpec":
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("capabilities must not contain duplicates")
        unknown_permissions = set(self.permissions) - set(self.capabilities)
        if unknown_permissions:
            raise ValueError(
                f"permissions reference unknown capabilities: {sorted(unknown_permissions)}"
            )
        return self


class ToolDefinition(BaseModel):
    name: str
    description: str
    category: str
    connector: str
    risk_level: RiskLevel
    reversible: bool
    approval_required: bool
    required_scopes: list[str] = Field(default_factory=list)
    argument_schema: dict[str, Any] = Field(default_factory=dict)
    source: str = "builtin"


class ConnectorManifest(BaseModel):
    name: str
    version: str = "1.0"
    category: str
    authentication_type: str
    capabilities: list[str]
    required_scopes: list[str]
    risk_metadata: dict[str, Any] = Field(default_factory=dict)
    transport: str = "native"
    source: str = "builtin"
    base_url: str | None = None


class CompileRequest(BaseModel):
    request: str = Field(min_length=10, max_length=5000)
    timezone: str = Field(default="America/Chicago", min_length=1, max_length=100)


class ConnectionRequirement(BaseModel):
    provider: str
    category: str
    capabilities: list[str]
    permissions: dict[str, PermissionDecision]


class AgentPreview(BaseModel):
    spec: AgentSpec
    required_connections: list[ConnectionRequirement]
    warnings: list[str] = Field(default_factory=list)


class AgentCreate(BaseModel):
    spec: AgentSpec
    description: str = Field(default="", max_length=2000)


class AgentUpdate(BaseModel):
    spec: AgentSpec | None = None
    description: str | None = Field(default=None, max_length=2000)
    status: AgentStatus | None = None


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    name: str
    description: str
    agent_spec: dict[str, Any]
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class RunStepResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    step_number: int
    type: str
    name: str
    status: str
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    started_at: datetime
    completed_at: datetime | None


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    status: str
    trigger_type: str
    current_step: int
    result: dict[str, Any] | None
    started_at: datetime
    completed_at: datetime | None
    error: str | None
    steps: list[RunStepResponse] = Field(default_factory=list)
