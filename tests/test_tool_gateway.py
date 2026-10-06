from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from apps.api.app.db import SessionLocal
from apps.api.app.domain.contracts import PermissionDecision
from apps.api.app.models import AuditLogModel, ConnectorConnectionModel
from apps.api.app.services.tool_gateway import (
    ToolGateway,
    ToolGatewayApprovalRequired,
    ToolGatewayValidationError,
)


def test_gateway_enforces_policy_before_connector_execution() -> None:
    with SessionLocal() as db:
        db.add(
            ConnectorConnectionModel(
                user_id="local-user",
                provider="gmail",
                status="connected",
                metadata_json={"mode": "mock"},
            )
        )
        db.commit()
        with pytest.raises(ToolGatewayApprovalRequired):
            asyncio.run(
                ToolGateway().execute(
                    db,
                    capability="email.send",
                    arguments={},
                    requested_permission=PermissionDecision.ALLOW,
                    context={
                        "user_id": "local-user",
                        "agent_id": "agent-test",
                        "run_id": "run-test",
                    },
                )
            )
        db.commit()
        audits = db.scalars(select(AuditLogModel)).all()
        assert len(audits) == 1
        assert audits[0].event_type == "policy.decision"
        assert audits[0].decision == "require_approval"


def test_gateway_rejects_unknown_arguments_before_execution() -> None:
    with SessionLocal() as db:
        with pytest.raises(ToolGatewayValidationError, match="unsupported arguments"):
            asyncio.run(
                ToolGateway().execute(
                    db,
                    capability="email.search",
                    arguments={"access_token": "must-not-be-accepted"},
                    requested_permission=PermissionDecision.ALLOW,
                    context={
                        "user_id": "local-user",
                        "agent_id": "agent-test",
                        "run_id": "run-test",
                    },
                )
            )
