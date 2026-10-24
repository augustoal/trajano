"""Local demo world: frozen clock, seeded catalog, buyer, rule sellers, MCP log.

    uv run python scripts/demo_world.py

Leaves demo.db, demo_buyer.json, and .cursor/mcp.json for the buyer agent.
Keeps running until Ctrl+C. Do not commit those three artifacts.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from sqlmodel import Session, SQLModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agents.rule_seller import all_rules
from agents.rule_seller import run as run_seller
from data.seed import load_catalog
from data.world import provision_buyer
from trajano.app import create_app
from trajano.config import reset_settings
from trajano.db import freeze_clock, get_engine, init_db, reset_engine

ROOT = Path(__file__).resolve().parent.parent
DB_URL = "sqlite:///./demo.db"
BUYER_FILE = ROOT / "demo_buyer.json"
MCP_FILE = ROOT / ".cursor" / "mcp.json"
BASE_URL = "http://127.0.0.1:8000"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

# ANSI
_BUYER = "\033[96m"  # cyan
_SELLER = "\033[93m"  # yellow
_ERR = "\033[91m"
_RESET = "\033[0m"
_DIM = "\033[2m"


def _setup_database() -> dict[str, str]:
    os.environ["TRAJANO_DATABASE_URL"] = DB_URL
    reset_settings()
    reset_engine()
    import trajano.models  # noqa: F401  (register metadata)

    engine = get_engine()
    SQLModel.metadata.drop_all(engine)
    init_db()
    freeze_clock(NOW)

    with Session(engine, expire_on_commit=False) as session:
        keys = load_catalog(session)
        buyer = provision_buyer(
            session,
            {
                "max_unit_price": "0.6875",
                "max_total": "223.22",
                "max_qty": 600,
            },
            NOW + timedelta(days=30),
        )
        session.commit()

    BUYER_FILE.write_text(json.dumps(buyer, indent=2) + "\n")
    MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MCP_FILE.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "trajano-buyer": {
                        "url": f"{BASE_URL}/mcp/buyer/",
                        "headers": {"Authorization": f"Bearer {buyer['api_key']}"},
                    }
                }
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {BUYER_FILE.relative_to(ROOT)} and {MCP_FILE.relative_to(ROOT)}")
    print(f"buyer agent_id={buyer['agent_id']}  sellers={len(keys)}")
    return keys


def _fmt_args(tool: str, args: dict[str, Any]) -> str:
    if tool == "search_listings":
        keys = ("qty", "ship_to", "diameter", "length_mm", "material")
        return " ".join(f"{key}={args[key]}" for key in keys if key in args)
    if tool == "make_offer":
        bits = []
        if args.get("thread_id") is not None:
            bits.append(f"thread={args['thread_id']}")
        if args.get("unit_price") is not None:
            bits.append(f"unit={args['unit_price']}")
        if args.get("total") is not None:
            bits.append(f"total={args['total']}")
        return " ".join(bits)
    if tool == "counter_offer":
        bits = []
        if args.get("offer_id") is not None:
            bits.append(f"offer={args['offer_id']}")
        if args.get("unit_price") is not None:
            bits.append(f"unit={args['unit_price']}")
        if args.get("qty") is not None:
            bits.append(f"qty={args['qty']}")
        return " ".join(bits)
    if tool == "accept_offer":
        return f"offer={args.get('offer_id')} mandate=signed"
    parts: list[str] = []
    order = (
        "listing_id",
        "thread_id",
        "offer_id",
        "order_id",
        "qty",
        "unit_price",
        "ship_to",
        "need_by",
        "reason_code",
        "awaiting",
    )
    for key in order:
        if key in args and args[key] is not None:
            label = "listing" if key == "listing_id" else key.removesuffix("_id")
            if key.endswith("_id") and key != "listing_id":
                label = key[: -len("_id")]
            parts.append(f"{label}={args[key]}")
    return " ".join(parts) if parts else _DIM + "{}" + _RESET


def _tool_body(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the structured tool result out of an MCP JSON-RPC response."""
    if "error" in payload and "result" not in payload:
        return {"error": payload["error"]}
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    parsed = json.loads(item.get("text") or "")
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return result if "error" in result or "hits" in result or "thread" in result else None


