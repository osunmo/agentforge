from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..domain.contracts import (
    AgentSpec,
    AgentStatus,
    PermissionDecision,
    RunStatus,
)
from ..models import (
    AgentModel,
    AgentRunModel,
    ApprovalModel,
    AuditLogModel,
    ConnectorConnectionModel,
    RunStepModel,
)
from .tool_gateway import (
    ToolGateway,
    ToolGatewayApprovalRequired,
    tool_gateway,
)


class AgentNotDeployableError(RuntimeError):
    pass


class MissingConnectionsError(RuntimeError):
    def __init__(self, providers: list[str]) -> None:
        self.providers = providers
        super().__init__(f"missing connected providers: {', '.join(providers)}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(
    db: Session,
    *,
    event_type: str,
    agent_id: str,
    run_id: str,
    details: dict[str, Any],
    decision: str | None = None,
) -> None:
    db.add(
        AuditLogModel(
            agent_id=agent_id,
            run_id=run_id,
            event_type=event_type,
            decision=decision,
            details=details,
        )
    )


def _safe_step_output(capability: str, output: dict[str, Any]) -> dict[str, Any]:
    if capability == "finance.transactions.list":
        return {
            "transaction_count": output["transaction_count"],
            "total_spend": output["total_spend"],
            "currency": output["currency"],
            "categories": output["categories"],
            "notable": output["notable"],
        }
    return output


def _build_briefing(outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    finance = outputs.get("finance.transactions.list", {})
    assignments = outputs.get("education.assignments.list", {})
    email = outputs.get("email.search", {})
    return {
        "title": "Weekly briefing",
        "finance": {
            "total_spend": finance.get("total_spend", 0),
            "currency": finance.get("currency", "USD"),
            "notable": finance.get("notable", []),
        },
        "school": assignments.get("items", []),
        "email": {
            "needs_attention": email.get("unanswered", 0),
            "items": email.get("items", []),
        },
        "suggested_priorities": [
            "Reply to the interview scheduling email",
            "Submit the financial aid document",
            "Begin Algorithms HW4",
        ],
    }


def _tool_arguments(
    capability: str, outputs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if capability == "email.search":
        return {
            "query": (
                "in:inbox newer_than:14d -category:promotions -category:social "
                "{is:important is:starred is:unread}"
            ),
            "max_results": 10,
        }
    if capability == "email.read":
        search_items = outputs.get("email.search", {}).get("items", [])
        return {
            "message_ids": [
                item["id"]
                for item in search_items
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ][:10]
        }
    return {}


class BuiltinRuntime:
    def __init__(self, gateway: ToolGateway = tool_gateway) -> None:
        self.gateway = gateway

    async def start_run(
        self, db: Session, agent: AgentModel, trigger_type: str = "manual"
    ) -> AgentRunModel:
        if agent.status != AgentStatus.DEPLOYED.value:
            raise AgentNotDeployableError("agent must be deployed before it can run")

        spec = AgentSpec.model_validate(agent.agent_spec)
        required_providers = sorted(set(spec.connectors.values()))
        connected = set(
            db.scalars(
                select(ConnectorConnectionModel.provider).where(
                    ConnectorConnectionModel.user_id == agent.user_id,
                    ConnectorConnectionModel.status == "connected",
                )
            ).all()
        )
        missing = [provider for provider in required_providers if provider not in connected]
        if missing:
            raise MissingConnectionsError(missing)

        run = AgentRunModel(
            agent_id=agent.id,
            status=RunStatus.RUNNING.value,
            trigger_type=trigger_type,
            current_step=0,
        )
        db.add(run)
        db.flush()
        _audit(
            db,
            event_type="run.started",
            agent_id=agent.id,
            run_id=run.id,
            details={"trigger_type": trigger_type},
        )
        db.commit()

        outputs: dict[str, dict[str, Any]] = {}
        active_step: RunStepModel | None = None
        try:
            for step_number, capability in enumerate(spec.capabilities, start=1):
                if step_number > spec.limits.max_tool_calls:
                    raise RuntimeError("maximum tool call limit exceeded")
                permission = spec.permissions.get(capability, PermissionDecision.ALLOW)
                arguments = _tool_arguments(capability, outputs)
                step = RunStepModel(
                    run_id=run.id,
                    step_number=step_number,
                    type="tool",
                    name=capability,
                    status=RunStatus.RUNNING.value,
                    input=arguments,
                )
                db.add(step)
                db.flush()
                active_step = step
                try:
                    execution = await self.gateway.execute(
                        db,
                        capability=capability,
                        arguments=arguments,
                        requested_permission=permission,
                        context={
                            "agent_id": agent.id,
                            "run_id": run.id,
                            "user_id": agent.user_id,
                        },
                    )
                except ToolGatewayApprovalRequired:
                    db.add(
                        ApprovalModel(
                            user_id=agent.user_id,
                            run_id=run.id,
                            tool_name=capability,
                            payload={"arguments": arguments},
                        )
                    )
                    step.status = RunStatus.WAITING_FOR_APPROVAL.value
                    run.status = RunStatus.WAITING_FOR_APPROVAL.value
                    db.commit()
                    return self._load_run(db, run.id)
                output = execution.output
                safe_output = _safe_step_output(capability, output)
                outputs[capability] = safe_output
                step.output = safe_output
                step.status = RunStatus.COMPLETED.value
                step.completed_at = _now()
                run.current_step = step_number
                db.commit()
                active_step = None

            briefing = _build_briefing(outputs)
            final_step_number = len(spec.capabilities) + 1
            if final_step_number > spec.limits.max_steps:
                raise RuntimeError("maximum step limit exceeded")
            db.add(
                RunStepModel(
                    run_id=run.id,
                    step_number=final_step_number,
                    type="reasoning",
                    name="briefing.compile",
                    status=RunStatus.COMPLETED.value,
                    input={"sources": sorted(outputs)},
                    output=briefing,
                    completed_at=_now(),
                )
            )
            run.current_step = final_step_number
            run.result = briefing
            run.status = RunStatus.COMPLETED.value
            run.completed_at = _now()
            _audit(
                db,
                event_type="run.completed",
                agent_id=agent.id,
                run_id=run.id,
                details={"step_count": final_step_number},
            )
            db.commit()
        except Exception as exc:
            if active_step is not None and active_step.status == RunStatus.RUNNING.value:
                active_step.status = RunStatus.FAILED.value
                active_step.completed_at = _now()
            run.status = RunStatus.FAILED.value
            run.error = str(exc)
            run.completed_at = _now()
            _audit(
                db,
                event_type="run.failed",
                agent_id=agent.id,
                run_id=run.id,
                details={"error_type": type(exc).__name__},
            )
            db.commit()
            raise

        return self._load_run(db, run.id)

    @staticmethod
    def _load_run(db: Session, run_id: str) -> AgentRunModel:
        return db.scalar(
            select(AgentRunModel)
            .options(selectinload(AgentRunModel.steps))
            .where(AgentRunModel.id == run_id)
        )


builtin_runtime = BuiltinRuntime()
