from __future__ import annotations

import re
from collections import defaultdict

from ..domain.contracts import (
    AgentPreview,
    AgentSpec,
    CompileRequest,
    ConnectionRequirement,
    ManualTrigger,
    ModelPolicy,
    PermissionDecision,
    RuntimePolicy,
    RunLimits,
    ScheduleTrigger,
)
from ..domain.tool_registry import get_tool


class UnsupportedRequestError(ValueError):
    pass


DAY_TO_CRON = {
    "sunday": "SUN",
    "monday": "MON",
    "tuesday": "TUE",
    "wednesday": "WED",
    "thursday": "THU",
    "friday": "FRI",
    "saturday": "SAT",
}


def _detect_schedule(text: str, timezone: str) -> ScheduleTrigger | ManualTrigger:
    day = next((name for name in DAY_TO_CRON if name in text), None)
    if not day and not any(word in text for word in ("weekly", "every week", "schedule")):
        return ManualTrigger()

    day = day or "sunday"
    hour = 19
    minute = 0
    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text)
    if time_match:
        hour = int(time_match.group(1)) % 12
        minute = int(time_match.group(2) or 0)
        if time_match.group(3) == "pm":
            hour += 12
    return ScheduleTrigger(
        cron=f"{minute} {hour} * * {DAY_TO_CRON[day]}", timezone=timezone
    )


def compile_request(payload: CompileRequest) -> AgentPreview:
    text = payload.request.strip()
    normalized = text.lower()
    capabilities: list[str] = []

    if any(term in normalized for term in ("gmail", "email", "inbox", "unanswered")):
        capabilities.extend(["email.search", "email.read"])
    if any(term in normalized for term in ("canvas", "assignment", "course", "homework")):
        capabilities.append("education.assignments.list")
    if any(
        term in normalized
        for term in ("bank", "plaid", "spending", "transaction", "finance", "money")
    ):
        capabilities.append("finance.transactions.list")

    capabilities = list(dict.fromkeys(capabilities))
    if not capabilities:
        raise UnsupportedRequestError(
            "V1 can currently compile requests involving Gmail, Canvas, or read-only banking data"
        )

    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    connectors: dict[str, str] = {}
    permissions: dict[str, PermissionDecision] = {}
    for capability in capabilities:
        tool = get_tool(capability)
        if tool is None:  # guarded by the registry, retained as a safe failure mode
            raise UnsupportedRequestError(f"unsupported capability: {capability}")
        connectors[tool.category] = tool.connector
        permissions[capability] = (
            PermissionDecision.REQUIRE_APPROVAL
            if tool.approval_required
            else PermissionDecision.ALLOW
        )
        grouped[(tool.connector, tool.category)].append(capability)

    is_combined_briefing = len(connectors) > 1 or "brief" in normalized or "summar" in normalized
    name = "weekly_briefing" if is_combined_briefing else f"{next(iter(connectors))}_assistant"
    private_data = (
        "local_only" if "finance" in connectors else "prefer_local"
    )

    spec = AgentSpec(
        name=name,
        objective=text,
        triggers=[_detect_schedule(normalized, payload.timezone)],
        capabilities=capabilities,
        connectors=connectors,
        permissions=permissions,
        memory=["last_successful_run", "handled_item_ids"],
        model_policy=ModelPolicy(
            private_data=private_data,
            general_reasoning="local",
        ),
        runtime=RuntimePolicy(harness="hermes"),
        limits=RunLimits(max_steps=30, max_runtime_seconds=300, max_tool_calls=20),
    )

    requirements = [
        ConnectionRequirement(
            provider=provider,
            category=category,
            capabilities=items,
            permissions={item: permissions[item] for item in items},
        )
        for (provider, category), items in grouped.items()
    ]
    warnings = [
        "All V1 banking capabilities are read-only.",
        "Mock connectors are used until OAuth integrations are configured.",
    ]
    return AgentPreview(
        spec=spec,
        required_connections=requirements,
        warnings=warnings,
    )

