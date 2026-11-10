"""Headless Trajano world for an outside harness, such as a Nanda Town Lab run.

    uv run python -m scripts.town_world --task t01 --state state.json --audit audit.json

The world is the one eval.run builds: a fresh database, the clock frozen at the
task's morning, the seeded catalog, one buyer with its signed mandate, the server
on a free local port, and the five rule sellers. Once it is up, state.json holds
what the harness needs to act as the buyer over MCP. On SIGTERM or SIGINT the
script writes audit.json, the buyer's orders and threads as the database records
them, scored against the solver's answer key, and exits. The harness checks its
buyer's account of the deal against that file, not against the buyer itself.
"""

import argparse
import asyncio
import json
import os
import signal
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from data.seed import load_catalog
from data.world import buyer_orders, provision_buyer
from eval.run import _fresh_world, _server, rule_sellers, score
from eval.solver import load_tasks, solve
from trajano.db import get_engine
from trajano.models import Thread

_LIMITS = ("max_unit_price", "max_total", "max_qty")


def task_with_limits(task_id: str, overrides: dict[str, Any]) -> dict[str, Any]:
    """The eval task, with any mandate limit replaced. The answer key follows suit."""
    task = next((item for item in load_tasks() if item["id"] == task_id), None)
    if task is None:
        raise SystemExit(f"no task {task_id} in eval/tasks.yaml")
    limits = {**task["mandate"], **{k: v for k, v in overrides.items() if v is not None}}
    return {**task, "mandate": limits}


def audit(task: dict[str, Any], agent_id: int) -> dict[str, Any]:
    with Session(get_engine(), expire_on_commit=False) as session:
        orders = buyer_orders(session, agent_id)
        threads = session.exec(
            select(Thread).where(Thread.buyer_agent_id == agent_id).order_by(Thread.id)
        ).all()
    return {
        "task_id": task["id"],
        "orders": orders,
        "threads": [
            {"thread_id": t.id, "listing_id": t.listing_id, "status": t.status.value}
            for t in threads
        ],
        **score(task, orders),
        "answer_key": solve(task)["best"],
    }


def _write(path: Path, content: dict[str, Any]) -> None:
    # The harness polls for the file, so it must never see it half written.
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(content, indent=2, default=str) + "\n")
    os.replace(partial, path)


async def _serve(
    base_url: str, seller_keys: dict[str, str], state_path: Path, state: dict[str, Any]
) -> str | None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    sellers = asyncio.ensure_future(rule_sellers(base_url, seller_keys))
    _write(state_path, state)
    stopped = asyncio.ensure_future(stop.wait())
    await asyncio.wait({sellers, stopped}, return_when=asyncio.FIRST_COMPLETED)
    sellers.cancel()
    stopped.cancel()
    outcome = await asyncio.gather(sellers, return_exceptions=True)
    error = outcome[0]
    if isinstance(error, Exception):
        return f"{type(error).__name__}: {error}"
    return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--task", default="t01")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--max-unit-price")
    parser.add_argument("--max-total")
    parser.add_argument("--max-qty", type=int)
    args = parser.parse_args(argv)
    task = task_with_limits(args.task, {key: getattr(args, key) for key in _LIMITS})
    today = date.fromisoformat(str(task["today"]))
    with _fresh_world(datetime.combine(today, clock_time(9), tzinfo=UTC)) as now:
        with Session(get_engine(), expire_on_commit=False) as session:
            seller_keys = load_catalog(session)
            buyer = provision_buyer(session, task["mandate"], now + timedelta(days=30))
            session.commit()
        with _server() as base_url:
            state = {
                "task_id": task["id"],
                "today": str(task["today"]),
                "url": f"{base_url}/mcp/buyer/",
                "api_key": buyer["api_key"],
                "mandate": buyer["mandate"],
                "spec": task["spec"],
                "limits": {key: task["mandate"][key] for key in _LIMITS},
                "sellers": sorted(seller_keys),
            }
            seller_error = asyncio.run(_serve(base_url, seller_keys, args.state, state))
        _write(args.audit, {**audit(task, buyer["agent_id"]), "seller_error": seller_error})


if __name__ == "__main__":
    main()
