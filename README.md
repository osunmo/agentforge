# AgentForge

AgentForge is a local-first AI agent factory that turns natural-language requests into deployable AI agents.

Instead of manually wiring together models, APIs, tools, and workflows, a user can simply describe what they want:

> “Connect my Gmail, Canvas, bank account, and calendar. Every Sunday, summarize my spending, upcoming assignments, and important unanswered emails.”

AgentForge determines the required capabilities, connects the appropriate services, selects between local and cloud models, generates an agent specification, applies permissions and approval rules, and runs the resulting agent.

## Core Architecture

```text
AgentForge
├── Agent Builder
├── Model Router
│   ├── Ollama
│   └── OpenRouter
├── Agent Runtime
│   └── Hermes
├── Connector Platform
│   ├── Gmail
│   ├── Canvas
│   ├── Plaid
│   ├── GitHub
│   ├── Google Calendar
│   ├── Slack
│   └── more
├── Policy Engine
├── Credential Broker
├── Memory
├── Scheduler / Event System
├── Sandboxed Execution
└── Audit / Activity Logs
```

## Current Backend Status

This repository currently contains the first runnable backend vertical slice for AgentForge.

It provides:

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

Mock mode never contacts real accounts.

The Gmail adapter can currently search and read message metadata from an authorized account. Live sending, deleting, and modifying email are intentionally unavailable.

Discovered OpenAPI connectors can be authorized with OAuth but remain non-executable until a sandbox-tested HTTP adapter exists.

Later milestones include:

- remote MCP registry search;
- token revocation;
- Redis worker execution;
- scheduling;
- Hermes runtime integration;
- Ollama and OpenRouter model providers;
- live Canvas and Plaid adapters;
- generated connector sandboxing.

## Key Ideas

- Natural-language agent creation
- Local-first execution for sensitive data
- OpenRouter for cloud model access
- Ollama for local models
- Hermes as an initial agent runtime
- Capability-based connector abstraction
- MCP, OpenAPI, and native API integrations
- Human approval for risky actions
- Secure credential handling