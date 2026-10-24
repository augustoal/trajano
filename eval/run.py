"""Run buyer agents through the tasks under condition A (storefront) or B (MCP).

Each run gets a fresh database with the seeded catalog, a clock frozen at the
task's morning, one buyer with its signed mandate, and a live server on a
local port. The sellers run alongside the buyer until it finishes. The run is
scored from the orders actually in the database, then written as one JSON
line in the format eval.report reads.

    uv run python -m eval.run --conditions B --runs 3 --out results.jsonl

--buyer and --sellers take "module:function". A buyer is
`async (brief) -> {"cost_usd": float}`; sellers are
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
_ATTRIBUTES = ("diameter", "material", "grade", "standard")
_REJECTED = "REJECTED"


class Brief(TypedDict):
    condition: str
    url: str
    api_key: str
    task: dict[str, Any]
    mandate: dict[str, Any]


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
                api_key=account["api_key"],
                task={key: task[key] for key in ("id", "spec", "note") if key in task},
                mandate=account["mandate"],
            )
            started = time.monotonic()
            report, error = asyncio.run(
                _drive(buyer(brief), sellers(base_url, seller_keys), timeout_s)
            )
            latency = round(time.monotonic() - started, 3)
        with Session(get_engine(), expire_on_commit=False) as session:
            orders = buyer_orders(session, account["agent_id"])
    return {
        "task_id": task["id"],
        "condition": condition,
        "run": run,
        **score(task, orders),
        "latency_s": latency,
        "cost_usd": float((report or {}).get("cost_usd") or 0),
        "error": error,
    }


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
    return await llm_agent.run(brief["url"], brief["api_key"], "buyer", task)


async def llm_sellers(base_url: str, seller_keys: dict[str, str]) -> None:
    from agents import llm_agent

    url = f"{base_url}/mcp/seller/"
    await asyncio.gather(
        *(llm_agent.run(url, key, "seller", {"name": name}) for name, key in seller_keys.items())
    )


def _load(spec: str) -> Callable[..., Any]:
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="eval.run")
    parser.add_argument("--tasks", default="all", help="comma-separated ids, or all")
    parser.add_argument("--conditions", default="A,B")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", type=Path, default=Path("results.jsonl"))
    parser.add_argument("--buyer", default="eval.run:llm_buyer")
    parser.add_argument("--sellers", default="eval.run:llm_sellers")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)

    tasks = load_tasks()
    if args.tasks != "all":
        wanted = set(args.tasks.split(","))
        tasks = [task for task in tasks if task["id"] in wanted]
    conditions = args.conditions.split(",")
    buyer, sellers = _load(args.buyer), _load(args.sellers)
    with args.out.open("a") as out:
        for task in tasks:
            for condition in conditions:
                for run in range(1, args.runs + 1):
                    row = run_task(
                        task, condition, run, buyer=buyer, sellers=sellers, timeout_s=args.timeout
                    )
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    print(task["id"], condition, run, row["order_total"], row["violations"])


if __name__ == "__main__":
    main()
