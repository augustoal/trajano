"""Deterministic buyer for end-to-end runs: search, quote, maybe counter, accept the best.

A task is the eval task shape plus how to negotiate:

    id: t01
    spec: {diameter, length_mm, material, grade, standard, qty, ship_to, need_by}
    mandate: the signed MandateDocument, as JSON
    quotes: 2              # rfq the top N hits that fit the mandate
    counter_percent: 5     # counter each first offer this much lower; omit to not counter
    wait_for: CONFIRMED    # after accepting, wait for this order status; null to not wait
    timeout_s: 30

It never reads seller_notes or any text field: only structured fields decide.
"""

import asyncio
import contextlib
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from agents.client import ToolCallError, TrajanoClient

_SPEC_FIELDS = (
    "diameter",
    "length_mm",
    "length_tolerance_mm",
    "material",
    "grade",
    "standard",
    "qty",
    "ship_to",
    "need_by",
)
_CLOSED = {"DECLINED", "WITHDRAWN", "EXPIRED", "REJECTED"}
_ORDER_STEPS = ("SUBMITTED", "CONFIRMED", "SHIPPED", "DELIVERED")


async def run(url: str, api_key: str, task: dict[str, Any]) -> dict[str, Any]:
    async with TrajanoClient(url, api_key, role="buyer") as client:
        return await buy(client, task)


async def buy(client: TrajanoClient, task: dict[str, Any]) -> dict[str, Any]:
    task_id = str(task.get("id", "task"))
    spec = task["spec"]
    mandate = task["mandate"]
    deadline = time.monotonic() + float(task.get("timeout_s", 30))
    poll = float(task.get("poll_seconds", 0.1))
    result: dict[str, Any] = {"task_id": task_id, "status": "error", "threads": []}

    try:
        found = await client.call(
            "search_listings", **{name: spec[name] for name in _SPEC_FIELDS if name in spec}
        )
    except ToolCallError as exc:
        return {**result, "error": exc.code}
    hits = [hit for hit in found["hits"] if _fits(hit["landed"], mandate)]
    if not hits:
        return {**result, "status": "no_match" if not found["hits"] else "over_mandate"}

    threads: list[int] = []
    for hit in hits[: int(task.get("quotes", 2))]:
        listing_id = hit["listing"]["id"]
        try:
            sent = await client.call(
                "request_quote",
                listing_id=listing_id,
                qty=spec["qty"],
                ship_to=spec["ship_to"],
                need_by=spec["need_by"],
                idempotency_key=f"{task_id}-rfq-{listing_id}",
            )
        except ToolCallError:
            continue
        threads.append(sent["thread_id"])
    result["threads"] = threads
    if not threads:
        return result

    offers = await _collect_offers(client, task, threads, deadline, poll)
    if offers is None:
        await _withdraw_all(client, task_id, threads, "OTHER")
        return {**result, "status": "timeout"}

    usable = sorted(
        (offer for offer in offers.values() if _acceptable(offer, spec, mandate)),
        key=lambda offer: (Decimal(offer["total"]), offer["listing_id"]),
    )
    for offer in usable:
        try:
            sent = await client.call(
                "accept_offer",
                offer_id=offer["offer_id"],
                offer_hash=offer["hash"],
                mandate=mandate,
                idempotency_key=f"{task_id}-accept-{offer['offer_id']}",
            )
        except ToolCallError:
            # Stock went, the offer expired, or the mandate failed: try the next one.
            continue
        others = [thread_id for thread_id in threads if thread_id != offer["thread_id"]]
        await _withdraw_all(client, task_id, others, "FOUND_ALTERNATIVE")
        order = await _wait_for_order(client, sent["order_id"], task, deadline, poll)
        return {
            **result,
            "status": "ordered",
            "order_id": sent["order_id"],
            "order_status": order["status"],
            "thread_id": offer["thread_id"],
            "listing_id": offer["listing_id"],
            "unit_price": offer["unit_price"],
            "qty": offer["qty"],
            "total": offer["total"],
            "delivery_date": offer["delivery_date"],
            "countered": offer["final"],
        }

    await _withdraw_all(client, task_id, threads, "PRICE")
    return {**result, "status": "no_offer" if not offers else "over_mandate"}


