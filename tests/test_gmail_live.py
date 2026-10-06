from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select

from apps.api.app.db import SessionLocal
from apps.api.app.models import AuditLogModel, CredentialModel
from apps.api.app.services.connectors import GmailConnector, connector_registry
from apps.api.app.services.oauth import complete_authorization
from tests.test_builder import GOLDEN_REQUEST


GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
ACCESS_TOKEN = "live-gmail-access-token-never-log"
REFRESH_TOKEN = "live-gmail-refresh-token-never-log"


def _oauth_configuration() -> dict[str, object]:
    return {
        "authorization_endpoint": "https://accounts.google.test/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.google.test/token",
        "client_id": "gmail-live-test-client",
        "client_secret": "gmail-live-test-secret",
        "client_auth_method": "client_secret_post",
        "redirect_uri": "http://127.0.0.1:8000/v1/oauth/gmail/callback",
        "scopes": [GMAIL_SCOPE],
    }


def _gmail_message(message_id: str) -> dict[str, object]:
    suffix = (
        "Interview scheduling"
        if message_id == "msg-1"
        else "Professor meeting change"
        if message_id == "msg-2"
        else "Already answered thread"
    )
    labels = ["INBOX", "UNREAD", "IMPORTANT"] if message_id == "msg-1" else ["INBOX", "UNREAD"]
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": labels,
        "snippet": f"Preview for {suffix}",
        "payload": {
            "headers": [
                {"name": "Subject", "value": suffix},
                {"name": "From", "value": "Recruiter <recruiter@example.test>"},
                {"name": "Date", "value": "Mon, 21 Sep 2026 10:00:00 -0500"},
            ]
        },
    }


def test_live_gmail_oauth_to_weekly_briefing_is_auditable(client) -> None:
    configured = client.post("/v1/oauth/gmail/configure", json=_oauth_configuration())
    assert configured.status_code == 200, configured.text
    started = client.post("/v1/oauth/gmail/start")
    assert started.status_code == 200, started.text
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    assert query["access_type"] == ["offline"]
    assert query["include_granted_scopes"] == ["true"]
    state = query["state"][0]

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        assert form["grant_type"] == ["authorization_code"]
        assert form["code_verifier"]
        return httpx.Response(
            200,
            json={
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": GMAIL_SCOPE,
            },
        )

    async def authorize() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(token_endpoint)
        ) as token_client:
            with SessionLocal() as db:
                result = await complete_authorization(
                    db,
                    "gmail",
                    state,
                    "google-authorization-code",
                    http_client=token_client,
                )
                assert result.status == "connected"

    asyncio.run(authorize())

    gmail_requests: list[str] = []

    def gmail_api(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        gmail_requests.append(str(request.url))
        if request.url.path == "/gmail/v1/users/me/messages":
            assert request.url.params.get("q")
            assert request.url.params.get("maxResults") in {"5", "10"}
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"id": "msg-1", "threadId": "thread-msg-1"},
                        {"id": "msg-2", "threadId": "thread-msg-2"},
                        {"id": "msg-3", "threadId": "thread-msg-3"},
                    ],
                    "resultSizeEstimate": 3,
                },
            )
        if "/threads/" in request.url.path:
            thread_id = request.url.path.rsplit("/", 1)[-1]
            message_id = thread_id.removeprefix("thread-")
            messages = [_gmail_message(message_id)]
            if message_id == "msg-3":
                sent_reply = _gmail_message("msg-3-sent")
                sent_reply["labelIds"] = ["SENT"]
                messages.append(sent_reply)
            return httpx.Response(200, json={"id": thread_id, "messages": messages})
        message_id = request.url.path.rsplit("/", 1)[-1]
        assert request.url.params.get("format") == "metadata"
        return httpx.Response(200, json=_gmail_message(message_id))

    gmail_client = httpx.AsyncClient(transport=httpx.MockTransport(gmail_api))
    connector_registry.register(
        GmailConnector(
            http_client=gmail_client,
            api_base_url="https://gmail.googleapis.test/gmail/v1",
        ),
        replace=True,
    )

    diagnostic = client.post(
        "/v1/connectors/gmail/test",
        json={
            "query": "in:inbox newer_than:14d {is:important is:unread}",
            "max_results": 5,
        },
    )
    assert diagnostic.status_code == 200, diagnostic.text
    assert diagnostic.json()["provider"] == "gmail"
    assert diagnostic.json()["unanswered"] == 2
    assert "access_token" not in diagnostic.text

    preview = client.post(
        "/v1/agents/compile",
        json={"request": GOLDEN_REQUEST, "timezone": "America/Chicago"},
    ).json()
    created = client.post(
        "/v1/agents", json={"spec": preview["spec"], "description": "Live Gmail path"}
    )
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    for provider in ("canvas", "plaid"):
        assert client.post(f"/v1/connectors/{provider}/connect").status_code == 200
    assert client.post(f"/v1/agents/{agent_id}/deploy").status_code == 200

    executed = client.post(f"/v1/agents/{agent_id}/run")

    assert executed.status_code == 201, executed.text
    run = executed.json()
    assert run["status"] == "completed"
    assert run["result"]["email"]["needs_attention"] == 2
    assert [item["subject"] for item in run["result"]["email"]["items"]] == [
        "Interview scheduling",
        "Professor meeting change",
    ]
    assert len(gmail_requests) == 10
    email_read_step = next(step for step in run["steps"] if step["name"] == "email.read")
    assert email_read_step["input"]["message_ids"] == ["msg-1", "msg-2"]

    audit = client.get(f"/v1/runs/{run['id']}/audit").json()
    audit_text = str([event["details"] for event in audit])
    assert ACCESS_TOKEN not in audit_text
    assert REFRESH_TOKEN not in audit_text
    assert sum(event["event_type"] == "tool.executed" for event in audit) == 4
    gmail_events = [
        event
        for event in audit
        if event["event_type"] == "tool.executed"
        and event["details"]["connector"] == "gmail"
    ]
    assert all(event["details"]["connection_mode"] == "oauth2" for event in gmail_events)

    with SessionLocal() as db:
        encrypted = db.scalar(
            select(CredentialModel.encrypted_payload).where(
                CredentialModel.provider == "gmail",
                CredentialModel.kind == "oauth_tokens",
            )
        )
        assert encrypted is not None
        assert ACCESS_TOKEN not in encrypted
        assert REFRESH_TOKEN not in encrypted
        all_audits = " ".join(
            str(log.details) for log in db.scalars(select(AuditLogModel)).all()
        )
        assert ACCESS_TOKEN not in all_audits
        assert REFRESH_TOKEN not in all_audits

    asyncio.run(gmail_client.aclose())
