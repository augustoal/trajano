"""Deterministic seller: a YAML of pricing rules applied to each thread in the inbox.

decide() is pure. run() is the loop around it: get_inbox, get_thread, the
seller's own get_listing, decide, then one tool call. The rules never leave
this process; the server only knows the listing's price_floor.
"""

import asyncio
import contextlib
import logging
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, Field

from agents.client import ToolCallError, TrajanoClient
from trajano.domain.pricing import billed_qty, landed_cost, tier_price, transit_days
from trajano.schemas import PriceableListing, PriceTier

logger = logging.getLogger(__name__)

# Keep these aligned with trajano.schemas.DeclineReason.
DECLINE_REASONS = (
    "OUT_OF_STOCK",
    "CANT_MEET_DATE",
    "BELOW_FLOOR",
    "SPEC_MISMATCH",
    "OTHER",
)

UNIT = Decimal("0.0001")
RULES_DIR = Path(__file__).resolve().parent.parent / "data" / "sellers"


class _Rules(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VolumeDiscount(_Rules):
    min_qty: int = Field(ge=1)
    percent: Decimal = Field(ge=0, lt=100)


class CounterRules(_Rules):
    max_discount_percent: Decimal = Field(default=Decimal("0"), ge=0, lt=100)
    meet_at_limit: bool = False


class OrderRules(_Rules):
    confirm: bool = True
    ship: bool = True
    deliver: bool = True


class SellerRules(_Rules):
    seller: str
    min_margin_percent: Decimal = Field(default=Decimal("0"), ge=0)
    volume_discounts: list[VolumeDiscount] = []
    counter: CounterRules = CounterRules()
    offer_ttl_hours: int | None = Field(default=None, ge=1, le=48)
    orders: OrderRules = OrderRules()


class OfferAction(TypedDict):
    action: Literal["offer"]
    unit_price: str
    qty: int
    total: str
    delivery_date: str
    lines: list[dict[str, str]]
    expires_at: NotRequired[str]


class DeclineAction(TypedDict):
    action: Literal["decline"]
    reason_code: str


class AnswerAction(TypedDict):
    action: Literal["answer"]
    text: str


class ConfirmAction(TypedDict):
    action: Literal["confirm"]
    order_id: int


class RejectAction(TypedDict):
    action: Literal["reject"]
    order_id: int
    reason_code: str


class ShipAction(TypedDict):
    action: Literal["ship"]
    order_id: int
    tracking: str


class DeliverAction(TypedDict):
    action: Literal["deliver"]
    order_id: int


Action = (
    OfferAction
    | DeclineAction
    | AnswerAction
    | ConfirmAction
    | RejectAction
    | ShipAction
    | DeliverAction
)


def load_rules(path: str | Path) -> SellerRules:
    return SellerRules.model_validate(yaml.safe_load(Path(path).read_text()))


def all_rules(directory: Path = RULES_DIR) -> dict[str, SellerRules]:
    """Every seller's rules, keyed by seller name."""
    rules = (load_rules(path) for path in sorted(directory.glob("*.yaml")))
    return {item.seller: item for item in rules}


def decide(thread: dict[str, Any], listing: dict[str, Any], rules: SellerRules) -> Action | None:
    """The seller's next move on a thread view, given its own listing view.

    thread is get_thread's ThreadView and listing the seller get_listing's
    ListingSeller, both as JSON. None means there is nothing to do. Buyer text
    is never read: questions get the listing's structured facts back.
    """
    actions = set(thread["next_actions"])
    status = thread["status"]

    if "answer" in actions and thread.get("pending_question"):
        return AnswerAction(action="answer", text=_facts(listing))
    if status == "QUOTE_REQUESTED" and "offer" in actions:
        return _quote(thread, listing, rules)
    if status == "COUNTERED" and "offer" in actions:
        return _answer_counter(thread, listing, rules)
    if status == "ACCEPTED" and "order_confirm" in actions:
        order_id = _order_id(thread)
        if rules.orders.confirm:
            return ConfirmAction(action="confirm", order_id=order_id)
        return RejectAction(action="reject", order_id=order_id, reason_code="OTHER")
    if status == "CONFIRMED" and "shipped" in actions and rules.orders.ship:
        order_id = _order_id(thread)
        return ShipAction(action="ship", order_id=order_id, tracking=f"TRJ-{order_id:08d}")
    if status == "SHIPPED" and "delivered" in actions and rules.orders.deliver:
        return DeliverAction(action="deliver", order_id=_order_id(thread))
    return None


def list_price(listing: dict[str, Any], qty_billed: int, rules: SellerRules) -> Decimal:
    """The opening unit price: the tier, less any volume discount, never under the margin."""
    price = tier_price(_priceable(listing), qty_billed)
    discount = max(
        (step.percent for step in rules.volume_discounts if step.min_qty <= qty_billed),
        default=Decimal("0"),
    )
    price = _unit(price * (100 - discount) / 100)
    return max(price, min_price(listing, rules))


def min_price(listing: dict[str, Any], rules: SellerRules) -> Decimal:
    floor = Decimal(str(listing["price_floor"]))
    return (floor * (100 + rules.min_margin_percent) / 100).quantize(UNIT, rounding=ROUND_UP)


def _quote(thread: dict[str, Any], listing: dict[str, Any], rules: SellerRules) -> Action:
    qty = int(thread["qty"])
    refusal = _cannot_supply(thread, listing, qty)
    if refusal is not None:
        return refusal
    billed = billed_qty(qty, int(listing["pack_size"]))
    return _offer(thread, listing, rules, qty, list_price(listing, billed, rules))


def _answer_counter(thread: dict[str, Any], listing: dict[str, Any], rules: SellerRules) -> Action:
    counter = _latest(thread, "counter")["payload"]
    qty = int(counter.get("qty") or thread["qty"])
    refusal = _cannot_supply(thread, listing, qty)
    if refusal is not None:
        return refusal
    billed = billed_qty(qty, int(listing["pack_size"]))
    opening = list_price(listing, billed, rules)
    asked = Decimal(str(counter["unit_price"])) if counter.get("unit_price") else opening
    limit = max(
        (opening * (100 - rules.counter.max_discount_percent) / 100).quantize(
            UNIT, rounding=ROUND_UP
        ),
        min_price(listing, rules),
    )
    if asked >= limit:
        return _offer(thread, listing, rules, qty, min(asked, opening))
    if rules.counter.meet_at_limit:
        return _offer(thread, listing, rules, qty, limit)
    return DeclineAction(action="decline", reason_code="BELOW_FLOOR")


def _cannot_supply(
    thread: dict[str, Any], listing: dict[str, Any], qty: int
) -> DeclineAction | None:
    billed = billed_qty(qty, int(listing["pack_size"]))
    if billed > int(listing["stock"]):
        return DeclineAction(action="decline", reason_code="OUT_OF_STOCK")
    if billed < int(listing["moq"]):
        return DeclineAction(action="decline", reason_code="SPEC_MISMATCH")
    need_by = thread.get("need_by")
    if need_by and _delivery(thread, listing) > date.fromisoformat(need_by):
        return DeclineAction(action="decline", reason_code="CANT_MEET_DATE")
    return None


def _offer(
    thread: dict[str, Any],
    listing: dict[str, Any],
    rules: SellerRules,
    qty: int,
    unit_price: Decimal,
) -> OfferAction:
    landed = landed_cost(_priceable(listing), qty, thread["ship_to"], unit_price=unit_price)
    action = OfferAction(
        action="offer",
        unit_price=format(landed.unit_price, "f"),
        qty=qty,
        total=format(landed.total, "f"),
        delivery_date=_delivery(thread, listing).isoformat(),
        lines=[
            {"kind": line.kind, "description": line.description, "amount": format(line.amount, "f")}
            for line in landed.lines
        ],
    )
    if rules.offer_ttl_hours is not None:
        expires = _asked_at(thread) + timedelta(hours=rules.offer_ttl_hours)
        action["expires_at"] = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
    return action


def _delivery(thread: dict[str, Any], listing: dict[str, Any]) -> date:
    # Counted from the server's timestamp on the buyer's message, not this
    # process's clock, so a frozen server clock gives the same dates.
    days = int(listing["lead_time_days"]) + transit_days(listing["ships_from"], thread["ship_to"])
    return _asked_at(thread, "rfq").date() + timedelta(days=days)


def _asked_at(thread: dict[str, Any], *types: str) -> datetime:
    message = _latest(thread, *(types or ("rfq", "counter")))
    stamp = datetime.fromisoformat(message["created_at"].replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _latest(thread: dict[str, Any], *types: str) -> dict[str, Any]:
    for message in reversed(thread["messages"]):
        if message["type"] in types:
            return message
    raise ValueError(f"thread {thread['id']} has no {' or '.join(types)} message")


def _order_id(thread: dict[str, Any]) -> int:
    return int(_latest(thread, "accept")["payload"]["order_id"])


def _facts(listing: dict[str, Any]) -> str:
    return (
        f"{listing['sku']}: {listing['diameter']}x{listing['length_mm']} mm, "
        f"{listing['material']} {listing['grade']}, {listing['standard']}. "
        f"MOQ {listing['moq']}, packs of {listing['pack_size']}, {listing['stock']} in stock, "
        f"lead time {listing['lead_time_days']} days from {listing['ships_from']}. "
        "Request a quote for a binding price."
    )


def _priceable(listing: dict[str, Any]) -> PriceableListing:
    return PriceableListing(
        id=int(listing["id"]),
        version=int(listing["version"]),
        price_tiers=[
            PriceTier(min_qty=int(tier["min_qty"]), unit_price=Decimal(str(tier["unit_price"])))
            for tier in listing["price_tiers"]
        ],
        pack_size=int(listing["pack_size"]),
        ships_from=listing["ships_from"],
        currency=listing.get("currency", "USD"),
    )


def _unit(value: Decimal) -> Decimal:
    return value.quantize(UNIT, rounding=ROUND_HALF_UP)


async def run(
    url: str,
    api_key: str,
    rules: SellerRules,
    *,
    stop: asyncio.Event | None = None,
    poll_seconds: float = 0.2,
) -> dict[str, int]:
    """Serve the inbox until stop is set. Returns a count of actions taken and errors."""
    async with TrajanoClient(url, api_key, role="seller") as client:
        return await serve(client, rules, stop=stop, poll_seconds=poll_seconds)


async def serve(
    client: TrajanoClient,
    rules: SellerRules,
    *,
    stop: asyncio.Event | None = None,
    poll_seconds: float = 0.2,
) -> dict[str, int]:
    stop = stop or asyncio.Event()
    stats: Counter[str] = Counter()
    # A thread we could not act on stays on our side until it changes.
    skipped: set[tuple[int, str]] = set()
    while not stop.is_set():
        async for item in client.inbox():
            seen = (item["thread_id"], item["updated_at"])
            if seen in skipped:
                continue
            if not await handle(client, item["thread_id"], rules, stats):
                skipped.add(seen)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
    return dict(stats)


async def handle(
    client: TrajanoClient, thread_id: int, rules: SellerRules, stats: Counter[str]
) -> bool:
    """Act once on a thread. False when there was nothing to do or the server refused."""
    thread = await client.get_thread(thread_id)
    listing = (await client.call("get_listing", listing_id=thread["listing_id"]))["listing"]
    action = decide(thread, listing, rules)
    if action is None:
        return False
    # One key per thread state: a retry or a second pass over the same state replays.
    key = f"{action['action']}-t{thread_id}-m{thread['messages'][-1]['id']}"
    try:
        await _execute(client, thread_id, action, key)
    except ToolCallError as exc:
        logger.info("thread %s: %s refused: %s", thread_id, action["action"], exc)
        if action["action"] == "offer" and exc.code == "OUT_OF_STOCK":
            # Another buyer took the stock between our read and the offer.
            stats["stock_race"] += 1
            await _execute(
                client, thread_id, DeclineAction(action="decline", reason_code="OUT_OF_STOCK"), key
            )
            stats["decline"] += 1
            return True
        stats[f"error:{exc.code}"] += 1
        return False
    stats[action["action"]] += 1
    return True


async def _execute(client: TrajanoClient, thread_id: int, action: Action, key: str) -> None:
    match action["action"]:
        case "offer":
            await client.call(
                "make_offer",
                thread_id=thread_id,
                qty=action["qty"],
                unit_price=action["unit_price"],
                total=action["total"],
                delivery_date=action["delivery_date"],
                lines=action["lines"],
                expires_at=action.get("expires_at"),
                idempotency_key=key,
            )
        case "decline":
            await client.call(
                "decline",
                thread_id=thread_id,
                reason_code=action["reason_code"],
                idempotency_key=key,
            )
        case "answer":
            await client.call(
                "answer_question", thread_id=thread_id, text=action["text"], idempotency_key=key
            )
        case "confirm":
            await client.call("confirm_order", order_id=action["order_id"], idempotency_key=key)
        case "reject":
            await client.call(
                "reject_order",
                order_id=action["order_id"],
                reason_code=action["reason_code"],
                idempotency_key=key,
            )
        case "ship":
            await client.call(
                "mark_shipped",
                order_id=action["order_id"],
                tracking=action["tracking"],
                idempotency_key=key,
            )
        case "deliver":
            await client.call("mark_delivered", order_id=action["order_id"], idempotency_key=key)
