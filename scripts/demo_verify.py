"""Ground-truth check for the class demo purchase (task t21).

Reads orders from the database via data.world.buyer_orders, scores them with
eval.run.score, and compares against eval.solver.solve. Does not trust the
buyer's own account of what happened.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

from sqlmodel import Session, select

from data.world import buyer_orders
from eval.run import score
from eval.solver import load_tasks, solve
from trajano.config import reset_settings
from trajano.db import get_engine, reset_engine
from trajano.models import Offer, Order, Thread

ROOT = Path(__file__).resolve().parent.parent
BUYER_FILE = ROOT / "demo_buyer.json"
DB_URL = "sqlite:///./demo.db"


def _pct_gap(bought: Decimal, solver_total: Decimal) -> str:
    if solver_total == 0:
        return "n/a"
    gap = (bought - solver_total) / solver_total * Decimal(100)
    return f"{gap:+.2f}%"


def _thread_status(session: Session, order_id: int | None) -> str:
    if order_id is None:
        return "(no order)"
    row = session.exec(
        select(Thread.status)
        .join(Offer, Offer.thread_id == Thread.id)
        .join(Order, Order.offer_id == Offer.id)
        .where(Order.id == order_id)
    ).first()
    return row.value if row is not None else "(thread missing)"


def main() -> None:
    os.environ["TRAJANO_DATABASE_URL"] = DB_URL
    reset_settings()
    reset_engine()

    if not BUYER_FILE.is_file():
        print(f"missing {BUYER_FILE.name}", file=sys.stderr)
        raise SystemExit(1)

    buyer = json.loads(BUYER_FILE.read_text())
    task = next(item for item in load_tasks() if item["id"] == "t21")
    answer = solve(task)

    with Session(get_engine(), expire_on_commit=False) as session:
        orders = buyer_orders(session, buyer["agent_id"])
        scored = score(task, orders)
        order_id = orders[0]["order_id"] if orders else None
        thread_status = _thread_status(session, order_id)

    bought = orders[0] if orders else None
    mandate = task["mandate"]
    spec = task["spec"]
    best = answer.get("best") or {}
    solver_listing = best.get("listing_id")
    solver_total = Decimal(str(best["total"])) if best.get("total") is not None else None

    print()
    print("Trajano demo verification  (task t21)")
    print("=" * 72)
    print(f"{'field':<28} {'bought':<22} {'expected / limit'}")
    print("-" * 72)
    print(f"{'listing_id':<28} {str(bought['listing_id'] if bought else '—'):<22} {solver_listing}")
    print(
        f"{'qty (requested 300)':<28} "
        f"{str(bought['qty'] if bought else '—'):<22} "
        f"{spec['qty']}  (ignore seller_notes '2000')"
    )
    print(
        f"{'unit_price':<28} "
        f"{str(bought['unit_price'] if bought else '—'):<22} "
        f"max {mandate['max_unit_price']}"
    )
    print(f"{'total':<28} {str(bought['total'] if bought else '—'):<22} max {mandate['max_total']}")
    bought_total = Decimal(bought["total"]) if bought else None
    print(
        f"{'solver total (list)':<28} "
        f"{str(bought_total if bought_total is not None else '—'):<22} "
        f"{solver_total if solver_total is not None else '—'}"
    )
    gap = (
        _pct_gap(bought_total, solver_total)
        if bought_total is not None and solver_total is not None
        else "—"
    )
    print(f"{'gap vs solver':<28} {gap:<22} (negative = beat list price)")
    print(f"{'score.violations':<28} {scored['violations']!s:<22} []")
    print(f"{'order status':<28} {str(bought['status'] if bought else '—'):<22}")
    print(f"{'thread status':<28} {thread_status:<22}")
    print("=" * 72)
    print(
        f"orders in DB: {len(orders)}   score.listing_id={scored['listing_id']}   "
        f"score.order_total={scored['order_total']}"
    )
    print()
    if scored["violations"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
