"""HW9 cold-start test: a fresh agent buys through Trajano from the documentation alone.

    uv run python -m scripts.cold_start --tasks cs-main,cs-titanium --runs 3 --label before

Each run builds the same world as eval.run (fresh database, frozen clock, seeded
catalog, five rule sellers, one buyer with its signed mandate, a live server).
Then a new model session starts with nothing but:

- a one-line role, the manager's request in business language (eval/cold_start_tasks.yaml),
- its credentials: the MCP connection (the API key rides in the HTTP header) and
  the signed mandate as JSON,
- the documentation, docs/AGENT_GUIDE.md, verbatim.

Its tools are whatever the buyer MCP server lists, plus two harness tools any agent
runtime has: wait (sleep) and report (hand the result to the manager). Nobody
relays messages, scripts calls or fixes arguments: unlike agents/llm_agent.py, the
mandate is NOT filled in for the model; it must pass it itself.

The run is judged from the database, never from the agent's word. For a purchase,
the agent's report (its purchase record for accounts payable) must also match the
order in the database: that is the check that it used the returned result.
Rows go to eval/results/hw9/<label>.jsonl and every turn to <label>.transcripts.jsonl.
"""

import argparse
import asyncio
import json
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
TASKS_FILE = ROOT / "eval" / "cold_start_tasks.yaml"
GUIDE_FILE = ROOT / "docs" / "AGENT_GUIDE.md"
OUT_DIR = ROOT / "eval" / "results" / "hw9"
DEFAULT_MODEL = "claude-opus-5-5"
PRICE_SLACK = Decimal("1.05")  # a purchase may cost at most 5% above the answer key
REASONS = ["not_in_catalog", "no_seller_can_meet_terms", "over_mandate", "other"]

ROLE = (
    "You are an autonomous procurement agent for a manufacturing company. Your company "
    "has just connected you to a marketplace service called Trajano. You have never used "
    "it before. Its documentation is below; its tools are available to you. Work on your "
    "own: nobody will answer questions during the task. When you are finished, call "
    "report exactly once."
)

WAIT_TOOL = {
    "name": "wait",
    "description": "Sleep for a few seconds. Part of your agent runtime, not of Trajano.",
    "input_schema": {
        "type": "object",
        "properties": {"seconds": {"type": "number", "minimum": 1, "maximum": 60}},
        "required": ["seconds"],
    },
}

REPORT_TOOL = {
    "name": "report",
    "description": (
        "Hand your result to the accounts-payable team. Part of your agent runtime, not "
        "of Trajano. Call exactly once, at the end. For a purchase, fill the purchase "
        "record from what Trajano returned."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ordered", "not_ordered"]},
            "reason_code": {
                "type": "string",
                "enum": REASONS,
                "description": "Why nothing was ordered. Omit when status is ordered.",
            },
            "order_id": {"type": ["integer", "null"]},
            "listing_id": {"type": ["integer", "null"]},
            "seller": {"type": ["string", "null"]},
            "qty_billed": {"type": ["integer", "null"]},
            "unit_price": {"type": ["string", "null"]},
            "total": {"type": ["string", "null"]},
            "delivery_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
            "summary": {"type": "string"},
        },
        "required": ["status", "summary"],
    },
}


# ---------------------------------------------------------------------------
# Pure helpers (tested without a server or a model)


def load_tasks(path: Path = TASKS_FILE) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text())["tasks"]


def brief(task: dict[str, Any], mandate: dict[str, Any], guide: str) -> str:
    """Everything the fresh agent is told. No spec, no call sequence, no answer."""
    return "\n".join(
        [
            "## Request from your manager",
            task["request"].strip(),
            "",
            "## Your credentials",
            "Your connection to Trajano is already authenticated with your company's API key.",
            "Your company's signed purchasing mandate:",
            "```json",
            json.dumps(mandate, indent=2),
            "```",
            "",
            "## Trajano documentation (docs/AGENT_GUIDE.md)",
            guide.strip(),
        ]
    )


def _money(value: Any, places: str = "0.01") -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal(places))
    except InvalidOperation:
        return None


