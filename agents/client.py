"""Thin MCP client for our own agents, plus inbox paging.

Connects to /mcp/buyer or /mcp/seller over streamable HTTP with the agent's
API key as a bearer token, exactly as a third-party agent would.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import date, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Literal, Self

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


class ToolCallError(Exception):
    def __init__(self, tool: str, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(f"{tool}: {code}: {message}")
        self.tool = tool
        self.code = code
        self.message = message
        self.retryable = retryable


class TrajanoClient:
    """Use as an async context manager. url is the server root, for example http://127.0.0.1:8000."""

    def __init__(
        self,
        url: str,
        api_key: str,
        role: Literal["buyer", "seller"] = "buyer",
        *,
        timeout_s: float = 30,
        retries: int = 3,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.role = role
        self.endpoint = f"{url.rstrip('/')}/mcp/{role}/"
        self.timeout_s = timeout_s
        self.retries = retries
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> Self:
        stack = AsyncExitStack()
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=httpx.Timeout(self.timeout_s),
            )
        )
        read, write, _session_id = await stack.enter_async_context(
            streamable_http_client(self.endpoint, http_client=http)
        )
        self._session = await stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        self._stack = stack
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """Call a tool and return its body. Raises ToolCallError on a structured error.

        None arguments are left out. A retryable error is retried with the same
        arguments, so the same idempotency key, after a short backoff.
        """
        if self._session is None:
            raise RuntimeError("use TrajanoClient as an async context manager")
        payload = {name: _jsonable(value) for name, value in arguments.items() if value is not None}
        for attempt in range(self.retries + 1):
            result = await self._session.call_tool(tool, payload)
            body = _body(result)
            if result.isError:
                raise ToolCallError(tool, "VALIDATION", body.get("text", "tool call failed"))
            error = body.get("error")
            if error is None:
                return body
            failure = ToolCallError(
                tool, error["code"], error["message"], bool(error.get("retryable"))
            )
            if not failure.retryable or attempt == self.retries:
                raise failure
            await asyncio.sleep(0.05 * 2**attempt)
        raise AssertionError("unreachable")

    async def body(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One call, no retry, errors returned in the body. For an LLM that reads the error."""
        if self._session is None:
            raise RuntimeError("use TrajanoClient as an async context manager")
        result = await self._session.call_tool(tool, _jsonable(arguments))
        return _body(result)

    async def tools(self) -> list[dict[str, Any]]:
        """name, description, and JSON input schema of every tool on this server."""
        if self._session is None:
            raise RuntimeError("use TrajanoClient as an async context manager")
        listed = await self._session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
            for tool in listed.tools
        ]

    async def get_inbox(self, since: str | None = None) -> list[dict[str, Any]]:
        return (await self.call("get_inbox", since=since))["threads"]

    async def inbox(self) -> AsyncIterator[dict[str, Any]]:
        """Every thread awaiting us, oldest change first, following the cursor across pages."""
        since: str | None = None
        while True:
            page = await self.get_inbox(since)
            if not page:
                return
            for item in page:
                yield item
            since = page[-1]["cursor"]

    async def get_thread(self, thread_id: int) -> dict[str, Any]:
        return (await self.call("get_thread", thread_id=thread_id))["thread"]


def _body(result: Any) -> dict[str, Any]:
    if result.structuredContent:
        return result.structuredContent
    text = result.content[0].text if result.content else ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return parsed if isinstance(parsed, dict) else {"text": text}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value
