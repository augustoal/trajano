"""Answer key: the cheapest feasible purchase for one task at list price.

It reads the same synthetic catalog the seed loads and prices with
pricing.landed_cost, so its total matches an offer at the tier price. A
negotiated counter can still beat it; the report measures that as a negative gap.
"""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from data.catalog import build
from trajano.domain.pricing import billed_qty, landed_cost, transit_days
from trajano.schemas import PriceableListing, PriceTier

TASKS_FILE = Path(__file__).with_name("tasks.yaml")
_ATTRIBUTES = ("diameter", "material", "grade", "standard")


def load_tasks(path: Path = TASKS_FILE) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text())["tasks"]


def catalog_rows() -> list[dict[str, Any]]:
    return [{**row, "seller": seller.name} for seller in build() for row in seller.listings]


def solve(task: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Feasible optimum for one task.

    A listing is a candidate when its attributes match the spec. It is
    excluded for MOQ, stock, or a delivery after need_by (today plus lead time
    plus transit), then for any mandate limit. The cheapest landed total wins,
    ties broken by listing id.
    """
    spec = task["spec"]
    mandate = task["mandate"]
    today = _day(task["today"])
    need_by = _day(spec["need_by"])
    qty = int(spec["qty"])
    length = Decimal(str(spec["length_mm"]))
    tolerance = Decimal(str(spec.get("length_tolerance_mm", 0)))

    candidates: list[int] = []
    excluded: dict[int, str] = {}
    feasible: list[tuple[Decimal, int, dict[str, Any]]] = []
    for row in rows if rows is not None else catalog_rows():
        if abs(Decimal(row["length_mm"]) - length) > tolerance:
            continue
        if any(spec.get(name) not in (None, row[name]) for name in _ATTRIBUTES):
            continue
        listing_id = row["listing_id"]
        candidates.append(listing_id)
        billed = billed_qty(qty, row["pack_size"])
        delivery = today + timedelta(
            days=row["lead_time_days"] + transit_days(row["ships_from"], spec["ship_to"])
        )
        if row["moq"] > billed:
            excluded[listing_id] = "MOQ"
            continue
        if row["stock"] < billed:
            excluded[listing_id] = "STOCK"
            continue
        if delivery > need_by:
            excluded[listing_id] = "DATE"
            continue
        priced = landed_cost(_priceable(row), qty, spec["ship_to"])
        if (
            priced.unit_price > Decimal(str(mandate["max_unit_price"]))
            or priced.qty_billed > int(mandate["max_qty"])
            or priced.total > Decimal(str(mandate["max_total"]))
        ):
            excluded[listing_id] = "MANDATE"
            continue
        feasible.append(
            (
                priced.total,
                listing_id,
                {
                    "listing_id": listing_id,
                    "sku": row["sku"],
                    "seller": row["seller"],
                    "qty_billed": priced.qty_billed,
                    "unit_price": format(priced.unit_price, "f"),
                    "total": format(priced.total, "f"),
                    "delivery_date": delivery.isoformat(),
                },
            )
        )

    answer: dict[str, Any] = {
        "task_id": task["id"],
        "feasible": bool(feasible),
        "candidates": candidates,
        "excluded": excluded,
        "best": None,
    }
    if feasible:
        answer["best"] = min(feasible, key=lambda item: (item[0], item[1]))[2]
    return answer


def _priceable(row: dict[str, Any]) -> PriceableListing:
    return PriceableListing(
        id=row["listing_id"],
        version=1,
        price_tiers=[
            PriceTier(min_qty=tier["min_qty"], unit_price=Decimal(tier["unit_price"]))
            for tier in row["price_tiers"]
        ],
        pack_size=row["pack_size"],
        ships_from=row["ships_from"],
        currency=row["currency"],
    )


def _day(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)
