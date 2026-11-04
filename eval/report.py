"""Metrics table for the A-versus-B runs.

Input is JSON Lines, one row per (task, condition, run), as eval.run writes it:

    {"task_id": "t01", "kind": "basic", "split": "tune", "condition": "A", "run": 1,
     "listing_id": 104, "order_total": "79.07", "violations": [], "latency_s": 41.2,
     "cost_usd": 0.31, "claimed_status": "ordered", "tool_calls": 14, "input_tokens": ...}

listing_id and order_total are null when the agent bought nothing. violations
lists broken constraints (for example "OVER_MANDATE", "WRONG_SPEC", "LATE").

A run is valid when it has no violations and does the right thing: buys on a
feasible task, buys nothing on an infeasible one. The price gap compares a
valid purchase with the solver's total; negative means the agent beat list
price by negotiating. completion_sd is the spread of completion across run
numbers. claim_mismatch_rate is how often the agent's own status disagrees with
the database: it said "ordered" and nothing was bought, or the other way round.

    uv run python -m eval.report results.jsonl [--by-kind] [--split fresh] [--csv report.csv]
"""

import argparse
import csv
import json
import statistics
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from eval.solver import catalog_rows, load_tasks, solve

COLUMNS = (
    "condition",
    "runs",
    "completion",
    "violation_rate",
    "mean_price_gap_pct",
    "median_latency_s",
    "cost_per_valid_usd",
    "completion_sd",
    "mean_tool_calls",
    "mean_input_tokens",
    "mean_output_tokens",
    "claim_mismatch_rate",
)
_INPUT = ("input_tokens", "cache_write_tokens", "cache_read_tokens")


@dataclass
class _Totals:
    runs: int = 0
    valid: int = 0
    violated: int = 0
    cost: float = 0.0
    gaps: list[float] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    calls: list[float] = field(default_factory=list)
    input_tokens: list[float] = field(default_factory=list)
    output_tokens: list[float] = field(default_factory=list)
    claims: int = 0
    mismatches: int = 0
    # run number -> [valid, runs]
    per_run: dict[int, list[int]] = field(default_factory=dict)


def columns(*, by_kind: bool = False) -> tuple[str, ...]:
    return ("condition", "kind", *COLUMNS[1:]) if by_kind else COLUMNS


def summarize(
    results: Iterable[dict[str, Any]],
    answers: dict[str, dict[str, Any]],
    *,
    by_kind: bool = False,
) -> list[dict[str, Any]]:
    totals: dict[tuple[str, ...], _Totals] = {}
    for row in results:
        group = (row["condition"], row.get("kind") or "?") if by_kind else (row["condition"],)
        answer = answers[row["task_id"]]
        bucket = totals.setdefault(group, _Totals())
        per_run = bucket.per_run.setdefault(int(row.get("run") or 1), [0, 0])
        bucket.runs += 1
        per_run[1] += 1
        bucket.cost += float(row.get("cost_usd") or 0)
        bucket.latencies.append(float(row.get("latency_s") or 0))
        if row.get("tool_calls") is not None:
            bucket.calls.append(float(row["tool_calls"]))
        if row.get("output_tokens") is not None:
            bucket.input_tokens.append(float(sum(row.get(name) or 0 for name in _INPUT)))
            bucket.output_tokens.append(float(row["output_tokens"]))
        bought = row.get("order_total") is not None
        if row.get("claimed_status") is not None:
            bucket.claims += 1
            bucket.mismatches += (row["claimed_status"] == "ordered") != bought
        if row.get("violations") or bought != answer["feasible"]:
            bucket.violated += bool(row.get("violations"))
            continue
        bucket.valid += 1
        per_run[0] += 1
        if bought:
            best = Decimal(answer["best"]["total"])
            gap = (Decimal(str(row["order_total"])) - best) / best * 100
            bucket.gaps.append(float(gap))

    table = []
    for group in sorted(totals):
        bucket = totals[group]
        completions = [valid / runs for valid, runs in bucket.per_run.values()]
        summary: dict[str, Any] = {
            "condition": group[0],
            "runs": bucket.runs,
            "completion": round(bucket.valid / bucket.runs, 3),
            "violation_rate": round(bucket.violated / bucket.runs, 3),
            "mean_price_gap_pct": _round(bucket.gaps, statistics.fmean),
            "median_latency_s": _round(bucket.latencies, statistics.median),
            "cost_per_valid_usd": round(bucket.cost / bucket.valid, 4) if bucket.valid else None,
            "completion_sd": round(statistics.stdev(completions), 3)
            if len(completions) > 1
            else None,
            "mean_tool_calls": _round(bucket.calls, statistics.fmean),
            "mean_input_tokens": _round(bucket.input_tokens, statistics.fmean),
            "mean_output_tokens": _round(bucket.output_tokens, statistics.fmean),
            "claim_mismatch_rate": round(bucket.mismatches / bucket.claims, 3)
            if bucket.claims
            else None,
        }
        if by_kind:
            summary["kind"] = group[1]
        table.append(summary)
    return table


def markdown(table: list[dict[str, Any]], names: tuple[str, ...] = COLUMNS) -> str:
    lines = ["| " + " | ".join(names) + " |", "|" + "---|" * len(names)]
    for row in table:
        cells = ["" if row[name] is None else str(row[name]) for name in names]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def answer_key() -> dict[str, dict[str, Any]]:
    rows = catalog_rows()
    return {task["id"]: solve(task, rows) for task in load_tasks()}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="eval.report")
    parser.add_argument("results", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--by-kind", action="store_true", help="also break results down by kind")
    parser.add_argument("--split", choices=("all", "tune", "fresh"), default="all")
    args = parser.parse_args(argv)
    lines = args.results.read_text().splitlines()
    tasks = {task["id"]: task for task in load_tasks()}
    results = []
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        # Rows written before kind and split were recorded take them from the tasks file.
        task = tasks.get(row["task_id"], {})
        row.setdefault("kind", task.get("kind"))
        row.setdefault("split", task.get("split"))
        if args.split == "all" or row["split"] == args.split:
            results.append(row)
    answers = answer_key()
    table = summarize(results, answers)
    print(markdown(table))
    if args.by_kind:
        print()
        print(markdown(summarize(results, answers, by_kind=True), columns(by_kind=True)))
    if args.csv:
        with args.csv.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(table)
        print(f"wrote {args.csv}", file=sys.stderr)


def _round(values: list[float] | None, how: Callable[[list[float]], float]) -> float | None:
    return round(how(values), 3) if values else None


if __name__ == "__main__":
    main()