def _fmt_result(tool: str, body: dict[str, Any] | None) -> str:
    if body is None:
        return _DIM + "ok" + _RESET
    err = body.get("error")
    if isinstance(err, dict):
        return f"{_ERR}{err.get('code', 'ERROR')}{_RESET} {err.get('message', '')}".rstrip()
    if tool == "search_listings":
        hits = body.get("hits") or []
        excluded = body.get("excluded") or []
        return f"{len(hits)} hits, {len(excluded)} excluded"
    if tool == "get_inbox":
        threads = body.get("threads") or []
        if not threads:
            return ""
        return f"{len(threads)} thread(s)"
    if tool == "get_thread":
        thread = body.get("thread") or {}
        status = thread.get("status", "?")
        n = len(thread.get("messages") or [])
        return f"{status} ({n} messages)"
    if tool == "get_listing":
        listing = body.get("listing") or {}
        return f"listing {listing.get('id', '?')} {listing.get('sku', '')}".rstrip()
    if tool == "get_order":
        order = body.get("order") or {}
        return f"order {order.get('id', '?')} {order.get('status', '')}".rstrip()
    status = body.get("status")
    bits: list[str] = []
    if status:
        bits.append(str(status))
    if body.get("thread_id") is not None:
        bits.append(f"thread {body['thread_id']}")
    if body.get("order_id") is not None:
        bits.append(f"order {body['order_id']}")
    if body.get("offer_id") is not None:
        bits.append(f"offer {body['offer_id']}")
    if tool == "make_offer":
        # seller offer args already logged; status is enough
        pass
    return " ".join(bits) if bits else _DIM + "ok" + _RESET


def _should_skip(side: str, tool: str, body: dict[str, Any] | None) -> bool:
    if side == "SELLER" and tool == "get_inbox":
        threads = (body or {}).get("threads") or []
        if not threads:
            return True
    return False


class McpCallLog:
    """ASGI wrapper: one colored line per MCP tools/call."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if not (path.startswith("/mcp/buyer") or path.startswith("/mcp/seller")):
            await self.app(scope, receive, send)
            return

        side = "BUYER" if "/buyer" in path else "SELLER"
        chunks: list[bytes] = []

        async def read() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if body:
                    chunks.append(body)
            return message

        response_chunks: list[bytes] = []
        status_code = 0

        async def write(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 0))
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    response_chunks.append(body)
            await send(message)

        await self.app(scope, read, write)

        raw = b"".join(chunks)
        if not raw:
            return
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(request, dict) or request.get("method") != "tools/call":
            return
        params = request.get("params") or {}
        tool = str(params.get("name") or "?")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}

        body: dict[str, Any] | None = None
        try:
            response = json.loads(b"".join(response_chunks) or b"null")
        except json.JSONDecodeError:
            response = None
        if isinstance(response, dict):
            body = _tool_body(response)
        elif status_code >= 400:
            body = {"error": {"code": f"HTTP_{status_code}", "message": ""}}

        if _should_skip(side, tool, body):
            return

        color = _BUYER if side == "BUYER" else _SELLER
        summary = _fmt_result(tool, body)
        if summary == "" and tool == "get_inbox":
            return
        arg_text = _fmt_args(tool, args)
        line = f"{color}{side:<6}{_RESET} {tool} {arg_text} -> {summary}"
        print(line, flush=True)


def _quiet_logs() -> None:
    import logging

    for name in (
        "",
        "httpx",
        "httpcore",
        "mcp",
        "mcp.server",
        "mcp.server.lowlevel",
        "mcp.server.lowlevel.server",
        "mcp.server.streamable_http",
        "mcp.client",
        "mcp.client.streamable_http",
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "agents.rule_seller",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def _start_server() -> uvicorn.Server:
    _quiet_logs()
    app = McpCallLog(create_app())
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="trajano-demo")
    thread.start()
    deadline = time.monotonic() + 15
    while True:
        try:
            if httpx.get(f"{BASE_URL}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("demo server did not become healthy on :8000")
        time.sleep(0.05)
    print(f"server ready at {BASE_URL}  (clock frozen at {NOW.isoformat()})")
    return server


async def _run_sellers(keys: dict[str, str]) -> None:
    stop = asyncio.Event()
    rules = all_rules()
    missing = [name for name in keys if name not in rules]
    if missing:
        raise RuntimeError(f"no YAML rules for sellers: {missing}")
    tasks = [
        asyncio.create_task(
            run_seller(BASE_URL, key, rules[name], stop=stop, poll_seconds=0.2),
            name=f"seller:{name}",
        )
        for name, key in keys.items()
    ]
    print(f"rule sellers running: {', '.join(sorted(keys))}", flush=True)
    print(_DIM + "MCP call log below. Ctrl+C to stop." + _RESET, flush=True)
    try:
        await asyncio.Future()
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    os.chdir(ROOT)
    _quiet_logs()
    keys = _setup_database()
    server = _start_server()
    try:
        asyncio.run(_run_sellers(keys))
    except KeyboardInterrupt:
        print("\nshutting down demo world", flush=True)
    finally:
        server.should_exit = True


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"{_ERR}demo_world failed: {exc}{_RESET}", file=sys.stderr)
        raise
