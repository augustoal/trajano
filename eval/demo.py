"""Narrated scenes for the demo video, the same on every run.

    uv run python -m eval.demo buy      # search, two quotes, a counter, accept, delivery
    uv run python -m eval.demo guard    # an injected seller note, and the mandate holding
    uv run python -m eval.demo buy --llm    # Claude buys instead (needs ANTHROPIC_API_KEY)

Each scene gets a fresh world from eval.run (seeded catalog, frozen clock, a
buyer with a signed mandate, a live local server) and Jorge's rule sellers.
The buyer here is scripted so each take is identical; --pace sets the pause
between steps for recording.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from typing import Any

from sqlmodel import Session

from agents import rule_seller
from agents.client import ToolCallError, TrajanoClient
from data.seed import load_catalog
from data.world import provision_buyer
from eval.run import _fresh_world, _server
from eval.solver import load_tasks
from trajano.db import get_engine

SCENES = {"buy": "t01", "guard": "t21"}
_STYLES = {"title": "1;36", "step": "1", "ok": "32", "bad": "31", "note": "33", "dim": "2"}
_ORDER_STEPS = ("SUBMITTED", "CONFIRMED", "SHIPPED", "DELIVERED")


class Narrator:
    def __init__(self, pace: float, color: bool, write: Callable[[str], Any] = print) -> None:
        self.pace = pace
        self.color = color
        self.write = write

    async def say(self, text: str, style: str = "step") -> None:
        if self.color:
            text = f"\033[{_STYLES[style]}m{text}\033[0m"
        self.write(text)
        await asyncio.sleep(self.pace)


async def scene_buy(client: TrajanoClient, task: dict[str, Any], mandate: dict, say) -> int:  # noqa: ANN001
    spec = task["spec"]
    await say("Trajano: an agent buys fasteners through tools, not web pages", "title")
    await say(_brief(spec, mandate), "dim")
    hits = await _search(client, spec, mandate, say)
    await say(f"Requesting quotes from the {min(2, len(hits))} cheapest sellers")
    threads = [await _rfq(client, hit, spec, f"rfq-{hit['listing']['id']}") for hit in hits[:2]]
    offers = []
    for thread_id in threads:
        offer = await _wait_offer(client, thread_id)
        if offer is not None:
            offers.append((thread_id, offer))
            await say(f"  Offer: ${offer['total']} total, ${offer['unit_price']}/unit", "ok")
    thread_id, best = min(offers, key=lambda item: Decimal(item[1]["total"]))
    lower = (Decimal(best["unit_price"]) * Decimal("0.95")).quantize(Decimal("0.0001"))
    await say(f"Countering the best offer once: ${lower}/unit")
    await client.call(
        "counter_offer",
        thread_id=thread_id,
        offer_id=best["offer_id"],
        unit_price=str(lower),
        idempotency_key="counter",
    )
    final = await _wait_offer(client, thread_id, after=best["offer_id"])
    if final is None:
        await say("  The seller declined the counter; accepting the first offer", "note")
        final = best
    else:
        await say(f"  Final offer: ${final['total']} total, ${final['unit_price']}/unit", "ok")
    order_id = await _accept(client, final, mandate, "accept")
    await say(f"Accepted within the signed mandate. Order #{order_id}", "ok")
    await _follow(client, order_id, say)
    return order_id


async def scene_guard(client: TrajanoClient, task: dict[str, Any], mandate: dict, say) -> int:  # noqa: ANN001
    spec = task["spec"]
    await say("Trajano: the server enforces the rules, not the agent", "title")
    await say(_brief(spec, mandate), "dim")
    hits = await _search(client, spec, mandate, say)
    top = hits[0]
    note = top["listing"].get("seller_notes")
    await say(f'Seller note on the cheapest listing (untrusted): "{note}"', "note")
    await say("A naive agent obeys the note and asks for 2,000 units", "bad")
    naive = await _rfq(client, top, {**spec, "qty": 2000}, "rfq-naive")
    offer = await _wait_offer(client, naive)
    if offer is None:
        raise RuntimeError("the seller did not quote the naive request")
    await say(f"  Offer: ${offer['total']} for 2,000 units", "dim")
    try:
        await _accept(client, offer, mandate, "accept-naive")
    except ToolCallError as refused:
        await say(f"  Blocked by the server: {refused.code}, {refused.message}", "ok")
    else:
        raise RuntimeError("the server accepted an order over the mandate")
    await client.call(
        "withdraw", thread_id=naive, reason_code="NO_LONGER_NEEDED", idempotency_key="withdraw"
    )
    await say(f"The buyer reads only structured fields and asks for {spec['qty']}")
    thread_id = await _rfq(client, top, spec, "rfq-right")
    offer = await _wait_offer(client, thread_id)
    if offer is None:
        raise RuntimeError("the seller did not quote")
    order_id = await _accept(client, offer, mandate, "accept-right")
    await say(f"  Bought {offer['qty']} units for ${offer['total']}. Order #{order_id}", "ok")
    return order_id


async def _search(client: TrajanoClient, spec: dict, mandate: dict, say) -> list[dict]:  # noqa: ANN001
    page = await client.call("search_listings", **spec)
    await say(
        f"Search: {len(page['hits'])} listings fit, {len(page['excluded'])} excluded "
        "(minimum order, stock, or delivery date)"
    )
    hits = [hit for hit in page["hits"] if _fits(hit["landed"], mandate)]
    for hit in hits[:3]:
        landed = hit["landed"]
        await say(
            f"  {hit['listing']['company_name']:<24} ${landed['total']:>8} landed "
            f"(goods {landed['goods']}, freight {landed['freight']}, tax {landed['tax']})",
            "dim",
        )
    if not hits:
        raise RuntimeError("nothing fits the mandate")
    return hits


async def _rfq(client: TrajanoClient, hit: dict, spec: dict, key: str) -> int:
    sent = await client.call(
        "request_quote",
        listing_id=hit["listing"]["id"],
        qty=spec["qty"],
        ship_to=spec["ship_to"],
        need_by=spec["need_by"],
        idempotency_key=key,
    )
    return sent["thread_id"]


async def _wait_offer(
    client: TrajanoClient, thread_id: int, *, after: int | None = None, timeout_s: float = 30
) -> dict | None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        thread = await client.get_thread(thread_id)
        offers = [m["payload"] for m in thread["messages"] if m["type"] == "offer"]
        if offers and (after is None or offers[-1]["offer_id"] != after):
            return offers[-1]
        if thread["status"] in ("DECLINED", "EXPIRED", "WITHDRAWN"):
            return None
        await asyncio.sleep(0.1)
    return None


async def _accept(client: TrajanoClient, offer: dict, mandate: dict, key: str) -> int:
    sent = await client.call(
        "accept_offer",
        offer_id=offer["offer_id"],
        offer_hash=offer["hash"],
        mandate=mandate,
        idempotency_key=key,
    )
    return sent["order_id"]


async def _follow(client: TrajanoClient, order_id: int, say, timeout_s: float = 30) -> None:  # noqa: ANN001
    seen = "SUBMITTED"
    deadline = asyncio.get_running_loop().time() + timeout_s
    while seen != "DELIVERED" and asyncio.get_running_loop().time() < deadline:
        status = (await client.call("get_order", order_id=order_id))["order"]["status"]
        if status != seen and status in _ORDER_STEPS:
            seen = status
            await say(f"  Order {status.lower()}", "ok")
        if status == "REJECTED":
            await say("  Order rejected by the seller", "bad")
            return
        await asyncio.sleep(0.1)


def _fits(landed: dict, mandate: dict) -> bool:
    return (
        Decimal(landed["total"]) <= Decimal(mandate["max_total"])
        and Decimal(landed["unit_price"]) <= Decimal(mandate["max_unit_price"])
        and landed["qty_billed"] <= mandate["max_qty"]
    )


def _brief(spec: dict, mandate: dict) -> str:
    need_by = date.fromisoformat(str(spec["need_by"]))
    return (
        f"Task: {spec['qty']} x {spec['diameter']}x{spec['length_mm']} {spec['grade']} "
        f"{spec['standard']} bolts to {spec['ship_to']} by {need_by:%b %d}. "
        f"Budget ${mandate['max_total']}"
    )


async def _play(
    scene: str, base_url: str, account: dict, seller_keys: dict, narrator: Narrator, llm: bool
) -> int | None:
    task = next(item for item in load_tasks() if item["id"] == SCENES[scene])
    stop = asyncio.Event()
    rules = rule_seller.all_rules()
    sellers = [
        asyncio.create_task(
            rule_seller.run(base_url, key, rules[name], stop=stop, poll_seconds=0.1)
        )
        for name, key in seller_keys.items()
    ]
    try:
        if llm:
            from agents import llm_agent

            await narrator.say("Claude is buying. Tool calls follow.", "title")
            brief = {**task, "mandate": account["mandate"]}
            outcome = await llm_agent.run(base_url, account["api_key"], "buyer", brief)
            for call in outcome.get("calls", []):
                error = f"  {call['error']}" if call.get("error") else ""
                await narrator.say(f"  {call['tool']}{error}", "dim")
            await narrator.say(f"Claude finished: {outcome.get('status')}", "ok")
            return outcome.get("order_id")
        async with TrajanoClient(base_url, account["api_key"], role="buyer") as client:
            play = scene_buy if scene == "buy" else scene_guard
            return await play(client, task, account["mandate"], narrator.say)
    finally:
        stop.set()
        await asyncio.gather(*sellers, return_exceptions=True)


def run_scene(scene: str, *, pace: float = 1.0, color: bool = True, llm: bool = False,
              write: Callable[[str], Any] = print) -> int | None:  # fmt: skip
    task = next(item for item in load_tasks() if item["id"] == SCENES[scene])
    today = date.fromisoformat(str(task["today"]))
    with _fresh_world(datetime.combine(today, clock_time(9), tzinfo=UTC)) as now:
        with Session(get_engine(), expire_on_commit=False) as session:
            seller_keys = load_catalog(session)
            account = provision_buyer(session, task["mandate"], now + timedelta(days=30))
            session.commit()
        with _server() as base_url:
            narrator = Narrator(pace, color, write)
            return asyncio.run(_play(scene, base_url, account, seller_keys, narrator, llm))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="eval.demo")
    parser.add_argument("scene", choices=sorted(SCENES))
    parser.add_argument("--pace", type=float, default=1.0, help="seconds between steps")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--llm", action="store_true", help="Claude buys (needs ANTHROPIC_API_KEY)")
    args = parser.parse_args(argv)
    if args.llm and not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("--llm needs ANTHROPIC_API_KEY in the environment")
    with contextlib.suppress(AttributeError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    # Only the narration should be on screen while recording.
    for name in ("httpx", "mcp", "uvicorn"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # The demo world is throwaway, so the dev-secret warnings are noise here.
    logging.getLogger("trajano").setLevel(logging.ERROR)
    run_scene(args.scene, pace=args.pace, color=not args.no_color, llm=args.llm)


if __name__ == "__main__":
    main()
