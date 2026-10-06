from .test_builder import GOLDEN_REQUEST


def test_golden_path_runs_and_produces_auditable_briefing(client):
    preview = client.post(
        "/v1/agents/compile",
        json={"request": GOLDEN_REQUEST, "timezone": "America/Chicago"},
    ).json()
    created = client.post(
        "/v1/agents",
        json={"spec": preview["spec"], "description": "Golden V1 demo"},
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]

    blocked_deploy = client.post(f"/v1/agents/{agent_id}/deploy")
    assert blocked_deploy.status_code == 409
    assert set(blocked_deploy.json()["detail"]["providers"]) == {
        "gmail",
        "canvas",
        "plaid",
    }

    for provider in ("gmail", "canvas", "plaid"):
        response = client.post(f"/v1/connectors/{provider}/connect")
        assert response.status_code == 200
        assert response.json()["mode"] == "mock"

    deployed = client.post(f"/v1/agents/{agent_id}/deploy")
    assert deployed.status_code == 200
    assert deployed.json()["status"] == "deployed"

    executed = client.post(f"/v1/agents/{agent_id}/run")
    assert executed.status_code == 201
    run = executed.json()
    assert run["status"] == "completed"
    assert len(run["steps"]) == 5
    assert run["result"]["finance"]["total_spend"] == 614.0
    assert len(run["result"]["school"]) == 4
    assert run["result"]["email"]["needs_attention"] == 3

    run_detail = client.get(f"/v1/runs/{run['id']}")
    assert run_detail.status_code == 200
    assert run_detail.json()["steps"][-1]["name"] == "briefing.compile"

    audit = client.get(f"/v1/runs/{run['id']}/audit")
    assert audit.status_code == 200
    events = audit.json()
    assert events[0]["event_type"] == "run.started"
    assert events[-1]["event_type"] == "run.completed"
    assert sum(event["event_type"] == "policy.decision" for event in events) == 4
    assert "items" not in str(
        [event["details"] for event in events if event["event_type"] == "tool.executed"]
    )

