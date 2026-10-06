# AgentForge

AgentForge is a local-first AI agent factory.

Instead of manually building automations or coding each agent yourself, you describe what you want in plain English.

Example:

> Connect my Gmail, Canvas, bank account, and calendar. Every Sunday, tell me what I spent, what assignments are due, and what important emails I need to answer.

From that prompt, AgentForge should:

1. Determine the capabilities required to satisfy the request.
2. Select the right connectors (for example Gmail, Plaid, Canvas, GitHub, Slack, and WhatsApp).
3. Guide the user through authorization for each required service.
4. Choose an appropriate model for the task.
5. Generate an executable agent configuration.
6. Deploy the agent.
7. Run the agent on a schedule or in response to events.
