from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.oauth import (
    OAuthConnectionStatus,
    OAuthProviderConfiguration,
    OAuthProviderResponse,
    OAuthStartResponse,
)
from ..services.oauth import (
    OAuthFlowError,
    complete_authorization,
    configure_provider,
    connection_status,
    start_authorization,
)


router = APIRouter(prefix="/v1/oauth", tags=["oauth"])


def _raise_http(exc: OAuthFlowError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


@router.post("/{provider}/configure", response_model=OAuthProviderResponse)
def configure(
    provider: str,
    payload: OAuthProviderConfiguration,
    db: Session = Depends(get_db),
) -> OAuthProviderResponse:
    try:
        return configure_provider(db, provider, payload)
    except OAuthFlowError as exc:
        _raise_http(exc)


@router.post("/{provider}/start", response_model=OAuthStartResponse)
def start(provider: str, db: Session = Depends(get_db)) -> OAuthStartResponse:
    try:
        return start_authorization(db, provider)
    except OAuthFlowError as exc:
        _raise_http(exc)


@router.get("/{provider}/callback", response_model=OAuthConnectionStatus)
async def callback(
    provider: str,
    code: str = Query(min_length=1, max_length=8000),
    state: str = Query(min_length=20, max_length=1000),
    db: Session = Depends(get_db),
) -> OAuthConnectionStatus:
    try:
        return await complete_authorization(db, provider, state, code)
    except OAuthFlowError as exc:
        _raise_http(exc)


@router.get("/{provider}/demo-callback", include_in_schema=False)
async def demo_callback(
    provider: str,
    code: str | None = Query(default=None, min_length=1, max_length=8000),
    state: str = Query(min_length=20, max_length=1000),
    error: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if error is not None or code is None:
        return RedirectResponse(url="/gmail-demo?error=provider_denied", status_code=303)
    try:
        await complete_authorization(db, provider, state, code)
    except OAuthFlowError as exc:
        return RedirectResponse(
            url=f"/gmail-demo?error={quote(exc.code, safe='')}", status_code=303
        )
    return RedirectResponse(url="/gmail-demo?connected=1", status_code=303)


@router.get("/{provider}/status", response_model=OAuthConnectionStatus)
def status(provider: str, db: Session = Depends(get_db)) -> OAuthConnectionStatus:
    return connection_status(db, provider)