def judge(
    task: dict[str, Any],
    orders: list[dict[str, Any]],
    scored: dict[str, Any],
    report: dict[str, Any] | None,
    answer_key: dict[str, Any] | None,
) -> dict[str, Any]:
    """Pass or fail each criterion fixed in eval/results/hw9/PLAN.md."""
    report = report or {}
    bought = [order for order in orders if order["status"] != "REJECTED"]
    checks: dict[str, bool] = {}
    if task["expect"] == "buy":
        order = bought[0] if len(bought) == 1 else None
        checks["exactly_one_order"] = order is not None
        checks["no_violations"] = scored.get("violations") == []
        ceiling = Decimal(answer_key["total"]) * PRICE_SLACK if answer_key is not None else None
        checks["price_within_5pct_of_answer_key"] = bool(
            order and ceiling is not None and Decimal(order["total"]) <= ceiling
        )
        checks["reported_ordered"] = report.get("status") == "ordered"
        checks["record_matches_database"] = bool(
            order
            and report.get("order_id") == order["order_id"]
            and report.get("listing_id") == order["listing_id"]
            and report.get("qty_billed") == order["qty"]
            and _money(report.get("total")) == _money(order["total"])
            and _money(report.get("unit_price"), "0.0001") == _money(order["unit_price"], "0.0001")
            and str(report.get("delivery_date")) == order["delivery_date"]
        )
    else:
        checks["no_order"] = not bought
        checks["reported_not_ordered"] = report.get("status") == "not_ordered"
        checks["right_reason"] = report.get("reason_code") == task.get("expect_reason")
    return {"passed": all(checks.values()), "checks": checks}


def tool_stats(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    calls = [call for turn in transcript for call in turn.get("calls", [])]
    by_tool: dict[str, int] = {}
    for call in calls:
        by_tool[call["tool"]] = by_tool.get(call["tool"], 0) + 1
    return {
        "turns": sum(1 for turn in transcript if "turn" in turn),
        "tool_calls": len(calls),
        "calls_by_tool": by_tool,
        "tool_errors": [call["error"] for call in calls if call.get("error")],
    }


# ---------------------------------------------------------------------------
# The agent


async def cold_agent(
    base_url: str,
    api_key: str,
    mandate: dict[str, Any],
    task: dict[str, Any],
    guide: str,
    model: str,
    transcript: list[dict[str, Any]],
    *,
    max_turns: int = 60,
    timeout_s: float = 600,
) -> dict[str, Any]:
    from agents.client import TrajanoClient
    from agents.llm_agent import _anthropic, _create, _redact, _step, _tally, cost_usd

    secrets = [api_key, mandate.get("signature") or ""]
    text = brief(task, mandate, guide)
    transcript.append({"brief": _redact(text, [s for s in secrets if s])})
    deadline = time.monotonic() + timeout_s
    usage: dict[str, dict[str, int]] = {}
    async with _anthropic() as anthropic, TrajanoClient(base_url, api_key) as client:
        api = anthropic.beta.messages
        tools = [*(await client.tools()), WAIT_TOOL, REPORT_TOOL]
        messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
        for number in range(1, max_turns + 1):
            if time.monotonic() > deadline:
                return {"status": "timeout", "usage": usage, "cost_usd": cost_usd(usage)}
            response, _charge = await _create(
                api,
                model=model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "medium"},
                system=ROLE,
                tools=tools,
                messages=messages,
            )
            _tally(usage, response, model)
            messages.append({"role": "assistant", "content": response.content})
            words = " ".join(b.text for b in response.content if b.type == "text")
            turn: dict[str, Any] = {"turn": number, "text": words, "calls": []}
            transcript.append(turn)
            uses = [block for block in response.content if block.type == "tool_use"]
            if not uses:
                if response.stop_reason in ("end_turn", "refusal"):
                    return {"status": "no_report", "summary": words, "usage": usage,
                            "cost_usd": cost_usd(usage)}  # fmt: skip
                continue
            results, reported = [], None
            for use in uses:
                error = None
                if use.name == "report":
                    reported = dict(use.input)
                    content = "ok"
                elif use.name == "wait":
                    await asyncio.sleep(min(float(use.input.get("seconds", 5)), 60))
                    content = "ok"
                else:
                    body = await client.body(use.name, dict(use.input))
                    error = (body.get("error") or {}).get("code")
                    content = json.dumps(body, default=str)
                turn["calls"].append(_step(use.name, dict(use.input), content, error, secrets))
                results.append({"type": "tool_result", "tool_use_id": use.id, "content": content})
            messages.append({"role": "user", "content": results})
            if reported is not None:
                return {**reported, "usage": usage, "cost_usd": cost_usd(usage)}
    return {"status": "max_turns", "usage": usage, "cost_usd": cost_usd(usage)}


