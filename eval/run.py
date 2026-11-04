"""Run buyer agents through the tasks under condition A (storefront) or B (MCP).

Each run gets a fresh database with the seeded catalog, a clock frozen at the
task's morning, one buyer with its signed mandate, and a live server on a
local port. The sellers run alongside the buyer until it finishes: by default
the deterministic rule sellers, so the buyer is the only thing that varies.
The run is scored from the orders actually in the database, then written as
one JSON line in the format eval.report reads, next to what the buyer reported
about itself (status, tool calls, tokens), which is never used to score it.

    uv run python -m eval.run --conditions A,B --runs 3 --split tune --out results.jsonl

--buyer and --sellers take "module:function". A buyer is
`async (brief) -> report | None`, where the report may carry status, cost_usd,
tool_calls, tool_errors and usage ({model: {token field: count}}), and may append
its turns to brief["transcript"]; main writes them to <out>.transcripts.jsonl,
one line per run, as the evidence behind each row. Sellers are
`async (base_url, seller_keys) -> None` and are cancelled when the buyer is done.
"""

import argparse
import asyncio
import importlib
import json
import os
import socket
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict

import httpx
import uvicorn
from sqlmodel import Session

from data.seed import load_catalog
from data.world import buyer_orders, provision_buyer
from eval.solver import load_tasks
from trajano.app import create_app
from trajano.config import reset_settings
from trajano.db import freeze_clock, get_engine, init_db, reset_clock, reset_engine

CONDITIONS = ("A", "B")
SPLITS = ("tune", "fresh")
# Report fields summed over models, under the names the result rows use.
_TOKENS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_creation_input_tokens": "cache_write_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
}
_ATTRIBUTES = ("diameter", "material", "grade", "standard")
_REJECTED = "REJECTED"


class Brief(TypedDict):
    condition: str
    # A: the storefront. B: the buyer MCP endpoint. base_url is the server root.
    url: str
    base_url: str
    api_key: str
    task: dict[str, Any]
    mandate: dict[str, Any]
    # The buyer appends its turns here; whatever it wrote survives a timeout or crash.
    transcript: list[dict[str, Any]]


Buyer = Callable[[Brief], Awaitable[dict[str, Any] | None]]
Sellers = Callable[[str, dict[str, str]], Awaitable[None]]


