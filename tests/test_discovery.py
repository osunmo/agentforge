from apps.api.app.db import SessionLocal
from apps.api.app.models import ConnectorConnectionModel


OPENAPI_DOCUMENT = {
    "openapi": "3.1.0",
    "info": {"title": "Example Tasks", "version": "1.0.0"},
    "servers": [{"url": "https://api.tasks.example/v1"}],
    "paths": {
        "/tasks": {
            "get": {
                "operationId": "listTasks",
                "summary": "List tasks",
                "x-agentforge-capability": "tasks.list",
            },
            "post": {
                "operationId": "createTask",
                "summary": "Create a task",
                "x-agentforge-capability": "tasks.create",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
            },
        }
    },
}


def _payload():
    return {
        "name": "example-tasks",
        "category": "tasks",
        "authentication_type": "oauth2",
        "required_scopes": ["tasks.read", "tasks.write"],
        "document": OPENAPI_DOCUMENT,
    }


def test_openapi_connector_can_be_inspected_without_registration(client):
    response = client.post("/v1/discovery/openapi/inspect", json=_payload())

    assert response.status_code == 200
    candidate = response.json()
    assert candidate["registration_status"] == "candidate"
    assert candidate["manifest"]["transport"] == "openapi"
    tools = {tool["name"]: tool for tool in candidate["tools"]}
    assert tools["tasks.list"]["approval_required"] is False
    assert tools["tasks.create"]["approval_required"] is True


def test_registered_openapi_connector_expands_the_capability_registry(client):
    registered = client.post("/v1/discovery/openapi/register", json=_payload())

    assert registered.status_code == 201
    assert registered.json()["registration_status"] == "registered_needs_authorization"

    resolved = client.post(
        "/v1/discovery/resolve", json={"capabilities": ["tasks.list", "unknown.read"]}
    )
    assert resolved.status_code == 200
    resolutions = {item["capability"]: item for item in resolved.json()}
    assert resolutions["tasks.list"]["provider"] == "example-tasks"
    assert resolutions["tasks.list"]["status"] == "authorization_required"
    assert resolutions["unknown.read"]["status"] == "discovery_required"

    connect = client.post("/v1/connectors/example-tasks/connect")
    assert connect.status_code == 409
    assert connect.json()["detail"]["code"] == "authorization_required"


def test_dynamic_capability_can_be_used_in_a_validated_agent_spec(client):
    assert client.post("/v1/discovery/openapi/register", json=_payload()).status_code == 201
    spec = {
        "agent_version": 1,
        "name": "task_reader",
        "objective": "Read tasks from the newly discovered task platform.",
        "triggers": [{"type": "manual"}],
        "capabilities": ["tasks.list"],
        "connectors": {"tasks": "example-tasks"},
        "permissions": {"tasks.list": "allow"},
        "model_policy": {"private_data": "prefer_local", "general_reasoning": "local"},
        "runtime": {"harness": "hermes"},
        "limits": {"max_steps": 10, "max_runtime_seconds": 60, "max_tool_calls": 5},
    }

    response = client.post("/v1/agents", json={"spec": spec})
    assert response.status_code == 201
    assert response.json()["agent_spec"]["capabilities"] == ["tasks.list"]


def test_authorized_dynamic_connector_still_reports_missing_adapter(client):
    assert client.post("/v1/discovery/openapi/register", json=_payload()).status_code == 201
    with SessionLocal() as db:
        db.add(
            ConnectorConnectionModel(
                user_id="local-user",
                provider="example-tasks",
                status="connected",
                metadata_json={"mode": "oauth2"},
            )
        )
        db.commit()

    response = client.post("/v1/discovery/resolve", json={"capabilities": ["tasks.list"]})

    assert response.status_code == 200
    assert response.json()[0]["status"] == "adapter_required"
