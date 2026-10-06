from __future__ import annotations

from ..domain.contracts import AgentSpec
from ..domain.tool_registry import ToolRegistry, tool_registry


class SpecRegistryValidationError(ValueError):
    pass


def validate_spec_references(
    spec: AgentSpec, registry: ToolRegistry = tool_registry
) -> AgentSpec:
    for capability in spec.capabilities:
        tool = registry.get(capability)
        if tool is None:
            raise SpecRegistryValidationError(f"unknown capability: {capability}")
        selected_provider = spec.connectors.get(tool.category)
        if selected_provider != tool.connector:
            raise SpecRegistryValidationError(
                f"capability {tool.name} requires connector mapping "
                f"{tool.category}={tool.connector}"
            )
    return spec

