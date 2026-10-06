from __future__ import annotations

from dataclasses import dataclass

from ..domain.contracts import PermissionDecision, PolicyDecision, RiskLevel, ToolDefinition


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str


class PolicyEngine:
    def evaluate(
        self, tool: ToolDefinition, requested_permission: PermissionDecision
    ) -> PolicyResult:
        if tool.risk_level == RiskLevel.PROHIBITED:
            return PolicyResult(PolicyDecision.DENY, "prohibited capability in V1")
        if requested_permission == PermissionDecision.DENY:
            return PolicyResult(PolicyDecision.DENY, "AgentSpec explicitly denies this tool")
        if tool.name.startswith("finance.") and not tool.reversible:
            return PolicyResult(PolicyDecision.DENY, "financial writes are prohibited in V1")
        if tool.approval_required or requested_permission == PermissionDecision.REQUIRE_APPROVAL:
            return PolicyResult(
                PolicyDecision.REQUIRE_APPROVAL,
                "external or irreversible action requires human approval",
            )
        return PolicyResult(PolicyDecision.ALLOW, "read-only or reversible operation")


policy_engine = PolicyEngine()

