from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr


ClientAuthMethod = Literal["client_secret_post", "client_secret_basic", "none"]


class OAuthProviderConfiguration(BaseModel):
    authorization_endpoint: str = Field(min_length=8, max_length=2000)
    token_endpoint: str = Field(min_length=8, max_length=2000)
    client_id: str = Field(min_length=1, max_length=1000)
    client_secret: SecretStr | None = None
    client_auth_method: ClientAuthMethod = "client_secret_post"
    redirect_uri: str = Field(min_length=8, max_length=2000)
    scopes: list[str] = Field(default_factory=list, max_length=100)


class OAuthProviderResponse(BaseModel):
    provider: str
    configured: bool
    client_auth_method: ClientAuthMethod
    redirect_uri: str
    scopes: list[str]
    has_client_secret: bool


class OAuthStartResponse(BaseModel):
    provider: str
    authorization_url: str
    expires_at: datetime


class OAuthConnectionStatus(BaseModel):
    provider: str
    configured: bool
    status: str
    client_id: str | None = None
    redirect_uri: str | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
