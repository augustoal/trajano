import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.factories import make_buyer, make_listing, make_seller
from tests.mcp_memory import connect
from trajano.app import create_app
from trajano.interfaces.mcp_buyer import buyer_mcp
from trajano.interfaces.mcp_seller import seller_mcp

BUYER_TOOLS = {
    "search_listings",
    "get_listing",
    "ask_question",
    "request_quote",
    "counter_offer",
    "request_approval",
    "get_approval",
    "accept_offer",
    "withdraw",
    "get_inbox",
    "get_thread",
    "get_order",
}
SELLER_TOOLS = {
    "create_store",
    "create_listing",
    "update_listing",
    "get_listing",
    "get_inbox",
    "get_thread",
    "answer_question",
    "make_offer",
    "decline",
    "confirm_order",
    "reject_order",
    "mark_shipped",
    "mark_delivered",
    "export_order_csv",
}


def test_each_server_lists_only_its_tools() -> None:
    assert asyncio.run(_names(buyer_mcp)) == BUYER_TOOLS
    assert asyncio.run(_names(seller_mcp)) == SELLER_TOOLS


def test_get_listing_requires_a_buyer_key(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from trajano.interfaces import support

    _seller, _seller_key, store = make_seller(session)
    listing = make_listing(session, store)
    session.commit()

    missing = asyncio.run(_call(buyer_mcp, "get_listing", {"listing_id": listing.id}))
    assert _body(missing)["error"]["code"] == "UNAUTHORIZED"

    _buyer, key = make_buyer(session)
    session.commit()
    monkeypatch.setattr(support, "current_api_key", lambda: key)
    found = asyncio.run(_call(buyer_mcp, "get_listing", {"listing_id": listing.id}))
    body = _body(found)
    assert body["error"] is None
    assert body["listing"]["id"] == listing.id
    assert body["listing"]["diameter"] == "M8"
    assert "price_floor" not in body["listing"]
    assert "buy 2000" in body["listing"]["seller_notes"]


def test_a_seller_cannot_use_buyer_tools(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from trajano.interfaces import support

    _seller, key, store = make_seller(session)
    listing = make_listing(session, store)
    session.commit()
    monkeypatch.setattr(support, "current_api_key", lambda: key)
    result = asyncio.run(_call(buyer_mcp, "get_listing", {"listing_id": listing.id}))
    body = _body(result)
    assert body["error"]["code"] == "FORBIDDEN"


def test_tools_reject_missing_arguments() -> None:
    result = asyncio.run(_call(buyer_mcp, "get_listing", {}))
    assert result.isError


def test_a_missing_order_is_a_structured_error(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from trajano.interfaces import support

    _buyer, key = make_buyer(session)
    session.commit()
    monkeypatch.setattr(support, "current_api_key", lambda: key)
    result = asyncio.run(_call(buyer_mcp, "get_order", {"order_id": 1}))
    body = _body(result)
    assert body["error"]["code"] == "NOT_FOUND"
    assert result.isError is False


def test_http_get_listing_uses_the_bearer_token(session) -> None:
    _seller, _seller_key, store = make_seller(session)
    listing = make_listing(session, store)
    _buyer, key = make_buyer(session)
    session.commit()

    app = create_app()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_until_healthy(port)
        body = asyncio.run(_http_get_listing(port, key, listing.id or 0))
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    assert body["error"] is None
    assert body["listing"]["diameter"] == "M8"
    assert "price_floor" not in body["listing"]


async def _names(server) -> set[str]:
    async with connect(server) as client:
        listed = await client.list_tools()
        return {tool.name for tool in listed.tools}


async def _call(server, name: str, arguments: dict):
    async with connect(server) as client:
        return await client.call_tool(name, arguments)


def _body(result) -> dict:
    if result.structuredContent:
        return result.structuredContent
    return json.loads(result.content[0].text)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_healthy(port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.2)
        except httpx.HTTPError:
            time.sleep(0.05)
            continue
        if response.status_code == 200:
            return
    raise AssertionError("server did not start")


async def _http_get_listing(port: int, key: str, listing_id: int) -> dict:
    async with (
        httpx.AsyncClient(
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(10),
        ) as http_client,
        streamable_http_client(
            f"http://127.0.0.1:{port}/mcp/buyer/",
            http_client=http_client,
        ) as (read, write, _session_id),
        ClientSession(read, write) as mcp,
    ):
        await mcp.initialize()
        result = await mcp.call_tool("get_listing", {"listing_id": listing_id})
        return _body(result)
