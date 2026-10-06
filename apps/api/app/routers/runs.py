from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..domain.contracts import RunResponse
from ..models import AgentRunModel, AuditLogModel


router = APIRouter(prefix="/v1/runs", tags=["runs"])


@router.get("/{run_id}", response_model=RunResponse)
def get_run(run_id: str, db: Session = Depends(get_db)) -> AgentRunModel:
    run = db.scalar(
        select(AgentRunModel)
        .options(selectinload(AgentRunModel.steps))
        .where(AgentRunModel.id == run_id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/{run_id}/audit")
def get_run_audit(run_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    if db.get(AgentRunModel, run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    logs = db.scalars(
        select(AuditLogModel)
        .where(AuditLogModel.run_id == run_id)
        .order_by(AuditLogModel.created_at)
    ).all()
    return [
        {
            "id": log.id,
            "event_type": log.event_type,
            "decision": log.decision,
            "details": log.details,
            "created_at": log.created_at,
        }
        for log in logs
    ]

