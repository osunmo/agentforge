def test_health_reports_database_and_registered_connectors(client):
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"] == "connected"
    assert payload["connectors_registered"] == 3


def test_tool_registry_is_exposed(client):
    response = client.get("/v1/tools")

    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()}
    assert "email.search" in names
    assert "education.assignments.list" in names
    assert "finance.transactions.list" in names


def test_openapi_contract_is_generated(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/v1/agents/compile" in paths
    assert "/v1/agents/{agent_id}/run" in paths
    assert "/v1/runs/{run_id}/audit" in paths
    assert "/v1/discovery/openapi/register" in paths


def test_gmail_demo_interface_is_served_by_the_api(client):
    response = client.get("/gmail-demo")

    assert response.status_code == 200
    assert "See the Gmail agent path work" in response.text
    assert "/static/gmail-demo.js" in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]
