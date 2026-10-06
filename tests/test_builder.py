from copy import deepcopy


GOLDEN_REQUEST = (
    "Connect my Gmail, Canvas, and bank account. Every Sunday at 7 PM summarize "
    "my spending, show assignments due next week, identify important unanswered "
    "emails, and give me one briefing."
)


def test_compile_golden_path_creates_validated_preview(client):
    response = client.post(
        "/v1/agents/compile",
        json={"request": GOLDEN_REQUEST, "timezone": "America/Chicago"},
    )

    assert response.status_code == 200
    preview = response.json()
    spec = preview["spec"]
    assert spec["name"] == "weekly_briefing"
    assert spec["triggers"] == [
        {"type": "schedule", "cron": "0 19 * * SUN", "timezone": "America/Chicago"}
    ]
    assert set(spec["connectors"].values()) == {"gmail", "canvas", "plaid"}
    assert spec["model_policy"]["private_data"] == "local_only"
    assert all(value == "allow" for value in spec["permissions"].values())
    assert {item["provider"] for item in preview["required_connections"]} == {
        "gmail",
        "canvas",
        "plaid",
    }


def test_unknown_capability_is_rejected_before_persistence(client):
    preview = client.post(
        "/v1/agents/compile", json={"request": GOLDEN_REQUEST}
    ).json()
    invalid_spec = deepcopy(preview["spec"])
    invalid_spec["capabilities"].append("bank.transfer")

    response = client.post(
        "/v1/agents",
        json={"spec": invalid_spec, "description": "must fail"},
    )

    assert response.status_code == 422
    assert "unknown capability" in response.text


def test_out_of_scope_request_fails_honestly(client):
    response = client.post(
        "/v1/agents/compile",
        json={"request": "Control my smart-home lights when I enter a room."},
    )

    assert response.status_code == 422
    assert "currently compile" in response.json()["detail"]

