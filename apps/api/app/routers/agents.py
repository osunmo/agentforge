from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..domain.contracts import (
    AgentCreate,
    AgentPreview,
    AgentResponse,
    AgentSpec,
    AgentStatus,
    AgentUpdate,
    CompileRequest,
    RunResponse,
)
from ..models import AgentModel, AgentRunModel, ConnectorConnectionModel
from ..services.builder import UnsupportedRequestError, compile_request
from ..services.runtime import (
    AgentNotDeployableError,
    MissingConnectionsError,
    builtin_runtime,
)
from ..services.validation import SpecRegistryValidationError, validate_spec_references


router = APIRouter(prefix="/v1/agents", tags=["agents"])


def _get_agent(db: Session, agent_id: str) -> AgentModel:
    agent = db.get(AgentModel, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent


@router.post("/compile", response_model=AgentPreview)
def compile_agent(payload: CompileRequest) -> AgentPreview:
    try:
        return compile_request(payload)
    except UnsupportedRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
def create_agent(payload: AgentCreate, db: Session = Depends(get_db)) -> AgentModel:
    spec = AgentSpec.model_validate(payload.spec)
    try:
        validate_spec_references(spec)
    except SpecRegistryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    agent = AgentModel(
        name=spec.name,
        description=payload.description,
        agent_spec=spec.model_dump(mode="json"),
        status=AgentStatus.DRAFT.value,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


@router.get("", response_model=list[AgentResponse])
def list_agents(db: Session = Depends(get_db)) -> list[AgentModel]:
    return list(db.scalars(select(AgentModel).order_by(AgentModel.created_at.desc())).all())


@router.get("/{agent_id}", response_model=AgentResponse)
def get_agent(agent_id: str, db: Session = Depends(get_db)) -> AgentModel:
    return _get_agent(db, agent_id)


@router.put("/{agent_id}", response_model=AgentResponse)
def update_agent(
    agent_id: str, payload: AgentUpdate, db: Session = Depends(get_db)
) -> AgentModel:
    agent = _get_agent(db, agent_id)
    if payload.spec is not None:
        spec = AgentSpec.model_validate(payload.spec)
        try:
            validate_spec_references(spec)
        except SpecRegistryValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        agent.name = spec.name
        agent.agent_spec = spec.model_dump(mode="json")
        agent.version += 1
    if payload.description is not None:
        agent.description = payload.description
    if payload.status is not None:
        agent.status = payload.status.value
    db.commit()
    db.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: str, db: Session = Depends(get_db)) -> Response:
    agent = _get_agent(db, agent_id)
    db.delete(agent)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{agent_id}/deploy", response_model=AgentResponse)
def deploy_agent(agent_id: str, db: Session = Depends(get_db)) -> AgentModel:
    agent = _get_agent(db, agent_id)
    spec = AgentSpec.model_validate(agent.agent_spec)
    try:
        validate_spec_references(spec)
    except SpecRegistryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    required = set(spec.connectors.values())
    connected = set(
        db.scalars(
            select(ConnectorConnectionModel.provider).where(
                ConnectorConnectionModel.user_id == agent.user_id,
                ConnectorConnectionModel.status == "connected",
            )
        ).all()
    )
    missing = sorted(required - connected)
    if missing:
        raise HTTPException(
            status_code=409,
            detail={"code": "missing_connections", "providers": missing},
        )
    agent.status = AgentStatus.DEPLOYED.value
    db.commit()
    db.refresh(agent)
    return agent


@router.post("/{agent_id}/run", response_model=RunResponse, status_code=201)
async def run_agent(agent_id: str, db: Session = Depends(get_db)) -> AgentRunModel:
    agent = _get_agent(db, agent_id)
    try:
        return await builtin_runtime.start_run(db, agent, trigger_type="manual")
    except AgentNotDeployableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MissingConnectionsError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "missing_connections", "providers": exc.providers},
        ) from exc


@router.get("/{agent_id}/runs", response_model=list[RunResponse])
def list_agent_runs(agent_id: str, db: Session = Depends(get_db)) -> list[AgentRunModel]:
    _get_agent(db, agent_id)
    return list(
        db.scalars(
            select(AgentRunModel)
            .options(selectinload(AgentRunModel.steps))
            .where(AgentRunModel.agent_id == agent_id)
            .order_by(AgentRunModel.started_at.desc())
        ).all()
    )
