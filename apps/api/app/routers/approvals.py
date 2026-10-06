from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ApprovalModel


router = APIRouter(prefix="/v1/approvals", tags=["approvals"])


def _serialize(item: ApprovalModel) -> dict[str, Any]:
    return {
        "id": item.id,
        "run_id": item.run_id,
        "tool_name": item.tool_name,
        "payload": item.payload,
        "status": item.status,
        "created_at": item.created_at,
        "resolved_at": item.resolved_at,
    }


@router.get("")
def list_approvals(
    status: str | None = None, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    statement = select(ApprovalModel).where(ApprovalModel.user_id == "local-user")
    if status:
        statement = statement.where(ApprovalModel.status == status)
    items = db.scalars(statement.order_by(ApprovalModel.created_at.desc())).all()
    return [_serialize(item) for item in items]


def _resolve(approval_id: str, resolution: Literal["approved", "rejected"], db: Session) -> dict[str, Any]:
    item = db.get(ApprovalModel, approval_id)
    if item is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if item.status != "pending":
        raise HTTPException(status_code=409, detail="approval has already been resolved")
    item.status = resolution
    item.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    return _serialize(item)


@router.post("/{approval_id}/approve")
def approve(approval_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _resolve(approval_id, "approved", db)


@router.post("/{approval_id}/reject")
def reject(approval_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _resolve(approval_id, "rejected", db)

