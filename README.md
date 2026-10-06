# AgentForge

AgentForge is a local-first AI agent factory that turns natural-language requests into deployable AI agents.

Instead of manually wiring together models, APIs, tools, and workflows, a user can simply describe what they want:

> “Connect my Gmail, Canvas, bank account, and calendar. Every Sunday, summarize my spending, upcoming assignments, and important unanswered emails.”

AgentForge then determines the required capabilities, connects the appropriate services, selects between local and cloud models, generates an agent specification, applies permissions and approval rules, and runs the resulting agent.

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
- Durable agent execution
- Event-driven workflows
- Observable tool calls and audit logs
- Future automatic connector generation

## Example

```text
User:
"Create an agent that watches Gmail for recruiter emails,
checks my calendar, and drafts replies."

AgentForge:
1. Resolves email and calendar capabilities
2. Connects Gmail and Google Calendar
3. Generates an AgentSpec
4. Selects a model
5. Deploys the agent
6. Requires approval before sending email
```

## Long-Term Goal

AgentForge aims to make agent creation feel like this:

> **Tell your computer what you want connected and what you want done, and it builds the agent for you.**