async def _collect_offers(
    client: TrajanoClient,
    task: dict[str, Any],
    threads: list[int],
    deadline: float,
    poll: float,
) -> dict[int, dict[str, Any]] | None:
    """Wait until every thread has a last offer or is closed. None on timeout.

    Declines and expiry are not our turn, so they never show in the inbox;
    polling the few open threads directly sees them.
    """
    task_id = str(task.get("id", "task"))
    percent = task.get("counter_percent")
    offers: dict[int, dict[str, Any]] = {}
    open_threads = set(threads)
    while open_threads:
        for thread_id in sorted(open_threads):
            thread = await client.get_thread(thread_id)
            if thread["status"] in _CLOSED:
                open_threads.discard(thread_id)
                offers.pop(thread_id, None)
                continue
            if thread["status"] != "OFFERED":
                continue
            offer = _latest_offer(thread)
            if percent is not None and "counter" in thread["next_actions"]:
                try:
                    await client.call(
                        "counter_offer",
                        thread_id=thread_id,
                        offer_id=offer["offer_id"],
                        unit_price=_lower(offer["unit_price"], percent),
                        idempotency_key=f"{task_id}-counter-{offer['offer_id']}",
                    )
                    continue
                except ToolCallError:
                    pass
            offers[thread_id] = offer
            open_threads.discard(thread_id)
        if not open_threads:
            break
        if time.monotonic() > deadline:
            return None
        await asyncio.sleep(poll)
    return offers


async def _wait_for_order(
    client: TrajanoClient, order_id: int, task: dict[str, Any], deadline: float, poll: float
) -> dict[str, Any]:
    wanted = task.get("wait_for", "CONFIRMED")
    while True:
        order = (await client.call("get_order", order_id=order_id))["order"]
        status = order["status"]
        if wanted is None or status == "REJECTED":
            return order
        if status in _ORDER_STEPS and _ORDER_STEPS.index(status) >= _ORDER_STEPS.index(wanted):
            return order
        if time.monotonic() > deadline:
            return order
        await asyncio.sleep(poll)


async def _withdraw_all(
    client: TrajanoClient, task_id: str, threads: list[int], reason: str
) -> None:
    for thread_id in threads:
        # Already declined or expired: nothing to close.
        with contextlib.suppress(ToolCallError):
            await client.call(
                "withdraw",
                thread_id=thread_id,
                reason_code=reason,
                idempotency_key=f"{task_id}-withdraw-{thread_id}",
            )


def _latest_offer(thread: dict[str, Any]) -> dict[str, Any]:
    message = next(m for m in reversed(thread["messages"]) if m["type"] == "offer")
    return {**message["payload"], "thread_id": thread["id"]}


def _fits(priced: dict[str, Any], mandate: dict[str, Any]) -> bool:
    return (
        Decimal(str(priced["unit_price"])) <= Decimal(str(mandate["max_unit_price"]))
        and int(priced.get("qty_billed", priced.get("qty", 0))) <= int(mandate["max_qty"])
        and Decimal(str(priced["total"])) <= Decimal(str(mandate["max_total"]))
    )


def _acceptable(offer: dict[str, Any], spec: dict[str, Any], mandate: dict[str, Any]) -> bool:
    return (
        _fits(offer, mandate)
        and int(offer["qty"]) >= int(spec["qty"])
        and offer["delivery_date"] <= str(spec["need_by"])
        and offer.get("ship_to") == spec["ship_to"]
    )


def _lower(unit_price: str, percent: float | str) -> str:
    lowered = Decimal(unit_price) * (100 - Decimal(str(percent))) / 100
    return format(lowered.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")
