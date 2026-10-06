from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


os.environ["DATABASE_URL"] = "sqlite:///./test-agentforge.db"
os.environ["AGENTFORGE_ENV"] = "test"
os.environ["AGENTFORGE_MASTER_KEY"] = Fernet.generate_key().decode("ascii")

from apps.api.app.db import Base, engine  # noqa: E402
from apps.api.app.domain.tool_registry import tool_registry  # noqa: E402
from apps.api.app.main import app  # noqa: E402
from apps.api.app.services.connectors import connector_registry  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_database() -> Generator[None, None, None]:
    tool_registry.reset()
    connector_registry.reset()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client
