from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from .config import settings
from .db import SessionLocal, engine, init_db
from .domain.tool_registry import list_tools
from .models import UserModel
from .routers import agents, approvals, connectors, discovery, oauth, runs
from .services.connectors import connector_registry
from .services.discovery import load_discovered_connectors


def _seed_local_user() -> None:
    with SessionLocal() as db:
        if db.get(UserModel, "local-user") is None:
            db.add(
                UserModel(
                    id="local-user",
                    email="local@agentforge.invalid",
                    display_name="Local User",
                )
            )
            db.commit()


def _load_connector_catalog() -> None:
    with SessionLocal() as db:
        load_discovered_connectors(db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    _seed_local_user()
    _load_connector_catalog()
    yield


app = FastAPI(
    title="AgentForge API",
    version="0.1.0",
    description="Local-first agent compiler and runtime control plane.",
    lifespan=lifespan,
)
static_directory = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=static_directory), name="static")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agents.router)
app.include_router(connectors.router)
app.include_router(runs.router)
app.include_router(approvals.router)
app.include_router(discovery.router)
app.include_router(oauth.router)


@app.get("/gmail-demo", include_in_schema=False)
def gmail_demo() -> FileResponse:
    return FileResponse(
        static_directory / "gmail-demo.html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


@app.get("/health", tags=["system"])
def health() -> dict[str, Any]:
    database_status = "connected"
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        database_status = "unavailable"
    return {
        "status": "ok" if database_status == "connected" else "degraded",
        "service": "agentforge-api",
        "version": app.version,
        "environment": settings.environment,
        "database": database_status,
        "connectors_registered": len(connector_registry.list()),
    }


@app.get("/v1/tools", tags=["tools"])
def tools() -> list[dict[str, Any]]:
    return [tool.model_dump(mode="json") for tool in list_tools()]


@app.get("/v1/users/local", tags=["system"])
def local_user() -> dict[str, str]:
    with SessionLocal() as db:
        user = db.scalar(select(UserModel).where(UserModel.id == "local-user"))
        return {"id": user.id, "email": user.email, "display_name": user.display_name}
