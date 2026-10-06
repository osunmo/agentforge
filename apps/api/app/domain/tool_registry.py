from __future__ import annotations

from .contracts import RiskLevel, ToolDefinition


DEFAULT_TOOLS = [
    ToolDefinition(
        name="email.search",
        description="Search email messages using a provider query.",
        category="email",
        connector="gmail",
        risk_level=RiskLevel.LOW,
        reversible=True,
        approval_required=False,
        required_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        argument_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 500},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="email.read",
        description="Read metadata for selected email messages.",
        category="email",
        connector="gmail",
        risk_level=RiskLevel.LOW,
        reversible=True,
        approval_required=False,
        required_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        argument_schema={
            "type": "object",
            "properties": {
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 200},
                    "maxItems": 25,
                }
            },
            "additionalProperties": False,
        },
    ),
    ToolDefinition(name="email.create_draft", description="Create an email draft without sending it.", category="email", connector="gmail", risk_level=RiskLevel.MEDIUM, reversible=True, approval_required=False, required_scopes=["gmail.compose"]),
    ToolDefinition(name="email.send", description="Send an external email message.", category="email", connector="gmail", risk_level=RiskLevel.HIGH, reversible=False, approval_required=True, required_scopes=["gmail.send"]),
    ToolDefinition(name="education.assignments.list", description="List assignments and due dates.", category="education", connector="canvas", risk_level=RiskLevel.LOW, reversible=True, approval_required=False, required_scopes=["canvas.read"]),
    ToolDefinition(name="finance.transactions.list", description="List read-only financial transactions.", category="finance", connector="plaid", risk_level=RiskLevel.HIGH, reversible=True, approval_required=False, required_scopes=["transactions:read"]),
]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self.reset()

    def reset(self) -> None:
        self._tools = {tool.name: tool.model_copy(deep=True) for tool in DEFAULT_TOOLS}

    def register(self, tool: ToolDefinition, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def list_capabilities(self) -> list[str]:
        return sorted(self._tools)

    def find_by_connector(self, provider: str) -> list[ToolDefinition]:
        return [tool for tool in self._tools.values() if tool.connector == provider]


tool_registry = ToolRegistry()


def get_tool(name: str) -> ToolDefinition | None:
    return tool_registry.get(name)


def list_tools() -> list[ToolDefinition]:
    return tool_registry.list()


def list_capabilities() -> list[str]:
    return tool_registry.list_capabilities()
