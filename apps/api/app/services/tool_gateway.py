from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.contracts import PermissionDecision, PolicyDecision, ToolDefinition
from ..domain.tool_registry import ToolRegistry, tool_registry
from ..models import AuditLogModel, ConnectorConnectionModel
from .connectors import ConnectorError, ConnectorRegistry, connector_registry
from .oauth import OAuthFlowError, get_access_grant
from .policy import PolicyEngine, policy_engine


class ToolGatewayError(RuntimeError):
    pass


class ToolGatewayValidationError(ToolGatewayError):
    pass


class ToolGatewayPolicyDenied(ToolGatewayError):
    pass


class ToolGatewayApprovalRequired(ToolGatewayError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ToolGatewayConnectionError(ToolGatewayError):
    pass


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    output: dict[str, Any]
    connector: str
    policy_decision: PolicyDecision


class ToolGateway:
    """The only runtime path from a capability to a connector invocation."""

    def __init__(
        self,
        *,
        tools: ToolRegistry = tool_registry,
        connectors: ConnectorRegistry = connector_registry,
        policy: PolicyEngine = policy_engine,
        oauth_http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.tools = tools
        self.connectors = connectors
        self.policy = policy
        self.oauth_http_client = oauth_http_client

    async def execute(
        self,
        db: Session,
        *,
        capability: str,
        arguments: dict[str, Any],
        requested_permission: PermissionDecision,
        context: dict[str, Any],
    ) -> ToolExecutionResult:
        tool = self.tools.get(capability)
        if tool is None:
            raise ToolGatewayValidationError(f"unknown capability: {capability}")
        self._validate_arguments(tool, arguments)
        agent_id = self._required_context(context, "agent_id")
        run_id = self._required_context(context, "run_id")
        user_id = self._required_context(context, "user_id")

        policy_result = self.policy.evaluate(tool, requested_permission)
        self._audit(
            db,
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
            event_type="policy.decision",
            decision=policy_result.decision.value,
            details={"tool": capability, "reason": policy_result.reason},
        )
        if policy_result.decision == PolicyDecision.DENY:
            raise ToolGatewayPolicyDenied(
                f"policy denied {capability}: {policy_result.reason}"
            )
        if policy_result.decision == PolicyDecision.REQUIRE_APPROVAL:
            raise ToolGatewayApprovalRequired(policy_result.reason)

        connection = db.scalar(
            select(ConnectorConnectionModel).where(
                ConnectorConnectionModel.user_id == user_id,
                ConnectorConnectionModel.provider == tool.connector,
                ConnectorConnectionModel.status == "connected",
            )
        )
        if connection is None:
            raise ToolGatewayConnectionError(
                f"connector {tool.connector} is not connected"
            )
        connector = self.connectors.get(tool.connector)
        if connector is None:
            raise ToolGatewayConnectionError(
                f"connector adapter is unavailable: {tool.connector}"
            )

        mode = str(connection.metadata_json.get("mode", "mock"))
        execution_context = {
            key: value for key, value in context.items() if not key.startswith("_")
        }
        execution_context["connection_mode"] = mode
        access_token: str | None = None
        if mode == "oauth2":
            try:
                grant = await get_access_grant(
                    db, tool.connector, http_client=self.oauth_http_client
                )
            except OAuthFlowError as exc:
                self._audit(
                    db,
                    user_id=user_id,
                    agent_id=agent_id,
                    run_id=run_id,
                    event_type="tool.authorization_failed",
                    decision=PolicyDecision.REQUIRE_REAUTH.value,
                    details={"tool": capability, "connector": tool.connector, "code": exc.code},
                )
                raise ToolGatewayConnectionError(exc.message) from exc
            missing_scopes = sorted(set(tool.required_scopes) - set(grant.scopes))
            if missing_scopes:
                self._audit(
                    db,
                    user_id=user_id,
                    agent_id=agent_id,
                    run_id=run_id,
                    event_type="tool.authorization_failed",
                    decision=PolicyDecision.REQUIRE_REAUTH.value,
                    details={
                        "tool": capability,
                        "connector": tool.connector,
                        "missing_scopes": missing_scopes,
                    },
                )
                raise ToolGatewayConnectionError(
                    f"connector {tool.connector} needs additional authorization scopes"
                )
            access_token = grant.access_token
            execution_context["_credential"] = {"access_token": access_token}
        elif mode != "mock":
            raise ToolGatewayConnectionError(
                f"unsupported connection mode for {tool.connector}: {mode}"
            )

        try:
            output = await connector.execute(capability, arguments, execution_context)
        except ConnectorError as exc:
            self._audit(
                db,
                user_id=user_id,
                agent_id=agent_id,
                run_id=run_id,
                event_type="tool.failed",
                decision=PolicyDecision.ALLOW.value,
                details={
                    "tool": capability,
                    "connector": tool.connector,
                    "error_type": type(exc).__name__,
                },
            )
            raise
        finally:
            execution_context.pop("_credential", None)
        if not isinstance(output, dict):
            raise ToolGatewayError("connector returned a non-object response")
        if access_token and access_token in repr(output):
            raise ToolGatewayError("connector response contained credential material")

        self._audit(
            db,
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
            event_type="tool.executed",
            decision=PolicyDecision.ALLOW.value,
            details={
                "tool": capability,
                "connector": tool.connector,
                "connection_mode": mode,
                "argument_keys": sorted(arguments),
                "record_count": output.get(
                    "transaction_count",
                    output.get("count", output.get("matched", output.get("read"))),
                ),
            },
        )
        return ToolExecutionResult(
            output=output,
            connector=tool.connector,
            policy_decision=policy_result.decision,
        )

    @staticmethod
    def _required_context(context: dict[str, Any], key: str) -> str:
        value = context.get(key)
        if not isinstance(value, str) or not value:
            raise ToolGatewayValidationError(f"missing execution context: {key}")
        return value

    @staticmethod
    def _audit(
        db: Session,
        *,
        user_id: str,
        agent_id: str,
        run_id: str,
        event_type: str,
        decision: str | None,
        details: dict[str, Any],
    ) -> None:
        db.add(
            AuditLogModel(
                user_id=user_id,
                agent_id=agent_id,
                run_id=run_id,
                event_type=event_type,
                decision=decision,
                details=details,
            )
        )

    @staticmethod
    def _validate_arguments(tool: ToolDefinition, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ToolGatewayValidationError("tool arguments must be an object")
        schema = tool.argument_schema
        if schema.get("type") != "object":
            return
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(arguments) - set(properties))
            if unknown:
                raise ToolGatewayValidationError(
                    f"unsupported arguments for {tool.name}: {unknown}"
                )
        missing = sorted(set(schema.get("required", [])) - set(arguments))
        if missing:
            raise ToolGatewayValidationError(
                f"missing arguments for {tool.name}: {missing}"
            )
        for name, value in arguments.items():
            value_schema = properties.get(name)
            if not isinstance(value_schema, dict):
                continue
            expected = value_schema.get("type")
            if expected == "string" and not isinstance(value, str):
                raise ToolGatewayValidationError(f"{name} must be a string")
            if expected == "integer" and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                raise ToolGatewayValidationError(f"{name} must be an integer")
            if expected == "array" and not isinstance(value, list):
                raise ToolGatewayValidationError(f"{name} must be an array")
            if isinstance(value, str) and len(value) > value_schema.get(
                "maxLength", len(value)
            ):
                raise ToolGatewayValidationError(f"{name} is too long")
            if isinstance(value, int):
                if value < value_schema.get("minimum", value):
                    raise ToolGatewayValidationError(f"{name} is below its minimum")
                if value > value_schema.get("maximum", value):
                    raise ToolGatewayValidationError(f"{name} exceeds its maximum")
            if isinstance(value, list) and len(value) > value_schema.get(
                "maxItems", len(value)
            ):
                raise ToolGatewayValidationError(f"{name} contains too many items")


tool_gateway = ToolGateway()
