"""In-memory MCP client.

mcp 1.30 dropped mcp.shared.memory. This is that transport: a ClientSession
speaking JSON-RPC to the FastMCP server in this process, with no socket.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from mcp.client.session import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage


@asynccontextmanager
async def connect(server: FastMCP) -> AsyncIterator[ClientSession]:
    lowlevel = server._mcp_server
    client_to_server_send, client_to_server_receive = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](20)
    server_to_client_send, server_to_client_receive = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](20)

    async with (
        client_to_server_send,
        client_to_server_receive,
        server_to_client_send,
        server_to_client_receive,
    ):

        async def run_server() -> None:
            await lowlevel.run(
                client_to_server_receive,
                server_to_client_send,
                lowlevel.create_initialization_options(),
                raise_exceptions=False,
            )

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run_server)
            try:
                async with ClientSession(
                    server_to_client_receive, client_to_server_send
                ) as session:
                    await session.initialize()
                    yield session
            finally:
                tasks.cancel_scope.cancel()
