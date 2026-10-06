from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from email.header import decode_header, make_header
from typing import Any
from urllib.parse import quote

import httpx

from ..config import settings
from ..domain.contracts import ConnectorManifest


class ConnectorError(RuntimeError):
    pass


class ConnectorAuthenticationError(ConnectorError):
    pass


class ConnectorPermissionError(ConnectorError):
    pass


class ConnectorRateLimitError(ConnectorError):
    pass


class ConnectorProviderError(ConnectorError):
    pass


class Connector(ABC):
    manifest: ConnectorManifest

    @abstractmethod
    async def health_check(self) -> dict[str, Any]: ...

    @abstractmethod
    async def execute(
        self, tool_name: str, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]: ...


class GmailConnector(Connector):
    manifest = ConnectorManifest(
        name="gmail",
        category="email",
        authentication_type="mock_oauth2",
        capabilities=["email.search", "email.read", "email.create_draft", "email.send"],
        required_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        risk_metadata={
            "personal_content": "high",
            "mock": True,
            "oauth2_supported": True,
            "read_only_live_adapter": True,
        },
    )

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str | None = None,
    ) -> None:
        self.http_client = http_client
        self.api_base_url = (api_base_url or settings.gmail_api_base_url).rstrip("/")

    async def health_check(self) -> dict[str, Any]:
        return {
            "status": "available",
            "mode": "hybrid",
            "supported_modes": ["mock", "oauth2"],
            "live_capabilities": ["email.search", "email.read"],
        }

    async def execute(
        self, tool_name: str, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        mode = context.get("connection_mode", "mock")
        if mode == "mock":
            return self._execute_mock(tool_name)
        if mode != "oauth2":
            raise ConnectorAuthenticationError("gmail connection mode is not authorized")
        credential = context.get("_credential")
        access_token = credential.get("access_token") if isinstance(credential, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise ConnectorAuthenticationError("gmail access token is unavailable")
        if tool_name not in {"email.search", "email.read"}:
            raise ConnectorPermissionError(
                f"live Gmail adapter does not allow capability {tool_name}"
            )

        async with self._client() as client:
            if tool_name == "email.search":
                return await self._search(client, access_token, arguments)
            return await self._read(client, access_token, arguments)

    @staticmethod
    def _execute_mock(tool_name: str) -> dict[str, Any]:
        if tool_name == "email.search":
            return {
                "matched": 3,
                "unanswered": 3,
                "items": [
                    {"id": "mail-101", "subject": "Interview scheduling", "priority": "high"},
                    {"id": "mail-102", "subject": "Professor meeting change", "priority": "high"},
                    {"id": "mail-103", "subject": "Financial aid document", "priority": "high"},
                ],
            }
        if tool_name == "email.read":
            return {"read": 3, "thread_ids": ["mail-101", "mail-102", "mail-103"]}
        if tool_name == "email.create_draft":
            return {"draft_id": "draft-001", "status": "created"}
        if tool_name == "email.send":
            return {"message_id": "sent-001", "status": "sent"}
        raise ConnectorError(f"gmail does not support tool {tool_name}")

    @asynccontextmanager
    async def _client(self):
        if self.http_client is not None:
            yield self.http_client
            return
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0), follow_redirects=False
        ) as client:
            yield client

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        access_token: str,
        *,
        params: Any = None,
    ) -> dict[str, Any]:
        try:
            response = await client.get(
                f"{self.api_base_url}{path}",
                params=params,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise ConnectorProviderError("Gmail API request failed") from exc
        if response.status_code == 401:
            raise ConnectorAuthenticationError("Gmail authorization has expired")
        if response.status_code == 403:
            raise ConnectorPermissionError("Gmail denied the requested operation")
        if response.status_code == 429:
            raise ConnectorRateLimitError("Gmail rate limit was reached")
        if response.status_code >= 500:
            raise ConnectorProviderError("Gmail is temporarily unavailable")
        if response.status_code >= 400:
            raise ConnectorProviderError("Gmail rejected the request")
        if len(response.content) > 2_000_000:
            raise ConnectorProviderError("Gmail response exceeded the safe size limit")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectorProviderError("Gmail returned an invalid response") from exc
        if not isinstance(payload, dict):
            raise ConnectorProviderError("Gmail returned an invalid response")
        return payload

    async def _search(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        query = arguments.get(
            "query",
            "in:inbox newer_than:14d -category:promotions -category:social "
            "{is:important is:starred is:unread}",
        )
        max_results = arguments.get("max_results", 10)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ConnectorError("email.search query must be a non-empty string")
        if not isinstance(max_results, int) or not 1 <= max_results <= 25:
            raise ConnectorError("email.search max_results must be between 1 and 25")
        listing = await self._request_json(
            client,
            "/users/me/messages",
            access_token,
            params={"q": query, "maxResults": max_results},
        )
        references = listing.get("messages", [])
        if not isinstance(references, list):
            raise ConnectorProviderError("Gmail returned an invalid message list")
        thread_ids: list[str] = []
        for reference in references[:max_results]:
            thread_id = reference.get("threadId") if isinstance(reference, dict) else None
            if not isinstance(thread_id, str) or not thread_id or thread_id in thread_ids:
                continue
            thread_ids.append(thread_id)
        items: list[dict[str, Any]] = []
        for thread_id in thread_ids:
            thread = await self._thread_metadata(client, access_token, thread_id)
            messages = thread.get("messages", [])
            if not isinstance(messages, list) or not messages:
                continue
            latest = messages[-1]
            if not isinstance(latest, dict):
                continue
            latest_labels = {
                str(label)
                for label in latest.get("labelIds", [])
                if isinstance(label, str)
            }
            if "SENT" in latest_labels:
                continue
            item = self._normalize_message(latest)
            item["thread_id"] = thread_id
            items.append(item)
        return {
            "matched": len(thread_ids),
            "result_size_estimate": int(listing.get("resultSizeEstimate", len(items))),
            "unanswered": len(items),
            "query": query,
            "items": items,
        }

    async def _read(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        message_ids = arguments.get("message_ids", [])
        if not isinstance(message_ids, list) or len(message_ids) > 25 or any(
            not isinstance(message_id, str) or not message_id or len(message_id) > 200
            for message_id in message_ids
        ):
            raise ConnectorError("email.read message_ids must contain at most 25 IDs")
        items = [
            self._normalize_message(
                await self._message_metadata(client, access_token, message_id)
            )
            for message_id in message_ids
        ]
        return {"read": len(items), "message_ids": message_ids, "items": items}

    async def _message_metadata(
        self, client: httpx.AsyncClient, access_token: str, message_id: str
    ) -> dict[str, Any]:
        params = [("format", "metadata")]
        params.extend(
            ("metadataHeaders", header)
            for header in ("Subject", "From", "Date", "Message-ID", "In-Reply-To")
        )
        return await self._request_json(
            client,
            f"/users/me/messages/{quote(message_id, safe='')}",
            access_token,
            params=params,
        )

    async def _thread_metadata(
        self, client: httpx.AsyncClient, access_token: str, thread_id: str
    ) -> dict[str, Any]:
        params = [("format", "metadata")]
        params.extend(
            ("metadataHeaders", header)
            for header in ("Subject", "From", "Date", "Message-ID", "In-Reply-To")
        )
        return await self._request_json(
            client,
            f"/users/me/threads/{quote(thread_id, safe='')}",
            access_token,
            params=params,
        )

    @staticmethod
    def _normalize_message(message: dict[str, Any]) -> dict[str, Any]:
        payload = message.get("payload")
        raw_headers = payload.get("headers", []) if isinstance(payload, dict) else []
        headers = {
            str(item.get("name", "")).lower(): GmailConnector._decode_header(
                str(item.get("value", ""))
            )
            for item in raw_headers
            if isinstance(item, dict)
        }
        labels = {
            str(label) for label in message.get("labelIds", []) if isinstance(label, str)
        }
        priority = (
            "high"
            if labels.intersection({"IMPORTANT", "STARRED"})
            else "medium"
            if "UNREAD" in labels
            else "normal"
        )
        return {
            "id": str(message.get("id", "")),
            "thread_id": str(message.get("threadId", "")),
            "subject": headers.get("subject") or "(no subject)",
            "sender": headers.get("from", ""),
            "date": headers.get("date", ""),
            "snippet": str(message.get("snippet", ""))[:500],
            "priority": priority,
            "unread": "UNREAD" in labels,
            "needs_attention": True,
        }

    @staticmethod
    def _decode_header(value: str) -> str:
        try:
            return str(make_header(decode_header(value)))[:500]
        except (LookupError, UnicodeError):
            return value[:500]


class MockCanvasConnector(Connector):
    manifest = ConnectorManifest(
        name="canvas",
        category="education",
        authentication_type="mock_token",
        capabilities=["education.assignments.list"],
        required_scopes=["canvas.read"],
        risk_metadata={"student_data": "high", "mock": True},
    )

    async def health_check(self) -> dict[str, Any]:
        return {"status": "available", "mode": "mock"}

    async def execute(
        self, tool_name: str, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        if tool_name != "education.assignments.list":
            raise ConnectorError(f"canvas does not support tool {tool_name}")
        return {
            "count": 4,
            "items": [
                {"title": "Algorithms HW4", "due": "Thursday"},
                {"title": "OB Exam", "due": "Friday"},
                {"title": "Research draft", "due": "Sunday"},
                {"title": "Studio critique", "due": "Monday"},
            ],
        }


class MockPlaidConnector(Connector):
    manifest = ConnectorManifest(
        name="plaid",
        category="finance",
        authentication_type="mock_plaid_link",
        capabilities=["finance.transactions.list"],
        required_scopes=["transactions:read"],
        risk_metadata={"financial_data": "high", "read_only": True, "mock": True},
    )

    async def health_check(self) -> dict[str, Any]:
        return {"status": "available", "mode": "mock", "read_only": True}

    async def execute(
        self, tool_name: str, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        if tool_name != "finance.transactions.list":
            raise ConnectorError(f"plaid does not support tool {tool_name}")
        return {
            "transaction_count": 24,
            "total_spend": 614.0,
            "currency": "USD",
            "categories": {
                "food": 184.0,
                "transportation": 96.0,
                "shopping": 141.0,
                "subscriptions": 43.0,
                "other": 150.0,
            },
            "notable": ["Restaurant spending increased compared with last week."],
        }


class DiscoveredConnector(Connector):
    """A registered connector definition that still needs an auth/execution adapter."""

    def __init__(self, manifest: ConnectorManifest) -> None:
        self.manifest = manifest

    async def health_check(self) -> dict[str, Any]:
        return {
            "status": "adapter_required",
            "mode": self.manifest.transport,
            "execution_ready": False,
        }

    async def execute(
        self, tool_name: str, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        raise ConnectorError(
            f"connector {self.manifest.name} is discovered but has no authorized execution adapter"
        )


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}
        self.reset()

    def reset(self) -> None:
        self._connectors = {}
        for connector in (GmailConnector(), MockCanvasConnector(), MockPlaidConnector()):
            self.register(connector)

    def register(self, connector: Connector, *, replace: bool = False) -> None:
        if connector.manifest.name in self._connectors and not replace:
            raise ValueError(f"connector already registered: {connector.manifest.name}")
        self._connectors[connector.manifest.name] = connector

    def get(self, provider: str) -> Connector | None:
        return self._connectors.get(provider)

    def require(self, provider: str) -> Connector:
        connector = self.get(provider)
        if connector is None:
            raise ConnectorError(f"unknown connector provider: {provider}")
        return connector

    def list(self) -> list[Connector]:
        return list(self._connectors.values())

    def find_by_capability(self, capability: str) -> Connector | None:
        return next(
            (
                connector
                for connector in self._connectors.values()
                if capability in connector.manifest.capabilities
            ),
            None,
        )


connector_registry = ConnectorRegistry()
