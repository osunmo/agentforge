# AgentForge backend

This directory contains the first runnable backend vertical slice for AgentForge.

It currently provides:

- typed and validated AgentSpec V1 contracts;
- deterministic compilation of the golden-path request;
- persistent agents, runs, steps, connector state, approvals, and audit logs;
- a hybrid Gmail connector with deterministic mock mode and live read-only API mode;
- normalized mock Canvas and Plaid connectors;
- a dynamic connector catalog that can inspect and register OpenAPI 3.x platforms;
- an encrypted credential broker and OAuth 2.0 authorization-code flow with PKCE;
- automatic OAuth access-token refresh without exposing refresh tokens to agents;
- a Tool Gateway that enforces policy, scopes, argument schemas, and audit boundaries;
- capability resolution across built-in and dynamically discovered connectors;
- minimum-permission policy decisions;
- a synchronous runtime that produces a combined weekly briefing from mock or authorized tools;
- FastAPI endpoints and end-to-end tests.

Mock mode never contacts real accounts. The Gmail adapter can now search and read message metadata from an authorized account; live sending, deleting, and modifying email are intentionally unavailable. Discovered OpenAPI connectors can be authorized with OAuth but remain non-executable until a sandbox-tested HTTP adapter exists. Remote MCP registry search, token revocation, the Redis worker, scheduling, Hermes, and live model providers are later milestones.

## Run locally

```powershell
cd agentforge
$env:AGENTFORGE_MASTER_KEY = python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -m uvicorn apps.api.app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the API explorer.

Save the generated master key in a local secret manager or ignored `.env` file and reuse it. If it changes, previously stored credentials cannot be decrypted. Credential endpoints fail closed when the key is absent or invalid. This V1 API assumes a trusted local machine and should not be exposed publicly; API authentication is a later milestone.

## Test

```powershell
cd agentforge
python -m pytest
```

## Golden-path API flow

1. `POST /v1/agents/compile`
2. `POST /v1/agents`
3. Connect `gmail`, `canvas`, and `plaid` using the connector endpoints.
4. `POST /v1/agents/{agent_id}/deploy`
5. `POST /v1/agents/{agent_id}/run`
6. Inspect `/v1/runs/{run_id}` and `/v1/runs/{run_id}/audit`.

## Universal connector discovery

The platform is capability-driven rather than provider-driven. Use:

- `POST /v1/discovery/resolve` to resolve normalized capabilities;
- `POST /v1/discovery/openapi/inspect` to inspect an OpenAPI document without changing state;
- `POST /v1/discovery/openapi/register` to persist its connector and tool definitions.

OpenAPI documents are submitted as JSON. The service does not fetch arbitrary URLs during inspection. Read operations are allowed by default; inferred writes are conservatively marked as requiring human approval.

## OAuth connection flow

After registering a connector, configure and authorize its OAuth provider through:

1. `POST /v1/oauth/{provider}/configure`
2. `POST /v1/oauth/{provider}/start`
3. Open the returned `authorization_url` in the user's browser.
4. The provider redirects to `GET /v1/oauth/{provider}/callback`.
5. Inspect `GET /v1/oauth/{provider}/status`.

Provider client secrets and token responses are encrypted before persistence. OAuth state is hashed, PKCE uses `S256`, callback state is one-time and expires within ten minutes, and neither tokens nor authorization codes are written to audit details. `POST /v1/connectors/{provider}/disconnect` deletes the locally stored OAuth token.

## Live Gmail setup

Create a Google OAuth web client and configure the exact redirect URI used by your chosen test flow. Do not commit its client secret.

The simplest test path is the built-in interface:

1. Start the API and open `http://127.0.0.1:8000/gmail-demo`.
2. Copy the redirect URI displayed by the interface into the Google OAuth client.
3. Enter the OAuth client ID and secret, then select **Save and authorize Gmail**.
4. Approve read-only Gmail access on Google's consent screen.
5. After returning to AgentForge, select **Test Gmail** to view matching unanswered threads.

Use the same host consistently: `127.0.0.1` and `localhost` are different OAuth redirect URIs. The interface renders email fields as plain text and never stores the client secret in browser storage.

The equivalent API-only setup is:

```powershell
$env:GOOGLE_CLIENT_ID = "your-client-id"
$env:GOOGLE_CLIENT_SECRET = "your-client-secret"

$gmailOAuth = @{
  authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
  token_endpoint = "https://oauth2.googleapis.com/token"
  client_id = $env:GOOGLE_CLIENT_ID
  client_secret = $env:GOOGLE_CLIENT_SECRET
  client_auth_method = "client_secret_post"
  redirect_uri = "http://127.0.0.1:8000/v1/oauth/gmail/callback"
  scopes = @("https://www.googleapis.com/auth/gmail.readonly")
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/v1/oauth/gmail/configure" `
  -ContentType "application/json" `
  -Body $gmailOAuth

$authorization = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/v1/oauth/gmail/start"

Start-Process $authorization.authorization_url
```

After Google redirects back to AgentForge, `GET /v1/oauth/gmail/status` should report `connected`. The runtime will then use live Gmail for `email.search` and `email.read`; Canvas and Plaid remain in deterministic mock mode until their live adapters are implemented.

The Gmail scope is read-only but Google classifies Gmail data access as sensitive or restricted depending on the scope and usage. A development OAuth consent screen may require explicitly configured test users, while a public deployment may require Google verification.