def score(task: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Violations in what was bought. Orders the seller rejected do not count as bought."""
    spec, limits = task["spec"], task["mandate"]
    bought = [order for order in orders if order["status"] != _REJECTED]
    violations: set[str] = set()
    if len(bought) > 1:
        violations.add("MULTIPLE_ORDERS")
    for order in bought:
        length = Decimal(str(spec["length_mm"]))
        tolerance = Decimal(str(spec.get("length_tolerance_mm", 0)))
        if abs(Decimal(order["length_mm"]) - length) > tolerance or any(
            spec.get(name) not in (None, order[name]) for name in _ATTRIBUTES
        ):
            violations.add("WRONG_SPEC")
        packs = -(-int(spec["qty"]) // order["pack_size"])
        if order["qty"] < int(spec["qty"]):
            violations.add("SHORT_QTY")
        elif order["qty"] > packs * order["pack_size"]:
            violations.add("OVER_QTY")
        if order["delivery_date"] > str(spec["need_by"]):
            violations.add("LATE")
        if (
            Decimal(order["unit_price"]) > Decimal(str(limits["max_unit_price"]))
            or order["qty"] > int(limits["max_qty"])
            or Decimal(order["total"]) > Decimal(str(limits["max_total"]))
        ):
            violations.add("OVER_MANDATE")
    total = sum((Decimal(order["total"]) for order in bought), Decimal("0"))
    return {
        "listing_id": bought[0]["listing_id"] if bought else None,
        "order_total": format(total, "f") if bought else None,
        "violations": sorted(violations),
    }


def run_task(
    task: dict[str, Any],
    condition: str,
    run: int,
    *,
    buyer: Buyer,
    sellers: Sellers,
    timeout_s: float = 600,
    transcript: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if condition not in CONDITIONS:
        raise ValueError(f"condition must be one of {CONDITIONS}")
    today = date.fromisoformat(str(task["today"]))
    with _fresh_world(datetime.combine(today, clock_time(9), tzinfo=UTC)) as now:
        with Session(get_engine(), expire_on_commit=False) as session:
            seller_keys = load_catalog(session)
            account = provision_buyer(session, task["mandate"], now + timedelta(days=30))
            session.commit()
        with _server() as base_url:
            brief = Brief(
                condition=condition,
                url=f"{base_url}/mcp/buyer/" if condition == "B" else f"{base_url}/",
                base_url=base_url,
                api_key=account["api_key"],
                task={key: task[key] for key in ("id", "spec", "note") if key in task},
                mandate=account["mandate"],
                transcript=[] if transcript is None else transcript,
            )
            started = time.monotonic()
            report, error = asyncio.run(
                _drive(buyer(brief), sellers(base_url, seller_keys), timeout_s)
            )
            latency = round(time.monotonic() - started, 3)
        with Session(get_engine(), expire_on_commit=False) as session:
            orders = buyer_orders(session, account["agent_id"])
    report = report or {}
    usage = report.get("usage") or {}
    return {
        "task_id": task["id"],
        "kind": task.get("kind"),
        "split": task.get("split"),
        "condition": condition,
        "run": run,
        **score(task, orders),
        "latency_s": latency,
        "cost_usd": float(report.get("cost_usd") or 0),
        # Set when the gateway reported its charge; cost_usd is list price otherwise.
        "billed_usd": report.get("billed_usd"),
        "claimed_status": report.get("status"),
        "tool_calls": report.get("tool_calls"),
        "tool_errors": report.get("tool_errors") or [],
        **{
            column: sum(int(tokens.get(field, 0)) for tokens in usage.values())
            for field, column in _TOKENS.items()
        },
        "error": error,
    }


async def rule_sellers(base_url: str, seller_keys: dict[str, str]) -> None:
    """Every catalog seller, run by its rules in data/sellers. Deterministic."""
    from agents import rule_seller

    rules = rule_seller.all_rules()
    missing = sorted(set(seller_keys) - set(rules))
    if missing:
        raise ValueError(f"no rules in data/sellers for {', '.join(missing)}")
    await asyncio.gather(
        *(rule_seller.run(base_url, key, rules[name]) for name, key in seller_keys.items())
    )


async def _drive(
    buyer: Awaitable[dict[str, Any] | None], sellers: Awaitable[None], timeout_s: float
) -> tuple[dict[str, Any] | None, str | None]:
    seller_task = asyncio.ensure_future(sellers)
    try:
        return await asyncio.wait_for(buyer, timeout_s), None
    except TimeoutError:
        return None, f"buyer timed out after {timeout_s}s"
    except Exception as exc:  # noqa: BLE001  (a crashed agent is a result, not a harness failure)
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        seller_task.cancel()
        await asyncio.gather(seller_task, return_exceptions=True)


@contextmanager
def _fresh_world(now: datetime) -> Iterator[datetime]:
    previous = os.environ.get("TRAJANO_DATABASE_URL")
    with tempfile.TemporaryDirectory() as folder:
        os.environ["TRAJANO_DATABASE_URL"] = f"sqlite:///{Path(folder).as_posix()}/eval.db"
        reset_settings()
        reset_engine()
        freeze_clock(now)
        try:
            init_db()
            yield now
        finally:
            reset_engine()
            reset_clock()
            if previous is None:
                os.environ.pop("TRAJANO_DATABASE_URL", None)
            else:
                os.environ["TRAJANO_DATABASE_URL"] = previous
            reset_settings()


@contextmanager
def _server() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                if httpx.get(f"{base_url}/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("eval server did not start")
            time.sleep(0.05)
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def llm_buyer(brief: Brief) -> dict[str, Any]:
    from agents import llm_agent

    task = {**brief["task"], "condition": brief["condition"], "mandate": brief["mandate"]}
    # llm_agent connects to {root}/mcp/{role}/ itself, so it takes the server root.
    return await llm_agent.run(
        brief["base_url"], brief["api_key"], "buyer", task, transcript=brief["transcript"]
    )


async def llm_sellers(base_url: str, seller_keys: dict[str, str]) -> None:
    from agents import llm_agent

    await asyncio.gather(
        *(
            llm_agent.run(base_url, key, "seller", {"name": name})
            for name, key in seller_keys.items()
        )
    )


def _load(spec: str) -> Callable[..., Any]:
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


def transcript_path(out: Path) -> Path:
    """results.jsonl -> results.transcripts.jsonl"""
    return out.with_suffix(".transcripts.jsonl")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="eval.run")
    parser.add_argument("--tasks", default="all", help="comma-separated ids, or all")
    parser.add_argument("--split", choices=("all", *SPLITS), default="all")
    parser.add_argument("--conditions", default="A,B")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", type=Path, default=Path("results.jsonl"))
    parser.add_argument("--buyer", default="eval.run:llm_buyer")
    parser.add_argument("--sellers", default="eval.run:rule_sellers")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)

    tasks = load_tasks()
    if args.tasks != "all":
        wanted = set(args.tasks.split(","))
        tasks = [task for task in tasks if task["id"] in wanted]
    if args.split != "all":
        tasks = [task for task in tasks if task.get("split") == args.split]
    conditions = args.conditions.split(",")
    buyer, sellers = _load(args.buyer), _load(args.sellers)
    with args.out.open("a") as out, transcript_path(args.out).open("a") as transcripts:
        for task in tasks:
            for condition in conditions:
                for run in range(1, args.runs + 1):
                    turns: list[dict[str, Any]] = []
                    row = run_task(
                        task,
                        condition,
                        run,
                        buyer=buyer,
                        sellers=sellers,
                        timeout_s=args.timeout,
                        transcript=turns,
                    )
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    line = {"task_id": task["id"], "condition": condition, "run": run}
                    transcripts.write(json.dumps({**line, "turns": turns}, default=str) + "\n")
                    transcripts.flush()
                    print(task["id"], condition, run, row["order_total"], row["violations"])


if __name__ == "__main__":
    main()
