from apps.api.app.domain.contracts import (
    PermissionDecision,
    PolicyDecision,
    RiskLevel,
    ToolDefinition,
)
from apps.api.app.domain.tool_registry import get_tool
from apps.api.app.services.policy import PolicyEngine


def test_external_send_requires_approval():
    result = PolicyEngine().evaluate(get_tool("email.send"), PermissionDecision.ALLOW)
    assert result.decision == PolicyDecision.REQUIRE_APPROVAL


def test_prohibited_tool_is_denied():
    tool = ToolDefinition(
        name="finance.transfer",
        description="Move money",
        category="finance",
        connector="plaid",
        risk_level=RiskLevel.PROHIBITED,
        reversible=False,
        approval_required=True,
    )
    result = PolicyEngine().evaluate(tool, PermissionDecision.ALLOW)
    assert result.decision == PolicyDecision.DENY

