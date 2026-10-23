import csv
import io
from datetime import datetime
from decimal import Decimal

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from trajano.db import utc_now
from trajano.domain import mandates
from trajano.domain.errors import DomainError
from trajano.models import Agent, Listing, Offer, Order, Thread
from trajano.schemas import (
    CostLine,
    DeclineReason,
    ErrorCode,
    MandateDocument,
    OfferFingerprint,
    OrderStatus,
    OrderView,
    Role,
    offer_hash,
    quantize_money,
    quantize_unit,
)

# The seller transitions (confirm, reject, ship, deliver) run inside
# negotiation.send, which owns the idempotency key for that message. Here a
# repeat of the step the order already took returns the order unchanged.


def accept(
    session: Session,
    agent: Agent,
    offer: Offer,
    mandate: MandateDocument,
    idempotency_key: str,
) -> Order:
    """Create a SUBMITTED order in the caller's transaction.

    The stored offer must still hash to offer.hash; comparing the hash the buyer
    sent with offer.hash is the caller's step. Stock comes off the current
    version of the listing, since a new version carries the same inventory.
    """
    existing = _by_key(session, agent, idempotency_key)
    if existing is not None:
        return _same_offer(existing, offer)
    thread = session.get(Thread, offer.thread_id)
    listing = session.get(Listing, offer.listing_id)
    if thread is None or listing is None or thread.buyer_agent_id != agent.id:
        raise DomainError(ErrorCode.NOT_FOUND, f"offer {offer.id} not found")

    def stale(reason: str) -> DomainError:
        return DomainError(
            ErrorCode.STALE_OFFER,
            reason,
            action="accept",
            agent_id=agent.id,
            detail={"offer_id": offer.id, "reason": reason},
        )

    if offer_hash(_fingerprint(offer, listing)) != offer.hash:
        raise stale("the offer does not match its hash")
    if utc_now() > offer.expires_at:
        raise stale("the offer has expired")
    if mandate.currency != offer.currency:
        raise DomainError(
            ErrorCode.MANDATE_INVALID,
            f"mandate is in {mandate.currency}, the offer is in {offer.currency}",
            action="mandate_verify",
            agent_id=agent.id,
            detail={"mandate_id": mandate.id, "reason": "currency"},
        )
    approved = mandates.verify(
        session,
        agent,
        mandate,
        unit_price=Decimal(offer.unit_price),
        qty=offer.qty,
        total=Decimal(offer.total),
    )
    try:
        with session.begin_nested():
            taken = session.execute(
                update(Listing)
                .where(
                    Listing.store_id == listing.store_id,
                    Listing.sku == listing.sku,
                    Listing.is_current.is_(True),
                    Listing.stock >= offer.qty,
                )
                .values(stock=Listing.stock - offer.qty)
            )
            if taken.rowcount != 1:
                raise stale("not enough stock for this offer")
            order = Order(
                offer_id=offer.id or 0,
                buyer_company_id=agent.company_id,
                buyer_agent_id=agent.id or 0,
                seller_company_id=thread.seller_company_id,
                mandate_id=approved.id or 0,
                idempotency_key=idempotency_key,
                created_at=utc_now(),
            )
            session.add(order)
            session.flush()
    except IntegrityError:
        # The unique (buyer, key) index caught a retry that raced this one.
        existing = _by_key(session, agent, idempotency_key)
        if existing is None:
            raise
        return _same_offer(existing, offer)
    return order


def confirm(session: Session, agent: Agent, order_id: int, idempotency_key: str) -> Order:
    order = _seller_order(session, agent, order_id)
    if order.status is OrderStatus.CONFIRMED:
        return order
    _require(order, OrderStatus.SUBMITTED, "confirm")
    order.status = OrderStatus.CONFIRMED
    order.confirmed_at = utc_now()
    return _save(session, order)


def reject(
    session: Session,
    agent: Agent,
    order_id: int,
    reason_code: DeclineReason,
    idempotency_key: str,
) -> Order:
    """Reject a submitted order and put its stock back on the current listing version."""
    order = _seller_order(session, agent, order_id)
    if order.status is OrderStatus.REJECTED:
        return order
    _require(order, OrderStatus.SUBMITTED, "reject")
    offer = session.get(Offer, order.offer_id)
    listing = session.get(Listing, offer.listing_id) if offer is not None else None
    if offer is None or listing is None:
        raise DomainError(ErrorCode.INTERNAL, "order is missing its offer", retryable=True)
    session.execute(
        update(Listing)
        .where(
            Listing.store_id == listing.store_id,
            Listing.sku == listing.sku,
            Listing.is_current.is_(True),
        )
        .values(stock=Listing.stock + offer.qty)
    )
    order.status = OrderStatus.REJECTED
    order.reason_code = reason_code.value
    order.rejected_at = utc_now()
    return _save(session, order)


