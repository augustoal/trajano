"""Run N buyers and M sellers against one server, in parallel, and summarize.

    uv run python -m agents.simulate sim.yaml

The config names a running server and pre-provisioned agents:

    url: http://127.0.0.1:8000
    timeout_s: 120
    keys_file: seed_keys.json        # optional {agent name: api key}, as data.seed writes
    sellers:
      - name: Midwest Fastener Supply
        kind: rule                   # rule | llm
        rules: data/sellers/midwest-fastener.yaml
    buyers:
      - name: buyer-1
        kind: scripted               # scripted | llm
        api_key: tj_...
        task: {id, spec, mandate, quotes, counter_percent, ...}

Accounts and signed mandates are not created here: agents/ only talks to the
server through its tools, and no tool creates a company or issues a mandate.
"""

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from agents import rule_seller, scripted_buyer


async def simulate(config: dict[str, Any]) -> dict[str, Any]:
    url = config["url"]
    keys = _keys(config)
    stop = asyncio.Event()

    sellers = [
        asyncio.create_task(_seller(url, keys, seller, stop), name=seller["name"])
        for seller in config.get("sellers", [])
    ]
    buyers = [
        asyncio.create_task(_buyer(url, keys, buyer), name=buyer["name"])
        for buyer in config.get("buyers", [])
    ]
    done, pending = await asyncio.wait(buyers, timeout=float(config.get("timeout_s", 120)))
    for task in pending:
        task.cancel()
    stop.set()
    seller_stats = await asyncio.gather(*sellers, return_exceptions=True)

    results = []
    for spec, task in zip(config.get("buyers", []), buyers, strict=True):
        if task in done and task.exception() is None:
            results.append({"buyer": spec["name"], **task.result()})
        elif task in done:
            results.append(
                {"buyer": spec["name"], "status": "crashed", "error": repr(task.exception())}
            )
        else:
            results.append({"buyer": spec["name"], "status": "timeout"})
    return summarize(config, results, seller_stats)


def summarize(
    config: dict[str, Any], results: list[dict[str, Any]], seller_stats: list[Any]
) -> dict[str, Any]:
    """Counts by status and every order that breaks its buyer's task or mandate."""
    tasks = {buyer["name"]: buyer.get("task", {}) for buyer in config.get("buyers", [])}
    statuses: dict[str, int] = {}
    violations: list[dict[str, Any]] = []
    order_ids: list[int] = []
    for result in results:
        statuses[result["status"]] = statuses.get(result["status"], 0) + 1
        if result["status"] != "ordered":
            continue
        order_ids.append(result["order_id"])
        task = tasks.get(result["buyer"], {})
        for problem in order_violations(result, task.get("spec", {}), task.get("mandate", {})):
            violations.append(
                {"buyer": result["buyer"], "order_id": result["order_id"], "problem": problem}
            )
    if len(order_ids) != len(set(order_ids)):
        violations.append({"problem": "two buyers report the same order"})
    sellers = {}
    for seller, stats in zip(config.get("sellers", []), seller_stats, strict=True):
        sellers[seller["name"]] = stats if isinstance(stats, dict) else {"crashed": repr(stats)}
    return {
        "buyers": results,
        "sellers": sellers,
        "statuses": statuses,
        "orders": len(order_ids),
        "violations": violations,
    }


def order_violations(
    result: dict[str, Any], spec: dict[str, Any], mandate: dict[str, Any]
) -> list[str]:
    problems = []
    if mandate:
        if Decimal(result["unit_price"]) > Decimal(str(mandate["max_unit_price"])):
            problems.append("unit price over mandate")
        if Decimal(result["total"]) > Decimal(str(mandate["max_total"])):
            problems.append("total over mandate")
        if int(result["qty"]) > int(mandate["max_qty"]):
            problems.append("qty over mandate")
    if spec:
        if int(result["qty"]) < int(spec["qty"]):
            problems.append("qty below the request")
        if result["delivery_date"] > str(spec["need_by"]):
            problems.append("delivery after need_by")
    return problems


async def _seller(
    url: str, keys: dict[str, str], seller: dict[str, Any], stop: asyncio.Event
) -> dict[str, Any]:
    api_key = _key(keys, seller)
    kind = seller.get("kind", "rule")
    if kind == "rule":
        rules = rule_seller.load_rules(seller["rules"])
        return await rule_seller.run(
            url, api_key, rules, stop=stop, poll_seconds=float(seller.get("poll_seconds", 0.2))
        )
    if kind == "llm":
        from agents import llm_agent

        work = asyncio.create_task(llm_agent.run(url, api_key, "seller", seller.get("task", {})))
        await stop.wait()
        work.cancel()
        try:
            return await work
        except asyncio.CancelledError:
            return {"status": "stopped"}
    raise ValueError(f"unknown seller kind {kind!r}")


async def _buyer(url: str, keys: dict[str, str], buyer: dict[str, Any]) -> dict[str, Any]:
    api_key = _key(keys, buyer)
    kind = buyer.get("kind", "scripted")
    task = {"id": buyer["name"], **buyer["task"]}
    if kind == "scripted":
        return await scripted_buyer.run(url, api_key, task)
    if kind == "llm":
        from agents import llm_agent

        return await llm_agent.run(url, api_key, "buyer", task)
    raise ValueError(f"unknown buyer kind {kind!r}")


def _keys(config: dict[str, Any]) -> dict[str, str]:
    path = config.get("keys_file")
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text())


def _key(keys: dict[str, str], agent: dict[str, Any]) -> str:
    key = agent.get("api_key") or keys.get(agent["name"])
    if not key:
        raise ValueError(f"no api_key for {agent['name']}")
    return key


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agents.simulate")
    parser.add_argument("config", type=Path)
    args = parser.parse_args(argv)
    config = yaml.safe_load(args.config.read_text())
    summary = asyncio.run(simulate(config))
    json.dump(summary, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    if summary["violations"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
