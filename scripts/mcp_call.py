"""Fallback Trajano buyer MCP client for the class demo.

    uv run python scripts/mcp_call.py <tool> '<json args>'

Reads demo_buyer.json, calls the tool on the local server, prints the JSON body.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from agents.client import TrajanoClient

ROOT = Path(__file__).resolve().parent.parent
BUYER_FILE = ROOT / "demo_buyer.json"
BASE_URL = "http://127.0.0.1:8000"


async def _call(tool: str, arguments: dict) -> dict:
    buyer = json.loads(BUYER_FILE.read_text())
    async with TrajanoClient(BASE_URL, buyer["api_key"], role="buyer") as client:
        return await client.body(tool, arguments)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcp_call")
    parser.add_argument("tool")
    parser.add_argument("args_json", nargs="?", default="{}")
    args = parser.parse_args(argv)
    try:
        arguments = json.loads(args.args_json)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON args: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not isinstance(arguments, dict):
        print("args must be a JSON object", file=sys.stderr)
        raise SystemExit(2)
    if not BUYER_FILE.is_file():
        print(f"missing {BUYER_FILE.name}; start scripts/demo_world.py first", file=sys.stderr)
        raise SystemExit(1)
    result = asyncio.run(_call(args.tool, arguments))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