def ship(
    session: Session, agent: Agent, order_id: int, tracking: str, idempotency_key: str
) -> Order:
    order = _seller_order(session, agent, order_id)
    if order.status is OrderStatus.SHIPPED and order.tracking == tracking:
        return order
    _require(order, OrderStatus.CONFIRMED, "ship")
    order.status = OrderStatus.SHIPPED
    order.tracking = tracking
    order.shipped_at = utc_now()
    return _save(session, order)


def deliver(session: Session, agent: Agent, order_id: int, idempotency_key: str) -> Order:
    order = _seller_order(session, agent, order_id)
    if order.status is OrderStatus.DELIVERED:
        return order
    _require(order, OrderStatus.SHIPPED, "deliver")
    order.status = OrderStatus.DELIVERED
    order.delivered_at = utc_now()
    return _save(session, order)


def get_order(session: Session, agent: Agent, order_id: int) -> OrderView:
    """The buyer agent that placed it, or a seller agent of the selling company."""
    order = session.get(Order, order_id)
    if order is None or not (order.buyer_agent_id == agent.id or _is_seller(agent, order)):
        raise DomainError(ErrorCode.NOT_FOUND, f"order {order_id} not found")
    return order_view(order)


def export_order_csv(session: Session, agent: Agent, order_id: int) -> str:
    """CSV for one order the caller's company is allowed to see: one row per cost line."""
    order = session.get(Order, order_id)
    visible = order is not None and agent.company_id in (
        order.buyer_company_id,
        order.seller_company_id,
    )
    if order is None or not visible:
        raise DomainError(ErrorCode.NOT_FOUND, f"order {order_id} not found")
    offer = session.get(Offer, order.offer_id)
    listing = session.get(Listing, offer.listing_id) if offer is not None else None
    if offer is None or listing is None:
        raise DomainError(ErrorCode.INTERNAL, "order is missing its offer", retryable=True)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        [
            "order_id", "status", "created_at", "sku", "listing_version", "qty",
            "unit_price", "currency", "ship_to", "delivery_date", "tracking",
            "line", "description", "amount",
        ]
    )  # fmt: skip
    head = [
        order.id,
        order.status.value,
        _stamp(order.created_at),
        listing.sku,
        listing.version,
        offer.qty,
        quantize_unit(Decimal(offer.unit_price)),
        offer.currency,
        offer.ship_to,
        offer.delivery_date.isoformat(),
        order.tracking or "",
    ]
    for line in offer.lines:
        cost = CostLine(**line)
        writer.writerow(_cells([*head, cost.kind, cost.description, quantize_money(cost.amount)]))
    writer.writerow(_cells([*head, "total", "", quantize_money(Decimal(offer.total))]))
    return out.getvalue()


def order_view(order: Order) -> OrderView:
    return OrderView(
        id=order.id or 0,
        offer_id=order.offer_id,
        status=order.status,
        buyer_company_id=order.buyer_company_id,
        buyer_agent_id=order.buyer_agent_id,
        seller_company_id=order.seller_company_id,
        mandate_id=order.mandate_id,
        tracking=order.tracking,
        reason_code=order.reason_code,
        created_at=order.created_at,
    )


def _fingerprint(offer: Offer, listing: Listing) -> OfferFingerprint:
    return OfferFingerprint(
        listing_id=offer.listing_id,
        listing_version=listing.version,
        lines=[CostLine(**line) for line in offer.lines],
        unit_price=Decimal(offer.unit_price),
        qty=offer.qty,
        total=Decimal(offer.total),
        currency=offer.currency,
        ship_to=offer.ship_to,
        delivery_date=offer.delivery_date,
        expires_at=offer.expires_at,
        final=offer.final,
    )


def _by_key(session: Session, agent: Agent, idempotency_key: str) -> Order | None:
    return session.exec(
        select(Order).where(
            Order.buyer_agent_id == agent.id,
            Order.idempotency_key == idempotency_key,
        )
    ).first()


def _same_offer(order: Order, offer: Offer) -> Order:
    if order.offer_id != offer.id:
        raise DomainError(
            ErrorCode.CONFLICT, "idempotency key was already used to accept another offer"
        )
    return order


def _is_seller(agent: Agent, order: Order) -> bool:
    return agent.company_id == order.seller_company_id and Role.SELLER.value in agent.roles


def _seller_order(session: Session, agent: Agent, order_id: int) -> Order:
    order = session.get(Order, order_id)
    if order is None or not _is_seller(agent, order):
        raise DomainError(ErrorCode.NOT_FOUND, f"order {order_id} not found")
    return order


def _require(order: Order, status: OrderStatus, step: str) -> None:
    if order.status is not status:
        raise DomainError(
            ErrorCode.INVALID_TRANSITION,
            f"cannot {step} an order that is {order.status.value}",
        )


def _save(session: Session, order: Order) -> Order:
    session.add(order)
    session.flush()
    return order


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cells(values: list[object]) -> list[str]:
    # Seller text such as a SKU must not run as a spreadsheet formula.
    cells = []
    for value in values:
        text = "" if value is None else str(value)
        cells.append("'" + text if text[:1] in ("=", "+", "-", "@") else text)
    return cells