def run_one(
    task: dict[str, Any], model: str, guide: str, timeout_s: float
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from sqlmodel import Session

    from data.seed import load_catalog
    from data.world import buyer_orders, provision_buyer
    from eval.run import _drive, _fresh_world, _server, rule_sellers, score
    from eval.solver import solve
    from trajano.db import get_engine

    transcript: list[dict[str, Any]] = []
    today = date.fromisoformat(str(task["today"]))
    with _fresh_world(datetime.combine(today, clock_time(9), tzinfo=UTC)) as now:
        with Session(get_engine(), expire_on_commit=False) as session:
            seller_keys = load_catalog(session)
            account = provision_buyer(session, task["mandate"], now + timedelta(days=30))
            session.commit()
        with _server() as base_url:
            started = time.monotonic()
            agent = cold_agent(
                base_url, account["api_key"], account["mandate"], task, guide, model,
                transcript, timeout_s=timeout_s,
            )  # fmt: skip
            report, error = asyncio.run(
                _drive(agent, rule_sellers(base_url, seller_keys), timeout_s + 30)
            )
            latency = round(time.monotonic() - started, 1)
        with Session(get_engine(), expire_on_commit=False) as session:
            orders = buyer_orders(session, account["agent_id"])
    answer_key = solve(task)["best"]
    scored = score(task, orders)
    report = report or {}
    verdict = judge(task, orders, scored, report, answer_key)
    row = {
        "task_id": task["id"],
        "model": model,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        **verdict,
        "orders": orders,
        **scored,
        "answer_key": answer_key,
        "report": {k: v for k, v in report.items() if k != "usage"},
        **tool_stats(transcript),
        "latency_s": latency,
        "error": error,
    }
    return row, transcript


def main(argv: list[str] | None = None) -> None:
    import os

    parser = argparse.ArgumentParser(prog="scripts.cold_start")
    parser.add_argument("--tasks", default="all", help="comma-separated ids, or all")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--label", required=True, help="output name, e.g. before or after")
    parser.add_argument("--guide", type=Path, default=GUIDE_FILE)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)

    model = os.environ.get("TRAJANO_LLM_MODEL", DEFAULT_MODEL)
    guide = args.guide.read_text()
    tasks = load_tasks()
    if args.tasks != "all":
        wanted = args.tasks.split(",")
        tasks = [task for task in tasks if task["id"] in wanted]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{args.label}.jsonl"
    with out.open("a") as rows, out.with_suffix(".transcripts.jsonl").open("a") as turns:
        for task in tasks:
            for run in range(1, args.runs + 1):
                row, transcript = run_one(task, model, guide, args.timeout)
                row = {"label": args.label, "run": run, **row}
                rows.write(json.dumps(row, default=str) + "\n")
                rows.flush()
                line = {"label": args.label, "task_id": task["id"], "run": run}
                turns.write(json.dumps({**line, "turns": transcript}, default=str) + "\n")
                turns.flush()
                failed = [name for name, ok in row["checks"].items() if not ok]
                print(
                    f"{task['id']} run {run}: {'PASS' if row['passed'] else 'FAIL'}"
                    f"{' ' + ','.join(failed) if failed else ''} | orders {len(row['orders'])}"
                    f" total {row['order_total']} key {(row['answer_key'] or {}).get('total')}"
                    f" | calls {row['tool_calls']} {row['calls_by_tool']}"
                    f" errors {row['tool_errors']} | {row['latency_s']}s"
                    f" | report {row['report'].get('status')}"
                    f"/{row['report'].get('reason_code')}"
                    f"{' | ERROR ' + row['error'] if row['error'] else ''}",
                    flush=True,
                )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
