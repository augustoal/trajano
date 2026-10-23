"""Metrics table for the A-versus-B runs.

Input is JSON Lines, one row per (task, condition, run), as eval.run writes it:

    {"task_id": "t01", "condition": "A", "run": 1, "listing_id": 104,
     "order_total": "79.07", "violations": [], "latency_s": 41.2, "cost_usd": 0.31}

listing_id and order_total are null when the agent bought nothing. violations
lists broken constraints (for example "OVER_MANDATE", "WRONG_SPEC", "LATE").

A run is valid when it has no violations and does the right thing: buys on a
feasible task, buys nothing on an infeasible one. The price gap compares a
valid purchase with the solver's total; negative means the agent beat list
price by negotiating.

    uv run python -m eval.report results.jsonl [--csv report.csv]
"""

import argparse
import csv
import json
import statistics
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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
)


@dataclass
class _Totals:
    runs: int = 0
    valid: int = 0
    violated: int = 0
    cost: float = 0.0


def summarize(
    results: Iterable[dict[str, Any]], answers: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    totals: dict[str, _Totals] = {}
    gaps: dict[str, list[float]] = {}
    latencies: dict[str, list[float]] = {}
    for row in results:
        condition = row["condition"]
        answer = answers[row["task_id"]]
        bucket = totals.setdefault(condition, _Totals())
        bucket.runs += 1
        bucket.cost += float(row.get("cost_usd") or 0)
        latencies.setdefault(condition, []).append(float(row.get("latency_s") or 0))
        if row.get("violations"):
            bucket.violated += 1
            continue
        bought = row.get("order_total") is not None
        if bought != answer["feasible"]:
            continue
        bucket.valid += 1
        if bought:
            best = Decimal(answer["best"]["total"])
            gap = (Decimal(str(row["order_total"])) - best) / best * 100
            gaps.setdefault(condition, []).append(float(gap))

    table = []
    for condition in sorted(totals):
        bucket = totals[condition]
        table.append(
            {
                "condition": condition,
                "runs": bucket.runs,
                "completion": round(bucket.valid / bucket.runs, 3),
                "violation_rate": round(bucket.violated / bucket.runs, 3),
                "mean_price_gap_pct": _round(gaps.get(condition), statistics.fmean),
                "median_latency_s": _round(latencies.get(condition), statistics.median),
                "cost_per_valid_usd": round(bucket.cost / bucket.valid, 4)
                if bucket.valid
                else None,
            }
        )
    return table


def markdown(table: list[dict[str, Any]]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for row in table:
        cells = ["" if row[name] is None else str(row[name]) for name in COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def answer_key() -> dict[str, dict[str, Any]]:
    rows = catalog_rows()
    return {task["id"]: solve(task, rows) for task in load_tasks()}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="eval.report")
    parser.add_argument("results", type=Path)
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args(argv)
    lines = args.results.read_text().splitlines()
    results = [json.loads(line) for line in lines if line.strip()]
    table = summarize(results, answer_key())
    print(markdown(table))
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
